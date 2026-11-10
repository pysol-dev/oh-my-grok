#!/usr/bin/env bash
# Reproducible install for a fresh clone. Idempotent. Never uses sudo and never installs system packages.
#
#   scripts/bootstrap.sh            runtime venv (.venv) + language-server tools (tools/)
#   scripts/bootstrap.sh --no-tools runtime venv only
#   scripts/bootstrap.sh --check    verify only, install nothing (exit 8 when something is missing)
#
# Everything is installed from hash-pinned lock files:
#   requirements.lock                  -> .venv          (omo-grok runtime: mcp SDK, pytest, pyflakes)
#   tooling/requirements-tools.lock    -> tools/venv     (ruff, basedpyright, nodejs-wheel-binaries)
#   tooling/node/package-lock.json     -> tools/node     (typescript, typescript-language-server)
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
TOOLS="${OMO_TOOLS_DIR:-$ROOT/tools}"
WANT_TOOLS=1
CHECK=0
for a in "$@"; do
  case "$a" in
    --no-tools) WANT_TOOLS=0 ;;
    --check) CHECK=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

problems=0
say() { printf '%s\n' "$*"; }
bad() { printf 'MISSING: %s\n' "$*" >&2; problems=$((problems + 1)); }

pick_python() {
  for c in "${OMO_BOOTSTRAP_PYTHON:-}" python3.13 python3.12 python3; do
    [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

SYS_PY="$(pick_python)" || { echo "need Python >= 3.12 (set OMO_BOOTSTRAP_PYTHON)" >&2; exit 8; }

# --- runtime venv ---------------------------------------------------------------------------------------------
if [ "$CHECK" = 0 ]; then
  [ -x "$ROOT/.venv/bin/python" ] || "$SYS_PY" -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/python" -m pip install -q --disable-pip-version-check --require-hashes -r "$ROOT/requirements.lock"
fi
[ -x "$ROOT/.venv/bin/python" ] || bad ".venv (run scripts/bootstrap.sh)"
if [ -x "$ROOT/.venv/bin/python" ]; then
  "$ROOT/.venv/bin/python" -c 'import mcp' 2>/dev/null || bad "python package 'mcp' in .venv"
fi

# --- sandbox prerequisite (host package, not installed here) --------------------------------------------------------
if ! command -v bwrap >/dev/null 2>&1 && [ ! -x /usr/bin/bwrap ]; then
  bad "bubblewrap (bwrap): install the OS package 'bubblewrap'; candidate code and language servers never run without it"
fi

# --- language-server tools ------------------------------------------------------------------------------------
if [ "$WANT_TOOLS" = 1 ]; then
  if [ "$CHECK" = 0 ]; then
    [ -x "$TOOLS/venv/bin/python" ] || "$SYS_PY" -m venv "$TOOLS/venv"
    "$TOOLS/venv/bin/python" -m pip install -q --disable-pip-version-check --require-hashes \
      -r "$ROOT/tooling/requirements-tools.lock"
    NODE_BIN="$(dirname "$(find "$TOOLS/venv" -path '*nodejs_wheel/bin/node' | head -n1)")"
    NPM_CLI="$NODE_BIN/../lib/node_modules/npm/bin/npm-cli.js"
    mkdir -p "$TOOLS/node"
    cp "$ROOT/tooling/node/package.json" "$ROOT/tooling/node/package-lock.json" "$TOOLS/node/"
    # the wheel's own node + npm: no system node is required, and --ignore-scripts runs no package install scripts
    (cd "$TOOLS/node" && PATH="$NODE_BIN:$PATH" node "$NPM_CLI" ci --ignore-scripts --no-audit --no-fund >/dev/null)
  fi
  [ -x "$TOOLS/venv/bin/ruff" ] || bad "ruff in $TOOLS/venv"
  [ -x "$TOOLS/venv/bin/basedpyright-langserver" ] || bad "basedpyright-langserver in $TOOLS/venv"
  [ -n "$(find "$TOOLS/venv" -path '*nodejs_wheel/bin/node' 2>/dev/null | head -n1)" ] || bad "node (nodejs-wheel-binaries) in $TOOLS/venv"
  [ -f "$TOOLS/node/node_modules/typescript-language-server/lib/cli.mjs" ] || bad "typescript-language-server in $TOOLS/node"
  [ -f "$TOOLS/node/node_modules/typescript/lib/tsserver.js" ] || bad "typescript in $TOOLS/node"
fi

if [ "$problems" -ne 0 ]; then
  say "bootstrap: $problems problem(s); see MISSING lines above" >&2
  exit 8
fi
say "bootstrap ok: runtime=$ROOT/.venv tools=$([ "$WANT_TOOLS" = 1 ] && echo "$TOOLS" || echo skipped)"
say "next: $ROOT/omoctl doctor    and    $ROOT/omoctl mcp-command   (register that command as the 'omo' MCP app)"
