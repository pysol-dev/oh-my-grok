"""Round 6 (Momus re-review #6, criterion 5 item 6): `_rmtree` must clean trees the sandboxed code made hostile:
directories chmodded to 000 / 0o100 and trees far deeper than any recursion cap, without following symlinks."""
import os

from oh_my_grok import verifier


def _deep(root, n):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(n):
        os.mkdir("d", dir_fd=fd)
        nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = nfd
    return fd


def test_rmtree_cleans_000_and_exec_only_dirs(tmp_path):
    t = tmp_path / "t"
    (t / "a" / "b").mkdir(parents=True)
    (t / "a" / "b" / "f").write_text("x")
    (t / "z").mkdir()
    (t / "e").mkdir()
    (t / "e" / "g").write_text("y")
    os.chmod(t / "a" / "b", 0)
    os.chmod(t / "a", 0)
    os.chmod(t / "z", 0)
    os.chmod(t / "e", 0o100)
    os.chmod(t, 0)
    assert verifier._rmtree(t) == 0
    assert not os.path.lexists(t)


def test_rmtree_cleans_trees_deeper_than_any_recursion_limit(tmp_path):
    t = tmp_path / "t"
    t.mkdir()
    fd = _deep(t, 2500)
    with open(os.open("f", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd), "w") as f:
        f.write("x")
    os.close(fd)
    assert verifier._rmtree(t) == 0
    assert not os.path.lexists(t)


def test_rmtree_deep_hostile_tree_with_symlinks_leaves_outside_untouched(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("k")
    os.chmod(victim, 0o500)
    t = tmp_path / "t"
    t.mkdir()
    fd = _deep(t, 400)
    os.symlink(str(victim), "lnk", dir_fd=fd)
    os.mkdir("locked", dir_fd=fd)
    os.chmod("locked", 0, dir_fd=fd)
    os.close(fd)
    os.symlink(victim, t / "vl")
    try:
        assert verifier._rmtree(t) == 0
        assert not os.path.lexists(t)
        assert (victim / "keep").read_text() == "k" and (os.stat(victim).st_mode & 0o777) == 0o500
    finally:
        os.chmod(victim, 0o700)


def test_rmtree_reports_leftovers_instead_of_claiming_success(tmp_path, monkeypatch):
    t = tmp_path / "t"
    (t / "a").mkdir(parents=True)
    (t / "a" / "f").write_text("x")
    monkeypatch.setattr(verifier, "RMTREE_MAX_ENTRIES", 1)
    assert verifier._rmtree(t) > 0
    monkeypatch.setattr(verifier, "RMTREE_MAX_ENTRIES", 10)
    assert verifier._rmtree(t) == 0 and not os.path.lexists(t)
