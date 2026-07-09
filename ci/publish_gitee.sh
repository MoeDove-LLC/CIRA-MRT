#!/usr/bin/env bash
# Publish the generated files as a Gitee Release (uncompressed, original names).
#
# Required environment variables:
#   GITEE_TOKEN   Gitee private access token (repo scope). Set as a Gitee Go
#                 pipeline environment variable / secret.
#   GITEE_OWNER   repo owner (namespace), e.g. "yourname"
#   GITEE_REPO    repo path/name, e.g. "mrt-cn-routes"
# Optional:
#   GITEE_BRANCH  branch to tag from (default: master)
#
# Usage: ci/publish_gitee.sh [dist_dir]
set -euo pipefail

DIST="${1:-dist}"
: "${GITEE_TOKEN:?set GITEE_TOKEN}"
: "${GITEE_OWNER:?set GITEE_OWNER}"
: "${GITEE_REPO:?set GITEE_REPO}"
BRANCH="${GITEE_BRANCH:-master}"
API="https://gitee.com/api/v5"

# Dated tag so each run is a fresh release; Gitee shows the newest on the
# releases page. Use a fixed value here if you prefer a single rolling release.
TAG="build-$(date -u +%Y%m%d-%H%M)"

echo "==> Creating Gitee release ${TAG}"
resp="$(curl -fsS -X POST "${API}/repos/${GITEE_OWNER}/${GITEE_REPO}/releases" \
  -F "access_token=${GITEE_TOKEN}" \
  -F "tag_name=${TAG}" \
  -F "name=CN route lists ${TAG}" \
  -F "body=Auto-generated China ASN CIDR lists (IPv4/IPv6), uncompressed .txt." \
  -F "target_commitish=${BRANCH}")"

release_id="$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')"
echo "    release id = ${release_id}"

shopt -s nullglob
files=("${DIST}"/*.txt "${DIST}"/summary.json)
if [ ${#files[@]} -eq 0 ]; then
  echo "ERROR: no files found in ${DIST}" >&2
  exit 1
fi

for f in "${files[@]}"; do
  echo "==> Uploading $(basename "$f")"
  curl -fsS -X POST "${API}/repos/${GITEE_OWNER}/${GITEE_REPO}/releases/${release_id}/attach_files" \
    -F "access_token=${GITEE_TOKEN}" \
    -F "file=@${f}" >/dev/null
done

echo "==> Done. Download from: https://gitee.com/${GITEE_OWNER}/${GITEE_REPO}/releases/${TAG}"
