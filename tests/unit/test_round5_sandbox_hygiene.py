"""Round 5 minor items: the bwrap probe validates `bwrap --version` and records the version; _rmtree never follows
symlinks and never chmods anything outside the tree it deletes."""
import os
import subprocess

from omo_grok import sandbox
from omo_grok.verifier import _rmtree


class _CP:
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _probe_with(monkeypatch, version_out, rc=0, self_check=True):
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")
    if self_check:  # version-parsing tests: the namespace self-check is covered by the round-6 tests
        monkeypatch.setattr(sandbox, "namespace_self_check", lambda bw: None)

    def fake_run(argv, **kw):
        if argv[1:] == ["--version"]:
            return _CP(rc, version_out)
        return _CP(0, "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return sandbox.probe.__wrapped__()


def test_probe_records_version(monkeypatch):
    pr = _probe_with(monkeypatch, "bubblewrap 0.12.0\n")
    assert pr.ok and pr.version == "0.12.0" and pr.label == "bwrap 0.12.0" and "bubblewrap 0.12.0" in pr.detail


def test_probe_fails_closed_on_unexpected_version_output(monkeypatch):
    for out, rc in [("", 0), ("firejail 0.9", 0), ("bubblewrap 0.12.0\nextra", 0), ("bubblewrap x.y", 0),
                    ("bubblewrap 0.12.0", 1)]:
        pr = _probe_with(monkeypatch, out, rc)
        assert not pr.ok and pr.mechanism == "none" and "no sandbox available" in pr.detail, (out, rc)


def test_real_probe_has_version():
    pr = sandbox.probe()
    if pr.ok:
        assert pr.version and pr.label.startswith("bwrap ")


def test_rmtree_never_follows_symlinks_or_chmods_outside(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    (victim / "sub").mkdir()
    t = tmp_path / "stage"
    (t / "a" / "b").mkdir(parents=True)
    (t / "a" / "b" / "f").write_text("1")
    os.symlink(victim, t / "dirlink")
    os.symlink(victim / "sub", t / "a" / "sublink")
    os.symlink(victim / "keep", t / "filelink")
    os.chmod(t / "a" / "b", 0o500)
    os.chmod(t / "a", 0o500)
    os.chmod(t, 0o555)
    os.chmod(victim / "sub", 0o500)
    os.chmod(victim, 0o500)
    _rmtree(t)
    assert not os.path.lexists(t)
    assert (victim / "keep").read_text() == "x" and (victim / "sub").is_dir()
    assert oct(os.lstat(victim).st_mode & 0o777) == oct(0o500)
    assert oct(os.lstat(victim / "sub").st_mode & 0o777) == oct(0o500)
    os.chmod(victim / "sub", 0o700)
    os.chmod(victim, 0o700)


def test_rmtree_on_a_symlink_only_unlinks_it(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    link = tmp_path / "link"
    os.symlink(victim, link)
    _rmtree(link)
    assert not os.path.lexists(link) and (victim / "keep").exists()


def test_probe_fails_closed_when_the_self_check_gets_no_report(monkeypatch):
    """Round 6: a binary that prints the right version and exits 0 for everything is NOT a sandbox."""
    pr = _probe_with(monkeypatch, "bubblewrap 0.12.0\n", self_check=False)
    assert not pr.ok and "self-check failed" in pr.detail, pr
