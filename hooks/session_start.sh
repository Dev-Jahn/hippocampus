#!/usr/bin/env bash
# hippo SessionStart hook: the capsule (§6), at startup, resume, clear and after a compaction.
# Silent no-op unless the session's cwd sits inside a project that has .hippo/.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

cwd="$(json_get cwd)"
moment="$(json_get source)"
if ! project_root "$cwd" >/dev/null; then
  # An isolation:"worktree" agent compacts in <project>/.claude/worktrees/agent-<id>, past the
  # .git file the walk stops at. For a compaction only, walk `through` it as SubagentStart does,
  # and the CLI prints that agent's slice or nothing: a main session run inside a worktree has
  # no capsule at startup, and gets none after its own compaction either.
  [ "$moment" = compact ] && project_root "$cwd" through >/dev/null || exit 0
  moment=worktree-compact
fi

# The source rides along (the transcript path too, lib.sh) so that after a compaction main's
# capsule can point at the summary's `## hippo deltas` section (PreCompact asked for it, §3.4),
# and an agent's own compaction, which fires this hook as main's, gets its SubagentStart slice.
capsule="$(inject "$moment" "$cwd")"
[ -n "$capsule" ] || exit 0
context_json SessionStart "$capsule"
