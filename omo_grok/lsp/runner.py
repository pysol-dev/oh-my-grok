"""Run a real language server over a git revision inside the same bwrap sandbox as candidate code (no network,
HOME=/tmp, nothing of OMO_HOME visible) and turn its diagnostics into a verdict (spec/ADAPTIVE.md section 7).

Everything here fails closed: anything that stops us obtaining a *trusted* answer raises ``Unavailable`` and the
caller must treat that as a failed/blocked gate, never as a clean result."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path

from .. import sandbox
from . import delta, profiles
from .client import LspClient, LspError, MethodNotFound

WORK = sandbox.SANDBOX_WORK
CANARY_DIR = "omo_canary"
GIT_TIMEOUT = 120


class Unavailable(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


# --------------------------------------------------------------------------------------------------- git helpers
def _git(repo: Path, *args: str) -> bytes:
    try:
        cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        raise Unavailable("git_failed", f"{type(e).__name__}") from e
    if cp.returncode != 0:
        raise Unavailable("git_failed", cp.stderr.decode("utf-8", "replace")[:200])
    return cp.stdout


def ls_tree(repo: Path, sha: str) -> dict[str, tuple[str, str]]:
    out = _git(repo, "ls-tree", "-r", "-z", "--full-tree", sha)
    tree = {}
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, typ, blob = meta.decode().split()
        tree[path.decode("utf-8", "surrogateescape")] = (mode, blob)
    return tree


def changed_paths(repo: Path, base: str, cand: str) -> list[str]:
    out = _git(repo, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", base, cand)
    return sorted(p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p)


def diff_text(repo: Path, base: str, cand: str, paths: list[str]) -> str:
    if not paths:
        return ""
    return _git(repo, "diff", "--no-ext-diff", "--no-renames", "-U0", base, cand, "--", *paths).decode("utf-8", "replace")


def blob_id(data: bytes, like: str) -> str:
    h = hashlib.sha256() if len(like) == 64 else hashlib.sha1()  # noqa: S324 - git object ids
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def stage_revision(repo: Path, sha: str, dest: Path) -> None:
    dest.mkdir(parents=True)
    proc = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", sha], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            tf.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as e:
        proc.kill()
        raise Unavailable("stage_failed", f"{type(e).__name__}: {e}") from e
    finally:
        err = proc.stderr.read() if proc.stderr else b""
        proc.wait()
        for f in (proc.stdout, proc.stderr):
            if f:
                f.close()
    if proc.returncode != 0:
        raise Unavailable("stage_failed", err.decode("utf-8", "replace")[:200])


def overlay_base_config(repo: Path, base: str, cand_tree: dict, base_tree: dict, work: Path, config_files: list[str]):
    """Engine-owned analysis configuration is taken from the BASE revision: a candidate cannot loosen the checker."""
    names = set(config_files)
    for p in cand_tree:
        if p.rsplit("/", 1)[-1] in names:
            (work / p).unlink(missing_ok=True)
    for p, (mode, blob) in base_tree.items():
        if p.rsplit("/", 1)[-1] in names and mode in ("100644", "100755"):
            data = _git(repo, "cat-file", "blob", blob)
            (work / p).parent.mkdir(parents=True, exist_ok=True)
            (work / p).write_bytes(data)


# --------------------------------------------------------------------------------------------------- selection
def select_files(tree: dict, profile: dict, changed: list[str] | None) -> dict:
    exts = {e.lower() for e in profile["extensions"]}
    elig, skipped = [], []
    for p, (mode, _blob) in sorted(tree.items()):
        if profiles.ext_of(p) not in exts or mode not in ("100644", "100755"):
            continue
        if delta.match_any(p, profile["exclude"]):
            continue
        elig.append(p)
    if len(elig) <= profile["max_files"] or changed is None:
        files, scope = elig, "all"
    else:
        ch = set(changed)
        files, scope = [p for p in elig if p in ch], "changed_only"
    return {"files": files, "scope": scope, "eligible": len(elig), "skipped": skipped}


# --------------------------------------------------------------------------------------------------- tools
@lru_cache(maxsize=64)
def _file_sha(path: str, mtime_ns: int, size: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tool_identity(profile: dict) -> list[dict]:
    out = []
    for a in profile["argv"]:
        if a.startswith("/") and os.path.isfile(a):
            st = os.stat(a)
            out.append({"path": a, "sha256": _file_sha(a, st.st_mtime_ns, st.st_size), "size": st.st_size})
    return out


def check_tools(profile: dict) -> None:
    for a in profile["argv"]:
        if a.startswith("/") and not os.path.isfile(a):
            raise Unavailable("missing_tool", f"{a} does not exist; run scripts/bootstrap.sh")
    for d in profile["tool_dirs"]:
        if not os.path.isdir(d):
            raise Unavailable("missing_tool", f"tool directory {d} does not exist; run scripts/bootstrap.sh")
    if not os.access(profile["argv"][0], os.X_OK):
        raise Unavailable("missing_tool", f"{profile['argv'][0]} is not executable")


def sandbox_argv(sbx: sandbox.Probe, profile: dict, work: Path) -> list[str]:
    path = ":".join([*profile["path_dirs"], "/usr/bin", "/bin"])
    out = [sbx.bwrap or "bwrap", *sandbox.base_args(), "--bind", str(work), WORK]
    for d in profile["tool_dirs"]:
        out += ["--ro-bind", d, d]
    out += ["--chdir", WORK, "--setenv", "HOME", "/tmp", "--setenv", "PATH", path, "--setenv", "LANG", "C.UTF-8"]
    for k, v in sorted(profile["env"].items()):
        out += ["--setenv", k, str(v)]
    return [*out, "--", *profile["argv"]]


def _lookup(settings: dict, section):
    cur = settings
    for part in str(section or "").split("."):
        if not part:
            return settings or None
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _handler(profile: dict):
    settings = profile["settings"]

    def handle(method, params):
        if method == "workspace/configuration":
            return [_lookup(settings, (it or {}).get("section")) for it in (params or {}).get("items", [])]
        if method in ("client/registerCapability", "client/unregisterCapability", "window/workDoneProgress/create",
                      "window/showMessageRequest", "workspace/diagnostic/refresh"):
            return None
        if method == "workspace/workspaceFolders":
            return [{"uri": f"file://{WORK}", "name": "work"}]
        raise MethodNotFound(method)
    return handle


CLIENT_CAPS = {
    "textDocument": {"synchronization": {"dynamicRegistration": False},
                     "publishDiagnostics": {"versionSupport": True},
                     "diagnostic": {"dynamicRegistration": False, "relatedDocumentSupport": False}},
    "workspace": {"configuration": True, "workspaceFolders": True, "didChangeConfiguration": {"dynamicRegistration": False}},
    "general": {"positionEncodings": ["utf-16"]},
}


class Session:
    def __init__(self, sbx, profile: dict, work: Path, deadline: float):
        self.profile, self.work, self.deadline = profile, work, deadline
        self.client = LspClient(sandbox_argv(sbx, profile, work), env={"PATH": "/usr/bin:/bin"},
                                preexec=sandbox.preexec_with(profile["address_space_mb"] << 20
                                                             if profile["address_space_mb"] else None),
                                request_handler=_handler(profile))
        self.mode = None
        self.server = {}
        self.versions: dict[str, int] = {}

    def _left(self, cap: float) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise LspError("overall language-server deadline exceeded", "timeout")
        return min(cap, left)

    def __enter__(self):
        c = self.client
        c.start()
        try:
            res = c.request("initialize", {
                "processId": None, "rootUri": f"file://{WORK}", "clientInfo": {"name": "omo-grok"},
                "workspaceFolders": [{"uri": f"file://{WORK}", "name": "work"}], "capabilities": CLIENT_CAPS,
                "initializationOptions": self.profile["init_options"] or None,
            }, timeout=self._left(self.profile["startup_timeout_seconds"]))
            caps = (res or {}).get("capabilities") or {}
            self.server = {"name": ((res or {}).get("serverInfo") or {}).get("name"),
                           "version": ((res or {}).get("serverInfo") or {}).get("version")}
            has_pull = bool(caps.get("diagnosticProvider"))
            want = self.profile["mode"]
            if want == "pull" and not has_pull:
                raise Unavailable("pull_unsupported", "profile requires pull diagnostics but the server has none")
            self.mode = "pull" if (want == "pull" or (want == "auto" and has_pull)) else "push"
            c.notify("initialized", {})
            c.notify("workspace/didChangeConfiguration", {"settings": self.profile["settings"]})
        except BaseException:
            c.close()
            raise
        return self

    def __exit__(self, *exc):
        self.client.close()

    def diagnostics(self, rel: str, text: str) -> list[dict]:
        c, prof = self.client, self.profile
        uri = f"file://{WORK}/{rel}"
        lang = prof["language_ids"].get(profiles.ext_of(rel), prof["language"])
        version = self.versions[uri] = self.versions.get(uri, 0) + 1
        c.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": lang, "version": version,
                                                           "text": text}})
        timeout = self._left(prof["file_timeout_seconds"])
        if self.mode == "pull":
            res = c.request("textDocument/diagnostic", {"textDocument": {"uri": uri}}, timeout=timeout)
            if not isinstance(res, dict) or not isinstance(res.get("items"), list) or res.get("kind") == "unchanged":
                raise Unavailable("bad_pull_report", f"unusable diagnostic report for {rel}")
            raw = res["items"]
        else:
            raw = c.wait_push(uri, version, prof["quiet_seconds"], timeout)
        c.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        return [delta.normalize(rel, d) for d in raw if isinstance(d, dict)]


# --------------------------------------------------------------------------------------------------- canary
def run_canary(sess: Session, work: Path) -> dict:
    """Proof, per run, that the server really type/lint-checks here: the known-bad file must yield one of the
    expected codes and the known-good file must yield no error. Without it 'no diagnostics' means nothing."""
    cn = sess.profile["canary"]
    d = work / CANARY_DIR
    d.mkdir(exist_ok=True)
    out = {}
    for kind in ("bad", "good"):
        (d / cn[kind]["path"]).write_text(cn[kind]["text"])
    try:
        bad = sess.diagnostics(f"{CANARY_DIR}/{cn['bad']['path']}", cn["bad"]["text"])
        good = sess.diagnostics(f"{CANARY_DIR}/{cn['good']['path']}", cn["good"]["text"])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    want = {str(c) for c in cn["bad"]["expect_codes"]}
    seen = sorted({x["code"] for x in bad})
    out["bad_codes"], out["good_errors"] = seen, [x["code"] for x in good if x["severity"] == "error"]
    if not want & set(seen):
        raise Unavailable("canary_failed", f"known-bad file produced {seen}, expected one of {sorted(want)}")
    if out["good_errors"]:
        raise Unavailable("canary_failed", f"known-good file produced errors {out['good_errors']}")
    out["ok"] = True
    return out


# --------------------------------------------------------------------------------------------------- analysis
def analyze(repo: Path, sha: str, name: str, profile: dict, files: list[str], sbx: sandbox.Probe, scratch: Path,
            overlay: dict | None = None, deadline_s: float = 900) -> dict:
    """Diagnostics of ``files`` at revision ``sha``. ``overlay`` = {"base": sha} replaces engine-owned config
    files with the base revision's. Raises Unavailable if no trusted answer can be produced."""
    if not sbx.ok:
        raise Unavailable("sandbox_unavailable", sbx.detail)
    check_tools(profile)
    run_dir = Path(tempfile.mkdtemp(prefix=f"lsp-{uuid.uuid4().hex[:8]}-", dir=scratch))
    try:
        work = run_dir / "work"
        tree = ls_tree(repo, sha)
        if tree:
            stage_revision(repo, sha, work)
        else:
            work.mkdir(parents=True)
        if overlay:
            overlay_base_config(repo, overlay["base"], tree, ls_tree(repo, overlay["base"]), work,
                                profile["config_files"])
        deadline = time.monotonic() + deadline_s
        diags, unanalysed = [], []
        try:
            with Session(sbx, profile, work, deadline) as sess:
                canary = run_canary(sess, work)
                for rel in files:
                    path = work / rel
                    try:
                        data = path.read_bytes()
                    except OSError:
                        unanalysed.append({"path": rel, "reason": "unreadable"})
                        continue
                    if len(data) > profile["max_file_bytes"]:
                        unanalysed.append({"path": rel, "reason": "too_large"})
                        continue
                    if blob_id(data, tree[rel][1]) != tree[rel][1]:
                        raise Unavailable("blob_mismatch", f"staged {rel} differs from the revision's blob")
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        unanalysed.append({"path": rel, "reason": "not_utf8"})
                        continue
                    diags += sess.diagnostics(rel, text)
                server, mode = sess.server, sess.mode
        except LspError as e:
            raise Unavailable(f"lsp_{e.kind}", str(e)[:400]) from e
        except OSError as e:
            raise Unavailable("lsp_io", f"{type(e).__name__}: {e}") from e
        return {"profile": name, "revision": sha, "mode": mode, "server": server, "canary": canary,
                "files_analysed": len(files) - len(unanalysed), "unanalysed": unanalysed, "diagnostics": diags,
                "identity": tool_identity(profile), "overlay_base": (overlay or {}).get("base")}
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def profile_hash(profile: dict) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:16]


