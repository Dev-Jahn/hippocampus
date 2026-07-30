#!/usr/bin/env bash
# clerk_run.sh <prompt-file> <input-file>
#
# Concatenates prompt-file ++ input-file into one query, fires it at a
# headless low-cost model backend, and prints the model's raw response to
# stdout. Backend resolution: $WAYSTONE_CLERK_BACKEND (auto|codex|claude|mock,
# default auto). auto picks codex if the codex CLI is installed, else claude,
# else exits 3. The whole call is bounded to $WAYSTONE_CLERK_TIMEOUT seconds
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

BACKEND=${WAYSTONE_CLERK_BACKEND:-auto}
MODEL=${WAYSTONE_CLERK_MODEL:-gpt-5.6-luna}
TIMEOUT=${WAYSTONE_CLERK_TIMEOUT:-120}

# 재귀 금지 (DESIGN §2): 아래 backend는 자기 훅을 가진 세션을 띄울 수 있다.
# 이 표식이 환경에 있으면 waystone 훅들은 즉시 exit 0 한다 — clerk는 clerk를 낳지 않는다.
export WAYSTONE_CLERK=1

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
  marker="$(mktemp "${TMPDIR:-/tmp}/waystone-clerk-timeout.XXXXXX" 2>/dev/null)" || marker=""
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
    if [ -z "${WAYSTONE_MOCK_OUTPUT:-}" ] || [ ! -r "${WAYSTONE_MOCK_OUTPUT:-/nonexistent}" ]; then
      exit 4
    fi
    with_timeout "$TIMEOUT" cat "$WAYSTONE_MOCK_OUTPUT"
    exit $?
    ;;
  codex)
    if ! command -v codex >/dev/null 2>&1; then
      exit 3
    fi
    with_timeout "$TIMEOUT" codex exec \
      -m "$MODEL" \
      -c model_reasoning_effort="low" \
      -s read-only \
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
    with_timeout "$TIMEOUT" claude -p \
      --model haiku \
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
