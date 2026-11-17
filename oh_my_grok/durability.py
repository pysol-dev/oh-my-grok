"""Encrypted state bundles, off-box copies, and the restore decision.

See spec/DURABILITY.md. Hook bytes are read only to be archived. They are never logged, printed,
or placed in an exception. The encryption passphrase is read from the environment by the caller
and is never put on a command line.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import canonical_json, load_policy, sha256_text
from .errors import Conflict, IntegrityError_, OmgError, ValidationError
from .paths import CODE_ROOT, Paths, get_paths
from .redact import redact, redact_obj, register_secret

BUNDLE_FORMAT = 1
PUBLIC_REPO = "pysol-dev/oh-my-grok"
DEFAULT_AGENT_STORE = Path("/cursor/stores/user/omg-backups")
UID_ENCRYPTION = "omg-backup-encryption"
UID_SIGNING = "omg-backup-signing"
KIT_FILES = ("encryption.pub", "encryption.sec.asc", "signing.pub")
EVENT_FIELDS = ("id", "ts", "job_id", "task_id", "kind", "actor", "correlation_id", "data_json")
GITHUB_NOTES = "oh-my-grok encrypted backup. Git holds the recovery kit only; this release is the bundle."
KIT_README = (
    "oh-my-grok recovery kit\n\n"
    "This directory is the recovery kit: encryption.pub, the passphrase-protected encryption.sec.asc,\n"
    "and signing.pub. Backup bundles are release assets, not git objects. Do not force-push.\n"
)

# Exit codes shared with scripts/recover.sh. See spec/DURABILITY.md.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_BUNDLE = 10
EXIT_DIVERGED = 11
EXIT_VERIFY = 12
EXIT_DECRYPT = 13
EXIT_CHECK_BEHIND = 14


class NoBundle(OmgError):
    code = "no_bundle"
    exit_code = EXIT_NO_BUNDLE


class Diverged(OmgError):
    code = "diverged"
    exit_code = EXIT_DIVERGED


class BundleInvalid(IntegrityError_):
    """Signature or manifest failure. Fail closed; change nothing."""

    exit_code = EXIT_VERIFY


class DecryptFailed(OmgError):
    code = "decrypt_failed"
    exit_code = EXIT_DECRYPT


def bundles_dir(paths: Paths) -> Path:
    return paths.backups_dir / "bundles"


def kit_dir(paths: Paths) -> Path:
    return paths.state_dir / "recovery-kit"


def gnupg_home(paths: Paths) -> Path:
    return paths.secrets_dir / "backup-gnupg"


def mcp_command_record() -> str:
    """The command recorded in every manifest. Not the runtime fallback in paths.mcp_command."""
    py = CODE_ROOT / ".venv" / "bin" / "python"
    server = CODE_ROOT / "oh_my_grok" / "mcp_server.py"
    return f"{py} {server}"


def normalize_repo(value: str) -> str:
    text = (value or "").strip().lower()
    if text.endswith(".git"):
        text = text[:-4]
    for prefix in ("https://github.com/", "http://github.com/", "ssh://git@github.com/", "git@github.com:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip("/")


def refuse_public(value: str) -> None:
    """Reject the public oh-my-grok repo before any git or gh command runs."""
    norm = normalize_repo(value)
    if norm == PUBLIC_REPO or norm.startswith(PUBLIC_REPO + "/"):
        raise ValidationError("refusing backup target that points at the public oh-my-grok repo")


def validate_targets(targets) -> None:
    if targets is None:
        return
    if not isinstance(targets, list):
        raise ValidationError("backup_targets must be a list")
    for index, target in enumerate(targets):
        if not isinstance(target, dict) or not isinstance(target.get("type"), str):
            raise ValidationError(f"backup_targets[{index}] must be an object with a type")
        kind = target["type"]
        if kind == "agent_store":
            extra = set(target) - {"type", "path"}
            if extra:
                raise ValidationError(f"unknown agent_store keys: {sorted(extra)}")
            if "path" in target and (not isinstance(target["path"], str) or not target["path"].strip()):
                raise ValidationError("agent_store path must be a non-empty string")
        elif kind == "github_release":
            extra = set(target) - {"type", "repo", "checkout", "mirror", "remote"}
            if extra:
                raise ValidationError(f"unknown github_release keys: {sorted(extra)}")
            repo = target.get("repo")
            if not isinstance(repo, str) or not repo.strip():
                raise ValidationError("github_release target needs repo")
            refuse_public(repo)
            if target.get("remote"):
                if not isinstance(target["remote"], str):
                    raise ValidationError("github_release remote must be a string")
                refuse_public(target["remote"])
            for key in ("checkout", "mirror"):
                if key in target and (not isinstance(target[key], str) or not target[key].strip()):
                    raise ValidationError(f"github_release {key} must be a non-empty string")
        else:
            raise ValidationError(f"unknown backup target type {kind!r}")


def bundle_epoch(path: Path) -> float | None:
    """UTC timestamp encoded in omg-YYYYmmddTHHMMSSZ[-n].tar.gpg."""
    name = path.name
    if not name.startswith("omg-") or not name.endswith(".tar.gpg"):
        return None
    stamp = name[len("omg-"):-len(".tar.gpg")]
    if "-" in stamp:
        stamp = stamp.split("-", 1)[0]
    if len(stamp) != 16 or stamp[8] != "T" or not stamp.endswith("Z"):
        return None
    try:
        moment = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment.timestamp()


def bundle_tag(name: str) -> str:
    if not name.endswith(".tar.gpg"):
        raise ValidationError("not a bundle name")
    return name[: -len(".tar.gpg")]


# ---------------------------------------------------------------- filesystem hygiene
def _shred_file(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if not path.exists():
        return
    try:
        subprocess.run(["shred", "-u", "-n", "1", "--", str(path)], capture_output=True, timeout=60, check=False)
    except OSError:
        pass
    if path.exists():
        try:
            size = path.stat().st_size
            with open(path, "r+b", buffering=0) as handle:
                handle.write(b"\x00" * max(size, 1))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
        path.unlink(missing_ok=True)


def _special_file(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode)


def shred_tree(path: Path) -> None:
    """Overwrite file contents, then remove the tree. Symlinks are unlinked, not followed."""
    if path.is_symlink() or _special_file(path):
        path.unlink(missing_ok=True)
        return
    if not path.exists():
        return
    if path.is_file():
        _shred_file(path)
        return
    children = sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for child in children:
        if _special_file(child) or child.is_symlink() or child.is_file():
            if child.is_file() and not child.is_symlink() and not _special_file(child):
                _shred_file(child)
            else:
                child.unlink(missing_ok=True)
        elif child.is_dir():
            try:
                child.rmdir()
            except OSError:
                shutil.rmtree(child, ignore_errors=True)
    try:
        path.rmdir()
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def _restrict_tree(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        return
    os.chmod(path, 0o700)
    for child in path.rglob("*"):
        if child.is_symlink() or _special_file(child):
            continue
        try:
            os.chmod(child, 0o700 if child.is_dir() else 0o600)
        except OSError:
            continue


def _private_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="omg-backup-"))
    os.chmod(path, 0o700)
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scrub_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("OMG_BACKUP_PASSPHRASE", None)
    return env


def _gpg_env() -> dict[str, str]:
    """gpg-agent needs a private runtime directory. This box has no /run/user session."""
    env = _scrub_env()
    current = env.get("XDG_RUNTIME_DIR")
    if not current or not Path(current).is_dir():
        path = Path(tempfile.gettempdir()) / f"omg-gpg-run-{os.getuid()}"
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        env["XDG_RUNTIME_DIR"] = str(path)
    return env


def _socket_safe(homedir: Path) -> Path:
    """GnuPG puts agent sockets inside the homedir. AF_UNIX paths must stay short.

    The key files stay in ``homedir``. When that path is too long for ``S.gpg-agent.browser``,
    gpg is pointed at a private symlink with a short name.
    """
    homedir.mkdir(parents=True, exist_ok=True)
    os.chmod(homedir, 0o700)
    if len(str(homedir.resolve() / "S.gpg-agent.browser")) <= 104:
        return homedir
    short_root = Path(tempfile.gettempdir()) / f"omg-gnupg-{os.getuid()}"
    short_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(short_root, 0o700)
    link = short_root / hashlib.sha256(str(homedir.resolve()).encode()).hexdigest()[:12]
    target = homedir.resolve()
    if link.is_symlink():
        try:
            current = link.resolve()
        except OSError:
            current = None
        if current != target:
            link.unlink()
    elif link.exists():
        raise ValidationError("refusing a non-symlink gpg socket path")
    if not link.exists():
        link.symlink_to(target, target_is_directory=True)
    return link


def _gpg(homedir: Path, args: list[str], *, passphrase_file: Path | None = None,
         timeout: int = 180) -> subprocess.CompletedProcess:
    homedir = _socket_safe(homedir)
    cmd = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--homedir", str(homedir)]
    if passphrase_file is not None:
        cmd.extend(["--passphrase-file", str(passphrase_file)])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_gpg_env(), check=False)


def _gpg_ok(cp: subprocess.CompletedProcess, what: str) -> None:
    if cp.returncode != 0:
        detail = redact((cp.stderr or "").strip())[:300]
        raise ValidationError(f"gpg {what} failed" + (f": {detail}" if detail else ""))


def _fingerprint(homedir: Path, uid: str) -> str:
    cp = _gpg(homedir, ["--with-colons", "--list-keys", uid])
    _gpg_ok(cp, "list-keys")
    for line in cp.stdout.splitlines():
        if line.startswith("fpr:"):
            parts = line.split(":")
            if len(parts) > 9 and parts[9]:
                return parts[9]
    raise ValidationError("backup key has no fingerprint")


def _secret_uids(homedir: Path) -> list[str]:
    cp = _gpg(homedir, ["--with-colons", "--list-secret-keys"])
    if cp.returncode != 0:
        return []
    found = []
    for line in cp.stdout.splitlines():
        if line.startswith("uid:"):
            parts = line.split(":")
            if len(parts) > 9 and parts[9]:
                found.append(parts[9])
    return found


def _require_passphrase(passphrase: str) -> str:
    if not isinstance(passphrase, str) or not passphrase or passphrase != passphrase.strip():
        raise ValidationError("OMG_BACKUP_PASSPHRASE must be a non-empty single line from the environment")
    if "\n" in passphrase or "\x00" in passphrase or len(passphrase) < 12:
        raise ValidationError("OMG_BACKUP_PASSPHRASE must be a single line of at least 12 characters")
    register_secret(passphrase)
    return passphrase


# ---------------------------------------------------------------- keys
def _write_passphrase_file(directory: Path, passphrase: str) -> Path:
    path = directory / "passphrase"
    path.write_bytes(passphrase.encode("utf-8"))
    os.chmod(path, 0o600)
    return path


def init_keys(paths: Paths, *, passphrase: str, kit: Path | None = None) -> dict:
    """Create the encryption keypair and the on-box signing key. One-time."""
    passphrase = _require_passphrase(passphrase)
    paths.ensure()
    kit = kit or kit_dir(paths)
    if kit.is_symlink():
        raise ValidationError("recovery kit directory must not be a symlink")
    home = gnupg_home(paths)
    if kit.exists() and any((kit / name).exists() for name in KIT_FILES):
        raise Conflict("recovery kit already exists")
    if home.exists() and any(home.iterdir()):
        raise Conflict("backup keyring already exists")
    tmp = _private_dir()
    finished = False
    try:
        work = tmp / "gpg"
        work.mkdir(mode=0o700)
        os.chmod(work, 0o700)
        phrase = _write_passphrase_file(tmp, passphrase)
        batch = tmp / "encryption.batch"
        batch.write_text(
            "Key-Type: ECDSA\nKey-Curve: nistp256\nSubkey-Type: ECDH\nSubkey-Curve: nistp256\n"
            f"Name-Real: {UID_ENCRYPTION}\nExpire-Date: 0\n%commit\n")
        os.chmod(batch, 0o600)
        _gpg_ok(_gpg(work, ["--generate-key", str(batch)], passphrase_file=phrase), "generate encryption key")
        kit.mkdir(parents=True, exist_ok=True)
        os.chmod(kit, 0o700)
        pub = kit / "encryption.pub"
        sec = kit / "encryption.sec.asc"
        _gpg_ok(_gpg(work, ["--armor", "--output", str(pub), "--export", UID_ENCRYPTION]), "export encryption public key")
        _gpg_ok(_gpg(work, ["--armor", "--output", str(sec), "--export-secret-keys", UID_ENCRYPTION],
                     passphrase_file=phrase), "export encryption secret")
        os.chmod(pub, 0o600)
        os.chmod(sec, 0o600)
        enc_fpr = _fingerprint(work, UID_ENCRYPTION)
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        _gpg_ok(_gpg(home, ["--import", str(pub)]), "import encryption public key")
        sign_batch = tmp / "signing.batch"
        sign_batch.write_text(
            "Key-Type: EdDSA\nKey-Curve: ed25519\n"
            f"Name-Real: {UID_SIGNING}\nExpire-Date: 0\n%no-protection\n%commit\n")
        os.chmod(sign_batch, 0o600)
        _gpg_ok(_gpg(home, ["--generate-key", str(sign_batch)]), "generate signing key")
        signing_pub = kit / "signing.pub"
        _gpg_ok(_gpg(home, ["--armor", "--output", str(signing_pub), "--export", UID_SIGNING]), "export signing public key")
        os.chmod(signing_pub, 0o600)
        _restrict_tree(home)
        _restrict_tree(kit)
        sign_fpr = _fingerprint(home, UID_SIGNING)
        remember_kit(paths, kit)
        finished = True
        return {"kit": str(kit), "gnupg_home": str(home), "encryption_fingerprint": enc_fpr,
                "signing_fingerprint": sign_fpr}
    finally:
        shred_tree(tmp)
        if not finished:
            shred_tree(kit)
            shred_tree(home)


def _require_runtime_keys(paths: Paths) -> Path:
    home = gnupg_home(paths)
    if not home.is_dir():
        raise ValidationError("backup keys are not initialized; run omg backup init-key")
    cp = _gpg(home, ["--list-keys", UID_ENCRYPTION])
    if cp.returncode != 0:
        raise ValidationError("backup encryption public key is missing; run omg backup init-key")
    cp = _gpg(home, ["--list-secret-keys", UID_SIGNING])
    if cp.returncode != 0:
        raise ValidationError("backup signing key is missing; run omg backup init-key")
    return home


# ---------------------------------------------------------------- generation and manifest
def event_stream(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, job_id, task_id, kind, actor, correlation_id, data_json FROM events ORDER BY id ASC"
    ).fetchall()
    return [{field: row[field] for field in EVENT_FIELDS} for row in rows]


def generation_of(events: list[dict]) -> dict:
    tail = events[-50:]
    return {"max_event_id": int(events[-1]["id"]) if events else 0,
            "tail_sha256": sha256_text(canonical_json(tail))}


def relate_streams(local_events: list[dict], backup_events: list[dict]) -> str:
    """equal, local_prefix (backup is a strict extension), local_newer, or diverged."""
    if local_events == backup_events:
        return "equal"
    if len(local_events) < len(backup_events) and backup_events[:len(local_events)] == local_events:
        return "local_prefix"
    if len(backup_events) < len(local_events) and local_events[:len(backup_events)] == backup_events:
        return "local_newer"
    return "diverged"


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def inspect_database(path: Path) -> tuple[str, list[dict] | None]:
    """Return missing, corrupt, or ok plus the event stream when the file is readable."""
    if not path.exists():
        return "missing", None
    try:
        conn = _open_ro(path)
    except sqlite3.Error:
        return "corrupt", None
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error:
            return "corrupt", None
        if row is None or row[0] != "ok":
            return "corrupt", None
        try:
            return "ok", event_stream(conn)
        except sqlite3.Error:
            return "corrupt", None
    finally:
        conn.close()


def _schema_version(path: Path) -> int:
    conn = _open_ro(path)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
        if not found:
            return 0
        value = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        return int(value or 0)
    finally:
        conn.close()


def _git_sha() -> str:
    try:
        cp = subprocess.run(["git", "-C", str(CODE_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                            timeout=15, check=False, env=_scrub_env())
    except OSError:
        return "unknown"
    sha = (cp.stdout or "").strip()
    if cp.returncode == 0 and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        return sha
    return "unknown"


def _environment() -> dict:
    out = {"git_sha": _git_sha()}
    pairs = (("requirements_lock_sha256", CODE_ROOT / "requirements.lock"),
             ("tools_lock_sha256", CODE_ROOT / "tooling" / "requirements-tools.lock"))
    for key, path in pairs:
        if path.is_file() and not path.is_symlink():
            out[key] = _sha256_file(path)
    return out


def _add_bytes(tree: Path, rel: str, data: bytes, files: dict) -> None:
    dest = tree / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    os.chmod(dest, 0o600)
    files[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "mode": "0600"}


def _add_tree(tree: Path, src_root: Path, prefix: str, files: dict) -> None:
    if not src_root.exists():
        return
    if src_root.is_symlink():
        raise IntegrityError_(f"refusing to archive symlink {prefix or src_root.name}")
    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            rel_under = path.relative_to(src_root).as_posix()
            rel = f"{prefix}/{rel_under}" if prefix else rel_under
            if path.is_symlink():
                raise IntegrityError_(f"refusing to archive symlink {rel}")
            try:
                data = path.read_bytes()
            except OSError:
                raise IntegrityError_(f"could not read {rel}") from None
            _add_bytes(tree, rel, data, files)


def _read_hooks(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise IntegrityError_("hooks.json must not be a symlink")
    try:
        return path.read_bytes()
    except OSError:
        raise IntegrityError_("could not read hooks.json") from None


def _allocate_name(directory: Path, when: datetime) -> str:
    base = when.strftime("omg-%Y%m%dT%H%M%SZ")
    name = base + ".tar.gpg"
    suffix = 2
    while (directory / name).exists():
        name = f"{base}-{suffix}.tar.gpg"
        suffix += 1
    return name


def _safe_extract(tar_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    with tarfile.open(tar_path, "r") as archive:
        for member in archive.getmembers():
            name = member.name
            parts = Path(name).parts
            if not name or name.startswith("/") or ".." in parts or member.issym() or member.islnk() or member.isdev():
                raise BundleInvalid("bundle contains an unsafe path")
        archive.extractall(dest, filter="data")


def verify_tree(root: Path, manifest: dict) -> None:
    if manifest.get("bundle_format") != BUNDLE_FORMAT:
        raise BundleInvalid("unsupported bundle format")
    for key in ("schema_version", "omg_version", "git_sha", "created_at", "mcp_command", "generation", "files"):
        if key not in manifest:
            raise BundleInvalid("bundle manifest is incomplete")
    generation = manifest["generation"]
    if not isinstance(generation, dict) or "max_event_id" not in generation or "tail_sha256" not in generation:
        raise BundleInvalid("bundle manifest generation is incomplete")
    files = manifest["files"]
    if not isinstance(files, dict):
        raise BundleInvalid("bundle manifest files are invalid")
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            rel = path.relative_to(root).as_posix()
            if rel == "manifest.json":
                continue
            if path.is_symlink():
                raise BundleInvalid("bundle contains a symlink")
            found[rel] = _sha256_file(path)
    if set(found) != set(files):
        raise BundleInvalid("bundle manifest does not match archive contents")
    for rel, meta in files.items():
        if not isinstance(meta, dict) or found[rel] != meta.get("sha256"):
            raise BundleInvalid(f"hash mismatch for {rel}")


# ---------------------------------------------------------------- bundle build
def create_bundle(conn: sqlite3.Connection, paths: Paths, *, push: bool = True,
                  runner=None, now: datetime | None = None) -> dict:
    """Build, encrypt, and sign one bundle. Hook bytes never appear in the returned dict."""
    paths.ensure()
    home = _require_runtime_keys(paths)
    when = now or datetime.now(timezone.utc)
    directory = bundles_dir(paths)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    name = _allocate_name(directory, when)
    tmp = _private_dir()
    try:
        tree = tmp / "tree"
        tree.mkdir(mode=0o700)
        files: dict = {}
        db_name = paths.db_path.name
        if db_name not in ("omg.sqlite3", "omo.sqlite3"):
            raise IntegrityError_("refusing to archive an unexpected database name")
        snapshot = tree / "state" / db_name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        db.backup(conn, snapshot)
        side = sqlite3.connect(str(snapshot))
        try:
            side.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            side.execute("PRAGMA journal_mode=DELETE")
        finally:
            side.close()
        for suffix in ("-wal", "-shm"):
            extra = Path(str(snapshot) + suffix)
            if extra.exists() or extra.is_symlink():
                extra.unlink()
        _add_bytes(tree, f"state/{db_name}", snapshot.read_bytes(), files)
        health, events = inspect_database(snapshot)
        if health != "ok" or events is None:
            raise IntegrityError_("snapshot failed integrity_check")
        policy = paths.config_dir / "policy.json"
        if policy.is_symlink():
            raise IntegrityError_("refusing to archive symlink config/policy.json")
        if policy.is_file():
            try:
                _add_bytes(tree, "config/policy.json", policy.read_bytes(), files)
            except OSError:
                raise IntegrityError_("could not read config/policy.json") from None
        _add_tree(tree, paths.jobs_dir, "jobs", files)
        _add_tree(tree, paths.logs_dir, "state/logs", files)
        hooks = _read_hooks(paths.hooks_path)
        if hooks is not None:
            _add_bytes(tree, "state/secrets/hooks.json", hooks, files)
            del hooks
        manifest = {
            "bundle_format": BUNDLE_FORMAT,
            "schema_version": _schema_version(snapshot),
            "omg_version": _omg_version(),
            "git_sha": _git_sha(),
            "created_at": when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "mcp_command": mcp_command_record(),
            "generation": generation_of(events),
            "environment": _environment(),
            "files": files,
        }
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        (tree / "manifest.json").write_bytes(manifest_bytes)
        os.chmod(tree / "manifest.json", 0o600)
        tar_path = tmp / "bundle.tar"
        with tarfile.open(tar_path, "w") as archive:
            for dirpath, dirnames, filenames in os.walk(tree, followlinks=False):
                current = Path(dirpath)
                dirnames[:] = [item for item in dirnames if not (current / item).is_symlink()]
                for filename in sorted(filenames):
                    path = current / filename
                    archive.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False)
        enc_fpr = _fingerprint(home, UID_ENCRYPTION)
        sign_fpr = _fingerprint(home, UID_SIGNING)
        cipher = tmp / name
        _gpg_ok(_gpg(home, ["--trust-model", "always", "--encrypt", "--recipient", enc_fpr,
                            "--output", str(cipher), str(tar_path)]), "encrypt bundle")
        signature = tmp / (name + ".sig")
        _gpg_ok(_gpg(home, ["--detach-sign", "--local-user", sign_fpr, "--output", str(signature), str(cipher)]),
                "sign bundle")
        dest = directory / name
        _install_file(cipher, dest)
        _install_file(signature, directory / (name + ".sig"))
        sidecar = directory / (name + ".manifest.json")
        sidecar.write_bytes(manifest_bytes)
        os.chmod(sidecar, 0o600)
    finally:
        shred_tree(tmp)
    policy = load_policy(paths.config_dir)
    _prune_dir(directory, policy)
    pushed: list[dict] = []
    if push:
        pushed = push_targets(paths, directory / name, runner=runner)
    dest = directory / name
    return {"name": name, "path": str(dest), "sha256": _sha256_file(dest), "bytes": dest.stat().st_size,
            "generation": manifest["generation"], "schema_version": manifest["schema_version"],
            "created_at": manifest["created_at"], "pushed": redact_obj(pushed)}


def _omg_version() -> str:
    from . import __version__
    return __version__


def _install_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    if src.resolve() == dest.resolve():
        os.chmod(dest, 0o600)
        return
    partial = dest.with_name(dest.name + ".partial")
    shutil.copyfile(src, partial)
    os.chmod(partial, 0o600)
    os.replace(partial, dest)
    os.chmod(dest, 0o600)


def newest_bundle(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    found = []
    for path in directory.glob("omg-*.tar.gpg"):
        epoch = bundle_epoch(path)
        if epoch is None or not path.is_file() or path.is_symlink():
            continue
        found.append((epoch, path.name, path))
    if not found:
        return None
    found.sort()
    return found[-1][2]


def load_sidecar(bundle: Path) -> dict | None:
    path = Path(str(bundle) + ".manifest.json")
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- retention
def select_keep(names: list[str], *, keep_hourly: int, keep_daily: int) -> tuple[set[str], set[str]]:
    parsed = []
    for name in names:
        epoch = bundle_epoch(Path(name))
        if epoch is None:
            continue
        parsed.append((epoch, name))
    parsed.sort()
    by_date: dict = {}
    for epoch, name in parsed:
        by_date[datetime.fromtimestamp(epoch, timezone.utc).date()] = name
    daily = {by_date[day] for day in sorted(by_date)[-keep_daily:]}
    hourly = {name for _, name in parsed[-keep_hourly:]}
    kept = set(daily | hourly)
    if parsed:
        kept.add(parsed[-1][1])
    return kept, daily


def apply_size_cap(files: list[Path], kept: set[str], daily: set[str], cap: int) -> set[str]:
    chosen = [path for path in files if path.name in kept]
    chosen.sort(key=lambda path: bundle_epoch(path) or 0)

    def total(group: list[Path]) -> int:
        size = 0
        for path in group:
            size += path.stat().st_size
            sig = Path(str(path) + ".sig")
            if sig.is_file():
                size += sig.stat().st_size
        return size

    while len(chosen) > 1 and total(chosen) > cap:
        newest = chosen[-1]
        victims = [path for path in chosen if path != newest and path.name not in daily]
        if not victims:
            victims = [path for path in chosen if path != newest]
        chosen.remove(victims[0])
    return {path.name for path in chosen}


def _prune_dir(directory: Path, policy: dict) -> list[str]:
    files = [path for path in directory.glob("omg-*.tar.gpg")
             if path.is_file() and not path.is_symlink() and bundle_epoch(path) is not None]
    kept, daily = select_keep([path.name for path in files], keep_hourly=int(policy["backup_keep_hourly"]),
                              keep_daily=int(policy["backup_keep_daily"]))
    kept = apply_size_cap(files, kept, daily, int(policy["backup_size_cap_bytes"]))
    removed = []
    for path in files:
        if path.name in kept:
            continue
        path.unlink()
        Path(str(path) + ".sig").unlink(missing_ok=True)
        Path(str(path) + ".manifest.json").unlink(missing_ok=True)
        removed.append(path.name)
    return removed


# ---------------------------------------------------------------- command runners
def _is_repoish(arg: str) -> bool:
    if not isinstance(arg, str) or arg.startswith("-") or arg.startswith("/"):
        return False
    low = arg.lower()
    if "github.com" in low or low.startswith("git@"):
        return True
    return arg.count("/") == 1 and " " not in arg


def _forbid_force(argv: list[str]) -> None:
    if not argv or argv[0] != "git":
        return
    if not any(token in argv for token in ("push", "clone", "fetch")):
        return
    banned = {"--force", "--force-with-lease", "--force-if-includes", "-f"}
    if banned & set(argv):
        raise ValidationError("refusing force-push")
    for arg in argv:
        if isinstance(arg, str) and arg.startswith("+") and ":" in arg:
            raise ValidationError("refusing force-push")


def _invoke(runner, argv: list[str], *, cwd: Path | None = None, env: dict | None = None):
    _forbid_force(argv)
    for arg in argv:
        if _is_repoish(arg):
            refuse_public(arg)
    return runner(argv, cwd=cwd, env=env)


def subprocess_runner(argv, cwd=None, env=None):
    return subprocess.run(list(argv), cwd=str(cwd) if cwd else None, env=env or _scrub_env(),
                          capture_output=True, text=True, timeout=180, check=False)


def default_runner(argv, cwd=None, env=None):
    return _invoke(subprocess_runner, list(argv), cwd=cwd, env=env)


def resolve_store(target: dict) -> Path:
    if target.get("path"):
        return Path(target["path"]).expanduser()
    env = os.environ.get("OMG_BACKUP_AGENT_STORE")
    if env:
        return Path(env)
    return DEFAULT_AGENT_STORE


def _publish_kit(kit: Path, dest: Path) -> None:
    if not _kit_complete(kit):
        raise ValidationError("recovery kit is incomplete")
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    for name in KIT_FILES:
        _install_file(kit / name, dest / name)


def _kit_complete(path: Path | None) -> bool:
    return bool(path) and path.is_dir() and not path.is_symlink() and all(
        (path / name).is_file() and not (path / name).is_symlink() for name in KIT_FILES)


def kit_pointer_path(paths: Paths) -> Path:
    return paths.secrets_dir / "backup-kit.path"


def remember_kit(paths: Paths, kit: Path) -> None:
    """Record the kit directory chosen by init-key. Publication reads this, not a hardcoded default."""
    if kit.is_symlink():
        raise ValidationError("recovery kit directory must not be a symlink")
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.secrets_dir, 0o700)
    _write_mode(kit_pointer_path(paths), (str(kit.resolve()) + "\n").encode(), 0o600)


def resolve_kit(paths: Paths) -> Path:
    """The kit init-key recorded, when that directory still exists. Otherwise the default location."""
    pointer = kit_pointer_path(paths)
    if pointer.is_file() and not pointer.is_symlink():
        try:
            raw = pointer.read_text().strip()
        except OSError:
            raw = ""
        if raw:
            candidate = Path(raw)
            if candidate.is_symlink():
                raise ValidationError("recovery kit directory must not be a symlink")
            if candidate.exists():
                return candidate
    return kit_dir(paths)


def push_agent_store(paths: Paths, bundle: Path, target: dict, policy: dict) -> dict:
    store = resolve_store(target)
    try:
        refuse_public(str(store))
    except ValidationError:
        raise
    bundles = store / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    os.chmod(store, 0o700)
    os.chmod(bundles, 0o700)
    _install_file(bundle, bundles / bundle.name)
    sig = Path(str(bundle) + ".sig")
    side = Path(str(bundle) + ".manifest.json")
    if not sig.is_file():
        raise ValidationError("bundle signature is missing")
    _install_file(sig, bundles / sig.name)
    if side.is_file():
        _install_file(side, bundles / side.name)
    _publish_kit(resolve_kit(paths), store / "kit")
    removed = _prune_dir(bundles, policy)
    latest = newest_bundle(bundles)
    (store / "LATEST").write_text((latest.name if latest else bundle.name) + "\n")
    os.chmod(store / "LATEST", 0o600)
    return {"type": "agent_store", "ok": True, "path": str(store), "bundle": bundle.name, "pruned": removed}


def _same_remote(left: str, right: str) -> bool:
    if normalize_repo(left) == normalize_repo(right):
        return True
    left_path, right_path = Path(left), Path(right)
    if left_path.exists() and right_path.exists():
        return left_path.resolve() == right_path.resolve()
    return False


def _remote_for(target: dict) -> str:
    remote = target.get("remote") or f"https://github.com/{target['repo']}.git"
    refuse_public(target["repo"])
    refuse_public(remote)
    return remote


def _git(runner, checkout: Path, args: list[str]):
    return _invoke(runner, ["git", "-C", str(checkout), *args])


def push_github(paths: Paths, bundle: Path, target: dict, policy: dict, *, runner=None) -> dict:
    runner = runner or subprocess_runner
    repo = target["repo"]
    refuse_public(repo)
    remote = _remote_for(target)
    checkout = Path(target["checkout"]) if target.get("checkout") else (
        paths.state_dir / "backups" / "github" / repo.replace("/", "__"))
    checkout.mkdir(parents=True, exist_ok=True)
    if not (checkout / ".git").is_dir():
        cp = _invoke(runner, ["git", "init", "-b", "main", str(checkout)])
        if cp.returncode != 0:
            raise ValidationError("git init of the recovery checkout failed")
        cp = _git(runner, checkout, ["remote", "add", "origin", remote])
        if cp.returncode != 0:
            raise ValidationError("git remote add failed")
    else:
        cp = _git(runner, checkout, ["remote", "get-url", "origin"])
        if cp.returncode != 0:
            raise ValidationError("recovery checkout has no origin")
        current = (cp.stdout or "").strip()
        refuse_public(current)
        if not _same_remote(current, remote):
            raise ValidationError("recovery checkout origin does not match the configured remote")
    kit_dest = checkout / "kit"
    _publish_kit(resolve_kit(paths), kit_dest)
    readme = checkout / "README.md"
    if not readme.exists():
        readme.write_text(KIT_README)
        os.chmod(readme, 0o600)
    _git(runner, checkout, ["add", "--", "kit", "README.md"])
    status = _git(runner, checkout, ["status", "--porcelain", "--", "kit", "README.md"])
    if (status.stdout or "").strip():
        cp = _invoke(runner, ["git", "-C", str(checkout), "-c", "user.name=omg-backup",
                              "-c", "user.email=omg-backup@localhost", "commit", "-m", "update recovery kit"])
        if cp.returncode != 0:
            raise ValidationError("git commit of the recovery kit failed")
    # A commit can succeed while the push fails. The next run has a clean tree and must still push.
    cp = _git(runner, checkout, ["push", "origin", "HEAD:main"])
    if cp.returncode != 0:
        raise ValidationError("git push of the recovery kit failed")
    tag = bundle_tag(bundle.name)
    sig = Path(str(bundle) + ".sig")
    view = _invoke(runner, ["gh", "release", "view", tag, "--repo", repo, "--json", "assets"])
    if view.returncode != 0:
        cp = _invoke(runner, ["gh", "release", "create", tag, str(bundle), str(sig), "--repo", repo,
                              "--title", tag, "--notes", GITHUB_NOTES])
        if cp.returncode != 0:
            raise ValidationError("gh release create failed")
    pruned = _prune_github_releases(repo, policy, runner)
    return {"type": "github_release", "ok": True, "repo": repo, "tag": tag, "pruned": pruned}


def _prune_github_releases(repo: str, policy: dict, runner) -> list[str]:
    refuse_public(repo)
    cp = _invoke(runner, ["gh", "release", "list", "--repo", repo, "--limit", "400", "--json", "tagName"])
    if cp.returncode != 0:
        raise ValidationError("gh release list failed")
    try:
        rows = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValidationError("gh release list was not json") from exc
    names = []
    tags = {}
    for row in rows:
        tag = row.get("tagName") or ""
        bundle_name = tag + ".tar.gpg"
        if bundle_epoch(Path(bundle_name)) is None:
            continue
        names.append(bundle_name)
        tags[bundle_name] = tag
    kept, _daily = select_keep(names, keep_hourly=int(policy["backup_keep_hourly"]),
                               keep_daily=int(policy["backup_keep_daily"]))
    removed = []
    for name, tag in tags.items():
        if name in kept:
            continue
        cp = _invoke(runner, ["gh", "release", "delete", tag, "--repo", repo, "--yes", "--cleanup-tag"])
        if cp.returncode != 0:
            raise ValidationError("gh release delete failed")
        removed.append(tag)
    return removed


def github_has_bundle(repo: str, bundle_name: str, runner) -> bool:
    refuse_public(repo)
    tag = bundle_tag(bundle_name)
    cp = _invoke(runner, ["gh", "release", "view", tag, "--repo", repo, "--json", "assets"])
    if cp.returncode != 0:
        return False
    try:
        data = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return False
    names = {item.get("name") for item in data.get("assets") or []}
    return bundle_name in names and (bundle_name + ".sig") in names


def push_targets(paths: Paths, bundle: Path, *, runner=None) -> list[dict]:
    policy = load_policy(paths.config_dir)
    results = []
    for target in policy["backup_targets"]:
        if target["type"] == "agent_store":
            results.append(push_agent_store(paths, bundle, target, policy))
        elif target["type"] == "github_release":
            results.append(push_github(paths, bundle, target, policy, runner=runner))
        else:
            raise ValidationError(f"unknown backup target type {target['type']!r}")
    failed = [item for item in results if not item.get("ok")]
    if failed:
        raise OmgError("off-box push failed")
    return results


def target_has_bundle(target: dict, bundle_name: str, *, runner=None) -> bool:
    runner = runner or subprocess_runner
    sig_name = bundle_name + ".sig"
    if target["type"] == "agent_store":
        directory = resolve_store(target) / "bundles"
        return (directory / bundle_name).is_file() and (directory / sig_name).is_file()
    if target.get("mirror"):
        directory = Path(target["mirror"])
        return (directory / bundle_name).is_file() and (directory / sig_name).is_file()
    return github_has_bundle(target["repo"], bundle_name, runner)


# ---------------------------------------------------------------- hourly and doctor
def hourly_backup(conn: sqlite3.Connection, paths: Paths, *, now: float | None = None, runner=None) -> dict:
    policy = load_policy(paths.config_dir)
    if policy["features"]["backup"] != "on":
        return {"skipped": True, "reason": "backup off"}
    moment = time.time() if now is None else now
    latest = newest_bundle(bundles_dir(paths))
    interval = float(policy["backup_interval_seconds"])
    if latest is not None and moment - (bundle_epoch(latest) or 0) < interval:
        missing = [target for target in policy["backup_targets"]
                   if not target_has_bundle(target, latest.name, runner=runner)]
        if missing:
            pushed = push_targets(paths, latest, runner=runner)
            return {"skipped": True, "reason": "fresh", "bundle": latest.name, "pushed": redact_obj(pushed)}
        return {"skipped": True, "reason": "fresh", "bundle": latest.name}
    created = create_bundle(conn, paths, push=True, runner=runner)
    return {"skipped": False, "bundle": created["name"], "bytes": created["bytes"], "pushed": created["pushed"]}


def _age_detail(bundle: Path | None, policy: dict, now: float) -> dict:
    if bundle is None:
        return {"check": "backup_age", "ok": False, "status": "FAIL", "detail": "no local bundle"}
    epoch = bundle_epoch(bundle)
    if epoch is None:
        return {"check": "backup_age", "ok": False, "status": "FAIL", "detail": "bundle name has no timestamp"}
    age = now - epoch
    limit = float(policy["backup_max_age_seconds"])
    ok = age <= limit
    return {"check": "backup_age", "ok": ok, "status": "PASS" if ok else "FAIL",
            "detail": f"age_seconds={int(age)} max={int(limit)} bundle={bundle.name}"}


def _venv_detail(bundle: Path | None) -> dict:
    if bundle is None:
        return {"check": "backup_venv_tools", "ok": False, "status": "FAIL", "detail": "no local bundle"}
    manifest = load_sidecar(bundle)
    if not manifest:
        return {"check": "backup_venv_tools", "ok": False, "status": "FAIL", "detail": "manifest sidecar missing"}
    venv_py = CODE_ROOT / ".venv" / "bin" / "python"
    problems = []
    if not venv_py.is_file():
        problems.append("venv python missing")
    env = manifest.get("environment") if isinstance(manifest.get("environment"), dict) else {}
    current = _environment()
    for key, label in (("requirements_lock_sha256", "requirements.lock"),
                       ("tools_lock_sha256", "tooling/requirements-tools.lock")):
        recorded = env.get(key)
        live = current.get(key)
        if recorded != live:
            problems.append(f"{label} drifted")
    detail = "matches last bundle" if not problems else "; ".join(problems)
    recorded_sha = manifest.get("git_sha")
    live_sha = current.get("git_sha")
    if recorded_sha and live_sha and recorded_sha != live_sha:
        detail += f"; git sha at backup {recorded_sha} now {live_sha}"
    ok = not problems
    return {"check": "backup_venv_tools", "ok": ok, "status": "PASS" if ok else "FAIL", "detail": detail}


def doctor_report(paths: Paths, *, now: float | None = None, runner=None) -> list[dict]:
    policy = load_policy(paths.config_dir)
    if policy["features"]["backup"] != "on":
        return [
            {"check": "backup_age", "ok": True, "status": "SKIP", "detail": "backup off"},
            {"check": "backup_offbox", "ok": True, "status": "SKIP", "detail": "backup off"},
            {"check": "backup_venv_tools", "ok": True, "status": "SKIP", "detail": "backup off"},
        ]
    moment = time.time() if now is None else now
    bundle = newest_bundle(bundles_dir(paths))
    checks = [_age_detail(bundle, policy, moment)]
    targets = policy["backup_targets"]
    if not targets:
        checks.append({"check": "backup_offbox", "ok": True, "status": "PASS",
                       "detail": "no off-box targets configured"})
    elif bundle is None:
        checks.append({"check": "backup_offbox", "ok": False, "status": "FAIL", "detail": "no local bundle"})
    else:
        missing = []
        for target in targets:
            label = target.get("repo") or target.get("path") or target["type"]
            try:
                present = target_has_bundle(target, bundle.name, runner=runner)
            except (OmgError, OSError):
                present = False
            if not present:
                missing.append(str(label))
        ok = not missing
        detail = "present" if ok else "missing " + ", ".join(missing)
        checks.append({"check": "backup_offbox", "ok": ok, "status": "PASS" if ok else "FAIL", "detail": detail})
    checks.append(_venv_detail(bundle))
    return checks


def status_report(paths: Paths, *, now: float | None = None) -> dict:
    """Read-only. Does not open hooks.json and does not create a bundle."""
    policy = load_policy(paths.config_dir)
    moment = time.time() if now is None else now
    bundle = newest_bundle(bundles_dir(paths))
    age = None
    if bundle is not None and bundle_epoch(bundle) is not None:
        age = moment - bundle_epoch(bundle)
    return {"feature": policy["features"]["backup"], "latest": None if bundle is None else bundle.name,
            "age_seconds": None if age is None else int(age),
            "targets": [target["type"] for target in policy["backup_targets"]]}


# ---------------------------------------------------------------- restore
def _kit_candidates(paths: Paths) -> list[Path]:
    found = []
    override = os.environ.get("OMG_BACKUP_KIT")
    if override:
        found.append(Path(override))
    store = os.environ.get("OMG_BACKUP_AGENT_STORE")
    if store:
        found.append(Path(store) / "kit")
    try:
        policy = load_policy(paths.config_dir)
    except OmgError:
        policy = {"backup_targets": []}
    for target in policy.get("backup_targets") or []:
        if not isinstance(target, dict):
            continue
        if target.get("type") == "agent_store":
            found.append(resolve_store(target) / "kit")
        elif target.get("type") == "github_release" and target.get("mirror"):
            found.append(Path(target["mirror"]) / "kit")
    mirror = os.environ.get("OMG_BACKUP_GITHUB_MIRROR")
    if mirror:
        found.append(Path(mirror) / "kit")
    if DEFAULT_AGENT_STORE.is_dir():
        found.append(DEFAULT_AGENT_STORE / "kit")
    found.append(resolve_kit(paths))
    found.append(kit_dir(paths))
    return found


def _bundle_sources(paths: Paths) -> list[tuple[int, Path, Path | None]]:
    """(rank, directory, kit) higher rank wins a timestamp tie. Agent store outranks a local copy."""
    sources = []
    store_env = os.environ.get("OMG_BACKUP_AGENT_STORE")
    if store_env:
        sources.append((30, Path(store_env) / "bundles", Path(store_env) / "kit"))
    try:
        policy = load_policy(paths.config_dir)
    except OmgError:
        policy = {"backup_targets": []}
    for target in policy.get("backup_targets") or []:
        if not isinstance(target, dict):
            continue
        if target.get("type") == "agent_store":
            store = resolve_store(target)
            sources.append((30, store / "bundles", store / "kit"))
        elif target.get("type") == "github_release" and target.get("mirror"):
            mirror = Path(target["mirror"])
            sources.append((10, mirror, mirror / "kit"))
    if not store_env and DEFAULT_AGENT_STORE.is_dir():
        sources.append((30, DEFAULT_AGENT_STORE / "bundles", DEFAULT_AGENT_STORE / "kit"))
    mirror_env = os.environ.get("OMG_BACKUP_GITHUB_MIRROR")
    if mirror_env:
        mirror = Path(mirror_env)
        sources.append((10, mirror, mirror / "kit"))
    sources.append((20, bundles_dir(paths), resolve_kit(paths)))
    return sources


def locate_bundle(paths: Paths, *, runner=None) -> tuple[Path, Path, list[Path]]:
    """Newest bundle, the kit that should verify it, and private dirs to shred afterwards.

    Only directories this function created are returned for shredding. A bundle that already
    lives in an agent store or under OMG_HOME is not in that list.
    """
    ranked = []
    owned: list[Path] = []
    for rank, directory, kit in _bundle_sources(paths):
        bundle = newest_bundle(directory)
        if bundle is None:
            continue
        epoch = bundle_epoch(bundle) or 0
        ranked.append((epoch, rank, bundle, kit))
    repo = os.environ.get("OMG_BACKUP_GITHUB_REPO")
    if repo:
        refuse_public(repo)
        work = _private_dir()
        try:
            bundle, kit = fetch_github_release(repo, work, runner or subprocess_runner)
            hold = _private_dir()
            owned.append(hold)
            held_bundle = hold / bundle.name
            _install_file(bundle, held_bundle)
            sig = Path(str(bundle) + ".sig")
            if sig.is_file():
                _install_file(sig, hold / sig.name)
            held_kit = hold / "kit"
            if _kit_complete(kit):
                _publish_kit(kit, held_kit)
            ranked.append((bundle_epoch(held_bundle) or 0, 10, held_bundle, held_kit))
        finally:
            shred_tree(work)
    if not ranked:
        for directory in owned:
            shred_tree(directory)
        raise NoBundle("no backup bundle found")
    ranked.sort()
    _epoch, _rank, bundle, kit = ranked[-1]
    if not _kit_complete(kit):
        for candidate in _kit_candidates(paths):
            if _kit_complete(candidate):
                kit = candidate
                break
    if not _kit_complete(kit):
        for directory in owned:
            shred_tree(directory)
        raise BundleInvalid("signing key not available")
    return bundle, kit, owned


def fetch_github_release(repo: str, work: Path, runner) -> tuple[Path, Path]:
    """Clone the kit and download the newest release assets. Never force-pushes. Tests pass a fake runner."""
    refuse_public(repo)
    remote = f"https://github.com/{repo}.git"
    refuse_public(remote)
    checkout = work / "repo"
    cp = _invoke(runner, ["git", "clone", "--depth", "1", "--", remote, str(checkout)])
    if cp.returncode != 0:
        raise NoBundle("could not clone the private backup repo")
    cp = _invoke(runner, ["gh", "release", "list", "--repo", repo, "--limit", "100", "--json", "tagName"])
    if cp.returncode != 0:
        raise NoBundle("could not list backup releases")
    try:
        rows = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise NoBundle("backup release list was not json") from exc
    tags = []
    for row in rows:
        tag = row.get("tagName") or ""
        epoch = bundle_epoch(Path(tag + ".tar.gpg"))
        if epoch is not None:
            tags.append((epoch, tag))
    if not tags:
        raise NoBundle("private repo has no backup releases")
    tags.sort()
    tag = tags[-1][1]
    assets = work / "assets"
    assets.mkdir(mode=0o700)
    cp = _invoke(runner, ["gh", "release", "download", tag, "--repo", repo, "--dir", str(assets),
                          "--pattern", "omg-*"])
    if cp.returncode != 0:
        raise NoBundle("could not download the backup release")
    bundle = assets / f"{tag}.tar.gpg"
    if not bundle.is_file():
        raise NoBundle("downloaded release has no bundle")
    return bundle, checkout / "kit"


def verify_signature(bundle: Path, kit: Path) -> None:
    sig = Path(str(bundle) + ".sig")
    if not sig.is_file() or sig.is_symlink() or bundle.is_symlink():
        raise BundleInvalid("bundle signature is missing")
    tmp = _private_dir()
    try:
        home = tmp / "gpg"
        home.mkdir(mode=0o700)
        os.chmod(home, 0o700)
        cp = _gpg(home, ["--import", str(kit / "signing.pub")])
        if cp.returncode != 0:
            raise BundleInvalid("could not import the signing public key")
        cp = _gpg(home, ["--trust-model", "always", "--verify", str(sig), str(bundle)])
        if cp.returncode != 0:
            raise BundleInvalid("bundle signature is not valid")
    finally:
        shred_tree(tmp)


def decrypt_bundle(bundle: Path, kit: Path, dest_tar: Path, passphrase: str) -> None:
    passphrase = _require_passphrase(passphrase) if len(passphrase) >= 12 else passphrase
    register_secret(passphrase)
    tmp = _private_dir()
    try:
        phrase = _write_passphrase_file(tmp, passphrase)
        home = tmp / "gpg"
        home.mkdir(mode=0o700)
        os.chmod(home, 0o700)
        cp = _gpg(home, ["--import", str(kit / "encryption.sec.asc")], passphrase_file=phrase)
        if cp.returncode != 0:
            raise DecryptFailed("could not import the encryption secret")
        cp = _gpg(home, ["--output", str(dest_tar), "--decrypt", str(bundle)], passphrase_file=phrase)
        if cp.returncode != 0 or not dest_tar.is_file():
            raise DecryptFailed("could not decrypt bundle")
        os.chmod(dest_tar, 0o600)
    finally:
        shred_tree(tmp)


def _remove_db_family(path: Path) -> None:
    for extra in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if extra.is_symlink() or extra.exists():
            extra.unlink()


def _restore_database(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    staged = dest.with_name(dest.name + ".restoring")
    _remove_db_family(staged)
    db.restore(src, staged)
    os.chmod(staged, 0o600)
    _remove_db_family(dest)
    os.replace(staged, dest)
    os.chmod(dest, 0o600)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(dest) + suffix)
        if sidecar.is_symlink() or sidecar.exists():
            sidecar.unlink()


def _replace_tree(dest: Path, src: Path) -> None:
    if dest.is_symlink():
        raise IntegrityError_(f"refusing to replace symlink {dest.name}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    if not src.exists():
        return
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        current = Path(dirpath)
        relative = current.relative_to(src)
        target = dest / relative
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            source = current / name
            if source.is_symlink():
                raise IntegrityError_(f"refusing to restore symlink {(relative / name).as_posix()}")
            try:
                data = source.read_bytes()
            except OSError:
                raise IntegrityError_(f"could not read {(relative / name).as_posix()}") from None
            out = target / name
            out.write_bytes(data)
            os.chmod(out, 0o600)
            del data


def _write_mode(dest: Path, data: bytes, mode: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    partial = dest.with_name(dest.name + ".partial")
    try:
        partial.write_bytes(data)
        os.chmod(partial, mode)
        os.replace(partial, dest)
        os.chmod(dest, mode)
    except BaseException:
        _shred_file(partial)
        raise
    finally:
        del data


def apply_snapshot(tree: Path, paths: Paths, manifest: dict, bundle: Path) -> None:
    databases = [path for path in (tree / "state").glob("*.sqlite3") if path.is_file() and not path.is_symlink()]
    if len(databases) != 1:
        raise BundleInvalid("bundle must contain exactly one sqlite database")
    src = databases[0]
    if src.name not in ("omg.sqlite3", "omo.sqlite3"):
        raise BundleInvalid("bundle database name is not omg.sqlite3 or omo.sqlite3")
    health, _events = inspect_database(src)
    if health != "ok":
        raise BundleInvalid("snapshot failed integrity_check")
    paths.ensure()
    other = "omo.sqlite3" if src.name == "omg.sqlite3" else "omg.sqlite3"
    _remove_db_family(paths.state_dir / other)
    _restore_database(src, paths.state_dir / src.name)
    _replace_tree(paths.jobs_dir, tree / "jobs")
    _replace_tree(paths.logs_dir, tree / "state" / "logs")
    policy_src = tree / "config" / "policy.json"
    if policy_src.is_file() and not policy_src.is_symlink():
        try:
            policy_bytes = policy_src.read_bytes()
        except OSError:
            raise IntegrityError_("could not read snapshot policy") from None
        _write_mode(paths.config_dir / "policy.json", policy_bytes, 0o600)
    hooks_src = tree / "state" / "secrets" / "hooks.json"
    if hooks_src.is_symlink():
        raise BundleInvalid("snapshot hooks.json is a symlink")
    if hooks_src.is_file():
        try:
            hook_bytes = hooks_src.read_bytes()
        except OSError:
            raise IntegrityError_("could not read snapshot hooks.json") from None
        paths.secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.secrets_dir, 0o700)
        _write_mode(paths.hooks_path, hook_bytes, 0o600)
    manifest_bytes = (tree / "manifest.json").read_bytes()
    directory = bundles_dir(paths)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    _install_file(bundle, directory / bundle.name)
    sig = Path(str(bundle) + ".sig")
    if sig.is_file():
        _install_file(sig, directory / sig.name)
    side = directory / (bundle.name + ".manifest.json")
    side.write_bytes(manifest_bytes)
    os.chmod(side, 0o600)


def _relation(paths: Paths, snapshot_db: Path) -> str:
    local_state, local_events = inspect_database(paths.db_path)
    if local_state != "ok" or local_events is None:
        return local_state
    remote_state, remote_events = inspect_database(snapshot_db)
    if remote_state != "ok" or remote_events is None:
        raise BundleInvalid("snapshot database failed integrity_check")
    return relate_streams(local_events, remote_events)


def _emit(payload: dict) -> None:
    print(json.dumps(redact_obj(payload), indent=2, sort_keys=True))


def run_recover(*, check: bool, passphrase: str, runner=None) -> int:
    """Verify, decrypt, and maybe restore. Writes nothing to the home when check is set or the decision is a no-op."""
    register_secret(passphrase)
    if not passphrase:
        print("OMG_BACKUP_PASSPHRASE is not set", file=sys.stderr)
        return EXIT_USAGE
    paths = get_paths()
    owned: list[Path] = []
    tmp = None
    try:
        try:
            bundle, kit, owned = locate_bundle(paths, runner=runner)
        except NoBundle as exc:
            print(redact(exc.message), file=sys.stderr)
            return EXIT_NO_BUNDLE
        except BundleInvalid as exc:
            print(redact(exc.message), file=sys.stderr)
            return EXIT_VERIFY
        try:
            verify_signature(bundle, kit)
        except BundleInvalid as exc:
            print(redact(exc.message), file=sys.stderr)
            return EXIT_VERIFY
        tmp = _private_dir()
        tar_path = tmp / "bundle.tar"
        tree = tmp / "tree"
        try:
            decrypt_bundle(bundle, kit, tar_path, passphrase)
        except DecryptFailed as exc:
            print(redact(exc.message), file=sys.stderr)
            return EXIT_DECRYPT
        except ValidationError as exc:
            print(redact(exc.message), file=sys.stderr)
            return EXIT_USAGE
        try:
            _safe_extract(tar_path, tree)
            manifest = json.loads((tree / "manifest.json").read_text())
            verify_tree(tree, manifest)
        except (BundleInvalid, IntegrityError_, OSError, ValueError):
            print("bundle manifest does not match archive contents", file=sys.stderr)
            return EXIT_VERIFY
        databases = list((tree / "state").glob("*.sqlite3"))
        if len(databases) != 1:
            print("bundle must contain exactly one sqlite database", file=sys.stderr)
            return EXIT_VERIFY
        try:
            relation = _relation(paths, databases[0])
        except BundleInvalid:
            print("snapshot database failed integrity_check", file=sys.stderr)
            return EXIT_VERIFY
        if relation == "diverged":
            _emit({"action": "diverged", "bundle": bundle.name, "relation": relation})
            return EXIT_DIVERGED
        if relation in ("equal", "local_newer"):
            _emit({"action": "noop", "bundle": bundle.name, "relation": relation})
            return EXIT_OK
        if relation not in ("missing", "corrupt", "local_prefix"):
            print("refusing to restore", file=sys.stderr)
            return EXIT_DIVERGED
        if check:
            _emit({"action": "would_restore", "bundle": bundle.name, "relation": relation})
            return EXIT_CHECK_BEHIND
        try:
            apply_snapshot(tree, paths, manifest, bundle)
        except (BundleInvalid, IntegrityError_) as exc:
            print(redact(exc.message), file=sys.stderr)
            return EXIT_VERIFY
        _emit({"action": "restored", "bundle": bundle.name, "relation": relation})
        return EXIT_OK
    finally:
        if tmp is not None:
            shred_tree(tmp)
        for directory in owned:
            shred_tree(directory)
