"""`omoctl hook serve`: local-only one-time-token form. Token/Host/Origin gates, exact storage, no echo/log."""
import http.client
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from omo_grok import hook_form
from omo_grok.paths import get_paths

ROOT = Path(__file__).resolve().parents[2]
URL = "https://hooks.example.test/routine/abc123"
KEY = "whk_FORMSECRET_0123456789"


def _engine():
    from omo_grok.engine import Engine
    return Engine()


@pytest.fixture
def srv(omo_home, tmp_path):
    s = hook_form.HookFormServer(get_paths(), _engine, idle_seconds=60, url_file=tmp_path / "url")
    t = threading.Thread(target=s.serve, daemon=True)
    t.start()
    for _ in range(50):
        if (tmp_path / "url").exists():
            break
        time.sleep(0.05)
    yield s
    s.finished.set()
    t.join(timeout=10)


def req(s, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{s.port}"}
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    c.putrequest(method, path, skip_host=True)
    for k, v in h.items():
        c.putheader(k, v)
    data = body.encode() if body is not None else b""
    if body is not None:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data)
    r = c.getresponse()
    out = (r.status, r.read().decode(), dict(r.getheaders()))
    c.close()
    return out


def test_url_file_is_0600_and_holds_the_url(srv, tmp_path):
    f = tmp_path / "url"
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert f.read_text().strip() == srv.url and srv.url.startswith("http://127.0.0.1:")
    assert len(srv.token) >= 40


@pytest.mark.parametrize("path", ["/", "/setup", "/setup/", "/setup/wrong", "/favicon.ico", "/setup/{t}x",
                                  "/setup/{t}?a=1", "/setup/{t}/", "/SETUP/{t}"])
def test_bad_token_or_path_is_404(srv, path):
    st, body, _ = req(srv, "GET", path.format(t=srv.token))
    assert st == 404 and srv.token not in body
    st, _, _ = req(srv, "POST", path.format(t=srv.token), urlencode({"url_explorer": URL, "key_explorer": KEY}))
    assert st == 404
    assert not (Path(os.environ["OMO_HOME"]) / "state" / "secrets" / "hooks.json").exists()


@pytest.mark.parametrize("hdrs", [{"Host": "evil.example"}, {"Host": "localhost:{p}"}, {"Host": "127.0.0.1"},
                                  {"Host": "evil.example:{p}"}, {"Origin": "http://evil.example"},
                                  {"Origin": "http://localhost:{p}"}, {"Origin": "null"}])
def test_foreign_host_or_origin_is_rejected(srv, hdrs):
    h = {k: v.format(p=srv.port) for k, v in hdrs.items()}
    host = h.pop("Host", None)
    path = f"/setup/{srv.token}"
    st, _, _ = req(srv, "GET", path, headers=h, host=host)
    assert st == 403
    st, _, _ = req(srv, "POST", path, urlencode({"url_explorer": URL, "key_explorer": KEY}), headers=h, host=host)
    assert st == 403
    assert not (Path(os.environ["OMO_HOME"]) / "state" / "secrets" / "hooks.json").exists()


def test_get_renders_all_roles_in_order_with_password_fields(srv):
    st, body, hdr = req(srv, "GET", f"/setup/{srv.token}")
    assert st == 200 and hdr["Cache-Control"] == "no-store" and "default-src 'none'" in hdr["Content-Security-Policy"]
    pos = [body.index(f"name='url_{r}'") for r in hook_form.FORM_ROLES]
    assert pos == sorted(pos)
    assert body.count("type='password'") == 2 * len(hook_form.FORM_ROLES)
    assert body.count("autocomplete='off' spellcheck='false'") == 2 * len(hook_form.FORM_ROLES)
    for role, bot, routine in [("metis", "Metis", "omo-bus-metis"), ("qa-executor", "Metis", "omo-bus-qa-executor"),
                               ("code-reviewer", "Momus", "omo-bus-code-reviewer")]:
        assert f"{role} &mdash; bot {bot}, routine {routine}" in body
        assert f"Open {bot}'s info pane &gt; Routines &gt; {routine}" in body
    assert "Configured already: <b>no</b>" in body and "hephaestus" not in body


