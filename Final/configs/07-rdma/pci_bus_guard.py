#!/usr/bin/python3
"""Per-VM OnDefineDomain hook: fix overlapping PCIe expander bus ranges.

Only target/@busNr attributes change. Preserve the rest of AIE's XML verbatim,
including GPU generic initiators, MMIO, hostdev bindings, and NUMA placement.
This is a lab workaround, not a replacement for an upstream allocator fix.
"""

import argparse
import re
import sys
import xml.etree.ElementTree as ET


def repair_domain(xml):
    root = ET.fromstring(xml)
    controllers = {
        int(c.attrib["index"]): c
        for c in root.findall("./devices/controller")
        if c.get("type") == "pci"
    }
    expanders = {
        index: c for index, c in controllers.items()
        if c.get("model") == "pcie-expander-bus"
    }
    if not expanders:
        return xml, []

    children = {index: [] for index in controllers}
    for index, controller in controllers.items():
        address = controller.find("address")
        if address is not None and address.get("type") == "pci":
            parent = int(address.attrib["bus"], 0)
            if parent in children:
                children[parent].append(index)

    def descendant_bridges(index, seen):
        if index in seen:
            raise ValueError("cycle in PCI controller topology")
        seen = seen | {index}
        return sum(1 + descendant_bridges(c, seen) for c in children[index])

    ranges = []
    for index, controller in expanders.items():
        target = controller.find("target")
        if target is None or target.get("busNr") is None:
            raise ValueError("expander has no explicit busNr")
        start = int(target.attrib["busNr"])
        size = 1 + descendant_bridges(index, set())
        ranges.append((index, start, start + size - 1, size))

    occupied = set()
    collision = False
    for _, start, end, _ in ranges:
        if start < 1 or end > 255:
            collision = True
        buses = set(range(start, end + 1))
        collision |= bool(occupied & buses)
        occupied |= buses
    if not collision:
        return xml, []

    # Allocate downward in controller-index order, reserving every child bridge.
    boundary = 256
    updates = {}
    messages = []
    for index, start, end, size in sorted(ranges):
        boundary -= size
        if boundary <= max(controllers):
            raise ValueError("insufficient PCI bus space; refusing unsafe layout")
        updates[index] = boundary
        messages.append(
            "PCI controller %d: bus range %d-%d -> %d-%d"
            % (index, start, end, boundary, boundary + size - 1)
        )

    changed = set()

    def rewrite_controller(match):
        block = match.group(0)
        header = block[:block.index(">") + 1]
        index_match = re.search(r"\bindex\s*=\s*(['\"])(\d+)\1", header)
        if not index_match or int(index_match.group(2)) not in updates:
            return block
        index = int(index_match.group(2))
        value = updates[index]

        def rewrite_target(target_match):
            return re.sub(
                r"(\bbusNr\s*=\s*)(['\"])(\d+)\2",
                lambda m: m.group(1) + m.group(2) + str(value) + m.group(2),
                target_match.group(0), count=1,
            )

        result, count = re.subn(r"<target\b[^>]*>", rewrite_target, block, count=1)
        if count != 1:
            raise ValueError("unable to safely rewrite expander target")
        changed.add(index)
        return result

    # Exclude self-closing controllers so a root controller cannot consume the
    # following expander's closing tag as part of the same regex match.
    result = re.sub(
        r"<controller\b(?![^>]*?/\s*>)[^>]*>.*?</controller>",
        rewrite_controller, xml, flags=re.S,
    )
    if changed != set(updates):
        raise ValueError("not all expander controllers were rewritten")
    # Parse again and ensure the result is stable; no unbounded recursive repair.
    ET.fromstring(result)
    return result, messages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vmi")  # Standard KubeVirt sidecar-shim argument.
    parser.add_argument("--domain", required=True)
    args = parser.parse_args()
    result, messages = repair_domain(args.domain)
    for message in messages:
        print(message, file=sys.stderr)
    sys.stdout.write(result)


if __name__ == "__main__":
    main()
