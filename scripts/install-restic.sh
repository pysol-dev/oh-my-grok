#!/usr/bin/env bash
# Install the pinned restic binary into the tools directory.
#
#   scripts/install-restic.sh           download or copy the pinned binary, then chmod the tools copy
#   scripts/install-restic.sh --check   verify the installed binary only
#
# The pin is tooling/restic.lock (override with OMG_RESTIC_LOCK). The archive sha256 is checked before
# decompress. A cached file is accepted only when its sha256 matches the archive or the uncompressed
# binary. The cache file is never chmod'd, utime'd, or linked. chmod runs only on the tools copy, and
# only after that copy's sha256 matches.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
if [ -n "${OMG_TOOLS_DIR:-}" ]; then
  TOOLS="$OMG_TOOLS_DIR"
elif [ -n "${OMG_TOOLS:-}" ]; then
  TOOLS="$OMG_TOOLS"
elif [ -n "${OMO_TOOLS_DIR:-}" ]; then
  TOOLS="$OMO_TOOLS_DIR"
elif [ -n "${OMO_TOOLS:-}" ]; then
  TOOLS="$OMO_TOOLS"
else
  TOOLS="$ROOT/tools"
fi
LOCK="${OMG_RESTIC_LOCK:-$ROOT/tooling/restic.lock}"
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$LOCK" ]; then
  echo "restic lock not found: $LOCK" >&2
  exit 1
fi

version="" archive="" url="" sha256="" binary_sha256="" cache_name="" gpg_signer=""
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  key=${line%%=*}
  val=${line#*=}
  case "$key" in
    version) version=$val ;;
    archive) archive=$val ;;
    url) url=$val ;;
    sha256) sha256=$val ;;
    binary_sha256) binary_sha256=$val ;;
    cache_name) cache_name=$val ;;
    gpg_signer) gpg_signer=$val ;;
  esac
done < "$LOCK"

hex64='^[0-9a-f]{64}$'
if [ -z "$version" ] || [ -z "$archive" ] || [ -z "$sha256" ] || [ -z "$binary_sha256" ]; then
  echo "restic lock is missing version, archive, sha256, or binary_sha256" >&2
  exit 1
fi
if ! printf '%s' "$sha256" | grep -Eq "$hex64"; then
  echo "restic lock sha256 is not 64 hex characters" >&2
  exit 1
fi
if ! printf '%s' "$binary_sha256" | grep -Eq "$hex64"; then
  echo "restic lock binary_sha256 is not 64 hex characters" >&2
  exit 1
fi

dest="$TOOLS/restic"
file_sha() { sha256sum "$1" | awk '{print $1}'; }

version_ok() {
  local got
  got="$("$dest" version 2>/dev/null || true)"
  case "$got" in
    *"restic ${version}"*) return 0 ;;
  esac
  echo "restic version mismatch: ${got:-<none>}" >&2
  return 1
}

if [ "$CHECK" = 1 ]; then
  if [ ! -x "$dest" ] || [ -L "$dest" ]; then
    echo "MISSING: restic ${version} in $TOOLS" >&2
    exit 1
  fi
  if [ "$(file_sha "$dest")" != "$binary_sha256" ]; then
    echo "restic sha256 mismatch: $dest" >&2
    exit 1
  fi
  version_ok
  exit 0
fi

case "$dest" in
  /cursor/stores|/cursor/stores/*)
    echo "refusing to install restic onto the store" >&2
    exit 1
    ;;
esac
if [ -L "$dest" ] || [ -L "$TOOLS" ]; then
  echo "refusing to install restic through a symlink" >&2
  exit 1
fi

# Already installed and still the pinned bytes.
if [ -x "$dest" ] && [ "$(file_sha "$dest")" = "$binary_sha256" ] && version_ok; then
  exit 0
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/omg-restic-install.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
partial="$tmp/restic.partial"
archive_path=""

take_archive() {
  local src="$1" sum
  sum="$(sha256sum "$src" | awk '{print $1}')" || return 1
  [ "$sum" = "$sha256" ] || return 1
  cp -- "$src" "$tmp/$archive"
  archive_path="$tmp/$archive"
}

take_binary() {
  local src="$1" sum
  sum="$(sha256sum "$src" | awk '{print $1}')" || return 1
  [ "$sum" = "$binary_sha256" ] || return 1
  cp -- "$src" "$partial"
}

if [ -n "$url" ]; then
  if curl -fsSL --retry 3 --retry-delay 2 -o "$tmp/$archive" "$url"; then
    if [ "$(file_sha "$tmp/$archive")" = "$sha256" ]; then
      archive_path="$tmp/$archive"
    else
      echo "downloaded restic archive sha256 mismatch; trying the cache" >&2
      rm -f "$tmp/$archive"
    fi
  else
    echo "restic download failed; trying the cache" >&2
    rm -f "$tmp/$archive"
  fi
fi

if [ -z "$archive_path" ] && [ ! -f "$partial" ]; then
  candidates=()
  if [ -n "${OMG_RESTIC_CACHE:-}" ]; then
    candidates+=("$OMG_RESTIC_CACHE")
  fi
  if [ -n "${OMG_RESTIC_CACHE_DIR:-}" ]; then
    candidates+=("$OMG_RESTIC_CACHE_DIR/$cache_name" "$OMG_RESTIC_CACHE_DIR/$archive")
  fi
  if [ -d /cursor/stores ]; then
    for store in /cursor/stores/*; do
      [ -d "$store" ] || continue
      if [ -n "$cache_name" ]; then
        candidates+=("$store/restic/$cache_name" "$store/$cache_name")
      fi
      candidates+=("$store/restic/$archive" "$store/$archive")
    done
  fi
  if [ "${#candidates[@]}" -gt 0 ]; then
    for src in "${candidates[@]}"; do
      [ -f "$src" ] && [ ! -L "$src" ] || continue
      if take_binary "$src"; then
        break
      fi
      if take_archive "$src"; then
        break
      fi
    done
  fi
fi

if [ -n "$archive_path" ] && [ ! -f "$partial" ]; then
  python3 - "$archive_path" "$partial" <<'PY'
import bz2, pathlib, sys
src, dest = sys.argv[1], sys.argv[2]
pathlib.Path(dest).write_bytes(bz2.decompress(pathlib.Path(src).read_bytes()))
PY
fi

if [ ! -f "$partial" ]; then
  echo "restic ${version} not downloaded and no sha-matching cache file found" >&2
  exit 1
fi
if [ "$(file_sha "$partial")" != "$binary_sha256" ]; then
  echo "restic binary sha256 mismatch after install" >&2
  exit 1
fi

mkdir -p "$TOOLS"
mv -- "$partial" "$dest"
# The store copy was never chmod'd. This is the tools copy, after the sha check.
chmod 0755 "$dest"
version_ok
exit 0
