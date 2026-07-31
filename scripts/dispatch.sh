#!/usr/bin/env bash
# 호환 shim. 정본은 `hippo dispatch` (DESIGN §3.6) — dispatch가 CLI 하위 명령이 되기
# 전에 만들어진 소비 프로젝트의 진입점(예: tools/dispatch)이 계속 동작하도록 남긴다.
set -u

# bin/hippo 를 이 스크립트의 실제 위치 기준으로 해소한다 (심링크 경유 호출 대비).
src="${BASH_SOURCE[0]}"
while [ -L "$src" ]; do
  dir=$(cd -P "$(dirname "$src")" && pwd)
  src=$(readlink "$src")
  case "$src" in /*) ;; *) src="$dir/$src" ;; esac
done
root=$(cd -P "$(dirname "$src")/.." && pwd)

exec "$root/bin/hippo" dispatch "$@"
