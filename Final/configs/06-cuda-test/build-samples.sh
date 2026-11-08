#!/bin/bash
# Build NVIDIA cuda-samples deviceQuery + vectorAdd inside the GPU VM.
# Prereqs: nvidia-smi healthy, and:
#   sudo dnf install -y cuda-toolkit-13-4 git cmake
set -euo pipefail

SAMPLES_DIR=${SAMPLES_DIR:-$HOME/cuda-samples}
ARCH=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
echo "GPU compute capability: ${ARCH} (sm_${ARCH})"

if [ ! -d "$SAMPLES_DIR" ]; then
    git clone https://github.com/NVIDIA/cuda-samples "$SAMPLES_DIR"
fi
cd "$SAMPLES_DIR"

export PATH=/usr/local/cuda/bin:$PATH
command -v nvcc >/dev/null || { echo "nvcc not found — install cuda-toolkit"; exit 1; }

rm -rf build
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_CUDA_ARCHITECTURES=${ARCH}
cmake --build build --target deviceQuery vectorAdd -j"$(nproc)"

echo
echo "Binaries:"
find build -name deviceQuery -o -name vectorAdd | grep -E 'deviceQuery|vectorAdd$'
