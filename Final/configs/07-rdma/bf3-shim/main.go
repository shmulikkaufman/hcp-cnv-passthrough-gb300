// bf3-shim: minimal kubelet device-plugin that advertises manually-created
// BlueField-3 VFs (bound to vfio-pci on the host) under an extended resource,
// and emits the KubeVirt-style PCI_RESOURCE_<NAME> environment variable in
// Allocate responses — mirroring what NVIDIA's device plugin does for
// nvidia.com/31c2 — plus the /dev/vfio device mounts.
//
// Why this exists: the sriov-network-device-plugin hardcodes the "PCIDEVICE_"
// env prefix (pool_stub.go GetEnvs), while KubeVirt's virt-launcher discovers
// hostDevices/gpus PCI addresses via "PCI_RESOURCE_<RESOURCE>" env vars
// (kubevirt v1.PCIResourcePrefix). The mismatch makes KubeVirt hostDevices
// fail with "the number of device plugin HostDevice/s do not match the number
// of devices" when the resource is provided by the sriov plugin.
//
// Build: CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o bf3-shim .
package main

import (
	"context"
	"crypto/sha256"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	pluginapi "k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1"
)

type shimServer struct {
	pluginapi.UnimplementedDevicePluginServer
	resourceName string
	sysfsPrefix  string
	devices      []string // PCI addresses (BDF), e.g. 0016:01:00.1
}

// Each resource needs its own endpoint in kubelet's shared plugin directory.
// A fixed name lets one plugin unlink another resource's live socket.
func pluginEndpoint(resource string) string {
	digest := sha256.Sum256([]byte(resource))
	return fmt.Sprintf("pci-shim-%x.sock", digest[:12])
}

// envVarName replicates kubevirt's util.ResourceNameToEnvVar("PCI_RESOURCE", resource):
// PCI_RESOURCE_ + uppercase(resource) with non-alphanumerics replaced by '_'.
func envVarName(resource string) string {
	var b strings.Builder
	for _, r := range strings.ToUpper(resource) {
		if (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		} else {
			b.WriteRune('_')
		}
	}
	return "PCI_RESOURCE_" + b.String()
}

// iommuGroupDev resolves /dev/vfio/<group> for a PCI address via sysfs.
func (s *shimServer) iommuGroupDev(pciAddr string) (string, error) {
	link := filepath.Join(s.sysfsPrefix, "bus", "pci", "devices", pciAddr, "iommu_group")
	dest, err := os.Readlink(link)
	if err != nil {
		return "", fmt.Errorf("readlink %s: %w", link, err)
	}
	return "/dev/vfio/" + filepath.Base(dest), nil
}

// vfioCdevNames resolves the kernel 6.x per-device cdev paths
// (/dev/vfio/devices/vfioN) for a PCI address by reading the authoritative
// sysfs interface: /sys/bus/pci/devices/<BDF>/vfio-dev/. This is exactly what
// the upstream KubeVirt fix (PR #18300, CNV-91802) does — minor numbers are
// assigned at probe time and are NOT stable across reboots, so they must be
// re-read at every Allocate() call. Returns nil on kernels without the API
// (vfio-dev/ absent), which is safe.
func (s *shimServer) vfioCdevNames(pciAddr string) []string {
	dir := filepath.Join(s.sysfsPrefix, "bus", "pci", "devices", pciAddr, "vfio-dev")
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var paths []string
	for _, e := range entries {
		paths = append(paths, "/dev/vfio/devices/"+e.Name())
	}
	return paths
}

// healthyDevice reports whether the device is still bound to vfio-pci.
func (s *shimServer) healthyDevice(pciAddr string) bool {
	link := filepath.Join(s.sysfsPrefix, "bus", "pci", "devices", pciAddr, "driver")
	dest, err := os.Readlink(link)
	if err != nil {
		return false
	}
	return filepath.Base(dest) == "vfio-pci"
}

func (s *shimServer) GetDevicePluginOptions(context.Context, *pluginapi.Empty) (*pluginapi.DevicePluginOptions, error) {
	return &pluginapi.DevicePluginOptions{PreStartRequired: false}, nil
}

func (s *shimServer) ListAndWatch(_ *pluginapi.Empty, stream pluginapi.DevicePlugin_ListAndWatchServer) error {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		devs := make([]*pluginapi.Device, 0, len(s.devices))
		for _, addr := range s.devices {
			health := pluginapi.Healthy
			if !s.healthyDevice(addr) {
				health = pluginapi.Unhealthy
			}
			devs = append(devs, &pluginapi.Device{ID: addr, Health: health})
		}
		if err := stream.Send(&pluginapi.ListAndWatchResponse{Devices: devs}); err != nil {
			return err
		}
		<-ticker.C
	}
}

