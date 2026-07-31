#!/usr/bin/env bash
# hippo release: projects only the ship allowlist of the current HEAD (dev branch) onto main.
# Pushing main triggers .github/workflows/sync-marketplace.yml, which refreshes the marketplace
# pin (sha, version, description). main is effectively a dist branch — never commit to it directly.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

[ -z "$(git status --porcelain)" ] || { echo "working tree dirty — commit before releasing" >&2; exit 1; }
src=$(git rev-parse HEAD)
ver=$(jq -r .version .claude-plugin/plugin.json)

echo "== gate: tests =="
tests/run.sh

# Only what an installed plugin needs (dev-only excluded: tests/ DESIGN.md CLAUDE.md .claude/ ci.yml).
# The README logos live in .github/assets/ — absent here, so they never reach the distribution.
SHIP=(
  .claude-plugin
  .codex-plugin
  .github/workflows/sync-marketplace.yml
  .github/workflows/sync-codex-marketplace.yml
  .gitignore
  LICENSE
  README.md
  bin
  cli
  clerks
  hooks
  scripts
  skills
)

idx=$(mktemp -u)
export GIT_INDEX_FILE="$idx"
git read-tree --empty
git add -- "${SHIP[@]}"
tree=$(git write-tree)
unset GIT_INDEX_FILE
rm -f "$idx"

parent=$(git rev-parse -q --verify origin/main || git rev-parse -q --verify main || true)
msg="release v${ver} (from ${src:0:9})"
if [ -n "$parent" ]; then
  commit=$(git commit-tree "$tree" -p "$parent" -m "$msg")
else
  commit=$(git commit-tree "$tree" -m "$msg")
fi

git push origin "${commit}:refs/heads/main"
git tag -f "v${ver}" "$commit"
git push -f origin "v${ver}"
echo "released v${ver}: main=${commit:0:9} (src ${src:0:9}) — CI refreshes the marketplace pin"
