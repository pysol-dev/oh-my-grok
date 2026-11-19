#!/usr/bin/env bash
# Restore oh-my-grok state from the restic repository named by backup_repo.
#
# Lock handling is done by python3 -m oh_my_grok.recover, in this order:
#   1. Read locks before changing the home, the config, or jobs.
#   2. A lock from another machine younger than 30 minutes, or a lock whose pid is still
#      alive here, waits with --retry-lock 2m and then exits 16. It is not removed.
#   3. Any other lock is stale. It is removed with plain `restic unlock`.
#      Never pass --remove-all.
#   4. After a stale lock is removed, `restic check` must succeed before a snapshot is applied.
#
# A snapshot whose event stream extends the local stream is restored. A local stream that is
# already ahead is kept. Diverged streams exit 11 and change nothing. A newer schema is refused.
# An older schema is migrated forward after the files are in place.
#
# Exit codes:
#   0  restored, already current, or local state is newer
#   1  internal error; a traceback is not printed
#   2  usage, or python3 / OMG_RESTIC_PASSWORD missing
#   8  scripts/bootstrap.sh failed
#  10  no snapshot
#  11  local state diverged; nothing changed
#  12  check failed, or the snapshot schema is newer than this code; nothing changed
#  13  password rejected
#  14  --check only: a restore would be required
#  15  restore was applied, then watchdog ensure or doctor failed
#  16  a lock is still held after --retry-lock 2m
#
# OMG_RESTIC_PASSWORD is read from the environment and never printed.
# OMG_DISABLE_TRANSPORT=1 is exported so a restored hooks file cannot POST.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    -h|--help)
      sed -n '2,32p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "need python3" >&2; exit 2; }
if [ -z "${OMG_RESTIC_PASSWORD:-}" ]; then
  echo "OMG_RESTIC_PASSWORD is not set" >&2
  exit 2
fi

if [ "$CHECK" = 0 ]; then
  env -u OMG_RESTIC_PASSWORD "$ROOT/scripts/bootstrap.sh" || exit 8
fi

export OMG_DISABLE_TRANSPORT=1
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
args=()
if [ "$CHECK" = 1 ]; then
  args+=(--check)
fi
set +e
python3 -m oh_my_grok.recover "${args[@]}"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
if [ "$CHECK" = 1 ]; then
  exit 0
fi

if ! "$ROOT/omg" watchdog ensure; then
  echo "watchdog ensure failed" >&2
  exit 15
fi
if ! "$ROOT/omg" doctor; then
  echo "doctor reported problems after restore" >&2
  exit 15
fi
exit 0
