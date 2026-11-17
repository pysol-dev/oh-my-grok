#!/usr/bin/env bash
# Restore oh-my-grok state from the newest signed backup bundle.
#
# Needs system python3 and gpg. Does not use the project venv for decrypt or the restore
# decision. ./omg (the venv) is used only after a restore decision, and only when --check
# was not passed.
#
# Exit codes (spec/DURABILITY.md):
#   0  restored, already current, or local state is newer (nothing overwritten)
#   2  usage, or python3 / gpg / OMG_BACKUP_PASSPHRASE missing
#   8  scripts/bootstrap.sh failed
#  10  no bundle found
#  11  local state diverged; nothing changed
#  12  bad signature or manifest mismatch; nothing changed
#  13  decrypt failed; nothing changed
#  14  --check only: a restore would be required
#  15  restore was applied, then watchdog ensure or doctor failed
#
# --check does not run bootstrap and does not write the home, the config dir, or jobs.
# OMG_BACKUP_PASSPHRASE is read from the environment and never printed.
# OMG_DISABLE_TRANSPORT, when set, is inherited by ./omg and the watchdog it starts.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "need python3" >&2; exit 2; }
command -v gpg >/dev/null 2>&1 || { echo "need gpg" >&2; exit 2; }
if [ -z "${OMG_BACKUP_PASSPHRASE:-}" ]; then
  echo "OMG_BACKUP_PASSPHRASE is not set" >&2
  exit 2
fi

if [ "$CHECK" = 0 ]; then
  "$ROOT/scripts/bootstrap.sh" || exit 8
fi

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
