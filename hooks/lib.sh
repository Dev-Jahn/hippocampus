# hippo hook library — sourced by every hook in this directory, never run on its own.
# Sourcing it is the common gate: it exits 0 for a clerk's own session, reads the stdin JSON
# into $input, and defines json_get, project_root, inject and context_json (§3.4).

# A clerk runs headless Claude/codex sessions of its own; their hooks must not fire, or a Stop
# hook would spawn a clerk that spawns a clerk (no recursion, §2).
[ -n "${HIPPO_CLERK:-}" ] && exit 0

# jq is preferred but not required: python3 is already a hard dependency of the CLI shim, so a
# missing jq must not turn a hook into a silent death.
if command -v jq >/dev/null 2>&1; then
  HAVE_JQ=1
elif command -v python3 >/dev/null 2>&1; then
  HAVE_JQ=0
else
  exit 0
fi

input="$(cat)"

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

# project_root <cwd> [through]  -> prints the project that owns <cwd>, or nothing (every hook
# is then a silent no-op, §3.1). The CLI's walk_hippo in shell — the two must agree (§3.3):
# walking up, a .git directory ends the walk; a .hippo/ at or below a .git *file* (a linked
# worktree) is a checked-out copy, never the project's state; $HOME ends the walk unless it is
# <cwd> itself. Only the file system is read — this runs at every Stop and SessionStart.
# `through` crosses a linked worktree into its main worktree (SubagentStart, and SessionStart for
# a compaction: the host isolates a subagent in its own worktree under the project's
# .claude/worktrees/, and that subagent is still the project's worker); so does a dispatched lane
# (HIPPO_DISPATCH). Without either a worktree session has no project: no capsule, no scribe.
project_root() {
  local dir="$1" through="${2:-}" found="" main parent
  [ -n "$dir" ] && [ -d "$dir" ] || return 1
  # HIPPO_DIR, planted by the dispatch wrapper, names the ledger that launched this lane wherever
  # its cwd is (§9.1) — the CLI re-derives it from env anyway; here it decides whether to run.
  if [ -n "${HIPPO_DIR:-}" ] && [ -d "$HIPPO_DIR" ]; then
    dirname "$HIPPO_DIR"
    return 0
  fi
  while :; do
    [ -n "${HOME:-}" ] && [ "$dir" = "$HOME" ] && [ "$dir" != "$1" ] && break
    [ -z "$found" ] && [ -d "$dir/.hippo" ] && found="$dir"
    [ -d "$dir/.git" ] && break   # a repo root: never adopt a project from beyond it
    if [ -e "$dir/.git" ]; then
      # A linked worktree: whatever was found inside it is a copy.
      [ -z "${HIPPO_DISPATCH:-}" ] && [ -z "$through" ] && return 1
      if main="$(main_worktree "$dir")"; then
        [ -d "$main/.hippo" ] || return 1
        printf '%s\n' "$main"
        return 0
      fi
      found=""   # a submodule: its superproject is above it
    fi
    [ -n "${HOME:-}" ] && [ "$dir" = "$HOME" ] && break
    parent="${dir%/*}"
    [ -n "$parent" ] || parent=/
    [ "$parent" = "$dir" ] && break
    dir="$parent"
  done
  [ -n "$found" ] || return 1
  printf '%s\n' "$found"
}

# main_worktree <linked worktree root>  -> the main worktree's path: the .git file's gitdir, whose
# commondir is <main>/.git. Fails for a submodule (no commondir) and a bare repository.
main_worktree() {
  local line gitdir common
  IFS= read -r line < "$1/.git" || [ -n "$line" ] || return 1
  case "$line" in "gitdir: "*) gitdir="${line#gitdir: }" ;; *) return 1 ;; esac
  case "$gitdir" in /*|[A-Za-z]:*) ;; *) gitdir="$1/$gitdir" ;; esac
  IFS= read -r common < "$gitdir/commondir" 2>/dev/null || [ -n "$common" ] || return 1
  case "$common" in /*|[A-Za-z]:*) ;; *) common="$gitdir/$common" ;; esac
  common="$(cd "$common" 2>/dev/null && pwd -P)" || return 1
  [ "${common##*/}" = .git ] || return 1
  printf '%s\n' "${common%/.git}"
}

plugin_root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)}"

# inject <moment> <cwd>  -> the CLI's text for that moment. The moment travels in HIPPO_INJECT,
# an internal env var rather than a flag (the surface stays what the skill documents): a
# SessionStart source, `subagent` or `precompact`; stdin's transcript_path, prompt_id and
# trigger ride in HIPPO_TRANSCRIPT, HIPPO_PROMPT and HIPPO_TRIGGER, which is how a compaction
# tells an agent's own from main's (§3.4). The CLI runs from <cwd> because it re-derives .hippo/
# from its own working directory, which is not necessarily this hook's.
inject() {
  local transcript prompt trigger
  transcript="$(json_get transcript_path)"
  prompt="$(json_get prompt_id)"
  trigger="$(json_get trigger)"
  (cd "$2" 2>/dev/null && HIPPO_INJECT="$1" HIPPO_TRANSCRIPT="$transcript" \
    HIPPO_PROMPT="$prompt" HIPPO_TRIGGER="$trigger" \
    "$plugin_root/bin/hippo" status --inject 2>/dev/null)
}

# context_json <event> <text>  -> the hookSpecificOutput envelope. It ships as JSON, not bare
# text: codex 0.146 parses SessionStart stdout strictly as JSON and rejects anything else
# ("hook returned invalid session start JSON output" — 0.144 accepted the bare text, so this broke
# on a host upgrade, not on a hippo change), and Claude Code injects SubagentStart context only
# from this envelope (plain text is dropped, measured on 2.1.281). One shape serves both hosts —
# branching on the host would be a guess that drifts the moment either contract moves.
context_json() {
  if [ "$HAVE_JQ" = "1" ]; then
    jq -n --arg e "$1" --arg c "$2" \
      '{hookSpecificOutput: {hookEventName: $e, additionalContext: $c}}'
  else
    printf '%s' "$2" | python3 -c '
import json, sys
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": sys.argv[1], "additionalContext": sys.stdin.read()}}))
' "$1"
  fi
}
