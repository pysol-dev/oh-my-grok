"""Restore entry for system python3: ``python3 -m oh_my_grok.recover [--check]``.

``scripts/recover.sh`` is the operator wrapper. It runs bootstrap and, after a successful decision,
``./omg watchdog ensure`` and ``./omg doctor``. This module does not import the project venv.
"""
from __future__ import annotations

import os
import sys

from .durability import EXIT_USAGE, run_recover
from .redact import redact, register_secret


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg != "--check" for arg in args):
        print("usage: python3 -m oh_my_grok.recover [--check]", file=sys.stderr)
        return EXIT_USAGE
    check = "--check" in args or os.environ.get("OMG_RECOVER_CHECK") == "1"
    passphrase = os.environ.get("OMG_BACKUP_PASSPHRASE") or ""
    if not passphrase:
        print("OMG_BACKUP_PASSPHRASE is not set", file=sys.stderr)
        return EXIT_USAGE
    register_secret(passphrase)
    try:
        return run_recover(check=check, passphrase=passphrase)
    except Exception as exc:  # noqa: BLE001 - the wrapper must not print a traceback that might quote a secret
        print(f"internal error: {redact(str(exc))[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