def _cached_base(cache_dir: Path | None, key: str):
    if cache_dir is None:
        return None
    p = cache_dir / f"{key}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _config_hits(cfg: dict, changed: list[str], only_profiles: list[str] | None) -> list[str]:
    """Protected checker-config paths in the diff, whether or not a source file selected a profile."""
    hits = []
    for name, prof in profiles.enabled_profiles(cfg).items():
        if only_profiles is not None and name not in only_profiles:
            continue
        for path in delta.config_violations(changed, prof["config_files"]):
            if path not in hits:
                hits.append(path)
    return hits


def evaluate(repo: Path, base: str, cand: str, cfg: dict, sbx: sandbox.Probe, scratch: Path,
             cache_dir: Path | None = None, only_profiles: list[str] | None = None) -> dict:
    """The delta verdict for base -> cand. status: clean | findings | unavailable | not_applicable."""
    scratch.mkdir(parents=True, exist_ok=True)
    changed = changed_paths(repo, base, cand)
    apply, uncovered = profiles.profiles_for(cfg, changed)
    if only_profiles is not None:
        apply = {n: p for n, p in apply.items() if n in only_profiles}
    res = {"base": base, "candidate": cand, "changed": len(changed), "profiles": {}, "uncovered": uncovered,
           "new": [], "config_violations": [], "suppressions": [], "reasons": []}
    if not apply and not uncovered:
        hits = _config_hits(cfg, changed, only_profiles)
        if hits:
            res["config_violations"] = hits
            res["status"] = "findings"
        else:
            res["status"] = "not_applicable"
        return res
    if uncovered:
        res["reasons"].append("diagnostics_unavailable: no enabled language-server profile for "
                              + ", ".join(sorted(uncovered)))
    cand_tree, base_tree = ls_tree(repo, cand), ls_tree(repo, base)
    for pname, prof in apply.items():
        entry = res["profiles"][pname] = {"status": "pending"}
        try:
            sel = select_files(cand_tree, prof, changed)
            entry["scope"], entry["files"] = sel["scope"], len(sel["files"])
            base_files = [p for p in sel["files"] if p in base_tree]
            ck = f"{base}-{profile_hash(prof)}-{hashlib.sha256(chr(0).join(base_files).encode()).hexdigest()[:12]}"
            base_run = _cached_base(cache_dir, ck)
            ident = tool_identity(prof)
            if base_run is None or base_run.get("identity") != ident:
                base_run = analyze(repo, base, pname, prof, base_files, sbx, scratch)
                if cache_dir is not None:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    (cache_dir / f"{ck}.json").write_text(json.dumps(base_run))
            cand_run = analyze(repo, cand, pname, prof, sel["files"], sbx, scratch, overlay={"base": base})
            new = delta.new_diagnostics(base_run["diagnostics"], cand_run["diagnostics"], prof["fail_on"])
            cfgv = delta.config_violations(changed, prof["config_files"])
            sup = delta.suppressions(diff_text(repo, base, cand, [p for p in changed if profiles.ext_of(p) in
                                                                  {e.lower() for e in prof["extensions"]}]),
                                     prof["suppression_patterns"])
            entry.update(status="ok", mode=cand_run["mode"], server=cand_run["server"], canary=cand_run["canary"],
                         identity=cand_run["identity"], base_diagnostics=len(base_run["diagnostics"]),
                         candidate_diagnostics=len(cand_run["diagnostics"]), new=new,
                         unanalysed=cand_run["unanalysed"], config_violations=cfgv, suppressions=sup)
            res["new"] += new
            res["config_violations"] += [p for p in cfgv if p not in res["config_violations"]]
            res["suppressions"] += sup
            if cand_run["unanalysed"]:
                res["reasons"].append(f"{pname}: {len(cand_run['unanalysed'])} file(s) could not be analysed")
        except Unavailable as e:
            entry.update(status="unavailable", reason=e.reason, detail=e.detail[:400])
            res["reasons"].append(f"{pname}: {e}")
    for path in _config_hits(cfg, changed, only_profiles):
        if path not in res["config_violations"]:
            res["config_violations"].append(path)
    unavailable = bool(uncovered) or any(e["status"] != "ok" for e in res["profiles"].values()) or any(
        e.get("unanalysed") for e in res["profiles"].values())
    if unavailable:
        res["status"] = "unavailable"
    elif res["new"] or res["config_violations"] or res["suppressions"]:
        res["status"] = "findings"
    else:
        res["status"] = "clean"
    return res