def test_valid_post_stores_exactly_skips_blanks_and_never_echoes(srv, capfd):
    pasted_url = "\x1b[200~" + URL + "\x1b[201~\r\n"
    form = {"url_metis": pasted_url, "key_metis": ' "' + KEY + '" ', "url_explorer": "", "key_explorer": "",
            "url_momus": "ftp://hooks.example.test/x", "key_momus": KEY + "M", "action": "save"}
    st, body, _ = req(srv, "POST", f"/setup/{srv.token}", urlencode(form),
                      headers={"Origin": f"http://127.0.0.1:{srv.port}"})
    assert st == 200
    home = Path(os.environ["OMO_HOME"])
    data = json.loads((home / "state" / "secrets" / "hooks.json").read_text())
    assert data["metis"]["url"] == URL and data["metis"]["key"] == KEY
    assert set(data) == {"metis"}
    assert "<b>metis</b>: configured" in body and "<b>explorer</b>: skipped" in body
    assert "<b>momus</b>: error" in body and "scheme=other" in body
    assert "Configured already: <b>yes</b>" in body
    out = capfd.readouterr()
    for v in (URL, KEY, "hooks.example.test", "FORMSECRET", "abc123"):
        assert v not in body and v not in out.out and v not in out.err
    # nothing outside hooks.json (db, events, logs) contains the values
    for p in home.rglob("*"):
        if p.is_file() and p.name != "hooks.json":
            raw = p.read_bytes()
            assert KEY.encode() not in raw and b"hooks.example.test" not in raw, p
    # resubmitting fixes a failure
    st, body, _ = req(srv, "POST", f"/setup/{srv.token}",
                      urlencode({"url_momus": URL + "/m", "key_momus": KEY + "M"}))
    assert "<b>momus</b>: configured" in body and "<b>metis</b>: skipped" in body
    data = json.loads((home / "state" / "secrets" / "hooks.json").read_text())
    assert data["momus"]["url"] == URL + "/m" and data["metis"]["key"] == KEY


def test_one_blank_field_is_an_error_not_a_store(srv):
    st, body, _ = req(srv, "POST", f"/setup/{srv.token}", urlencode({"url_librarian": URL, "key_librarian": ""}))
    assert "<b>librarian</b>: error" in body and URL not in body
    assert not (Path(os.environ["OMO_HOME"]) / "state" / "secrets" / "hooks.json").exists()


def test_bad_content_type_or_oversize_is_400(srv):
    p = f"/setup/{srv.token}"
    st, _, _ = req(srv, "POST", p, "x" * 10, headers={"Content-Type": "text/plain"})
    assert st == 400
    st, _, _ = req(srv, "POST", p, "a=" + "x" * (hook_form.MAX_BODY + 10))
    assert st == 400


def test_finish_stops_the_server_and_removes_the_url_file(omo_home, tmp_path):
    s = hook_form.HookFormServer(get_paths(), _engine, idle_seconds=60, url_file=tmp_path / "u")
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("r", s.serve()), daemon=True)
    t.start()
    time.sleep(0.3)
    st, body, _ = req(s, "POST", f"/setup/{s.token}", "action=finish")
    assert st == 200 and "stopped" in body
    t.join(timeout=10)
    assert not t.is_alive() and res["r"] == "finished" and not (tmp_path / "u").exists()


def test_idle_timeout_stops_the_server(omo_home, tmp_path):
    s = hook_form.HookFormServer(get_paths(), _engine, idle_seconds=1.5, url_file=tmp_path / "u")
    t0 = time.monotonic()
    assert s.serve() == "idle timeout" and time.monotonic() - t0 < 15


def test_cli_serve_prints_url_and_stops_on_finish(omo_home, tmp_path):
    uf = tmp_path / "cli_url"
    p = subprocess.Popen([str(ROOT / "omoctl"), "hook", "serve", "--url-file", str(uf), "--idle-minutes", "1"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        line = p.stdout.readline()
        url = line.strip().split(": ", 1)[1]
        assert url.startswith("http://127.0.0.1:") and uf.read_text().strip() == url
        port = int(url.split(":")[2].split("/")[0])
        path = url.split(str(port), 1)[1]
        fake = type("S", (), {"port": port})()
        assert req(fake, "GET", path)[0] == 200 and req(fake, "GET", "/nope")[0] == 404
        assert req(fake, "POST", path, "action=finish")[0] == 200
        out, err = p.communicate(timeout=20)
        assert p.returncode == 0 and "stopped (finished)" in out and "GET" not in err and path not in err
    finally:
        if p.poll() is None:
            p.kill()
