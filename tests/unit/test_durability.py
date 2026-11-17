"""Durability bundles: keys, encryption, retention, off-box fakes, restore decisions.

Hook bytes and the passphrase are asserted by absence. A failure says that secret material
appeared; it does not print the material.
"""
import io
import json
import os
import subprocess
import tarfile
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oh_my_grok import durability, features
from oh_my_grok.durability import (
    EXIT_CHECK_BEHIND,
    EXIT_DIVERGED,
    EXIT_OK,
    EXIT_VERIFY,
    KIT_FILES,
    relate_streams,
    run_recover,
)
from oh_my_grok.engine import Engine
from oh_my_grok.errors import Conflict, ValidationError
from oh_my_grok.hooks import set_hook
from oh_my_grok.paths import CODE_ROOT, get_paths
from oh_my_grok.transport import HttpResult, post_json

ROOT = Path(__file__).resolve().parents[2]
OMG = str(ROOT / "omg")
PASSPHRASE = "test-only-passphrase-0123456789"
HOOK_URL = "https://durability.invalid/hook/ZZDURABILITYPATH"
HOOK_KEY = "hk_DURABILITY_SENTINEL_0123456789"


def _gh_assets(paths: list[Path]) -> str:
    assets = [{"name": path.name, "size": path.stat().st_size} for path in paths]
    return json.dumps({"assets": assets})


def _remember_release(argv, releases: dict[str, list[Path]]) -> subprocess.CompletedProcess[str]:
    tag = argv[3]
    files = []
    for arg in argv[4:]:
        if str(arg).startswith("--"):
            break
        files.append(Path(arg))
    releases[tag] = files
    return subprocess.CompletedProcess(argv, 0, "", "")


def _fake_gh(argv, releases: dict[str, list[Path]]) -> subprocess.CompletedProcess[str] | None:
    """Handle gh for a private repo. Returns None when argv is not gh."""
    if not argv or argv[0] != "gh":
        return None
    if argv[:2] == ["gh", "api"]:
        return subprocess.CompletedProcess(argv, 0, json.dumps({"private": True}), "")
    if argv[:3] == ["gh", "release", "view"]:
        tag = argv[3]
        if tag not in releases:
            return subprocess.CompletedProcess(argv, 1, "", "missing")
        return subprocess.CompletedProcess(argv, 0, _gh_assets(releases[tag]), "")
    if argv[:3] in (["gh", "release", "create"], ["gh", "release", "upload"]):
        return _remember_release(argv, releases)
    if argv[:3] == ["gh", "release", "list"]:
        return subprocess.CompletedProcess(argv, 0, json.dumps([{"tagName": tag} for tag in releases]), "")
    if argv[:3] == ["gh", "release", "delete"]:
        releases.pop(argv[3], None)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return None


@pytest.fixture(autouse=True)
def _gpg_agent_per_test(tmp_path, monkeypatch):
    """Each test gets its own GNUPGHOME, and agents started for that home are stopped."""
    home = tmp_path / "gnupg-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    yield
    targets = [home]
    omg = os.environ.get("OMG_HOME")
    if omg:
        targets.append(Path(omg) / "state" / "secrets" / "backup-gnupg")
    short = Path(tempfile.gettempdir()) / f"omg-gnupg-{os.getuid()}"
    if short.is_dir():
        targets.extend(path for path in short.iterdir() if path.is_symlink() or path.is_dir())
    for directory in targets:
        subprocess.run(["gpgconf", "--homedir", str(directory), "--kill", "gpg-agent"],
                       capture_output=True, check=False)


def _absent(blob, needle: str) -> None:
    raw = blob if isinstance(blob, bytes) else str(blob).encode()
    if needle.encode() in raw:
        raise AssertionError("secret material appeared in command output")


def _policy(**extra):
    path = Path(os.environ["OMG_CONFIG_DIR"]) / "policy.json"
    data = json.loads(path.read_text())
    data.update(extra)
    path.write_text(json.dumps(data))


def _init(monkeypatch):
    monkeypatch.setenv("OMG_BACKUP_PASSPHRASE", PASSPHRASE)
    return durability.init_keys(get_paths(), passphrase=PASSPHRASE)


def _seed():
    engine = Engine(transport=lambda *a, **k: HttpResult(200, None, None))
    job = engine.submit("Fix calc.add in the synthetic fixture", "durability-seed-0001", mode="fixture",
                        acceptance_criteria=["unittest suite exits 0"])
    set_hook(get_paths(), "explorer", HOOK_URL, HOOK_KEY)
    (get_paths().logs_dir / "drill.log").write_text("drill-log\n")
    engine.close()
    return job["job_id"]


