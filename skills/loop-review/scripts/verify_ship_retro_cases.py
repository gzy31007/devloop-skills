#!/usr/bin/env python3
"""Isolated self-tests for /loop-ship, /loop-retro state machine, and crash-resilient archival."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent
DEVLOOP_OPS = SCRIPTS / "devloop_ops.py"


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _run(cmd: list[str], cwd: pathlib.Path, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), input=stdin_text, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd)


def _ops(*args: str, cwd: pathlib.Path, stdin_text: str | None = None) -> tuple[int, dict]:
    proc = _run([sys.executable, str(DEVLOOP_OPS), *args, "--json"], cwd, stdin_text=stdin_text)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}


def _handoff(version: str, branch: str, baseline: str) -> str:
    return (
        f"# 交接\n\n| 字段 | 内容 |\n| --- | --- |\n| 版本 / 主题 | `{version} / ship-retro` |\n"
        f"| 分支 | `{branch}` |\n| 基线 SHA | `{baseline}` |\n| 改动范围 | `app.py` |\n| 自测与返工验证证据 | `pytest -> exit 0` |\n\n"
        "## 文档同步声明\n\n| 文档 | 状态 | 摘要 |\n| --- | --- | --- |\n"
        "| `DECISIONS.md` | `已更新` | 新增条目 |\n| `FEATURE_MAP.md` | `已更新` | 新增功能行 |\n"
    )


def _seed_repo(repo: pathlib.Path, mode: str = "own", branch: str = "feat/v0.1-a", ho_ver: str = "v0.1") -> tuple[str, str]:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@users.noreply.github.com")
    _write(repo / "app.py", "def run():\n    return 1\n")
    if mode == "external":
        _write(repo / "package.json", json.dumps({"name": "upstream", "version": "9.9.9"}) + "\n")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-m", "init").returncode == 0
    assert _ops("init", "--cwd", str(repo), "--mode", mode, cwd=repo)[0] == 0
    if mode == "own":
        assert _git(repo, "add", ".").returncode == 0
        assert _git(repo, "commit", "-m", "init devloop").returncode == 0
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if branch != "main":
        assert _git(repo, "checkout", "-b", branch).returncode == 0
    doc_dir = (repo / ".loop") if mode == "external" else repo
    _write(doc_dir / "DECISIONS.md", "# DECISIONS\n\n2026-11-26 | [active] | 选择：A | 放弃：B | 触发：C\n")
    _write(doc_dir / "FEATURE_MAP.md", "# F\n\n| 版本 | 功能 | 入口 | 验证 |\n| --- | --- | --- | --- |\n| v0.1 | X | `x` | `t` |\n")
    _write(repo / "app.py", "def run():\n    return 2\n")
    _write(repo / ".loop" / "handoff.md", _handoff(ho_ver, branch, base_sha))
    if mode == "own":
        assert _git(repo, "add", "app.py", "DECISIONS.md", "FEATURE_MAP.md", ".loop/handoff.md").returncode == 0
    else:
        assert _git(repo, "add", "app.py").returncode == 0
    assert _git(repo, "commit", "-m", f"cand on {branch}").returncode == 0
    return base_sha, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _check_same_branch_and_sibling_spoof(work: pathlib.Path) -> None:
    same = work / "same-branch"
    _, cand = _seed_repo(same, "own", "main")
    assert _ops("review-record", "--cwd", str(same), "--verdict", "pass", cwd=same, stdin_text="# ok\npass.\n")[0] == 0
    st0 = _ops("review-status", "--cwd", str(same), cwd=same)[1]
    assert st0["recommended_next"] == "/loop-ship", st0

    assert _git(same, "tag", "-a", "v0.1", "-m", "rel v0.1").returncode == 0
    st1 = _ops("review-status", "--cwd", str(same), cwd=same)[1]
    assert st1["recommended_next"] == "/loop-retro" and st1["released_tag"] == "v0.1", st1

    c_sh, r_sh = _ops("ship-record", "--cwd", str(same), "--mode", "own", "--tag", "v0.1", "--reviewed-sha", cand, cwd=same)
    assert c_sh == 0 and r_sh["idempotent"] is False and r_sh["receipt"]["kind"] == "release_tag", r_sh
    assert _ops("ship-record", "--cwd", str(same), "--mode", "own", "--tag", "v0.1", cwd=same)[1]["idempotent"] is True
    c_cf, r_cf = _ops("ship-record", "--cwd", str(same), "--mode", "own", "--tag", "v0.1", "--push-status", "pushed", cwd=same)
    assert c_cf == 1 and any("[SHIP_RECEIPT_CONFLICT]" in v for v in r_cf["violations"]), r_cf
    assert _ops("review-archive", "--cwd", str(same), "--tag", "v0.1", cwd=same)[0] == 0
    assert _ops("review-status", "--cwd", str(same), cwd=same)[1]["recommended_next"] == "/loop-plan"

    spoof = work / "sibling-spoof"
    base_sp, _ = _seed_repo(spoof, "own", "feat/v0.1-a")
    assert _ops("review-record", "--cwd", str(spoof), "--verdict", "pass", cwd=spoof, stdin_text="# a\nok.\n")[0] == 0
    assert _git(spoof, "checkout", "-b", "feat/v0.1-b", base_sp).returncode == 0
    _write(spoof / "b.txt", "b\n")
    assert _git(spoof, "add", "b.txt").returncode == 0 and _git(spoof, "commit", "-m", "b").returncode == 0
    assert _git(spoof, "checkout", "main").returncode == 0
    assert _git(spoof, "merge", "--ff-only", "feat/v0.1-b").returncode == 0
    assert _git(spoof, "tag", "-a", "v0.1", "-m", "b tag").returncode == 0
    st_sp = _ops("review-status", "--cwd", str(spoof), "--version", "v0.1", cwd=spoof)[1]
    assert st_sp["released_tag"] == "" and st_sp["shipped_scope"] == "", st_sp


def _check_alias_and_post_tag_guard(work: pathlib.Path) -> None:
    repo = work / "alias-tag"
    _, cand = _seed_repo(repo, "own", "feat/v0.1-alias", ho_ver="0.1")
    assert _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# ok\nalias.\n")[0] == 0
    assert _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--tag", "v0.1.0", "--ports", "none", cwd=repo)[0] == 0
    assert _git(repo, "checkout", "main").returncode == 0
    assert _git(repo, "merge", "--ff-only", "feat/v0.1-alias").returncode == 0
    assert _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1.0", cwd=repo)[0] == 1
    assert _git(repo, "tag", "-a", "v0.1.0", "-m", "rel v0.1.0").returncode == 0
    assert _ops("release-gate", "--cwd", str(repo), "--phase", "post-tag", "--tag", "v0.1.0", "--branch", "main", cwd=repo)[0] == 0
    assert _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1.0", "--reviewed-sha", cand, cwd=repo)[0] == 0
    st = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st["recommended_next"] == "/loop-retro" and st["released_tag"] == "v0.1.0" and st["ship_kind"] == "release_tag", st

    div = work / "nonff-tamper"
    base_d, cand_d = _seed_repo(div, "own", "feat/v0.1-div")
    assert _ops("review-record", "--cwd", str(div), "--verdict", "pass", cwd=div, stdin_text="# ok\ndiv.\n")[0] == 0
    assert _git(div, "checkout", "main").returncode == 0
    _write(div / "other.py", "x = 9\n")
    assert _git(div, "add", "other.py").returncode == 0 and _git(div, "commit", "-m", "main diverge").returncode == 0
    assert _git(div, "merge", "--no-ff", "feat/v0.1-div", "-m", "merge non-ff").returncode == 0
    assert _git(div, "tag", "-a", "v0.1", "-m", "bad merge tag").returncode == 0
    c_pt, r_pt = _ops("release-gate", "--cwd", str(div), "--phase", "post-tag", "--tag", "v0.1", "--branch", "main", "--reviewed-sha", cand_d, cwd=div)
    assert c_pt == 1 and any("[UNREVIEWED_DIFF] other.py" in v for v in r_pt["violations"]), r_pt
    c_auto, r_auto = _ops("release-gate", "--cwd", str(div), "--phase", "post-tag", "--tag", "v0.1", "--branch", "main", cwd=div)
    assert c_auto == 1 and any("[UNREVIEWED_DIFF] other.py" in v for v in r_auto["violations"]), r_auto


def _check_own_loop_and_post_archive_drift(work: pathlib.Path) -> pathlib.Path:
    repo = work / "own-full"
    _, cand = _seed_repo(repo, "own", "feat/v0.1-full")
    c_r, rec = _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# ok\nfull.\n")
    assert c_r == 0, rec
    scope_key = rec["scope_key"]
    assert _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["recommended_next"] == "/loop-ship"
    assert _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--tag", "v0.1", "--ports", "none", cwd=repo)[0] == 0
    assert _git(repo, "checkout", "main").returncode == 0
    assert _git(repo, "merge", "--ff-only", "feat/v0.1-full").returncode == 0
    assert _git(repo, "tag", "-a", "v0.1", "-m", "rel v0.1").returncode == 0
    assert _ops("release-gate", "--cwd", str(repo), "--phase", "post-tag", "--tag", "v0.1", "--branch", "main", "--reviewed-sha", cand, cwd=repo)[0] == 0
    assert _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", cwd=repo)[0] == 0

    _write(repo / "NOTES.md", "# retro note\n")
    assert _git(repo, "add", "NOTES.md").returncode == 0 and _git(repo, "commit", "-m", "chore(retro): add notes").returncode == 0
    retro_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["recommended_next"] == "/loop-retro"

    c_req, r_req = _ops("review-archive", "--cwd", str(repo), "--tag", "v0.1", cwd=repo)
    assert c_req == 1 and any("[RETRO_HEAD_REQUIRED]" in v for v in r_req["violations"]), r_req
    c_ar, r_ar = _ops("review-archive", "--cwd", str(repo), "--tag", "v0.1", "--retro-head", retro_sha, cwd=repo)
    assert c_ar == 0 and r_ar["retro_meta"]["retro_head_sha"] == retro_sha, r_ar
    assert not (repo / ".loop" / "reviews" / f"{scope_key}.ship.json").exists()
    st_plan = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st_plan["recommended_next"] == "/loop-plan" and st_plan["released_tag"] == "v0.1" and st_plan["archived_scope"] == scope_key, st_plan

    _write(repo / "app.py", "def run():\n    return 99\n")
    assert _git(repo, "add", "app.py").returncode == 0 and _git(repo, "commit", "-m", "unrelated work").returncode == 0
    st_clr = _ops("review-status", "--cwd", str(repo), "--version", "v0.1", cwd=repo)[1]
    assert st_clr["released_tag"] == "" and st_clr["archived_scope"] == "", st_clr
    return repo


def _check_dirty_crash_and_abandoned(work: pathlib.Path) -> None:
    repo = work / "crash-dirty"
    _, cand = _seed_repo(repo, "own", "feat/v0.1-cd")
    rec = _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# ok\ncd.\n")[1]
    scope_key = rec["scope_key"]
    c_un, r_un = _ops("review-archive", "--cwd", str(repo), cwd=repo)
    assert c_un == 1 and any("[SCOPE_NOT_SHIPPED]" in v for v in r_un["violations"]), r_un

    assert _git(repo, "checkout", "main").returncode == 0 and _git(repo, "merge", "--ff-only", "feat/v0.1-cd").returncode == 0
    assert _git(repo, "tag", "-a", "v0.1", "-m", "rel").returncode == 0
    assert _ops("ship-record", "--cwd", str(repo), "--mode", "own", "--tag", "v0.1", cwd=repo)[0] == 0

    _write(repo / "dirty.txt", "dirty\n")
    c_dt, r_dt = _ops("review-archive", "--cwd", str(repo), cwd=repo)
    assert c_dt == 1 and any("[DIRTY_WORKTREE]" in v for v in r_dt["violations"]), r_dt
    assert (repo / ".loop" / "reviews" / scope_key / "r01-pass.md").is_file()
    assert (repo / ".loop" / "reviews" / f"{scope_key}.ship.json").is_file()
    (repo / "dirty.txt").unlink()

    wt = work / "dirty-wt"
    assert _git(repo, "worktree", "add", "--detach", str(wt), cand).returncode == 0
    _write(repo / "state_dirty.txt", "x\n")
    c_ws, r_ws = _ops("review-archive", "--cwd", str(wt), "--state-root", str(repo), "--branch", "feat/v0.1-cd", cwd=wt)
    assert c_ws == 1 and any("[DIRTY_WORKTREE]" in v for v in r_ws["violations"]), r_ws
    (repo / "state_dirty.txt").unlink()
    assert _git(repo, "worktree", "remove", "--force", str(wt)).returncode == 0

    meta_p = repo / ".loop" / "archive" / f"{scope_key}.retro.json"
    _write(meta_p, "{corrupt")
    c_cr, r_cr = _ops("review-archive", "--cwd", str(repo), cwd=repo)
    assert c_cr == 1 and any("[ARCHIVE_META_CORRUPT]" in v for v in r_cr["violations"]), r_cr
    _write(meta_p, json.dumps({"scope_key": scope_key, "version": "v0.1", "branch": "feat/v0.1-cd", "reviewed_sha": cand, "retro_head_sha": "0" * 40, "archived_at": "2026-11-26T00:00:00Z", "close_reason": "shipped", "ship_kind": "release_tag", "tag": "v0.1", "tag_sha": "0" * 40}))
    c_mc, r_mc = _ops("review-archive", "--cwd", str(repo), cwd=repo)
    assert c_mc == 1 and any("[ARCHIVE_META_CONFLICT]" in v for v in r_mc["violations"]), r_mc
    tag_sha = _git(repo, "rev-parse", "v0.1^{commit}").stdout.strip()
    _write(meta_p, json.dumps({"scope_key": scope_key, "version": "v0.1", "branch": "feat/v0.1-cd", "reviewed_sha": cand, "retro_head_sha": tag_sha, "archived_at": "2026-11-26T00:00:00Z", "close_reason": "shipped", "ship_kind": "release_tag", "tag": "v0.1", "tag_sha": tag_sha}))
    assert _ops("review-archive", "--cwd", str(repo), cwd=repo)[0] == 0
    meta_p.unlink()
    c_mm, r_mm = _ops("review-archive", "--cwd", str(repo), cwd=repo)
    assert c_mm == 1 and any("[ARCHIVE_META_MISSING]" in v for v in r_mm["violations"]), r_mm

    abn = work / "abandoned-repo"
    _seed_repo(abn, "own", "feat/v0.1-abn")
    _ops("review-record", "--cwd", str(abn), "--verdict", "reject", "--category", "method", cwd=abn, stdin_text="# rej\nno.\n")
    _write(abn / ".loop" / "reviews" / "bad.ship.json", "{corrupt")
    assert _ops("review-status", "--cwd", str(abn), cwd=abn)[1]["recommended_next"] == "fix_corrupt_records"
    (abn / ".loop" / "reviews" / "bad.ship.json").unlink()
    c_ab, r_ab = _ops("review-archive", "--cwd", str(abn), "--close-reason", "abandoned", cwd=abn)
    assert c_ab == 0 and r_ab["retro_meta"]["close_reason"] == "abandoned", r_ab
    st_ab = _ops("review-status", "--cwd", str(abn), "--version", "v0.1", cwd=abn)[1]
    assert st_ab["released_tag"] == "" and st_ab["archived_scope"] == "", st_ab


def _check_external_ship_retro(work: pathlib.Path) -> None:
    ext = work / "ext-ship-retro"
    _, cand = _seed_repo(ext, "external", "feat/v0.1-ext")
    main_before = _git(ext, "rev-parse", "main").stdout.strip()
    rec = _ops("review-record", "--cwd", str(ext), "--mode", "external", "--verdict", "pass", cwd=ext, stdin_text="# ext\nok.\n")[1]
    scope_key = rec["scope_key"]
    assert _ops("review-status", "--cwd", str(ext), "--mode", "external", cwd=ext)[1]["recommended_next"] == "/loop-ship"
    c_pm, r_pm = _ops("release-gate", "--cwd", str(ext), "--phase", "pre-merge", "--mode", "external", "--version", "v0.1", "--branch", "feat/v0.1-ext", "--ports", "none", cwd=ext)
    assert c_pm == 0 and r_pm["reviewed_sha"] == cand, r_pm
    c_sr, r_sr = _ops("ship-record", "--cwd", str(ext), "--mode", "external", "--version", "v0.1", "--branch", "feat/v0.1-ext", cwd=ext)
    assert c_sr == 0 and r_sr["receipt"]["kind"] == "pr_handoff", r_sr
    assert _git(ext, "rev-parse", "main").stdout.strip() == main_before and _git(ext, "tag").stdout.strip() == ""
    assert _git(ext, "status", "--porcelain").stdout.strip() == ""
    st_ret = _ops("review-status", "--cwd", str(ext), "--mode", "external", cwd=ext)[1]
    assert st_ret["recommended_next"] == "/loop-retro" and st_ret["ship_kind"] == "pr_handoff" and st_ret["shipped_scope"] == scope_key, st_ret
    c_ar, r_ar = _ops("review-archive", "--cwd", str(ext), "--version", "v0.1", "--branch", "feat/v0.1-ext", cwd=ext)
    assert c_ar == 0 and r_ar["retro_meta"]["ship_kind"] == "pr_handoff", r_ar
    st_end = _ops("review-status", "--cwd", str(ext), "--mode", "external", cwd=ext)[1]
    assert st_end["recommended_next"] == "/loop-plan" and st_end["archived_scope"] == scope_key and st_end["released_tag"] == "", st_end


def check_ship_and_retro_flows(work: pathlib.Path) -> str:
    _check_same_branch_and_sibling_spoof(work)
    _check_alias_and_post_tag_guard(work)
    _check_own_loop_and_post_archive_drift(work)
    _check_dirty_crash_and_abandoned(work)
    _check_external_ship_retro(work)
    return "ship/retro: same-branch, sibling-spoof, alias/non-ff post-tag, own/external loops, dirty/crash/abandoned verified"
