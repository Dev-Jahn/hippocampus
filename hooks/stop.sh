#!/usr/bin/env bash
# hippo Stop hook.
# Silent no-op unless cwd sits inside a project that has .hippo/.
# Launches the scribe detached and returns immediately (<100ms budget).
set -u

# A dispatched lane gets no scribe (§9.7): a per-lane Stop would multiply clerk cost by the
# batch width, and a scribe reading a lane's transcript would write src=scribe rows — verdicts —
# out of a worker's self-narrative. HIPPO_DISPATCH, planted by the wrapper, is the gate.
[ -n "${HIPPO_DISPATCH:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ "$(json_get stop_hook_active)" = "true" ] && exit 0

transcript="$(json_get transcript_path)"
session="$(json_get session_id)"
cwd="$(json_get cwd)"
project_root "$cwd" >/dev/null || exit 0
[ -n "$transcript" ] || exit 0
[ -n "$session" ] || exit 0

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
  exec ${detach[@]+"${detach[@]}"} "$plugin_root/bin/hippo" scribe \
    --transcript "$transcript" --session "$session" \
    </dev/null >/dev/null 2>&1
) &

exit 0
