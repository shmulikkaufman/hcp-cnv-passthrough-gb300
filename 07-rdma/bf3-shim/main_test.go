package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
)

func TestResourceEndpointsAreIsolated(t *testing.T) {
	a := pluginEndpoint("openshift.io/mellanox_bf3roce_vf")
	b := pluginEndpoint("openshift.io/cx8_roce_vf")
	if a == b || a != pluginEndpoint("openshift.io/mellanox_bf3roce_vf") {
		t.Fatal("resource endpoints must be distinct and deterministic")
	}
	for _, name := range []string{a, b} {
		if filepath.Base(name) != name || !strings.HasSuffix(name, ".sock") {
			t.Fatalf("unsafe endpoint %q", name)
		}
	}
}

func TestAllocateMapsMultipleDevices(t *testing.T) {
	root := t.TempDir()
	ids := []string{"0000:03:00.2", "0002:03:00.2"}
	for i, id := range ids {
		dir := filepath.Join(root, "bus/pci/devices", id)
		if err := os.MkdirAll(filepath.Join(dir, "vfio-dev", []string{"vfio5", "vfio9"}[i]), 0o755); err != nil {
			t.Fatal(err)
		}
		for name, target := range map[string]string{"driver": "/drivers/vfio-pci", "iommu_group": []string{"/groups/93", "/groups/94"}[i]} {
			if err := os.Symlink(target, filepath.Join(dir, name)); err != nil {
				t.Fatal(err)
			}
		}
	}
	s := &shimServer{resourceName: "openshift.io/cx8_roce_vf", sysfsPrefix: root, devices: ids}
	req := &pluginapi.AllocateRequest{ContainerRequests: []*pluginapi.ContainerAllocateRequest{{DevicesIds: ids}}}
	resp, err := s.Allocate(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	r := resp.ContainerResponses[0]
	if r.Envs["PCI_RESOURCE_OPENSHIFT_IO_CX8_ROCE_VF"] != strings.Join(ids, ",") {
		t.Fatal("incorrect KubeVirt allocation environment")
	}
	want := map[string]bool{"/dev/vfio/vfio": false, "/dev/vfio/93": false, "/dev/vfio/94": false, "/dev/vfio/devices/vfio5": false, "/dev/vfio/devices/vfio9": false}
	for _, dev := range r.Devices {
		if _, ok := want[dev.HostPath]; !ok || dev.HostPath != dev.ContainerPath || dev.Permissions != "mrw" {
			t.Fatalf("unexpected device specification: %v", dev)
		}
		want[dev.HostPath] = true
	}
	for path, found := range want {
		if !found {
			t.Fatalf("missing device %s", path)
		}
	}
	if len(r.Mounts) != 0 {
		t.Fatal("character devices must use DeviceSpecs, not ordinary mounts")
	}
}

func TestAllocateRejectsUnknownDevice(t *testing.T) {
	s := &shimServer{devices: []string{"0000:03:00.2"}, sysfsPrefix: t.TempDir()}
	req := &pluginapi.AllocateRequest{ContainerRequests: []*pluginapi.ContainerAllocateRequest{{DevicesIds: []string{"0016:01:00.0"}}}}
	if _, err := s.Allocate(context.Background(), req); err == nil {
		t.Fatal("must not allocate an unadvertised device")
	}
}
