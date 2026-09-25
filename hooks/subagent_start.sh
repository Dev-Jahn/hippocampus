#!/usr/bin/env bash
# hippo SubagentStart hook (Claude Code only, §3.4): a native subagent starts with the live
# directives addressed to executors — the project's recorded rules reach the worker without main
# copying them into every brief — and one line saying hippo's task, directive and outcome writes
# are main's unless its brief asks for one.
# Silent no-op unless the session's cwd sits inside a project that has .hippo/.
# The host holds the subagent's first request until this returns, so it stays one CLI call.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# A fork already carries main's context, capsule included; hippo:lane only watches a codex lane,
# and the lane gets its own capsule from codex's SessionStart (§3.4).
case "$(json_get agent_type)" in
  fork|hippo:lane) exit 0 ;;
esac

# `through`: an isolation:"worktree" agent starts in <project>/.claude/worktrees/agent-<id>
# (measured, 2.1.281), past a .git file the walk would otherwise stop at.
cwd="$(json_get cwd)"
project_root "$cwd" through >/dev/null || exit 0

text="$(inject subagent "$cwd")"
[ -n "$text" ] || exit 0
context_json SubagentStart "$text"
