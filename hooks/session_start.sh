#!/usr/bin/env bash
# hippo SessionStart hook: the capsule (§6), at startup, resume, clear and after a compaction.
# Silent no-op unless the session's cwd sits inside a project that has .hippo/.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cwd="$(json_get cwd)"
project_root "$cwd" >/dev/null || exit 0

# The source rides along (the transcript path too, lib.sh) so that after a compaction main's
# capsule can point at the summary's `## hippo deltas` section (PreCompact asked for it, §3.4),
# and an agent's own compaction, which fires this hook as main's, gets its SubagentStart slice.
capsule="$(inject "$(json_get source)" "$cwd")"
[ -n "$capsule" ] || exit 0
context_json SessionStart "$capsule"
