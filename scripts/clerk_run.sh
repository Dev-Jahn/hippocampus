#!/usr/bin/env bash
# clerk_run.sh <prompt-file> <input-file>
#
# Concatenates prompt-file ++ input-file into one query, fires it at a
# headless low-cost model backend, and prints the model's raw response to
# stdout. Backend resolution: $HIPPO_CLERK_BACKEND (auto|codex|claude|mock,
# default auto). auto picks codex if the codex CLI is installed, else claude,
# else exits 3. $HIPPO_CLERK_MODEL overrides the model on either backend; unset,
# each backend falls back to its own default (codex: gpt-5.6-luna, claude: sonnet).
# The whole call is bounded to $HIPPO_CLERK_TIMEOUT seconds
# (default 120; exit 124 on timeout). Backend stderr is discarded.
set -u

usage() {
  echo "Usage: clerk_run.sh <prompt-file> <input-file>" >&2
}

if [ "$#" -ne 2 ]; then
  usage
  exit 2
fi

PROMPT_FILE=$1
INPUT_FILE=$2

for f in "$PROMPT_FILE" "$INPUT_FILE"; do
  if [ ! -r "$f" ]; then
    echo "clerk_run: no such file: $f" >&2
    exit 2
  fi
done

BACKEND=${HIPPO_CLERK_BACKEND:-auto}
# One override for both backends. The default is per-backend because the two name their
# models differently — a single default would be an invalid id on the other backend.
# Set this only when the backend is also pinned; a value meant for one backend is
# nonsense to the other, and the backend is what `auto` resolves, not what you asked for.
MODEL=${HIPPO_CLERK_MODEL:-}
TIMEOUT=${HIPPO_CLERK_TIMEOUT:-120}

# No recursion (DESIGN §2): the backends below can start sessions that carry their own hooks.
# With this marker in the environment every hippo hook exits 0 at once — a clerk never spawns a clerk.
export HIPPO_CLERK=1

if [ "$BACKEND" = "auto" ]; then
  if command -v codex >/dev/null 2>&1; then
    BACKEND=codex
  elif command -v claude >/dev/null 2>&1; then
    BACKEND=claude
  else
    exit 3
  fi
fi

# Bound the whole call regardless of whether a `timeout` binary is
# installed on this machine (plain macOS ships neither timeout nor gtimeout).
with_timeout() {
  local secs=$1
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
    return $?
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
    return $?
  fi
  # Hand-rolled watchdog. Two things a naive version gets wrong:
  #   1. every signal death reads as a timeout (rc 124) — including a plain
  #      Ctrl-C or an OOM kill, which are not timeouts;
  #   2. only the direct child is killed, so the backend's own children (codex's
  #      helper processes, for instance) survive as orphans holding the pipe.
  # A marker file written by the watchdog distinguishes the real timeout, and
  # the child is started in its own session so its whole group can be reaped.
  local marker
  marker="$(mktemp "${TMPDIR:-/tmp}/hippo-clerk-timeout.XXXXXX" 2>/dev/null)" || marker=""
  [ -n "$marker" ] && rm -f "$marker"
  local launcher=()
  if command -v python3 >/dev/null 2>&1; then
    launcher=(python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])')
  fi
  ${launcher[@]+"${launcher[@]}"} "$@" &
  local pid=$!
  (
    sleep "$secs"
    [ -n "$marker" ] && : > "$marker"
    kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
  ) >/dev/null 2>&1 &
  local watcher=$!
  wait "$pid"
  local status=$?
  kill "$watcher" >/dev/null 2>&1
  wait "$watcher" 2>/dev/null
  if [ -n "$marker" ] && [ -e "$marker" ]; then
    rm -f "$marker"
    return 124
  fi
  [ -n "$marker" ] && rm -f "$marker"
  return "$status"
}

COMBINED=$(cat "$PROMPT_FILE" "$INPUT_FILE")

case "$BACKEND" in
  mock)
    if [ -z "${HIPPO_MOCK_OUTPUT:-}" ] || [ ! -r "${HIPPO_MOCK_OUTPUT:-/nonexistent}" ]; then
      exit 4
    fi
    with_timeout "$TIMEOUT" cat "$HIPPO_MOCK_OUTPUT"
    exit $?
    ;;
  codex)
    if ! command -v codex >/dev/null 2>&1; then
      exit 3
    fi
    # --disable hooks: keep the Stop hook of the codex session this clerk starts from spawning
    # another clerk. Belt and braces with the HIPPO_CLERK guard (survives a stripped environment).
    with_timeout "$TIMEOUT" codex exec \
      -m "${MODEL:-gpt-5.6-luna}" \
      -c model_reasoning_effort="low" \
      -c service_tier="fast" \
      -s read-only \
      --disable hooks \
      --skip-git-repo-check \
      --color never \
      "$COMBINED" \
      < /dev/null 2>/dev/null
    exit $?
    ;;
  claude)
    if ! command -v claude >/dev/null 2>&1; then
      exit 3
    fi
    # Flags verified against `claude -p --help` on this machine (2026-07-31):
    #   --tools ""            no built-in tools at all (empirically confirmed:
    #                         the model cannot write files). Variadic, so it is
    #                         followed by another flag, never by the prompt.
    #   --strict-mcp-config   with no --mcp-config given, this loads no MCP servers.
    #   --setting-sources ""  load no user/project/local settings → no hooks,
    #                         no plugins of the host project inside the clerk.
    # Default model is sonnet. Haiku was demoted after a measured A/B (2026-07-31): it invented
    # outcome:accepted for a lane whose acceptance was still pending — a semantic error that
    # passes schema validation.
    with_timeout "$TIMEOUT" claude -p \
      --model "${MODEL:-sonnet}" \
      --tools "" \
      --strict-mcp-config \
      --setting-sources "" \
      "$COMBINED" \
      < /dev/null 2>/dev/null
    exit $?
    ;;
  *)
    echo "clerk_run: unknown backend: $BACKEND" >&2
    exit 2
    ;;
esac
