#!/bin/bash
# Run the quick cuda-samples validation inside the GPU VM (after build-samples.sh).
set -uo pipefail

SAMPLES_DIR=${SAMPLES_DIR:-$HOME/cuda-samples}
FAIL=0

echo "=== deviceQuery ==="
if "$SAMPLES_DIR/build/cpp/1_Utilities/deviceQuery/deviceQuery" \
    | tee /tmp/deviceQuery.out; then
    grep -q "Result = PASS" /tmp/deviceQuery.out || FAIL=1
else
    FAIL=1
fi

echo
echo "=== vectorAdd ==="
if "$SAMPLES_DIR/build/cpp/0_Introduction/vectorAdd/vectorAdd" \
    | tee /tmp/vectorAdd.out; then
    grep -q "Test PASSED" /tmp/vectorAdd.out || FAIL=1
else
    FAIL=1
fi

echo
if [ $FAIL -eq 0 ]; then
    echo "PASS: GB300 CUDA validation successful"
else
    echo "FAIL: check outputs above"
fi
exit $FAIL