func (s *shimServer) Allocate(ctx context.Context, req *pluginapi.AllocateRequest) (*pluginapi.AllocateResponse, error) {
	resp := &pluginapi.AllocateResponse{}
	allowed := make(map[string]bool, len(s.devices))
	for _, id := range s.devices {
		allowed[id] = true
	}
	for _, cr := range req.ContainerRequests {
		ids := make([]string, 0, len(cr.DevicesIds))
		// DeviceSpecs (not Mounts): kubelet creates the node in the container
		// AND adds cgroup device-controller allowlist entries — required for
		// libvirt to open the vfio devices (per upstream KubeVirt PR #18300).
		devices := []*pluginapi.DeviceSpec{
			{HostPath: "/dev/vfio/vfio", ContainerPath: "/dev/vfio/vfio", Permissions: "mrw"},
		}
		for _, id := range cr.DevicesIds {
			if !allowed[id] || !s.healthyDevice(id) {
				return nil, fmt.Errorf("device %q is not an advertised, vfio-pci-bound device", id)
			}
			ids = append(ids, id)
			// legacy VFIO group device
			dev, err := s.iommuGroupDev(id)
			if err != nil {
				return nil, fmt.Errorf("resolving iommu group for %s: %w", id, err)
			}
			devices = append(devices, &pluginapi.DeviceSpec{HostPath: dev, ContainerPath: dev, Permissions: "mrw"})
			// kernel 6.x per-device cdevs — what modern libvirt actually opens
			for _, cdev := range s.vfioCdevNames(id) {
				devices = append(devices, &pluginapi.DeviceSpec{HostPath: cdev, ContainerPath: cdev, Permissions: "mrw"})
			}
		}
		envs := map[string]string{envVarName(s.resourceName): strings.Join(ids, ",")}
		log.Printf("Allocate: devices=%v env[%s]=%s deviceSpecs=%v", ids, envVarName(s.resourceName), envs[envVarName(s.resourceName)], devices)
		resp.ContainerResponses = append(resp.ContainerResponses, &pluginapi.ContainerAllocateResponse{
			Envs:    envs,
			Devices: devices,
		})
	}
	return resp, nil
}

func (s *shimServer) PreStartContainer(context.Context, *pluginapi.PreStartContainerRequest) (*pluginapi.PreStartContainerResponse, error) {
	return &pluginapi.PreStartContainerResponse{}, nil
}

func serve(s *shimServer) error {
	endpoint := pluginEndpoint(s.resourceName)
	socketPath := filepath.Join(pluginapi.DevicePluginPath, endpoint)
	if err := os.Remove(socketPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	lis, err := net.Listen("unix", socketPath)
	if err != nil {
		return fmt.Errorf("listen %s: %w", socketPath, err)
	}
	// Sockets in the device plugin dir must be accessible by kubelet.
	if err := os.Chmod(socketPath, 0o660); err != nil {
		lis.Close()
		return err
	}

	grpcServer := grpc.NewServer()
	defer grpcServer.Stop()
	defer lis.Close()
	pluginapi.RegisterDevicePluginServer(grpcServer, s)

	// Serve BEFORE registering: kubelet dials back to our socket for
	// ListAndWatch immediately after Register() returns.
	serveErr := make(chan error, 1)
	go func() {
		serveErr <- grpcServer.Serve(lis)
	}()

	register := func() error {
		conn, err := grpc.Dial("unix://"+pluginapi.KubeletSocket,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
			grpc.WithBlock(), grpc.WithTimeout(10*time.Second))
		if err != nil {
			return fmt.Errorf("dial kubelet: %w", err)
		}
		defer conn.Close()
		client := pluginapi.NewRegistrationClient(conn)
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		_, err = client.Register(ctx, &pluginapi.RegisterRequest{
			Version:      pluginapi.Version,
			Endpoint:     endpoint,
			ResourceName: s.resourceName,
		})
		return err
	}

	if err := register(); err != nil {
		grpcServer.Stop()
		return fmt.Errorf("register with kubelet: %w", err)
	}
	log.Printf("registered %s with kubelet (devices: %v)", s.resourceName, s.devices)

	// If kubelet restarts it removes our socket; recreate and re-register.
	done := make(chan struct{})
	defer close(done)
	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ticker.C:
			}
			if _, err := os.Stat(socketPath); os.IsNotExist(err) {
				log.Printf("socket %s disappeared (kubelet restart?); stopping server", socketPath)
				grpcServer.Stop()
				return
			}
		}
	}()

	return <-serveErr
}

func main() {
	var (
		resource = flag.String("resource", "openshift.io/mellanox_bf3roce_vf", "extended resource name to register")
		devices  = flag.String("devices", "0016:01:00.1", "comma-separated PCI addresses (BDF) to advertise")
		sysfs    = flag.String("sysfs", "/host-sys", "host sysfs mount point")
	)
	flag.Parse()

	s := &shimServer{
		resourceName: *resource,
		sysfsPrefix:  *sysfs,
		devices:      strings.Split(*devices, ","),
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		<-sigCh
		os.Remove(filepath.Join(pluginapi.DevicePluginPath, pluginEndpoint(s.resourceName)))
		os.Exit(0)
	}()

	for {
		if err := serve(s); err != nil {
			log.Printf("serve error: %v; retrying in 10s", err)
		}
		time.Sleep(10 * time.Second)
	}
}
