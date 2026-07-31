#!/usr/bin/env bash
# Compatibility shim. The real surface is `hippo dispatch` (DESIGN §3.6); this stays so that
# entry points written before dispatch became a CLI subcommand (e.g. a project's tools/dispatch)
# keep working.
set -u

# Resolve bin/hippo from this script's real location (it may be reached through a symlink).
src="${BASH_SOURCE[0]}"
while [ -L "$src" ]; do
  dir=$(cd -P "$(dirname "$src")" && pwd)
  src=$(readlink "$src")
  case "$src" in /*) ;; *) src="$dir/$src" ;; esac
done
root=$(cd -P "$(dirname "$src")/.." && pwd)

exec "$root/bin/hippo" dispatch "$@"
