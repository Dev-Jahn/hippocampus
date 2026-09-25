#!/usr/bin/env bash
# hippo PreCompact hook (Claude Code only, §3.4): plain text on stdout (exit 0) is appended to
# the compaction instructions. It asks the summary to end with `## hippo deltas` — the commands
# that would record what the conversation changed and hippo's lists do not show yet — and
# SessionStart(compact) then points main at them.
# Silent no-op unless the session's cwd sits inside a project that has .hippo/.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# A subagent compacting its own context is not main: commands it ran would land as main's
# (src=cli). agent_id marks a subagent's events (SubagentStart, SubagentStop), but on 2.1.282 a
# subagent's own compaction fires this hook with main's session_id and transcript_path and no
# agent_id — nothing on stdin or in main's transcript tells it from main's (measured, §3.4) —
# so today it gets the request too; the check holds for a host that sends the marker.
[ -n "$(json_get agent_id)" ] && exit 0

cwd="$(json_get cwd)"
project_root "$cwd" >/dev/null || exit 0

inject precompact "$cwd"
exit 0
