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

echo "==> Generating index.html (browsable root for Cloudflare/Gitee Pages)"
{
  echo '<!doctype html><html lang="en"><head><meta charset="utf-8">'
  echo '<meta name="viewport" content="width=device-width,initial-scale=1">'
  echo '<title>China ASN CIDR lists</title>'
  echo '<style>body{font:15px/1.6 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}'
  echo 'h1{font-size:1.3rem}code{background:#f2f2f2;padding:.1em .3em;border-radius:3px}'
  echo 'li{margin:.2em 0}.muted{color:#666;font-size:.9em}</style></head><body>'
  echo '<h1>China ASN CIDR aggregation lists</h1>'
  echo '<p class="muted">Auto-generated from public MRT collectors (RouteViews, RIPE RIS, PCH). Plain-text CIDR lists, one prefix per line.</p>'
  echo '<ul>'
  for f in "$OUT"/*_v4.txt "$OUT"/*_v6.txt; do
    [ -e "$f" ] || continue
    b="$(basename "$f")"
    echo "<li><a href=\"./${b}\">${b}</a></li>"
  done
  echo '</ul>'
  if [ -e "$OUT/summary.json" ]; then
    echo '<p><a href="./summary.json">summary.json</a></p>'
  fi
  echo '</body></html>'
} > "$OUT/index.html"

echo "==> Output files:"
ls -lh "$OUT"
