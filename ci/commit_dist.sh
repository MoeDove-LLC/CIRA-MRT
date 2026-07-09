#!/usr/bin/env bash
# Commit the generated dist/ back into the repository so a static host
# (Cloudflare Pages / Gitee Pages) can serve the files directly.
# Idempotent: does nothing when the output is unchanged.
#
# Environment:
#   TARGET_BRANCH     branch to push to (default: current branch, else 'main')
#   DIST_COMMIT_MSG   commit message
#   GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL   committer identity
# The caller must have push credentials configured on 'origin'
# (GitHub Actions: actions/checkout does this; Gitee: set a token remote first).
set -euo pipefail

DIST="${1:-dist}"
MSG="${DIST_COMMIT_MSG:-chore(data): update route lists [skip ci]}"

branch="${TARGET_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)}"
[ "$branch" = "HEAD" ] && branch="${DEFAULT_BRANCH:-main}"

git config user.name  "${GIT_AUTHOR_NAME:-ci-bot}"
git config user.email "${GIT_AUTHOR_EMAIL:-ci-bot@users.noreply.github.com}"

# -A so that files no longer generated (e.g. after a group rename) are removed.
git add -A -f "$DIST"
if git diff --staged --quiet; then
  echo "dist/ unchanged - nothing to commit."
  exit 0
fi
git commit -m "$MSG"

for attempt in 1 2 3; do
  if git push origin "HEAD:refs/heads/${branch}"; then
    echo "Pushed dist/ to '${branch}'."
    exit 0
  fi
  echo "push failed (attempt ${attempt}); syncing with origin and retrying..."
  git fetch origin "${branch}" >/dev/null 2>&1 || true
  git rebase "origin/${branch}" >/dev/null 2>&1 || git rebase --abort >/dev/null 2>&1 || true
  sleep $((attempt * 3))
done

echo "ERROR: could not push dist/ to '${branch}'." >&2
exit 1
