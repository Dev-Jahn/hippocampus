#!/usr/bin/env bash
# hippo 릴리스: 현재 HEAD(개발 브랜치 dev)의 ship allowlist만 main으로 투영한다.
# main push → .github/workflows/sync-marketplace.yml 이 marketplace pin(sha·version·
# description)을 자동 갱신한다. main은 사실상 dist 브랜치다 — 직접 커밋 금지.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

[ -z "$(git status --porcelain)" ] || { echo "working tree dirty — 커밋 후 릴리스하라" >&2; exit 1; }
src=$(git rev-parse HEAD)
ver=$(jq -r .version .claude-plugin/plugin.json)

echo "== gate: tests =="
tests/run.sh

# 플러그인 설치본에 필요한 것만 (개발 전용 제외: tests/ DESIGN.md CLAUDE.md .claude/ ci.yml).
# README 로고는 .github/assets/ 에 산다 — 여기 없으므로 배포판에 실리지 않는다.
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
echo "released v${ver}: main=${commit:0:9} (src ${src:0:9}) — marketplace pin은 CI가 갱신"
