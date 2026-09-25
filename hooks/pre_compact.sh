#!/usr/bin/env bash
# hippo PreCompact hook (Claude Code only, §3.4): plain text on stdout (exit 0) is appended to
# the compaction instructions. It asks the summary to end with `## hippo deltas` — the commands
# that would record what the conversation changed and hippo's lists do not show yet — and
# SessionStart(compact) then points main at them.
# Silent no-op unless the session's cwd sits inside a project that has .hippo/.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# An agent compacting its own context is not main: commands it ran would land as main's
# (src=cli). On 2.1.282 its compaction fires this hook as main's — main's session_id and
# transcript_path, no agent_id (measured, §3.4) — so the CLI tells it apart on disk: main is
# at no point where it could be compacting, and an agent is. This check is for a host that
# marks the event the way it marks SubagentStart and SubagentStop.
[ -n "$(json_get agent_id)" ] && exit 0

# No `through` here, unlike SessionStart(compact): an isolation:"worktree" agent compacts in its
# worktree, and the walk's stop at that .git file is right either way — an agent's own
# compaction is asked for nothing (above), and a main session run inside a worktree has no
# capsule there, so no deltas either.
cwd="$(json_get cwd)"
project_root "$cwd" >/dev/null || exit 0

inject precompact "$cwd"
exit 0
