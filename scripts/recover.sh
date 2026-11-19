#!/usr/bin/env bash
# Restore oh-my-grok state from the restic repository named by backup_repo.
#
# Lock handling is done by python3 -m oh_my_grok.recover, in this order:
#   1. Read locks with --no-lock before changing the home, the config, or jobs.
#      An exclusive lock can be classified; it does not abort the read as exit 12.
#   2. An exclusive lock from another machine younger than 30 minutes, or an exclusive lock whose
#      pid is still alive here, waits with --retry-lock 2m and then exits 16. The two-minute wait
#      applies only to exclusive locks. A young non-exclusive foreign lock exits 16 immediately.
#      Neither is removed, and the restore does not continue.
#   3. A stale lock on this machine (the pid is not alive), including an exclusive lock left by
#      a killed restic, is removed with plain `restic unlock`. Never pass --remove-all.
#   4. After that unlock, `restic check` must succeed before a snapshot is applied.
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
#  16  a lock is still held after --retry-lock 2m (not the same code as divergence)
#
# OMG_RESTIC_PASSWORD is read from the environment and never printed.
# OMG_DISABLE_TRANSPORT=1 applies only to the restore process. The watchdog started
# afterwards does not inherit it.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    -h|--help)
      sed -n '2,33p' "$0"
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

if [ "$CHECK" = 0 ] && [ "${OMG_RECOVER_SKIP_BOOTSTRAP:-}" != "1" ]; then
  env -u OMG_RESTIC_PASSWORD "$ROOT/scripts/bootstrap.sh" || exit 8
fi

PY="${OMG_RECOVER_PYTHON:-python3}"
OMG="${OMG_BIN:-$ROOT/omg}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
args=()
if [ "$CHECK" = 1 ]; then
  args+=(--check)
fi
set +e
# Prefix, not export: this process sees the flag, and the parent shell does not keep it.
OMG_DISABLE_TRANSPORT=1 "$PY" -m oh_my_grok.recover "${args[@]}"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
if [ "$CHECK" = 1 ]; then
  exit 0
fi

if ! env -u OMG_DISABLE_TRANSPORT "$OMG" watchdog ensure; then
  echo "watchdog ensure failed" >&2
  exit 15
fi
if ! env -u OMG_DISABLE_TRANSPORT "$OMG" doctor; then
  echo "doctor reported problems after restore" >&2
  exit 15
fi
exit 0
