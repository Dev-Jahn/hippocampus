#!/usr/bin/env bash
# hippo Stop hook.
# Silent no-op unless cwd sits inside a project that has .hippo/.
# Launches the scribe detached and returns immediately (<100ms budget).
set -u

# A clerk runs headless Claude/codex sessions of its own; their hooks must not
# fire, or this hook would spawn a clerk that spawns a clerk (no recursion, §2).
[ -n "${HIPPO_CLERK:-}" ] && exit 0

# A dispatched lane gets no scribe (§9.7): a per-lane Stop would multiply clerk cost by the
# wave width, and a scribe reading a lane's transcript would write src=scribe rows — verdicts —
# out of a worker's self-narrative. HIPPO_DISPATCH, planted by the wrapper, is the gate.
[ -n "${HIPPO_DISPATCH:-}" ] && exit 0

# jq is preferred but not required: python3 is already a hard dependency of the
# CLI shim, so a missing jq must not turn this hook into a silent death.
if command -v jq >/dev/null 2>&1; then
  HAVE_JQ=1
elif command -v python3 >/dev/null 2>&1; then
  HAVE_JQ=0
else
  exit 0
fi

json_get() {  # json_get <top-level key>  -> value or empty string
  if [ "$HAVE_JQ" = "1" ]; then
    printf '%s' "$input" | jq -r --arg k "$1" '.[$k] // empty' 2>/dev/null
  else
    printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
v = d.get(sys.argv[1]) if isinstance(d, dict) else None
if v is None:
    pass
elif isinstance(v, bool):
    print("true" if v else "false")
else:
    print(v)
' "$1" 2>/dev/null
  fi
}

input="$(cat)"

[ "$(json_get stop_hook_active)" = "true" ] && exit 0

transcript="$(json_get transcript_path)"
session="$(json_get session_id)"
cwd="$(json_get cwd)"
[ -n "$cwd" ] || exit 0
[ -d "$cwd" ] || exit 0

# Walk upward from cwd looking for .hippo/, capped at the git root (inclusive)
# and at $HOME (never adopt a project from above the user's home).
dir="$cwd"
found=""
while :; do
  if [ -d "$dir/.hippo" ]; then
    found="$dir"
    break
  fi
  if [ -e "$dir/.git" ]; then   # -e: .git is a file in git worktrees
    break
  fi
  [ -n "${HOME:-}" ] && [ "$dir" = "$HOME" ] && break
  parent="$(dirname "$dir")"
  [ "$parent" = "$dir" ] && break
  dir="$parent"
done

[ -n "$found" ] || exit 0
[ -n "$transcript" ] || exit 0
[ -n "$session" ] || exit 0

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
root="${CLAUDE_PLUGIN_ROOT:-$(dirname "$script_dir")}"

# Real detach: the scribe must survive this hook's exit and must not hold the
# session's terminal. setsid(1) is absent on stock macOS, so fall back to a
# python3 setsid shim (python3 is a hard dependency anyway); a plain background
# job is the last resort. stdin is closed so a backend that reads it cannot
# block forever.
detach=()
if command -v setsid >/dev/null 2>&1; then
  detach=(setsid)
elif command -v python3 >/dev/null 2>&1; then
  detach=(python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])')
fi

(
  cd "$cwd" || exit 0
  exec ${detach[@]+"${detach[@]}"} "$root/bin/hippo" scribe \
    --transcript "$transcript" --session "$session" \
    </dev/null >/dev/null 2>&1
) &

exit 0
