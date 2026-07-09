#!/usr/bin/env bash
# Shared build step for GitHub Actions and Gitee Go.
# Installs deps, runs the generator, leaves outputs in $OUT (default: dist/).
set -euo pipefail

OUT="${1:-dist}"
CACHE="${MRT_CACHE_DIR:-${RUNNER_TEMP:-/tmp}/mrt-cache}"

echo "==> bgpdump (optional, for speed; native parser is used if missing)"
if ! command -v bgpdump >/dev/null 2>&1; then
  { sudo apt-get update && sudo apt-get install -y bgpdump; } \
    || { apt-get update && apt-get install -y bgpdump; } \
    || echo "WARN: could not install bgpdump; falling back to the built-in native parser"
fi

echo "==> Python dependencies"
python3 -m pip install --upgrade pip >/dev/null 2>&1 || true
pip install -r requirements.txt || pip install --break-system-packages -r requirements.txt

echo "==> Generating route lists into '${OUT}'"
mkdir -p "$OUT"
python3 mrt_cn_routes.py \
  --output-dir "$OUT" \
  --cache-dir "$CACHE" \
  --timeout 180 \
  --verbose

echo "==> Output files:"
ls -lh "$OUT"