def _capture(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = fn()
    return code, buf.getvalue()


def test_disable_transport_returns_before_any_request(monkeypatch):
    monkeypatch.setenv("OMG_DISABLE_TRANSPORT", "1")
    result = post_json(HOOK_URL, HOOK_KEY, {"job_id": "x"}, 1, 1)
    assert result.status is None and result.error == "transport disabled"
    _absent(result.error, HOOK_KEY)
    _absent(result.error, "ZZDURABILITYPATH")


def test_public_repository_is_refused_before_a_command_runs(omg_home):
    bypasses = (
        "pysol-dev/oh-my-grok",
        "PYSOL-DEV/OH-MY-GROK",
        "github.com/pysol-dev/oh-my-grok",
        "https://github.com/pysol-dev/oh-my-grok.git/",
        "https://www.github.com/pysol-dev/oh-my-grok",
        "https://user@github.com/pysol-dev/oh-my-grok.git",
        "ssh://git@github.com:22/pysol-dev/oh-my-grok.git",
        "ssh://git@ssh.github.com:443/pysol-dev/oh-my-grok.git",
        "git@github.com:pysol-dev/oh-my-grok.git",
        "ssh://git@github.com/pysol-dev/oh-my-grok",
    )
    for value in bypasses:
        with pytest.raises(ValidationError):
            durability.refuse_public(value)
        assert durability.canonical_github_repo(value) == "pysol-dev/oh-my-grok"
    with pytest.raises(ValidationError):
        durability.require_owner_name("https://github.com/example/private.git")
    with pytest.raises(ValidationError):
        durability.require_owner_name("github.com/pysol-dev/oh-my-grok")
    assert durability.require_owner_name("Example/Private-Backups") == "Example/Private-Backups"
    calls = []

    def runner(argv, cwd=None, env=None):
        calls.append(list(argv))
        raise AssertionError("runner invoked")

    bundle = get_paths().state_dir / "unused.tar.gpg"
    with pytest.raises(ValidationError):
        durability.push_github(get_paths(), bundle, {"type": "github_release", "repo": "pysol-dev/oh-my-grok"},
                               {"backup_keep_hourly": 48, "backup_keep_daily": 14}, runner=runner)
    assert calls == []
    _policy(backup_targets=[{"type": "github_release", "repo": "https://github.com/pysol-dev/oh-my-grok"}])
    with pytest.raises(ValidationError):
        Engine()


def test_recover_refuses_the_public_repo_before_clone(omg_home, monkeypatch):
    calls = []

    def runner(argv, cwd=None, env=None):
        calls.append(list(argv))
        raise AssertionError("runner invoked")

    monkeypatch.setenv("OMG_BACKUP_GITHUB_REPO", "git@github.com:pysol-dev/oh-my-grok.git")
    with pytest.raises(ValidationError):
        durability.locate_bundle(get_paths(), runner=runner)
    assert calls == []
    durability.refuse_public("example/omg-private-backups")
    durability.refuse_public("https://github.com/example/omg-private-backups.git")


def test_retention_keeps_hourly_and_daily_anchors(tmp_path):
    directory = tmp_path / "bundles"
    directory.mkdir()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    names = []
    for day in range(5):
        for hour in (12, 18):
            moment = base + timedelta(days=day, hours=hour)
            name = moment.strftime("omg-%Y%m%dT%H%M%SZ.tar.gpg")
            path = directory / name
            path.write_bytes(b"x" * (1000 if hour == 12 else 10))
            (directory / (name + ".sig")).write_bytes(b"s" * 20)
            names.append(name)
    policy = {"backup_keep_hourly": 2, "backup_keep_daily": 3, "backup_size_cap_bytes": 10_000_000}
    removed = durability._prune_dir(directory, policy)
    kept = {path.name for path in directory.glob("omg-*.tar.gpg")}
    newest = max(names)
    assert newest in kept and newest not in removed
    assert len(kept) == 4  # 2 hourly (newest day 12:00 and 18:00 are not both hourly; newest 2) + dailies
    # newest two are day 4 at 18:00 and day 4 at 12:00. Daily anchors are the newest of the last 3 days,
    # which is day 4 18:00, day 3 18:00, day 2 18:00. Union adds day 3 and day 2 at 18:00.
    assert "omg-20260105T180000Z.tar.gpg" in kept
    assert "omg-20260104T180000Z.tar.gpg" in kept
    assert "omg-20260103T180000Z.tar.gpg" in kept
    assert "omg-20260102T180000Z.tar.gpg" not in kept
    # size cap drops the oldest non-protected file and never the newest
    for path in directory.glob("omg-*.tar.gpg"):
        path.write_bytes(b"y" * 500)
        Path(str(path) + ".sig").write_bytes(b"s" * 500)
    policy["backup_size_cap_bytes"] = 1200
    durability._prune_dir(directory, policy)
    survived = {path.name for path in directory.glob("omg-*.tar.gpg")}
    assert newest in survived
    assert len(survived) < len(kept)
    total = sum(path.stat().st_size for path in directory.iterdir())
    assert total <= 1200 or survived == {newest}


def test_doctor_skips_when_backup_is_off_and_fails_age_when_on(omg_home):
    paths = get_paths()
    skipped = {item["check"]: item for item in durability.doctor_report(paths)}
    assert skipped["backup_age"]["status"] == "SKIP" and skipped["backup_age"]["ok"] is True
    assert skipped["backup_offbox"]["status"] == "SKIP"
    assert skipped["backup_venv_tools"]["status"] == "SKIP"
    _policy(features={"backup": "on"})
    missing = {item["check"]: item for item in durability.doctor_report(paths)}
    assert missing["backup_age"]["ok"] is False
    directory = durability.bundles_dir(paths)
    directory.mkdir(parents=True)
    old = directory / "omg-20200101T000000Z.tar.gpg"
    old.write_bytes(b"old")
    (directory / (old.name + ".sig")).write_bytes(b"sig")
    env = durability._environment()
    sidecar = {
        "git_sha": env["git_sha"],
        "environment": {k: env[k] for k in ("requirements_lock_sha256", "tools_lock_sha256") if k in env},
        "mcp_command": durability.mcp_command_record(),
    }
    Path(str(old) + ".manifest.json").write_text(json.dumps(sidecar))
    aged = {item["check"]: item for item in durability.doctor_report(paths, now=1_800_000_000)}
    assert aged["backup_age"]["ok"] is False
    assert aged["backup_offbox"]["ok"] is True  # no targets configured
    fresh_name = datetime.now(UTC).strftime("omg-%Y%m%dT%H%M%SZ.tar.gpg")
    fresh = directory / fresh_name
    fresh.write_bytes(b"new")
    Path(str(fresh) + ".manifest.json").write_text(json.dumps(sidecar))
    current = {item["check"]: item for item in durability.doctor_report(paths)}
    assert current["backup_age"]["ok"] is True
    assert current["backup_venv_tools"]["ok"] is True
    bad = json.loads(Path(str(fresh) + ".manifest.json").read_text())
    bad["environment"]["requirements_lock_sha256"] = "0" * 64
    Path(str(fresh) + ".manifest.json").write_text(json.dumps(bad))
    drifted = {item["check"]: item for item in durability.doctor_report(paths)}
    assert drifted["backup_venv_tools"]["ok"] is False
    store = paths.state_dir / "offbox"
    _policy(features={"backup": "on"}, backup_targets=[{"type": "agent_store", "path": str(store)}])
    absent = {item["check"]: item for item in durability.doctor_report(paths)}
    assert absent["backup_offbox"]["ok"] is False
    dest = store / "bundles"
    dest.mkdir(parents=True)
    (dest / fresh.name).write_bytes(b"new")
    (dest / (fresh.name + ".sig")).write_bytes(b"sig")
    present = {item["check"]: item for item in durability.doctor_report(paths)}
    assert present["backup_offbox"]["ok"] is True


def test_hourly_step_is_independent_of_the_watchdog_flag(omg_home):
    from oh_my_grok import watchdog
    engine = Engine()
    try:
        off = watchdog.tick(engine)
        assert off["steps"]["reconcile"]["skipped"] is True
        assert off["steps"]["backup"]["skipped"] is True and off["steps"]["backup"]["reason"] == "backup off"
    finally:
        engine.close()
    _policy(features={"watchdog": "off", "backup": "on"})
    set_hook(get_paths(), "explorer", HOOK_URL, HOOK_KEY)
    engine = Engine()
    try:
        failed = watchdog.tick(engine)
    finally:
        engine.close()
    assert failed["steps"]["reconcile"]["skipped"] is True
    assert failed["steps"]["backup"]["ok"] is False
    _absent(json.dumps(failed), HOOK_KEY)
    _absent(json.dumps(failed), "ZZDURABILITYPATH")
    directory = durability.bundles_dir(get_paths())
    directory.mkdir(parents=True, exist_ok=True)
    name = datetime.now(UTC).strftime("omg-%Y%m%dT%H%M%SZ.tar.gpg")
    (directory / name).write_bytes(b"cipher")
    engine = Engine()
    try:
        fresh = durability.hourly_backup(engine.conn, engine.paths)
    finally:
        engine.close()
    assert fresh["skipped"] is True and fresh["reason"] == "fresh" and fresh["bundle"] == name


def test_github_push_uses_a_local_remote_and_never_force(omg_home, tmp_path):
    paths = get_paths()
    kit = durability.kit_dir(paths)
    kit.mkdir(parents=True)
    for name in KIT_FILES:
        (kit / name).write_text("kit-file-" + name + "\n")
        os.chmod(kit / name, 0o600)
    bundle = tmp_path / "omg-20261117T010203Z.tar.gpg"
    bundle.write_bytes(b"ciphertext")
    (tmp_path / (bundle.name + ".sig")).write_bytes(b"signature")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    releases = {}
    calls = []

    def runner(argv, cwd=None, env=None):
        calls.append(list(argv))
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
        gh = _fake_gh(argv, releases)
        if gh is not None:
            if argv[:3] == ["gh", "release", "delete"]:
                assert "--force" not in argv
            return gh
        raise AssertionError(argv)

    target = {"type": "github_release", "repo": "example/omg-private-backups", "remote": str(bare),
              "checkout": str(tmp_path / "checkout")}
    policy = {"backup_keep_hourly": 48, "backup_keep_daily": 14, "backup_size_cap_bytes": 1_000_000}
    result = durability.push_github(paths, bundle, target, policy, runner=runner)
    assert result["ok"] is True and result["tag"] == "omg-20261117T010203Z"
    assert [path.name for path in releases[result["tag"]]] == [bundle.name, bundle.name + ".sig"]
    assert (tmp_path / "checkout").stat().st_mode & 0o777 == 0o700
    pushes = [cmd for cmd in calls if cmd and cmd[0] == "git" and "push" in cmd]
    assert pushes and all("--force" not in cmd and "-f" not in cmd and not any(a.startswith("+") for a in cmd)
                          for cmd in pushes)
    adds = [cmd for cmd in calls if cmd and cmd[0] == "git" and "add" in cmd]
    assert adds and all(not any(str(a).endswith(".tar.gpg") for a in cmd) for cmd in adds)
    assert (tmp_path / "checkout" / "kit" / "encryption.sec.asc").is_file()
    assert "PRIVATE" not in (tmp_path / "checkout" / "README.md").read_text()
    # A non-fast-forward is a failure and is not retried with --force.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    (other / "README.md").write_text("diverged\n")
    subprocess.run(["git", "-C", str(other), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "-c", "user.name=t", "-c", "user.email=t@localhost",
                    "commit", "-m", "diverge"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "push", "origin", "HEAD:main"], check=True, capture_output=True)
    head_before = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], check=True, capture_output=True,
                                 text=True).stdout.strip()
    (kit / "signing.pub").write_text("kit-file-signing.pub\nchanged\n")
    with pytest.raises(ValidationError):
        durability.push_github(paths, bundle, target, policy, runner=runner)
    head_after = subprocess.run(["git", "-C", str(bare), "rev-parse", "HEAD"], check=True, capture_output=True,
                                text=True).stdout.strip()
    assert head_after == head_before
    assert not any("--force" in cmd or (cmd[0] == "git" and "-f" in cmd) for cmd in calls)


