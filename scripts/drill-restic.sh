#!/usr/bin/env bash
# Disposable restic drill. Everything it creates is under mktemp in /tmp and is removed on exit.
# It does not read the agent store and it does not print the repository password.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
if [ -n "${OMG_RESTIC_BIN:-}" ]; then
  RESTIC="$OMG_RESTIC_BIN"
elif [ -x "$ROOT/tools/restic" ]; then
  RESTIC="$ROOT/tools/restic"
elif command -v restic >/dev/null 2>&1; then
  RESTIC="$(command -v restic)"
else
  echo "restic 0.19.1 is not installed" >&2
  exit 2
fi

version="$("$RESTIC" version 2>/dev/null || true)"
case "$version" in
  *"restic 0.19.1"*) ;;
  *) echo "need restic 0.19.1, found: ${version:-<none>}" >&2; exit 2 ;;
esac

work="$(mktemp -d /tmp/omg-restic-drill.XXXXXX)"
hold=""
sleeper=""
cleanup() {
  if [ -n "$hold" ]; then
    kill -9 "$hold" 2>/dev/null || true
    wait "$hold" 2>/dev/null || true
  fi
  if [ -n "$sleeper" ]; then
    kill "$sleeper" 2>/dev/null || true
    wait "$sleeper" 2>/dev/null || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

export OMG_RESTIC_PASSWORD
OMG_RESTIC_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
export RESTIC_PASSWORD_COMMAND='printenv OMG_RESTIC_PASSWORD'
export RESTIC_PACK_SIZE=4
export RESTIC_CACHE_DIR="$work/cache"
unset RESTIC_PASSWORD RESTIC_PASSWORD_FILE RESTIC_REPOSITORY RESTIC_REPOSITORY_FILE || true

repo="$work/repo"
src="$work/src"
out="$work/out"
mkdir -p "$src/state" "$src/jobs" "$repo"

python3 - "$src/state/sample.sqlite3" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("CREATE TABLE drill (id INTEGER PRIMARY KEY, value TEXT)")
conn.execute("INSERT INTO drill(value) VALUES ('kept')")
conn.commit()
conn.execute("PRAGMA integrity_check")
conn.close()
PY
printf 'drill-bytes\n' > "$src/jobs/note.txt"

echo "drill: init"
"$RESTIC" --repo "$repo" --pack-size 4 --cache-dir "$RESTIC_CACHE_DIR" init >/dev/null

echo "drill: backup"
"$RESTIC" --repo "$repo" --pack-size 4 --cache-dir "$RESTIC_CACHE_DIR" \
  backup --host omg-box --tag omg --tag gen:0 --tag schema:7 "$src" >/dev/null

echo "drill: restore"
"$RESTIC" --repo "$repo" --pack-size 4 --cache-dir "$RESTIC_CACHE_DIR" \
  restore latest --target "$out" >/dev/null

python3 - "$src" "$out" <<'PY'
import sqlite3, sys
from pathlib import Path
src, out = Path(sys.argv[1]), Path(sys.argv[2])
restored = list(out.rglob("sample.sqlite3"))
notes = list(out.rglob("note.txt"))
if len(restored) != 1 or len(notes) != 1:
    sys.exit("restore did not contain the sample files")
if notes[0].read_bytes() != (src / "jobs" / "note.txt").read_bytes():
    sys.exit("restored note differed")
if restored[0].read_bytes() != (src / "state" / "sample.sqlite3").read_bytes():
    sys.exit("restored sqlite differed")
conn = sqlite3.connect(restored[0])
try:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        sys.exit("integrity_check failed")
    if conn.execute("SELECT value FROM drill").fetchone()[0] != "kept":
        sys.exit("sqlite row differed")
finally:
    conn.close()
PY

echo "drill: stale lock"
mkfifo "$work/fifo"
sleep 60 > "$work/fifo" &
sleeper=$!
"$RESTIC" --repo "$repo" --pack-size 4 --cache-dir "$RESTIC_CACHE_DIR" \
  backup --stdin --stdin-filename hold.bin < "$work/fifo" >/dev/null 2>&1 &
hold=$!
locked=0
for _ in $(seq 1 50); do
  if "$RESTIC" --repo "$repo" --cache-dir "$RESTIC_CACHE_DIR" list locks 2>/dev/null | grep -q .; then
    locked=1
    break
  fi
  sleep 0.1
done
if [ "$locked" != 1 ]; then
  echo "drill: lock did not appear" >&2
  exit 1
fi
kill -9 "$hold" 2>/dev/null || true
wait "$hold" 2>/dev/null || true
hold=""
kill "$sleeper" 2>/dev/null || true
wait "$sleeper" 2>/dev/null || true
sleeper=""

set +e
"$RESTIC" --repo "$repo" --cache-dir "$RESTIC_CACHE_DIR" check >/dev/null 2>"$work/check.err"
check_rc=$?
set -e
if [ "$check_rc" -ne 11 ]; then
  echo "drill: expected check to exit 11 while the stale lock was held, got $check_rc" >&2
  cat "$work/check.err" >&2 || true
  exit 1
fi
# Plain unlock only. Do not pass --remove-all.
"$RESTIC" --repo "$repo" --cache-dir "$RESTIC_CACHE_DIR" unlock >/dev/null
"$RESTIC" --repo "$repo" --cache-dir "$RESTIC_CACHE_DIR" check >/dev/null
echo "drill ok"
