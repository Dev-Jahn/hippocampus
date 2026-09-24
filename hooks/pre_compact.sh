#!/usr/bin/env bash
# hippo PreCompact hook (Claude Code only, §3.4): plain text on stdout (exit 0) is appended to
# the compaction instructions. It asks the summary to end with `## hippo deltas` — the commands
# that would record what the conversation changed and hippo's lists do not show yet — and
# SessionStart(compact) then points main at them.
# Silent no-op unless the session's cwd sits inside a project that has .hippo/.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# A subagent compacting its own context is not main: commands it ran would land as main's
# (src=cli). Main's PreCompact carries no agent_id (measured); the host adds one to events fired
# inside a subagent, as SubagentStart/SubagentStop show — for compaction that is a guard, unmeasured.
[ -n "$(json_get agent_id)" ] && exit 0

cwd="$(json_get cwd)"
project_root "$cwd" >/dev/null || exit 0

inject precompact "$cwd"
exit 0