def test_a_rejected_kit_push_is_retried_on_the_next_publication(omg_home, tmp_path):
    paths = get_paths()
    kit = durability.kit_dir(paths)
    kit.mkdir(parents=True)
    for name in KIT_FILES:
        (kit / name).write_text("kit-" + name + "\n")
        os.chmod(kit / name, 0o600)
    bundle = tmp_path / "omg-20261117T030405Z.tar.gpg"
    bundle.write_bytes(b"ciphertext")
    (tmp_path / (bundle.name + ".sig")).write_bytes(b"signature")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    releases = {}
    pushes = []

    def runner(argv, cwd=None, env=None):
        if argv[0] == "git" and "push" in argv:
            pushes.append(list(argv))
            if len(pushes) == 1:
                return subprocess.CompletedProcess(argv, 1, "", "rejected")
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
        gh = _fake_gh(argv, releases)
        if gh is not None:
            return gh
        raise AssertionError(argv)

    target = {"type": "github_release", "repo": "example/omg-private-backups", "remote": str(bare),
              "checkout": str(tmp_path / "checkout")}
    policy = {"backup_keep_hourly": 48, "backup_keep_daily": 14, "backup_size_cap_bytes": 1_000_000}
    with pytest.raises(ValidationError, match="git push"):
        durability.push_github(paths, bundle, target, policy, runner=runner)
    unborn = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "HEAD"], capture_output=True, check=False)
    assert unborn.returncode != 0
    result = durability.push_github(paths, bundle, target, policy, runner=runner)
    assert result["ok"] is True
    assert len(pushes) == 2
    assert all("--force" not in cmd and "-f" not in cmd for cmd in pushes)
    listed = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "HEAD"],
                            check=True, capture_output=True, text=True)
    assert "kit/encryption.sec.asc" in listed.stdout.split()
    assert "kit/signing.pub" in listed.stdout.split()


