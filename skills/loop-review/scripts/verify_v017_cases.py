#!/usr/bin/env python3
"""Isolated unit and property tests for devloop v0.17 upgrades (K1 ports, K2 stale handoff, K3 sampling_note)."""

from __future__ import annotations

import json, os, pathlib, random, shutil, socket, subprocess, sys

SCRIPTS = pathlib.Path(__file__).resolve().parent
DEVLOOP_OPS = SCRIPTS / "devloop_ops.py"
REAPER = SCRIPTS / "reap_and_run.py"


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _run(cmd: list[str], cwd: pathlib.Path, stdin_text: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), input=stdin_text, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False, env=env)


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd)


def _ops(*args: str, cwd: pathlib.Path, stdin_text: str | None = None) -> tuple[int, dict]:
    proc = _run([sys.executable, "-B", str(DEVLOOP_OPS), *args, "--json"], cwd, stdin_text=stdin_text)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}


def _reap(args: list[str], cwd: pathlib.Path, env: dict) -> subprocess.CompletedProcess:
    return _run([sys.executable, "-B", str(REAPER), *args], cwd, env=env)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _handoff(version: str, branch: str, baseline: str, with_ev: bool = True) -> str:
    ev_row = "| 自测与返工验证证据 | `pytest -> exit 0` |\n" if with_ev else ""
    base_row = f"| 基线 SHA | `{baseline}` |\n" if baseline else ""
    return (
        f"# 交接\n\n| 字段 | 内容 |\n| --- | --- |\n| 版本 / 主题 | `{version} / v017` |\n"
        f"| 分支 | `{branch}` |\n{base_row}| 改动范围 | `app.py` |\n{ev_row}\n"
        "## 文档同步声明\n\n| 文档 | 状态 | 摘要 |\n| --- | --- | --- |\n"
        "| `DECISIONS.md` | `已更新` | 新增条目 |\n| `FEATURE_MAP.md` | `已更新` | 新增功能行 |\n"
    )


