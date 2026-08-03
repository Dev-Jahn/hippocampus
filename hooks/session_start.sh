#!/usr/bin/env bash
# hippo SessionStart hook.
# Silent no-op unless the session's cwd sits inside a project that has .hippo/.
set -u

# A clerk runs headless Claude/codex sessions of its own; their hooks must not
# fire, or a Stop hook would spawn a clerk that spawns a clerk (no recursion, §2).
[ -n "${HIPPO_CLERK:-}" ] && exit 0

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
  if [ -d "$dir/.git" ]; then   # a real repo root: never adopt a project from beyond it
    break
  fi
  # A .git *file* is a linked worktree. For a dispatched lane (HIPPO_DISPATCH, planted by
  # the wrapper) keep walking to the project root, same as the CLI (§9.1) — this is what
  # re-injects the capsule after the lane's own compaction. Ordinary sessions keep the
  # conservative stop.
  if [ -e "$dir/.git" ] && [ -z "${HIPPO_DISPATCH:-}" ]; then
    break
  fi
  [ -n "${HOME:-}" ] && [ "$dir" = "$HOME" ] && break
  parent="$(dirname "$dir")"
  [ "$parent" = "$dir" ] && break
  dir="$parent"
done

[ -n "$found" ] || exit 0

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
root="${CLAUDE_PLUGIN_ROOT:-$(dirname "$script_dir")}"

# cd into the session's cwd (as Stop does): the CLI re-derives .hippo/ from
# its own working directory, which is not necessarily this hook's.
cd "$cwd" || exit 0
capsule="$("$root/bin/hippo" status --inject 2>/dev/null)"
[ -n "$capsule" ] || exit 0

# The capsule ships as JSON, not bare text: codex 0.146 parses SessionStart stdout
# strictly as JSON and rejects anything else ("hook returned invalid session start
# JSON output" — 0.144 accepted the bare text, so this broke on a host upgrade, not
# on a hippo change). Claude Code reads the same `hookSpecificOutput.additionalContext`
# shape, so one output serves both hosts — the alternative, branching on the host, is a
# guess about which host is running and drifts the moment either contract moves.
if [ "$HAVE_JQ" = "1" ]; then
  jq -n --arg c "$capsule" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
else
  printf '%s' "$capsule" | python3 -c '
import json, sys
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart", "additionalContext": sys.stdin.read()}}))
'
fi