def test_custom_init_kit_is_the_directory_publication_copies(omg_home, monkeypatch, tmp_path):
    custom = tmp_path / "custom-kit"
    monkeypatch.setenv("OMG_BACKUP_PASSPHRASE", PASSPHRASE)
    info = durability.init_keys(get_paths(), passphrase=PASSPHRASE, kit=custom)
    assert Path(info["kit"]).resolve() == custom.resolve()
    assert Path(durability.kit_pointer_path(get_paths()).read_text().strip()) == custom.resolve()
    assert durability.resolve_kit(get_paths()).resolve() == custom.resolve()
    assert not durability._kit_complete(durability.kit_dir(get_paths()))
    default = durability.kit_dir(get_paths())
    default.mkdir(parents=True)
    for name in KIT_FILES:
        (default / name).write_text("wrong-kit-" + name)
    bundle = tmp_path / "omg-20261117T040506Z.tar.gpg"
    bundle.write_bytes(b"ciphertext")
    (tmp_path / (bundle.name + ".sig")).write_bytes(b"sig")
    Path(str(bundle) + ".manifest.json").write_text(json.dumps({
        "files": {"state/secrets/hooks.json": {"sha256": "abc", "bytes": 3}},
        "mcp_command": "/tmp/secret-box/python",
        "created_at": "2026-11-17T00:00:00.000000Z",
        "generation": {"max_event_id": 4, "tail_sha256": "ab"},
    }))
    store = tmp_path / "store"
    policy = {"backup_keep_hourly": 48, "backup_keep_daily": 14, "backup_size_cap_bytes": 1_000_000}
    durability.push_agent_store(get_paths(), bundle,
                                {"type": "agent_store", "path": str(store)}, policy)
    for name in KIT_FILES:
        assert durability._sha256_file(store / "kit" / name) == durability._sha256_file(custom / name)
    assert b"PRIVATE KEY" not in (store / "kit" / "signing.pub").read_bytes()
    assert b"wrong-kit-" not in (store / "kit" / "encryption.pub").read_bytes()
    assert not list((store / "bundles").glob("*.manifest.json"))
    public = json.loads((store / "bundles" / (bundle.name + ".public.json")).read_text())
    assert set(public) == {"bundle", "sha256", "created_at", "generation", "signature"}
    published = json.dumps(public)
    assert "hooks.json" not in published and "secret-box" not in published
    assert public["sha256"] == durability._sha256_file(bundle)
    assert public["generation"] == {"max_event_id": 4, "tail_sha256": "ab"}


