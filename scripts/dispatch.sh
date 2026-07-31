#!/usr/bin/env bash
# dispatch.sh — codex exec wrapper that auto-records ev:dispatch (DESIGN §3.6).
#
# Usage: dispatch.sh --kind <kind> --scope <scope> [--task <task-id>] <codex exec args...>
#
# --kind/--scope/--task are dispatch.sh's own flags and are stripped. Every
# other argument is forwarded to `codex exec` byte-for-byte. -m <model> and
# a -c model_reasoning_effort=<effort> found among those remaining args are
# read (not removed) to build the ledger's exec field "codex/<model>/<effort>".
set -u

usage() {
  echo "Usage: dispatch.sh --kind <kind> --scope <scope> [--task <task-id>] <codex exec args...>" >&2
}

kind=""
scope=""
task=""
rest=()

# Each two-argument flag checks that its value is actually there: without the
# check, `dispatch.sh --kind` dies with a raw bash "unbound variable" instead of
# this script's own usage.
need_value() {
  if [ "$2" -lt 2 ]; then
    echo "dispatch: $1 에 값이 없습니다" >&2
    usage
    exit 2
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --kind) need_value --kind "$#"; kind=$2; shift 2 ;;
    --kind=*) kind=${1#--kind=}; shift ;;
    --scope) need_value --scope "$#"; scope=$2; shift 2 ;;
    --scope=*) scope=${1#--scope=}; shift ;;
    --task) need_value --task "$#"; task=$2; shift 2 ;;
    --task=*) task=${1#--task=}; shift ;;
    *) rest+=("$1"); shift ;;
  esac
done

if [ -z "$kind" ] || [ -z "$scope" ]; then
  usage
  exit 2
fi

# Read (don't consume) -m/--model and -c model_reasoning_effort=<e> from the
# args destined for codex exec, to auto-fill the ledger's exec label.
model=""
effort=""
n=${#rest[@]}
i=0
while [ "$i" -lt "$n" ]; do
  arg=${rest[$i]}
  case "$arg" in
    -m|--model)
      i=$((i + 1))
      model=${rest[$i]:-}
      ;;
    -c)
      i=$((i + 1))
      val=${rest[$i]:-}
      case "$val" in
        model_reasoning_effort=*)
          effort=${val#model_reasoning_effort=}
          effort=${effort%\"}
          effort=${effort#\"}
          ;;
      esac
      ;;
  esac
  i=$((i + 1))
done

exec_field="codex/${model:-unset}/${effort:-unset}"

# 128-bit dispatch id. Keep the established d+hex shape while making collisions
# negligible even for ledgers with many thousands of dispatches.
id="d$(LC_ALL=C od -An -N16 -tx1 /dev/urandom | tr -d '[:space:]')"

# Resolve bin/hippo relative to this script's real location.
src="${BASH_SOURCE[0]}"
while [ -L "$src" ]; do
  d="$(cd -P "$(dirname "$src")" && pwd)"
  src="$(readlink "$src")"
  [[ "$src" != /* ]] && src="$d/$src"
done
root="$(cd -P "$(dirname "$src")/.." && pwd)"
hippo_bin="$root/bin/hippo"

log_args=(log dispatch --id "$id" --kind "$kind" --scope "$scope" --exec "$exec_field")
if [ -n "$task" ]; then
  log_args+=(--task "$task")
fi

# The ledger line goes to stderr, not stdout: DESIGN §3.6 reserves stdout's
# first line for the dispatch id.
if [ -x "$hippo_bin" ]; then
  HIPPO_SRC=wrapper "$hippo_bin" "${log_args[@]}" >&2
  log_status=$?
else
  log_status=127
fi
if [ "$log_status" -ne 0 ]; then
  echo "dispatch: hippo log dispatch 기록 실패 (exit $log_status) — 위임은 계속 진행합니다" >&2
fi

echo "dispatch:$id"

# ${rest[@]+...} guard: bash 3.2 (stock macOS) treats "${rest[@]}" on an empty
# array as an unbound variable under `set -u`.
exec codex exec ${rest[@]+"${rest[@]}"} < /dev/null
