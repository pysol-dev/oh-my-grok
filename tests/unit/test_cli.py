"""§10 item 1: CLI starts, has all documented subcommands, no missing imports; argument validation; TTY gates."""
import importlib
import json
import os
import pkgutil
import pty
import select
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OMOCTL = str(ROOT / "omoctl")
DOCUMENTED = [["doctor"], ["migrate"], ["status"], ["submit"], ["plan", "show"], ["approve"], ["deny"], ["cancel"],
              ["reconcile"], ["hook", "set"], ["hook", "check"], ["hook", "list"], ["test"], ["export"],
              ["resume"], ["backup"], ["restore"], ["roles", "list"], ["roles", "bind"], ["roles", "import"]]


def run(args, env=None, input_=None):
    return subprocess.run([OMOCTL, *args], capture_output=True, text=True, env=env, input=input_, timeout=120)


def test_every_module_imports():
    import omo_grok
    for m in pkgutil.iter_modules(omo_grok.__path__):
        importlib.import_module(f"omo_grok.{m.name}")


def test_top_level_help_lists_all_subcommands():
    r = run(["--help"])
    assert r.returncode == 0
    for c in ["doctor", "migrate", "status", "submit", "plan", "approve", "deny", "cancel", "reconcile", "hook",
              "test", "export"]:
        assert c in r.stdout


@pytest.mark.parametrize("cmd", DOCUMENTED, ids=lambda c: " ".join(c))
def test_each_subcommand_help(cmd):
    r = run([*cmd, "--help"])
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("args", [["approve", "J"], ["submit"], ["hook", "set", "nobody"], ["bogus"],
                                  ["deny", "J"], ["export", "J"]])
def test_argument_validation_exit_2(args):
    assert run(args).returncode == 2


def test_doctor_and_migrate_in_isolated_home(omo_home):
    env = dict(os.environ)
    r = run(["migrate", "--dry-run", "--json"], env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["pending"] == [1, 2]
    assert run(["migrate"], env=env).returncode == 0
    r = run(["doctor", "--json"], env=env)
    d = json.loads(r.stdout)
    assert r.returncode == 0, [c for c in d["checks"] if not c["ok"]]
    assert "NOT OPERATIONAL" in d["live_status"]


def test_invalid_ids_give_stable_codes(omo_home):
    env = dict(os.environ)
    r = run(["status", "not-a-uuid", "--json"], env=env)
    assert r.returncode == 2 and json.loads(r.stdout)["code"] == "invalid_input"
    r = run(["status", "00000000-0000-4000-8000-000000000000"], env=env)
    assert r.returncode == 3


def test_live_without_fixture_only_refused(omo_home):
    assert run(["test", "live"], env=dict(os.environ)).returncode == 2


def test_live_fixture_only_reports_not_run_without_prereqs(omo_home):
    r = run(["test", "live", "--fixture-only", "--json"], env=dict(os.environ))
    assert r.returncode == 9
    assert json.loads(r.stdout)["result"] == "NOT RUN"


def _pty_run(args, answers, env):
    """Run omoctl under a real pseudo-terminal, typing answers when prompts appear."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(OMOCTL, [OMOCTL, *args], env)
    out = b""
    answers = list(answers)
    deadline = time.time() + 60
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if answers and (out.rstrip().endswith(b":")):
                os.write(fd, answers.pop(0).encode() + b"\n")
                out += b"<ANSWERED>"
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def test_approve_requires_tty_and_records_soft_receipt(harness, omo_home):
    job = harness.submit()["job_id"]
    ph = harness.to_approval(job)
    env = dict(os.environ)
    # piped stdin (no TTY) -> refused, exit 7, nothing recorded
    r = run(["approve", job, "--plan-hash", ph], env=env, input_=ph[:12] + "\n")
    assert r.returncode == 7
    assert harness.e._one("SELECT COUNT(*) c FROM approvals")["c"] == 0
    # wrong confirmation under a TTY -> refused
    code, out = _pty_run(["approve", job, "--plan-hash", ph, "--no-edit"], ["nope"], env)
    assert code == 7
    # correct confirmation under a TTY -> soft approval recorded
    code, out = _pty_run(["approve", job, "--plan-hash", ph, "--no-edit"], [ph[:12]], env)
    assert code == 0, out
    ap = harness.e._one("SELECT * FROM approvals")
    assert ap["mode"] == "soft" and ap["plan_hash"] == ph and "tty" in ap["evidence"].lower()
    assert "SOFT" in out


def test_hook_set_uses_hidden_prompt_and_never_echoes(omo_home):
    env = dict(os.environ)
    secret = "whk_TOPSECRET_abcdef0123456789"
    code, out = _pty_run(["hook", "set", "explorer"], ["https://hooks.invalid/x", secret], env)
    assert code == 0, out
    assert secret not in out
    hooks = Path(omo_home) / "state" / "secrets" / "hooks.json"
    assert oct(hooks.stat().st_mode & 0o777) == "0o600"
    assert oct(hooks.parent.stat().st_mode & 0o777) == "0o700"
    r = run(["hook", "list", "--json"], env=env)
    assert json.loads(r.stdout)["explorer"] is True and secret not in r.stdout and "hooks.invalid" not in r.stdout
    r = run(["hook", "check", "explorer", "--json"], env=env)
    assert r.returncode == 0 and secret not in r.stdout and "hooks.invalid" not in r.stdout