def test_raw_sqlite_backup_still_writes_a_database(omg_home, tmp_path):
    env = os.environ.copy()
    dest = tmp_path / "snap.sqlite3"
    result = subprocess.run([OMG, "backup", "--out", str(dest), "--json"], capture_output=True, text=True,
                            env=env, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    assert dest.is_file() and (dest.stat().st_mode & 0o077) == 0


def test_keys_bundle_and_restore_decisions(omg_home, monkeypatch):
    gpg_commands = []
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "gpg":
            gpg_commands.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    def spy_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "gpg":
            gpg_commands.append(list(cmd))
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    info = _init(monkeypatch)
    kit = Path(info["kit"])
    assert (kit.stat().st_mode & 0o777) == 0o700
    for name in KIT_FILES:
        path = kit / name
        assert (path.stat().st_mode & 0o777) == 0o600
        _absent(path.read_bytes(), PASSPHRASE)
    assert b"PRIVATE KEY" not in (kit / "signing.pub").read_bytes()
    assert b"PRIVATE KEY" not in (kit / "encryption.pub").read_bytes()
    secrets = " ".join(durability._secret_uids(durability.gnupg_home(get_paths())))
    assert "omg-backup-signing" in secrets
    assert "omg-backup-encryption" not in secrets
    with pytest.raises(Conflict):
        durability.init_keys(get_paths(), passphrase=PASSPHRASE)
    _seed()
    hooks_hash_before = durability._sha256_file(get_paths().hooks_path)
    engine = Engine(transport=lambda *a, **k: HttpResult(200, None, None))
    try:
        created = durability.create_bundle(engine.conn, engine.paths, push=False)
    finally:
        engine.close()
    _absent(json.dumps(created), HOOK_KEY)
    _absent(json.dumps(created), "ZZDURABILITYPATH")
    _absent(json.dumps(created), PASSPHRASE)
    bundle = Path(created["path"])
    sidecar = json.loads(Path(str(bundle) + ".manifest.json").read_text())
    assert sidecar["mcp_command"] == f"{CODE_ROOT / '.venv' / 'bin' / 'python'} {CODE_ROOT / 'oh_my_grok' / 'mcp_server.py'}"
    assert sidecar["generation"]["max_event_id"] >= 1
    assert sidecar["schema_version"] >= 1
    assert features.normalize({})["backup"] == "off"

    db_path = get_paths().db_path
    db_path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    code, text = _capture(lambda: run_recover(check=True, passphrase=PASSPHRASE))
    assert code == EXIT_CHECK_BEHIND and "would_restore" in text
    assert not db_path.exists()
    _absent(text, HOOK_KEY)

    code, text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_OK and "restored" in text
    health, events = durability.inspect_database(db_path)
    assert health == "ok" and events is not None
    assert durability._sha256_file(get_paths().hooks_path) == hooks_hash_before
    assert (get_paths().hooks_path.stat().st_mode & 0o777) == 0o600
    _absent(text, HOOK_KEY)
    _absent(text, PASSPHRASE)

    before = durability._sha256_file(db_path)
    code, text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_OK and json.loads(text)["action"] == "noop"
    assert durability._sha256_file(db_path) == before

    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO events (ts, kind, data_json) VALUES ('t', 'note', '{\"added\":true}')")
    conn.commit()
    conn.close()
    code, text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_OK and json.loads(text)["relation"] == "local_newer"
    health, events = durability.inspect_database(db_path)
    assert health == "ok" and events is not None
    assert any(row["kind"] == "note" for row in events)

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE events SET data_json='{\"diverged\":true}' WHERE id=(SELECT MIN(id) FROM events)")
    conn.commit()
    mutated = conn.execute("SELECT data_json FROM events ORDER BY id ASC LIMIT 1").fetchone()[0]
    conn.close()
    code, text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_DIVERGED
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT data_json FROM events ORDER BY id ASC LIMIT 1").fetchone()[0] == mutated
    conn.close()

    db_path.write_bytes(b"not a database")
    code, _text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_OK
    health, events = durability.inspect_database(db_path)
    assert health == "ok" and events is not None and not any(row["kind"] == "note" for row in events)
    digest = durability._sha256_file(db_path)
    pin = (kit / "signing.fpr").read_text()
    (kit / "signing.fpr").write_text("ab" * 20 + "\n")
    code, _text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_VERIFY
    assert durability._sha256_file(db_path) == digest
    (kit / "signing.fpr").write_text(pin)
    local_pin = durability.signing_pin_path(get_paths())
    saved_pin = local_pin.read_text()
    local_pin.write_text("cd" * 20 + "\n")
    code, _text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_VERIFY
    assert durability._sha256_file(db_path) == digest
    local_pin.write_text(saved_pin)

    original = bundle.read_bytes()
    original_sig = Path(str(bundle) + ".sig").read_bytes()
    flipped = bytearray(original)
    flipped[-1] ^= 0x01
    bundle.write_bytes(bytes(flipped))
    digest = durability._sha256_file(db_path)
    code, _text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_VERIFY
    assert durability._sha256_file(db_path) == digest
    bundle.write_bytes(original)
    Path(str(bundle) + ".sig").write_bytes(b"not-a-signature")
    code, _text = _capture(lambda: run_recover(check=False, passphrase=PASSPHRASE))
    assert code == EXIT_VERIFY
    assert durability._sha256_file(db_path) == digest
    Path(str(bundle) + ".sig").write_bytes(original_sig)
    code, _text = _capture(lambda: run_recover(check=False, passphrase="wrong-passphrase-0123456789"))
    assert code == durability.EXIT_DECRYPT
    assert durability._sha256_file(db_path) == digest
    flat = [arg for cmd in gpg_commands for arg in cmd]
    assert "--passphrase-fd" in flat
    assert "--passphrase-file" not in flat
    assert PASSPHRASE not in flat


def test_job_scripts_keep_owner_execute_and_secrets_stay_private(tmp_path):
    jobs = tmp_path / "jobs"
    script = jobs / "abc" / "worktree" / "run.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho ok\n")
    os.chmod(script, 0o755)
    note = jobs / "abc" / "note.txt"
    note.write_text("note")
    os.chmod(note, 0o644)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "app.log").write_text("log")
    os.chmod(logs / "app.log", 0o755)
    tree = tmp_path / "tree"
    files: dict[str, dict[str, str]] = {}
    durability._add_tree(tree, jobs, "jobs", files, allow_execute=True)
    durability._add_tree(tree, logs, "state/logs", files, allow_execute=False)
    tar_path = tmp_path / "bundle.tar"
    with tarfile.open(tar_path, "w") as archive:
        for dirpath, _dirnames, filenames in os.walk(tree):
            for filename in filenames:
                path = Path(dirpath) / filename
                archive.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False)
    extracted = tmp_path / "extracted"
    durability._safe_extract(tar_path, extracted)
    restored_jobs = tmp_path / "restored-jobs"
    restored_logs = tmp_path / "restored-logs"
    durability._replace_tree(restored_jobs, extracted / "jobs", allow_execute=True)
    durability._replace_tree(restored_logs, extracted / "state" / "logs", allow_execute=False)
    assert (restored_jobs / "abc" / "worktree" / "run.sh").stat().st_mode & 0o777 == 0o700
    assert (restored_jobs / "abc" / "note.txt").stat().st_mode & 0o777 == 0o600
    assert (restored_logs / "app.log").stat().st_mode & 0o777 == 0o600
    assert files["jobs/abc/worktree/run.sh"]["mode"] == "0700"
    assert files["state/logs/app.log"]["mode"] == "0600"