def canary_all(cfg: dict, sbx: sandbox.Probe, scratch: Path, only: list[str] | None = None) -> dict:
    """Run only the canary of each enabled profile against a throwaway one-commit repository. This is what
    `omoctl lsp canary` reports: whether each server can really find a planted defect on THIS machine."""
    scratch.mkdir(parents=True, exist_ok=True)
    repo = Path(tempfile.mkdtemp(prefix="canary-repo-", dir=scratch))
    out = {}
    try:
        env = {"GIT_AUTHOR_NAME": "omo", "GIT_AUTHOR_EMAIL": "omo@invalid", "GIT_COMMITTER_NAME": "omo",
               "GIT_COMMITTER_EMAIL": "omo@invalid", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        for cmd in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "canary"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True, env=env)
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                             text=True).stdout.strip()
        for name, prof in profiles.enabled_profiles(cfg).items():
            if only is not None and name not in only:
                continue
            try:
                r = analyze(repo, sha, name, prof, [], sbx, scratch, deadline_s=300)
                out[name] = {"status": "ok", "mode": r["mode"], "server": r["server"], "canary": r["canary"],
                             "identity": r["identity"]}
            except Unavailable as e:
                out[name] = {"status": "unavailable", "reason": e.reason, "detail": e.detail[:300]}
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    return out


def tool_status(cfg: dict) -> dict:
    """Static presence check (no server is started): which profile files/dirs are missing."""
    out = {}
    for name, prof in profiles.enabled_profiles(cfg).items():
        missing = [a for a in prof["argv"] if a.startswith("/") and not os.path.isfile(a)]
        missing += [d for d in prof["tool_dirs"] if not os.path.isdir(d)]
        out[name] = {"present": not missing, "missing": missing, "languages": sorted(prof["extensions"])}
    return out
