import re
import unittest

from pci_bus_guard import repair_domain


def fixture(bus_numbers):
    parts = ["<domain><devices><controller type='pci' index='0' model='pcie-root'/>"]
    for index, start, count in bus_numbers:
        parts.append(
            "<controller type='pci' index='%s' model='pcie-expander-bus'>"
            "<target busNr='%s'><node>0</node></target></controller>" % (index, start)
        )
        for offset in range(count):
            parts.append(
                "<controller type='pci' index='%s' model='pcie-root-port'>"
                "<address type='pci' domain='0x0000' bus='0x%02x' slot='0x%02x' function='0x0'/>"
                "</controller>" % (index + offset + 1, index, offset)
            )
    parts.append("<hostdev type='pci'><alias name='gpu0'/></hostdev></devices></domain>")
    return "".join(parts)


class BusGuardTests(unittest.TestCase):
    def check_repair(self, ranges):
        original = fixture(ranges)
        changed, messages = repair_domain(original)
        self.assertTrue(messages)
        self.assertEqual(repair_domain(changed), (changed, []))
        strip_numbers = lambda xml: re.sub(r"busNr='\d+'", "busNr='X'", xml)
        self.assertEqual(strip_numbers(original), strip_numbers(changed))

    def test_duplicate_start(self):
        self.check_repair([(1, 249, 2), (4, 251, 1), (6, 249, 1)])

    def test_overlapping_child_ranges(self):
        self.check_repair([(1, 249, 2), (4, 252, 1), (6, 250, 1)])

    def test_one_nic_layout(self):
        self.check_repair([(1, 250, 1), (3, 252, 1), (5, 250, 1)])

    def test_valid_gpu_first_layout_unchanged(self):
        xml = fixture([(1, 254, 1), (3, 252, 1), (5, 249, 2)])
        self.assertEqual(repair_domain(xml), (xml, []))

    def test_four_nics(self):
        self.check_repair([(1, 247, 4), (6, 249, 1), (8, 247, 1)])

    def test_without_expanders(self):
        xml = "<domain><devices/></domain>"
        self.assertEqual(repair_domain(xml), (xml, []))


if __name__ == "__main__":
    unittest.main()