def test_explicit_kit_overrides_a_complete_source_kit(omg_home, monkeypatch, tmp_path):
    paths = get_paths()
    paths.ensure()
    directory = durability.bundles_dir(paths)
    directory.mkdir(parents=True, exist_ok=True)
    bundle = directory / "omg-20261117T010203Z.tar.gpg"
    bundle.write_bytes(b"cipher")
    (directory / (bundle.name + ".sig")).write_bytes(b"sig")
    local = durability.kit_dir(paths)
    local.mkdir(parents=True, exist_ok=True)
    override = tmp_path / "override-kit"
    override.mkdir()
    for name in KIT_FILES:
        (local / name).write_text("local-" + name)
        (override / name).write_text("override-" + name)
    monkeypatch.setenv("OMG_BACKUP_KIT", str(override))
    _found, kit, _owned = durability.locate_bundle(paths)
    assert kit.resolve() == override.resolve()
    assert (kit / "signing.pub").read_text() == "override-signing.pub"
    (override / "signing.fpr").unlink()
    with pytest.raises(durability.BundleInvalid):
        durability.locate_bundle(paths)


def test_event_stream_prefix_is_strict():
    base = [{"id": 1, "kind": "a"}, {"id": 2, "kind": "b"}]
    assert relate_streams(base, base) == "equal"
    assert relate_streams(base[:1], base) == "local_prefix"
    assert relate_streams(base, base[:1]) == "local_newer"
    changed = [dict(base[0], kind="nope"), base[1]]
    assert relate_streams(changed, base) == "diverged"
    assert relate_streams([], base) == "local_prefix"
    assert relate_streams([], []) == "equal"


