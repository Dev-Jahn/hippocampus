#!/usr/bin/env bash
# clerk_run.sh <prompt-file> <input-file>
#
# Concatenates prompt-file ++ input-file into one query, fires it at a
# headless low-cost model backend, and prints the model's raw response to
# stdout. Backend resolution: $HIPPO_CLERK_BACKEND (auto|codex|claude|mock,
# default auto). auto picks codex if the codex CLI is installed, else claude,
# else exits 3. $HIPPO_CLERK_MODEL overrides the model on either backend; unset,
# each backend falls back to its own default (codex: gpt-6-luna, claude: sonnet).
# The whole call is bounded to $HIPPO_CLERK_TIMEOUT seconds
# (default 120; exit 124 on timeout). Backend stderr stays off stdout; on a
# non-zero exit its last few lines (error lines first choice) go to stderr.
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

# Why a backend failed, in its own words. stdout stays the model's raw response (the contract),
# so the backend's stderr goes to a file of its own; on a non-zero exit its own error lines go
# to stderr, the cause first, where the scribe's failure dump records them. Measured on b200:
# with stderr discarded, 7 failed runs in a row (an expired codex login) each dumped
# "clerk rc=1" and an empty stderr. codex prints the reason as its last error line (measured
# with no login, 0.156.1: `ERROR: unexpected status 401 Unauthorized …`).
#
# Only the backend's own words count. codex echoes its whole prompt to stderr — turn-scribe.md,
# the rosters and the transcript digest — so an unanchored search would promote transcript text
# to "the cause" and, after three failures, into main's capsule (measured: a timeout quoted a
# digest line as `codex: …`). A kill leaves no error of the backend's own at all, so it is
# named from the exit code, and a line counts only when it starts the way the backends' own
# errors do: `ERROR:`/`Error:` or a timestamped tracing line. Digest lines start with `[N]`.
ERR_FILE=""
trap '[ -n "$ERR_FILE" ] && rm -f "$ERR_FILE"' EXIT

open_err_file() {  # a file for the backend's stderr, or none: a broken TMPDIR must not stop the call
  ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/hippo-clerk-err.XXXXXX" 2>/dev/null) || ERR_FILE=""
}

explain_failure() {  # explain_failure <rc>
  local rc=$1 picked
  [ "$rc" -ne 0 ] || return 0
  if [ "$rc" -eq 124 ]; then
    printf '%s: timed out after %ss\n' "$BACKEND" "$TIMEOUT" >&2
    return 0
  fi
  if [ "$rc" -gt 128 ]; then
    printf '%s: killed by signal %s\n' "$BACKEND" "$((rc - 128))" >&2
    return 0
  fi
  [ -n "$ERR_FILE" ] && [ -s "$ERR_FILE" ] || return 0
  picked=$(grep -E '^(ERROR|Error)[: ]|^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z +ERROR ' "$ERR_FILE" \
    | uniq | tail -n 5)
  [ -n "$picked" ] || return 0
  printf '%s: %s\n' "$BACKEND" "$(printf '%s\n' "$picked" | tail -n 1)" >&2
  printf '%s\n' "$picked" | sed 's/^/  /' >&2
}

case "$BACKEND" in
  mock)
    if [ -z "${HIPPO_MOCK_OUTPUT:-}" ] || [ ! -r "${HIPPO_MOCK_OUTPUT:-/nonexistent}" ]; then
      exit 4
    fi
    # Tests assert on what the backend was actually handed, not on what the caller meant to send.
    [ -n "${HIPPO_MOCK_CAPTURE:-}" ] && printf '%s' "$COMBINED" > "$HIPPO_MOCK_CAPTURE"
    with_timeout "$TIMEOUT" cat "$HIPPO_MOCK_OUTPUT"
    exit $?
    ;;
  codex)
    if ! command -v codex >/dev/null 2>&1; then
      exit 3
    fi
    # --disable hooks: keep the Stop hook of the codex session this clerk starts from spawning
    # another clerk. Belt and braces with the HIPPO_CLERK guard (survives a stripped environment).
    open_err_file
    with_timeout "$TIMEOUT" codex exec \
      -m "${MODEL:-gpt-6-luna}" \
      -c model_reasoning_effort="low" \
      -c service_tier="fast" \
      -s read-only \
      --disable hooks \
      --skip-git-repo-check \
      --color never \
      "$COMBINED" \
      < /dev/null 2>"${ERR_FILE:-/dev/null}"
    rc=$?
    explain_failure "$rc"
    exit "$rc"
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
    #   --no-session-persistence  save no session (verified 2026-09-27, 2.1.282):
    #                         without it every run was a transcript in the host project's
    #                         folder — 534 of 537 in one project, 506 in another — filling
    #                         --resume.
    # Default model is sonnet at low effort — the same tier as codex's gpt-6-luna at low above:
    # hippo's cheap tier is luna-low where codex exists and sonnet-low where it does not, and
    # never haiku. Haiku was demoted after a measured A/B (2026-07-31): it invented
    # outcome:accepted for a lane whose acceptance was still pending — a semantic error that
    # passes schema validation.
    # claude prints some failures on stdout (a bad model id, measured) — those reach the dump's
    # stdout section as they always did; what it says on stderr is kept the same way as codex's.
    open_err_file
    with_timeout "$TIMEOUT" claude -p \
      --model "${MODEL:-sonnet}" \
      --effort low \
      --tools "" \
      --strict-mcp-config \
      --setting-sources "" \
      --no-session-persistence \
      "$COMBINED" \
      < /dev/null 2>"${ERR_FILE:-/dev/null}"
    rc=$?
    explain_failure "$rc"
    exit "$rc"
    ;;
  *)
    echo "clerk_run: unknown backend: $BACKEND" >&2
    exit 2
    ;;
esac
