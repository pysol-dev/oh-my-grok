"""Pasted webhook URL/key sanitizing (bracketed paste, CR/LF, quotes, whitespace), no-echo diagnostics, re-prompt."""
import json
import os
from pathlib import Path

import pytest

from omo_grok import cli
from omo_grok.hooks import sanitize_input, url_diagnostic, validate_url
from test_cli import _pty_run

URL = "https://example.com/hook"


@pytest.mark.parametrize("raw,removed", [
    (URL, False),
    ("\x1b[200~" + URL + "\x1b[201~", True),
    ("[200~" + URL + "[201~", True),
    ("\x1b[200~" + URL + "\x1b[201~\r\n", True),
    (URL + "\r\n", True),
    (URL + "\r", True),
    ("  " + URL + "  ", False),
    ("\t" + URL + "\n", True),
    ('"' + URL + '"', False),
    ("'" + URL + "'", False),
    ("<" + URL + ">", False),
    (' "\x1b[200~' + URL + '\x1b[201~" \r\n', True),
    (URL[:8] + "\x00" + URL[8:], True),
    (URL + "\u200b", True),
])
def test_sanitize_recovers_the_url(raw, removed):
    clean, rem = sanitize_input(raw)
    assert clean == URL and rem is removed
    validate_url(clean)


def test_sanitize_strips_only_one_matching_pair_and_keeps_inner_text():
    assert sanitize_input("\"'x'\"")[0] == "'x'"
    assert sanitize_input('"x')[0] == '"x' and sanitize_input("<x")[0] == "<x"
    assert sanitize_input("a b")[0] == "a b"


@pytest.mark.parametrize("raw", ["", "   ", "\r\n", "\x1b[200~\x1b[201~", '""', "<>", None])
def test_sanitize_empty_inputs(raw):
    assert sanitize_input(raw)[0] == ""


def test_key_sanitized_the_same_way():
    key = "whk_abcdef0123456789"
    assert sanitize_input("\x1b[200~" + key + "\x1b[201~\r\n")[0] == key
    assert cli._key_problem(key, False) is None
    assert cli._key_problem("short", False) is not None


SECRETISH = "https://secret-host.example/very-secret-path?token=XYZ"


@pytest.mark.parametrize("value,scheme", [
    ("http://secret-host.example/very-secret-path", "http"),
    ("ftp://secret-host.example/very-secret-path", "other"),
    ("javascript:secret-host-very-secret-path", "other"),
    ("secret-host.example/very-secret-path", "empty"),
    ("", "empty"),
])
def test_diagnostic_never_echoes_the_value(value, scheme):
    d = url_diagnostic(value, True)
    assert f"scheme={scheme}" in d and f"length={len(value)}" in d and f"empty={not value}" in d
    assert "non_printable_or_paste_markers_removed=True" in d
    for frag in ("secret", "very", "example", "token", "ftp", "javascript"):
        assert frag not in d
    p = cli._url_problem(value, True)
    assert p and ("must be https" in p or "no host" in p)
    for frag in ("secret", "very", "token"):
        assert frag not in p


def test_prompt_reprompts_three_times_then_gives_up(monkeypatch, capsys):
    answers = iter(["ftp://secret-host/x", "\x1b[200~ \x1b[201~", "http://secret-host/x", URL])
    calls = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda p: calls.append(p) or next(answers))
    assert cli._prompt_hidden("url: ", cli._url_problem) is None
    assert len(calls) == cli.HOOK_PROMPT_ATTEMPTS == 3
    err = capsys.readouterr().err
    assert err.count("invalid input:") == 3 and "2 left" in err and "1 left" in err and "giving up" in err
    assert "secret-host" not in err


def test_prompt_accepts_a_bracketed_paste_on_the_second_try(monkeypatch, capsys):
    answers = iter(["oops", "\x1b[200~" + URL + "\x1b[201~"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda p: next(answers))
    assert cli._prompt_hidden("url: ", cli._url_problem) == URL
    assert "oops" not in capsys.readouterr().err


def test_hook_set_under_a_pty_with_bracketed_paste(omo_home):
    env = dict(os.environ)
    key = "whk_DUMMYKEY_0123456789"
    code, out = _pty_run(["hook", "set", "metis"],
                         ["\x1b[200~" + URL + "\x1b[201~", "\x1b[200~" + key + "\x1b[201~"], env)
    assert code == 0, out
    assert key not in out and "example.com" not in out and "stored" in out
    data = json.loads((Path(omo_home) / "state" / "secrets" / "hooks.json").read_text())
    assert data["metis"]["url"] == URL and data["metis"]["key"] == key


def test_hook_set_under_a_pty_reprompts_after_a_bad_paste(omo_home):
    env = dict(os.environ)
    key = "whk_DUMMYKEY_0123456789"
    code, out = _pty_run(["hook", "set", "momus"], ["ftp://secret-host/x", URL, key], env)
    assert code == 0, out
    assert "invalid input" in out and "scheme=other" in out and "secret-host" not in out and key not in out
    code, out = _pty_run(["hook", "set", "momus"], ["x", "y", "z"], env)
    assert code == 2 and "giving up" in out