def test_same_timestamp_suffixes_sort_by_integer_sequence():
    older = durability.bundle_sort_key("omg-20261117T010203000000Z.tar.gpg")
    second = durability.bundle_sort_key("omg-20261117T010203000000Z-0002.tar.gpg")
    tenth = durability.bundle_sort_key("omg-20261117T010203000000Z-0010.tar.gpg")
    assert older is not None and second is not None and tenth is not None
    assert older < second < tenth
    # Lexical order would put -10 before -2. The sequence is the integer.
    lexical_trap_low = durability.bundle_sort_key("omg-20261117T010203Z-2.tar.gpg")
    lexical_trap_high = durability.bundle_sort_key("omg-20261117T010203Z-10.tar.gpg")
    assert lexical_trap_low is not None and lexical_trap_high is not None
    assert lexical_trap_low < lexical_trap_high


def test_publication_refuses_a_repo_that_is_not_private(omg_home, tmp_path):
    calls = []

    def runner(argv, cwd=None, env=None):
        calls.append(list(argv))
        if argv[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"private": False}), "")
        raise AssertionError(argv)

    bundle = tmp_path / "omg-20261117T050607Z.tar.gpg"
    bundle.write_bytes(b"cipher")
    (tmp_path / (bundle.name + ".sig")).write_text("sig")
    with pytest.raises(ValidationError, match="not private"):
        durability.push_github(get_paths(), bundle, {"type": "github_release", "repo": "example/public-backups",
                                                     "remote": str(tmp_path / "bare.git"),
                                                     "checkout": str(tmp_path / "co")},
                               {"backup_keep_hourly": 1, "backup_keep_daily": 1, "backup_size_cap_bytes": 1000},
                               runner=runner)
    assert not any(cmd[0] == "git" for cmd in calls)

    def failing(argv, cwd=None, env=None):
        calls.append(list(argv))
        if argv[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(argv, 1, "", "unavailable")
        raise AssertionError(argv)

    calls.clear()
    with pytest.raises(ValidationError, match="could not verify"):
        durability.push_github(get_paths(), bundle, {"type": "github_release", "repo": "example/public-backups",
                                                     "checkout": str(tmp_path / "co2")},
                               {"backup_keep_hourly": 1, "backup_keep_daily": 1, "backup_size_cap_bytes": 1000},
                               runner=failing)
    assert not any(cmd[0] == "git" for cmd in calls)


def test_incomplete_release_is_reuploaded_and_checked(omg_home, tmp_path):
    paths = get_paths()
    kit = durability.kit_dir(paths)
    kit.mkdir(parents=True)
    for name in KIT_FILES:
        (kit / name).write_text("kit-" + name + "\n")
    bundle = tmp_path / "omg-20261117T060708Z.tar.gpg"
    bundle.write_bytes(b"ciphertext-bytes")
    signature = tmp_path / (bundle.name + ".sig")
    signature.write_bytes(b"signature-bytes")
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    tag = "omg-20261117T060708Z"
    state = {"assets": [{"name": bundle.name, "size": 1}]}
    uploads = []

    def runner(argv, cwd=None, env=None):
        if argv[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"private": True}), "")
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
        if argv[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"assets": state["assets"]}), "")
        if argv[:3] == ["gh", "release", "upload"]:
            uploads.append(list(argv))
            state["assets"] = [{"name": bundle.name, "size": bundle.stat().st_size},
                               {"name": signature.name, "size": signature.stat().st_size}]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "release", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"tagName": tag}]), "")
        if argv[:3] == ["gh", "release", "delete"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    result = durability.push_github(
        paths, bundle, {"type": "github_release", "repo": "example/private-backups", "remote": str(bare),
                        "checkout": str(tmp_path / "checkout")},
        {"backup_keep_hourly": 4, "backup_keep_daily": 2, "backup_size_cap_bytes": 1_000_000}, runner=runner)
    assert result["ok"] is True
    assert uploads and "--clobber" in uploads[0]
    assert not any(cmd[0] == "git" and "--force" in cmd for cmd in uploads)

    state["assets"] = [{"name": bundle.name, "size": 1}]

    def still_wrong(argv, cwd=None, env=None):
        if argv[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"private": True}), "")
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
        if argv[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"assets": state["assets"]}), "")
        if argv[:3] == ["gh", "release", "upload"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "release", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        raise AssertionError(argv)

    with pytest.raises(ValidationError, match="do not match"):
        durability.push_github(
            paths, bundle, {"type": "github_release", "repo": "example/private-backups", "remote": str(bare),
                            "checkout": str(tmp_path / "checkout")},
            {"backup_keep_hourly": 4, "backup_keep_daily": 2, "backup_size_cap_bytes": 1_000_000}, runner=still_wrong)


def test_github_recovery_skips_a_release_without_both_assets(tmp_path):
    newer = "omg-20261117T020000Z"
    older = "omg-20261117T010000Z"
    older_bytes = b"older-bundle"
    older_sig = b"older-sig"

    def runner(argv, cwd=None, env=None):
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "release", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([
                {"tagName": newer}, {"tagName": older}]), "")
        if argv[:3] == ["gh", "release", "view"]:
            tag = argv[3]
            if tag == newer:
                assets = [{"name": newer + ".tar.gpg", "size": 4}]
            else:
                assets = [{"name": older + ".tar.gpg", "size": len(older_bytes)},
                          {"name": older + ".tar.gpg.sig", "size": len(older_sig)}]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"assets": assets}), "")
        if argv[:3] == ["gh", "release", "download"]:
            tag = argv[3]
            dest = Path(argv[argv.index("--dir") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            if tag == older:
                (dest / (older + ".tar.gpg")).write_bytes(older_bytes)
                (dest / (older + ".tar.gpg.sig")).write_bytes(older_sig)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    bundle, _kit = durability.fetch_github_release("example/private-backups", tmp_path / "work", runner)
    assert bundle.name == older + ".tar.gpg"
    assert bundle.read_bytes() == older_bytes


def test_child_processes_do_not_receive_the_passphrase(monkeypatch, tmp_path):
    monkeypatch.setenv("OMG_BACKUP_PASSPHRASE", PASSPHRASE)
    seen = {}

    def spy(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, "", "")

    real = subprocess.run
    monkeypatch.setattr(durability.subprocess, "run", spy)
    durability.subprocess_runner(["true"])
    assert seen["env"] is not None and "OMG_BACKUP_PASSPHRASE" not in seen["env"]
    monkeypatch.setattr(durability.subprocess, "run", real)
    target = tmp_path / "secret-file"
    target.write_text("data")
    durability._shred_file(target)
    script = (ROOT / "scripts" / "recover.sh").read_text()
    assert script.index("unset OMG_BACKUP_PASSPHRASE") < script.index('"$ROOT/omg" watchdog ensure')
