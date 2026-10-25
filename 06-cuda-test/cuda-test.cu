// Quick CUDA functional test: prints device info, runs a small vector-add
// kernel, and verifies the result. Exits non-zero on failure.
// Build: nvcc -o cuda-test cuda-test.cu
#include <cstdio>
#include <cuda_runtime.h>

__global__ void vecAdd(const float *a, const float *b, float *c, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) c[i] = a[i] + b[i];
}

int main() {
    int dev = 0;
    cudaDeviceProp prop;
    if (cudaGetDeviceProperties(&prop, dev) != cudaSuccess) {
        fprintf(stderr, "cudaGetDeviceProperties failed\n");
        return 1;
    }
    printf("Device %d: %s\n", dev, prop.name);
    printf("  SMs: %d, CUDA cores/SM: %d, clock: %d kHz\n",
           prop.multiProcessorCount, 0 /*opaque in modern CUDA*/, prop.clockRate);
    printf("  Memory: %.1f GiB, ECC: %s\n",
           prop.totalGlobalMem / (1024.0 * 1024.0 * 1024.0),
           prop.ECCEnabled ? "on" : "off");
    printf("  Compute capability: %d.%d\n", prop.major, prop.minor);

    const int N = 1 << 20;
    size_t bytes = N * sizeof(float);
    float *ha = new float[N], *hb = new float[N], *hc = new float[N];
    for (int i = 0; i < N; i++) { ha[i] = i * 1.0f; hb[i] = 1.0f; }

    float *da, *db, *dc;
    cudaMalloc(&da, bytes); cudaMalloc(&db, bytes); cudaMalloc(&dc, bytes);
    cudaMemcpy(da, ha, bytes, cudaMemcpyHostToDevice);
    cudaMemcpy(db, hb, bytes, cudaMemcpyHostToDevice);

    cudaEvent_t t0, t1;
    cudaEventCreate(&t0); cudaEventCreate(&t1);
    cudaEventRecord(t0);
    vecAdd<<<(N + 255) / 256, 256>>>(da, db, dc, N);
    cudaEventRecord(t1);
    cudaEventSynchronize(t1);

    cudaError_t err = cudaGetLastError();
    if (err != cudaSuccess) {
        fprintf(stderr, "kernel launch failed: %s\n", cudaGetErrorString(err));
        return 1;
    }

    float ms = 0;
    cudaEventElapsedTime(&ms, t0, t1);
    cudaMemcpy(hc, dc, bytes, cudaMemcpyDeviceToHost);

    int errors = 0;
    for (int i = 0; i < N; i++) {
        if (hc[i] != ha[i] + hb[i]) { if (++errors < 5) fprintf(stderr, "mismatch at %d: %f != %f\n", i, hc[i], ha[i] + hb[i]); }
    }
    printf("  vector-add of %d elements: %s (kernel %.3f ms)\n",
           N, errors ? "FAILED" : "PASSED", ms);
    printf("  effective bandwidth: %.1f GB/s\n",
           3.0 * bytes / (ms / 1e3) / 1e9);

    cudaFree(da); cudaFree(db); cudaFree(dc);
    delete[] ha; delete[] hb; delete[] hc;
    return errors ? 1 : 0;
}
