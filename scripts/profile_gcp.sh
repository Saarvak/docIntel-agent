#!/usr/bin/env bash
# Run the DocIntel profiler on a GCP VM and keep a host snapshot beside the report.
# MinerU is the hosted API (mineru.net); do not start mineru-api on this VM.
#
#   cd ~/document_analysis/docIntel-agent
#   bash scripts/profile_gcp.sh
#   bash scripts/profile_gcp.sh --repeat 2 path/to/extra.pdf
#
# After it exits:  ls -lt profiles | head   and open profiles/<stamp>/REPORT.md

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${PROFILE_OUT:-$ROOT/profiles/$STAMP}"
mkdir -p "$OUT"

{
  echo "=== date ==="
  date -u
  echo
  echo "=== uname ==="
  uname -a
  echo
  echo "=== lscpu ==="
  lscpu 2>/dev/null || true
  echo
  echo "=== free -h ==="
  free -h 2>/dev/null || true
  echo
  echo "=== df -h ==="
  df -h "$ROOT" 2>/dev/null || true
  echo
  echo "=== nvidia-smi ==="
  nvidia-smi 2>/dev/null || echo "nvidia-smi not found"
  echo
  echo "=== GCP metadata ==="
  if curl -sf -H "Metadata-Flavor: Google" --max-time 2 \
      http://metadata.google.internal/computeMetadata/v1/instance/machine-type >/tmp/gcp-machine 2>/dev/null; then
    echo "machine-type: $(cat /tmp/gcp-machine)"
    echo -n "name: "; curl -sf -H "Metadata-Flavor: Google" --max-time 2 \
      http://metadata.google.internal/computeMetadata/v1/instance/name || true
    echo
    echo -n "zone: "; curl -sf -H "Metadata-Flavor: Google" --max-time 2 \
      http://metadata.google.internal/computeMetadata/v1/instance/zone || true
    echo
    echo -n "cpu-platform: "; curl -sf -H "Metadata-Flavor: Google" --max-time 2 \
      http://metadata.google.internal/computeMetadata/v1/instance/cpu-platform || true
    echo
  else
    echo "metadata server not reachable"
  fi
} > "$OUT/host_before.txt"

GPU_DMON_PID=""
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi dmon -s umt -d 1 -o DT > "$OUT/nvidia-dmon.csv" 2>/dev/null &
  GPU_DMON_PID=$!
fi

cleanup() {
  if [[ -n "$GPU_DMON_PID" ]] && kill -0 "$GPU_DMON_PID" 2>/dev/null; then
    kill "$GPU_DMON_PID" 2>/dev/null || true
    wait "$GPU_DMON_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

set +e
python profile_run.py --out "$OUT" --require-mineru "$@"
STATUS=$?
set -e

{
  echo
  echo "=== free -h (after) ==="
  free -h 2>/dev/null || true
  echo
  echo "=== nvidia-smi (after) ==="
  nvidia-smi 2>/dev/null || true
} >> "$OUT/host_before.txt"

echo
echo "Profile folder: $OUT"
echo "Read:           $OUT/REPORT.md"
exit "$STATUS"