def _seed_repo(repo: pathlib.Path, mode: str = "own", branch: str = "feat/v0.1-s") -> tuple[str, str]:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@users.noreply.github.com")
    _write(repo / "app.py", "def run():\n    return 1\n")
    assert (_git(repo, "add", ".").returncode, _git(repo, "commit", "-m", "init").returncode) == (0, 0)
    assert _ops("init", "--cwd", str(repo), "--mode", mode, cwd=repo)[0] == 0
    if mode == "own":
        assert (_git(repo, "add", ".").returncode, _git(repo, "commit", "-m", "init devloop").returncode) == (0, 0)
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if branch != "main":
        assert _git(repo, "checkout", "-b", branch).returncode == 0
    doc_dir = (repo / ".loop") if mode == "external" else repo
    _write(doc_dir / "DECISIONS.md", "# DECISIONS\n\n2026-11-27 | [active] | 选择：A | 放弃：B | 触发：C\n")
    _write(doc_dir / "FEATURE_MAP.md", "# F\n\n| 版本 | 功能 | 入口 | 验证 |\n| --- | --- | --- | --- |\n| v0.1 | X | `x` | `t` |\n")
    _write(repo / "app.py", "def run():\n    return 2\n")
    _write(repo / ".loop" / "handoff.md", _handoff("v0.1", branch, base_sha))
    assert _git(repo, "add", *(["app.py", "DECISIONS.md", "FEATURE_MAP.md", ".loop/handoff.md"] if mode == "own" else ["app.py"])).returncode == 0
    assert _git(repo, "commit", "-m", f"cand on {branch}").returncode == 0
    return base_sha, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _check_k1_resolve_and_prop(work: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    from devloop_ports import is_none_ports, parse_ports, resolve_ports

    cwd_d, fb_d, none_d, bad_d = work / "p-cwd", work / "p-fb", work / "p-none", work / "p-bad"
    _write(cwd_d / ".loop" / "config.json", json.dumps({"ports": "18750-18755"}))
    _write(fb_d / ".loop" / "config.json", json.dumps({"ports": "18760-18765"}))
    _write(none_d / ".loop" / "config.json", json.dumps({"ports": "NONE"}))
    _write(bad_d / ".loop" / "config.json", json.dumps({"ports": "99999"}))
    assert resolve_ports(cwd_d, "18770", fb_d) == ("18770", "cli")
    assert resolve_ports(cwd_d, "", fb_d) == ("18750-18755", "config")
    assert resolve_ports(none_d, "", fb_d) == ("none", "config")
    assert resolve_ports(cwd_d, "99999", fb_d) == ("99999", "cli")
    for junk in ("{bad", json.dumps({"ports": 123}), json.dumps({"ports": "  "}), json.dumps({"ports": None})):
        _write(work / "p-junk" / ".loop" / "config.json", junk)
        assert resolve_ports(work / "p-junk", "", fb_d) == ("18760-18765", "state_config")
    assert resolve_ports(work / "p-none-exist", "", work / "p-fb-missing") == ("18700-18799", "default")

    rng = random.Random(20261127)
    choices = ("", "  ", "none", "NONE", "18750-18752", "99999", 123, None, [1])
    for _ in range(200):
        c_val, f_val, cli_v = rng.choice(choices), rng.choice(choices), rng.choice(("", "  ", "None", "18753", "80000"))
        p_c, p_f = work / "prop-c", work / "prop-f"
        _write(p_c / ".loop" / "config.json", "{bad" if rng.random() < 0.2 else json.dumps({"ports": c_val}))
        _write(p_f / ".loop" / "config.json", json.dumps({"ports": f_val}))
        spec, src = resolve_ports(p_c, cli_v, p_f)
        assert src in ("cli", "config", "state_config", "default")
        if not is_none_ports(spec):
            try:
                assert parse_ports(spec)
            except ValueError:
                assert src in ("cli", "config", "state_config")
    return cwd_d, fb_d, bad_d


def _check_k1_cli_and_worktree(work: pathlib.Path, cwd_d: pathlib.Path, bad_d: pathlib.Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("OMB_REAPER_LOCK_HELD", "OMB_REAPER_ALLOWED_ROOT")}
    env["OMB_REAPER_LOCK_FILE"] = str(work / "v017-reaper.lock")
    assert json.loads(_reap(["--status", "--cwd", str(cwd_d), "--json"], cwd_d, env).stdout)["ports_source"] == "config"
    assert json.loads(_reap(["--status", "--cwd", str(cwd_d), "--ports", "18751", "--json"], cwd_d, env).stdout)["ports_source"] == "cli"
    s_none = _reap(["--status", "--cwd", str(cwd_d), "--ports", "NONE", "--json"], cwd_d, env)
    assert json.loads(s_none.stdout) == {"checked_ports": "none", "ports_source": "cli", "listening": {}}
    assert _reap(["--status", "--cwd", str(bad_d)], bad_d, env).returncode == 2
    assert _reap(["--cwd", str(cwd_d), sys.executable, "-c", "print('ok')"], cwd_d, env).returncode == 0
    for r_bad in (_reap(["--cwd", str(cwd_d), "--ports", "99999", sys.executable, "-c", "print(1)"], cwd_d, env), _reap(["--cwd", str(bad_d), sys.executable, "-c", "print(1)"], bad_d, env)):
        assert (r_bad.returncode, "REAPER_PROBE_ERROR" in r_bad.stderr, "Traceback" in r_bad.stderr) == (2, True, False)
    r_none = _reap(["--cwd", str(cwd_d), "--ports", "none", "--timeout", "1", sys.executable, "-c", "import time; time.sleep(10)"], cwd_d, env)
    assert (r_none.returncode, "REAPER_PORT_CLEANUP" in r_none.stdout) == (124, False)

    ext = work / "k1-ext-main"
    _, cand = _seed_repo(ext, "external", "feat/v0.1-ext")
    port = _free_port()
    cfg_p = ext / ".loop" / "config.json"
    _write(cfg_p, json.dumps({**json.loads(cfg_p.read_text(encoding="utf-8")), "ports": f"{port}-{port}"}))
    wt = work / "k1-ext-wt"
    assert _git(ext, "worktree", "add", "--detach", str(wt), cand).returncode == 0
    assert _ops("review-record", "--cwd", str(wt), "--state-root", str(ext), "--docs-root", str(ext), "--branch", "feat/v0.1-ext", "--verdict", "pass", cwd=wt, stdin_text="# ok\npass.\n")[0] == 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        st_wt = json.loads(_reap(["--status", "--cwd", str(wt), "--state-root", str(ext), "--json"], wt, env).stdout)
        assert (st_wt["ports_source"], str(port) in st_wt["listening"]) == ("state_config", True), st_wt
        c_g, r_g = _ops("release-gate", "--cwd", str(wt), "--state-root", str(ext), "--docs-root", str(ext), "--mode", "external", "--version", "v0.1", "--branch", "feat/v0.1-ext", "--phase", "pre-merge", cwd=wt)
        assert c_g == 1 and any("[PORTS_ACTIVE]" in v for v in r_g["violations"]), r_g
    assert _ops("release-gate", "--cwd", str(wt), "--state-root", str(ext), "--docs-root", str(ext), "--mode", "external", "--version", "v0.1", "--branch", "feat/v0.1-ext", "--phase", "pre-merge", cwd=wt)[0] == 0


def _check_k2_shipped(work: pathlib.Path) -> tuple[pathlib.Path, str]:
    repo = work / "k2-main"
    _seed_repo(repo, "own", "feat/v0.1-s")
    scope_a = _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# ok\npass.\n")[1]["scope_key"]
    assert (_git(repo, "checkout", "main").returncode, _git(repo, "merge", "--ff-only", "feat/v0.1-s").returncode) == (0, 0)
    assert _git(repo, "tag", "-a", "v0.1", "-m", "rel v0.1").returncode == 0
    assert _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", cwd=repo)[0] == 0
    assert _ops("review-archive", "--cwd", str(repo), "--tag", "v0.1", cwd=repo)[0] == 0
    st0 = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert (st0["released_tag"], st0["archived_scope"], st0["handoff_stale"]) == ("v0.1", scope_a, False), st0

    assert _ops("init", "--cwd", str(repo), cwd=repo)[0] == 0
    assert (_git(repo, "add", ".").returncode, _git(repo, "commit", "-m", "chore: refresh init").returncode) == (0, 0)
    for with_ev in (True, False):
        if not with_ev:
            _write(repo / ".loop" / "handoff.md", _handoff("v0.1", "feat/v0.1-s", "", with_ev=False))
            assert (_git(repo, "add", ".loop/handoff.md").returncode, _git(repo, "commit", "-m", "no evidence").returncode) == (0, 0)
        st1 = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
        assert (st1["recommended_next"], st1["handoff_stale"], st1["stale_archived_scope"]) == ("/loop-plan", True, scope_a), st1
        assert (st1["released_tag"], st1["archived_scope"], st1["pass_stale"], st1["preflight_checked"]) == ("", "", False, False)
    assert _ops("review-status", "--cwd", str(repo), "--version", "0.1", cwd=repo)[1]["handoff_stale"] is True
    assert _ops("review-status", "--cwd", str(repo), "--version", "v0.2", cwd=repo)[1]["handoff_stale"] is False

    same = work / "k2-same-main"
    _seed_repo(same, "own", "main")
    assert _ops("review-record", "--cwd", str(same), "--verdict", "pass", cwd=same, stdin_text="# ok\npass.\n")[0] == 0
    assert _git(same, "tag", "-a", "v0.1", "-m", "rel").returncode == 0
    assert _ops("ship-record", "--cwd", str(same), "--mode", "own", "--tag", "v0.1", cwd=same)[0] == 0
    assert _ops("review-archive", "--cwd", str(same), "--tag", "v0.1", cwd=same)[0] == 0
    _write(same / ".loop" / "handoff.md", _handoff("v0.1", "main", "", with_ev=True))
    assert (_git(same, "add", ".loop/handoff.md").returncode, _git(same, "commit", "-m", "drop baseline").returncode) == (0, 0)
    assert _ops("review-status", "--cwd", str(same), cwd=same)[1]["handoff_stale"] is True
    return repo, scope_a


def _check_k2_abandoned_legacy_spoof(work: pathlib.Path, repo: pathlib.Path, scope_a: str) -> None:
    abn = work / "k2-abandoned"
    _seed_repo(abn, "own", "feat/v0.1-abn")
    scope_abn = _ops("review-record", "--cwd", str(abn), "--verdict", "reject", "--category", "method", cwd=abn, stdin_text="# rej\nno.\n")[1]["scope_key"]
    assert _ops("review-archive", "--cwd", str(abn), "--close-reason", "abandoned", cwd=abn)[0] == 0
    assert (_git(abn, "checkout", "main").returncode, _git(abn, "merge", "--ff-only", "feat/v0.1-abn").returncode) == (0, 0)
    _write(abn / "app.py", "def run():\n    return 9\n")
    assert (_git(abn, "add", "app.py").returncode, _git(abn, "commit", "-m", "post abandoned").returncode) == (0, 0)
    st_ab = _ops("review-status", "--cwd", str(abn), cwd=abn)[1]
    assert (st_ab["recommended_next"], st_ab["handoff_stale"], st_ab["stale_archived_scope"], st_ab["released_tag"]) == ("/loop-plan", True, scope_abn, ""), st_ab

    leg_retro = repo / ".loop" / "archive" / f"{scope_a}.retro.json"
    leg_retro.unlink()
    st_leg = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st_leg["handoff_stale"] is True and "legacy archive" in st_leg["recommended_reason"], st_leg
    assert _git(repo, "tag", "-d", "v0.1").returncode == 0
    assert _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["handoff_stale"] is False
    _write(leg_retro, "{corrupt")
    assert _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["recommended_next"] == "fix_corrupt_records"
    leg_retro.unlink()

    spoof = work / "k2-spoof"
    b_sp, _ = _seed_repo(spoof, "own", "feat/v0.1-a")
    assert _ops("review-record", "--cwd", str(spoof), "--verdict", "pass", cwd=spoof, stdin_text="# a\nok.\n")[0] == 0
    assert (_git(spoof, "checkout", "main").returncode, _git(spoof, "merge", "--ff-only", "feat/v0.1-a").returncode) == (0, 0)
    assert _git(spoof, "tag", "-a", "v0.1", "-m", "a").returncode == 0
    assert _ops("ship-record", "--cwd", str(spoof), "--mode", "own", "--tag", "v0.1", cwd=spoof)[0] == 0
    assert _ops("review-archive", "--cwd", str(spoof), "--tag", "v0.1", cwd=spoof)[0] == 0
    assert _git(spoof, "checkout", "-b", "feat/v0.1-b", b_sp).returncode == 0
    _write(spoof / ".loop" / "handoff.md", _handoff("v0.1", "feat/v0.1-b", b_sp))
    assert (_git(spoof, "add", ".loop/handoff.md").returncode, _git(spoof, "commit", "-m", "sibling").returncode) == (0, 0)
    assert _ops("review-status", "--cwd", str(spoof), cwd=spoof)[1]["handoff_stale"] is False

    ng = work / "k2-nongit"
    ng.mkdir()
    st_ng = _ops("review-status", "--cwd", str(ng), cwd=ng)[1]
    assert (st_ng["handoff_stale"], st_ng["stale_archived_scope"]) == (False, "")


def _check_k3_sampling_note(work: pathlib.Path) -> None:
    from devloop_review_io import normalize_sampling_note, optional_note_ok
    repo = work / "k3-own"
    _seed_repo(repo, "own", "feat/v0.1-n")
    sk = _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# ok\nok.\n")[1]["scope_key"]
    assert (_git(repo, "checkout", "main").returncode, _git(repo, "merge", "--ff-only", "feat/v0.1-n").returncode, _git(repo, "tag", "-a", "v0.1", "-m", "v0.1").returncode) == (0, 0, 0)
    c_ws, r_ws = _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", "--sampling-note", "  \n\t ", cwd=repo)
    assert c_ws == 0 and "sampling_note" not in r_ws["receipt"], r_ws
    assert _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", cwd=repo)[1]["idempotent"] is True
    c_add, r_add = _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", "--sampling-note", "added later", cwd=repo)
    assert c_add == 1 and any("[SHIP_RECEIPT_CONFLICT]" in v for v in r_add["violations"]), r_add
    ship_f = repo / ".loop" / "reviews" / f"{sk}.ship.json"
    ship_f.unlink()
    for bad in ("a" * 500 + "1", "a" * 500 + "2"):
        c_tl, r_tl = _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", "--sampling-note", bad, cwd=repo)
        assert c_tl == 1 and any("[SAMPLING_NOTE_TOO_LONG]" in v for v in r_tl["violations"]) and not ship_f.exists(), r_tl
    c_ok, r_ok = _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", "--sampling-note", "  OK\ncheck  passed  ", cwd=repo)
    assert (c_ok, r_ok["receipt"]["sampling_note"]) == (0, "OK check passed"), r_ok
    assert _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", "--sampling-note", "OK check passed", cwd=repo)[1]["idempotent"] is True
    assert _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", "--sampling-note", "other", cwd=repo)[0] == 1
    assert "sampling_note" not in json.dumps(_ops("review-status", "--cwd", str(repo), cwd=repo)[1])
    orig_ship = ship_f.read_text(encoding="utf-8")
    for bad_val in ("", "   ", 123, None, "x" * 501):
        _write(ship_f, json.dumps({**json.loads(orig_ship), "sampling_note": bad_val}))
        assert any("[SHIP_RECEIPT_CORRUPT]" in v for v in _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["corrupt_records"])
    _write(ship_f, orig_ship)
    c_ar, r_ar = _ops("review-archive", "--cwd", str(repo), "--tag", "v0.1", cwd=repo)
    assert (c_ar, r_ar["retro_meta"]["sampling_note"]) == (0, "OK check passed"), r_ar
    retro_f = repo / ".loop" / "archive" / f"{sk}.retro.json"
    orig_retro = retro_f.read_text(encoding="utf-8")
    _write(ship_f, json.dumps({**json.loads(orig_ship), "sampling_note": "mismatch"}))
    c_rc, r_rc = _ops("review-archive", "--cwd", str(repo), "--tag", "v0.1", cwd=repo)
    assert c_rc == 1 and any("[ARCHIVE_META_CONFLICT]" in v for v in r_rc["violations"]), r_rc
    ship_f.unlink()
    _write(retro_f, json.dumps({**json.loads(orig_retro), "sampling_note": "   "}))
    assert any("[ARCHIVE_META_CORRUPT]" in v for v in _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["corrupt_records"])
    ext = work / "k3-ext"
    _seed_repo(ext, "external", "feat/v0.1-extn")
    _ops("review-record", "--cwd", str(ext), "--mode", "external", "--verdict", "pass", cwd=ext, stdin_text="# ok\nok.\n")
    assert _ops("ship-record", "--cwd", str(ext), "--mode", "external", "--version", "v0.1", "--branch", "feat/v0.1-extn", "--sampling-note", "ext note", cwd=ext)[0] == 0
    assert _ops("review-archive", "--cwd", str(ext), "--version", "v0.1", "--branch", "feat/v0.1-extn", cwd=ext)[1]["retro_meta"]["sampling_note"] == "ext note"
    rng = random.Random(20261127)
    for _ in range(200):
        raw = "".join(rng.choice("ab \t\n中") for _ in range(rng.randint(0, 620)))
        cleaned, err = normalize_sampling_note(raw)
        if err:
            assert "[SAMPLING_NOTE_TOO_LONG]" in err and not optional_note_ok({"sampling_note": " ".join(raw.split())})
        elif cleaned:
            assert optional_note_ok({"sampling_note": cleaned})


def check_v017_upgrades(work: pathlib.Path) -> str:
    cwd_d, _, bad_d = _check_k1_resolve_and_prop(work)
    _check_k1_cli_and_worktree(work, cwd_d, bad_d)
    repo, scope_a = _check_k2_shipped(work)
    _check_k2_abandoned_legacy_spoof(work, repo, scope_a)
    _check_k3_sampling_note(work)
    return "v0.17: ports resolve/none/reaper/gate, stale handoff/abandoned/legacy, sampling_note (seed=20261127) verified"
