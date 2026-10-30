#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="super-agent-cleanroom:$(git -C "$REPO_ROOT" rev-parse --short HEAD)"

echo "=== Building Docker Clean Room Image [$TAG] ==="
docker build -t "$TAG" -f "$REPO_ROOT/Dockerfile.cleanroom" "$REPO_ROOT"

echo "=== Running Clean Room Verification Container ==="
docker run --rm \
  -v "$REPO_ROOT:/workspace:ro" \
  -e PYTHONPATH=/workspace \
  "$TAG" \
  pytest -o cache_dir=/tmp/.pytest_cache -v \
    tests/test_repo_neutrality.py \
    tests/test_pool.py \
    tests/test_pool_replay_containment.py \
    tests/test_colleague_containment.py \
    tests/test_colleague_containment_trusted.py \
    tests/test_colleague_worker.py \
    tests/test_backup_and_rollback.py

echo "=== Docker Clean Room Verification PASSED ==="
