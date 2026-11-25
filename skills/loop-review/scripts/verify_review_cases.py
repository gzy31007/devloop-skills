#!/usr/bin/env python3
"""Isolated self-tests for cross-session review records, scopes, ignore guards, and archival."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent
DEVLOOP_OPS = SCRIPTS / "devloop_ops.py"


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _run(cmd: list[str], cwd: pathlib.Path, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), input=stdin_text, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd)


def _ops(*args: str, cwd: pathlib.Path, stdin_text: str | None = None) -> tuple[int, dict]:
    proc = _run([sys.executable, str(DEVLOOP_OPS), *args, "--json"], cwd, stdin_text=stdin_text)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}


def _setup_repo(repo: pathlib.Path, mode: str, branch: str, version: str = "v0.1") -> str:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@users.noreply.github.com")
    _write(repo / "app.py", "def run():\n    return 1\n")
    assert _git(repo, "add", "app.py").returncode == 0
    assert _git(repo, "commit", "-m", "init").returncode == 0
    assert _ops("init", "--cwd", str(repo), "--mode", mode, cwd=repo)[0] == 0
    doc_dir = (repo / ".loop") if mode == "external" else repo
    _write(doc_dir / "DECISIONS.md", "# DECISIONS\n\n2026-11-25 | [active] | 选择：A | 放弃：B | 触发：C\n")
    _write(doc_dir / "FEATURE_MAP.md", "# F\n\n| 版本 | 功能 | 入口 | 验证 |\n| --- | --- | --- | --- |\n| v0.1 | X | `x` | `t` |\n")
    if mode == "own":
        assert _git(repo, "add", ".").returncode == 0
        assert _git(repo, "commit", "-m", "init devloop").returncode == 0
    assert _git(repo, "checkout", "-b", branch).returncode == 0
    handoff = (
        f"# 交接\n\n| 字段 | 内容 |\n| --- | --- |\n"
        f"| 版本 / 主题 | `{version} / demo` |\n| 分支 | `{branch}` |\n| 改动范围 | `app.py` |\n\n"
        "| 文档 | 状态 | 摘要 |\n| --- | --- | --- |\n"
        "| `DECISIONS.md` | `已更新` | 新增条目 |\n| `FEATURE_MAP.md` | `已更新` | 新增功能行 |\n"
    )
    _write(repo / ".loop" / "handoff.md", handoff)
    if mode == "own":
        assert _git(repo, "add", ".loop/handoff.md").returncode == 0
        assert _git(repo, "commit", "-m", "handoff branch").returncode == 0
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _check_record_guards(work: pathlib.Path, repo: pathlib.Path, sha: str) -> None:
    nongit = work / "nongit-review"
    nongit.mkdir(parents=True, exist_ok=True)
    code, rep = _ops("review-record", "--cwd", str(nongit), "--verdict", "reject", "--category", "method", cwd=nongit, stdin_text="x\n")
    assert code == 2 and any("[NOT_GIT]" in v for v in rep["violations"]), rep

    _write(repo / "dirty.txt", "temp\n")
    code, rep = _ops("review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "method", cwd=repo, stdin_text="dirty\n")
    assert code == 1 and any("[DIRTY_WORKTREE]" in v for v in rep["violations"]), rep
    (repo / "dirty.txt").unlink()

    code, rep = _ops("review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "method", "--reviewed-sha", "0000000", cwd=repo, stdin_text="bad sha\n")
    assert code == 1 and any("[CANDIDATE_HEAD_MISMATCH]" in v for v in rep["violations"]), rep

    inside_file = _write(repo / "verdict.md", "# 内部临时文件\n")
    code, rep = _ops("review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "method", "--body-file", str(inside_file), cwd=repo)
    assert code == 1 and any("[BODY_FILE_INSIDE_REPO]" in v for v in rep["violations"]), rep
    inside_file.unlink()
    assert sha == _git(repo, "rev-parse", "HEAD").stdout.strip(), "HEAD moved unexpectedly"


def _check_reject_and_idempotency(work: pathlib.Path, repo: pathlib.Path) -> None:
    body1 = "# 审查打回\nBOM 剥离缺失导致首列错位。\n"
    code, r1 = _ops("review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "method", cwd=repo, stdin_text=body1)
    assert code == 0 and r1["idempotent"] is False, r1
    assert r1["reject_count"] == 1 and r1["method_reject_count"] == 1, r1
    assert _git(repo, "status", "--porcelain").stdout.strip() == "", "worktree dirtied after review-record"
    assert _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["recommended_next"] == "/loop-build"

    code, r1_dup = _ops("review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "method", cwd=repo, stdin_text=body1)
    assert code == 0 and r1_dup["idempotent"] is True, r1_dup
    assert r1_dup["reject_count"] == 1 and r1_dup["record"]["round"] == 1, r1_dup

    _write(repo / "app.py", "def run():\n    return 2\n")
    assert _git(repo, "add", "app.py").returncode == 0
    assert _git(repo, "commit", "-m", "fix rework").returncode == 0
    assert _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["recommended_next"] == "/loop-review"

    ext_body = _write(work / "outside-body.md", "# 环境打回\n本地端口被占用。\n")
    code, r2 = _ops("review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "env", "--body-file", str(ext_body), cwd=repo)
    assert code == 0 and r2["reject_count"] == 2, r2
    assert r2["env_reject_count"] == 1 and r2["cross_model_recommended"] is True, r2


def _check_pending_breaker_and_pass(repo: pathlib.Path) -> str:
    code, r3 = _ops("review-record", "--cwd", str(repo), "--verdict", "pending", cwd=repo, stdin_text="# 待人工验证\n请确认移动端表单视口。\n")
    assert code == 0 and r3["reject_count"] == 2 and r3["record"]["round"] == 3, r3
    assert r3["pending_count"] == 1, r3
    st_pending = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st_pending["recommended_next"] == "manual_browser_verify", st_pending

    code, r4 = _ops("review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "method", cwd=repo, stdin_text="# 第3次打回\n边界校验缺失。\n")
    assert code == 0 and r4["reject_count"] == 3 and r4["circuit_breaker_triggered"] is True, r4
    st_brk = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st_brk["recommended_next"] == "manual_decision", st_brk

    code, r5 = _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# 全绿放行\n全部修复并通过。\n")
    assert code == 0 and r5["record"]["round"] == 5 and r5["reject_count"] == 3, r5
    scope_dir = pathlib.Path(r5["record"]["path"]).parent
    for fname in ("r01-reject.md", "r02-reject.md", "r03-pending.md", "r04-reject.md", "r05-pass.md"):
        assert (scope_dir / fname).is_file(), f"missing {fname}"
    return r5["scope_key"]


def _check_branch_scope_and_worktree(work: pathlib.Path, repo: pathlib.Path, orig_scope: str) -> None:
    from devloop_review_store import make_scope_key
    assert make_scope_key("v0.1", "feat/v0.1-a") != make_scope_key("v0.1", "feat-v0.1-a"), "scope hash collision"
    assert _git(repo, "checkout", "-b", "feat-v0.1-a").returncode == 0
    code, st = _ops("review-status", "--cwd", str(repo), cwd=repo)
    assert code == 0 and st["latest_verdict"] == "none" and st["scope_key"] != orig_scope, st
    assert any(item["scope_key"] == orig_scope for item in st["pending_archive_scopes"]), st

    ext_repo = work / "ext-wt-main"
    ext_sha = _setup_repo(ext_repo, "external", "feat/v0.1-ext")
    wt = work / "ext-wt-blind"
    assert _git(ext_repo, "worktree", "add", "--detach", str(wt), ext_sha).returncode == 0
    code, rep = _ops(
        "review-record", "--cwd", str(wt), "--state-root", str(ext_repo), "--docs-root", str(ext_repo),
        "--branch", "feat/v0.1-ext", "--verdict", "pass", cwd=wt, stdin_text="# 客串放行\n隔离 worktree 验收通过。\n",
    )
    assert code == 0 and pathlib.Path(rep["record"]["path"]).is_relative_to(ext_repo), rep
    assert _git(wt, "status", "--porcelain").stdout.strip() == "", "wt dirtied"
    assert _git(ext_repo, "status", "--porcelain").stdout.strip() == "", "main ext repo dirtied"
    code, gate = _ops(
        "release-gate", "--cwd", str(wt), "--state-root", str(ext_repo), "--docs-root", str(ext_repo),
        "--branch", "feat/v0.1-ext", "--phase", "pre-merge", "--tag", "v0.1", "--ports", "none", cwd=wt,
    )
    assert code == 0 and gate["status"] == "PASSED", gate


def _check_ignore_guards(work: pathlib.Path) -> None:
    from devloop_git import resolve_git_exclude
    old_repo = work / "old-unignored"
    _setup_repo(old_repo, "own", "feat/v0.1-old")
    exc = resolve_git_exclude(old_repo)
    assert exc is not None, "missing exclude path"
    exc.write_text("", encoding="utf-8")
    code, rep = _ops("review-record", "--cwd", str(old_repo), "--verdict", "reject", "--category", "method", cwd=old_repo, stdin_text="blocked\n")
    assert code == 1 and any("[REVIEW_PATH_UNIGNORED]" in v for v in rep["violations"]), rep

    _write(old_repo / ".gitignore", "!.loop/reviews/**\n")
    assert _git(old_repo, "add", ".gitignore").returncode == 0
    assert _git(old_repo, "commit", "-m", "negate").returncode == 0
    code, rep = _ops("review-record", "--cwd", str(old_repo), "--verdict", "reject", "--category", "method", cwd=old_repo, stdin_text="blocked\n")
    assert code == 1 and any("[REVIEW_PATH_UNIGNORED]" in v for v in rep["violations"]), rep

    _write(old_repo / ".loop" / "reviews" / "keep.txt", "tracked\n")
    assert _git(old_repo, "add", "-f", ".loop/reviews/keep.txt").returncode == 0
    assert _git(old_repo, "commit", "-m", "track review dir").returncode == 0
    code, rep = _ops("review-record", "--cwd", str(old_repo), "--verdict", "reject", "--category", "method", cwd=old_repo, stdin_text="blocked\n")
    assert code == 1 and any("[REVIEW_PATH_TRACKED]" in v for v in rep["violations"]), rep


def _check_archive_and_pending_scopes(work: pathlib.Path) -> None:
    repo = work / "archive-repo"
    _setup_repo(repo, "own", "feat/v0.1-clean")
    code, r1 = _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# 一次过审\n零打回放行。\n")
    assert code == 0 and r1["reject_count"] == 0, r1
    scope_key = r1["scope_key"]

    assert _git(repo, "checkout", "main").returncode == 0
    assert _git(repo, "merge", "--no-ff", "feat/v0.1-clean", "-m", "merge v0.1").returncode == 0
    st = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert any(item["scope_key"] == scope_key for item in st["pending_archive_scopes"]), st

    code, arch = _ops("review-archive", "--cwd", str(repo), "--tag", "v0.1", cwd=repo)
    assert code == 0 and arch["archived"] is True and arch["idempotent"] is False, arch
    assert not (repo / ".loop" / "reviews" / scope_key).exists(), "source dir remained after archive"
    assert (repo / ".loop" / "archive" / scope_key / "r01-pass.md").is_file(), "archived file missing"
    assert _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["pending_archive_scopes"] == [], "pending_archive_scopes not empty"

    code, arch_dup = _ops("review-archive", "--cwd", str(repo), "--tag", "v0.1", cwd=repo)
    assert code == 0 and arch_dup["idempotent"] is True, arch_dup

    assert _git(repo, "checkout", "feat/v0.1-clean").returncode == 0
    _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# 新内容冲突\n不同正文触发归档冲突。\n")
    code, conflict = _ops("review-archive", "--cwd", str(repo), "--tag", "v0.1", cwd=repo)
    assert code == 1 and any("[ARCHIVE_CONFLICT]" in v for v in conflict["violations"]), conflict


def _check_archive_ambiguous(work: pathlib.Path) -> None:
    ambig = work / "archive-ambig"
    _setup_repo(ambig, "own", "feat/v0.1-x")
    c_x, r_x = _ops("review-record", "--cwd", str(ambig), "--verdict", "pass", cwd=ambig, stdin_text="# x\nok.\n")
    assert c_x == 0, r_x
    assert _git(ambig, "checkout", "-b", "feat/v0.1-y", "main").returncode == 0
    handoff_y = (
        "# 交接\n\n| 字段 | 内容 |\n| --- | --- |\n"
        "| 版本 / 主题 | `v0.1 / y` |\n| 分支 | `feat/v0.1-y` |\n| 改动范围 | `app.py` |\n\n"
        "| 文档 | 状态 | 摘要 |\n| --- | --- | --- |\n"
        "| `DECISIONS.md` | `已更新` | 新增条目 |\n| `FEATURE_MAP.md` | `已更新` | 新增功能行 |\n"
    )
    _write(ambig / ".loop" / "handoff.md", handoff_y)
    assert _git(ambig, "add", ".loop/handoff.md").returncode == 0
    assert _git(ambig, "commit", "-m", "y handoff").returncode == 0
    c_y, r_y = _ops("review-record", "--cwd", str(ambig), "--verdict", "pass", cwd=ambig, stdin_text="# y\nok.\n")
    assert c_y == 0, r_y
    assert _git(ambig, "checkout", "main").returncode == 0
    code, rep = _ops("review-archive", "--cwd", str(ambig), "--tag", "v0.1", cwd=ambig)
    assert code == 1 and any("[ARCHIVE_SCOPE_AMBIGUOUS]" in v for v in rep["violations"]), rep


def check_review_handoff_and_archive(work: pathlib.Path) -> str:
    repo = work / "review-main"
    sha = _setup_repo(repo, "own", "feat/v0.1-a")
    _check_record_guards(work, repo, sha)
    _check_reject_and_idempotency(work, repo)
    orig_scope = _check_pending_breaker_and_pass(repo)
    _check_branch_scope_and_worktree(work, repo, orig_scope)
    _check_ignore_guards(work)
    _check_archive_and_pending_scopes(work)
    _check_archive_ambiguous(work)
    return "review-record/status/archive: guards, idempotency, breaker, branch-hash, linked-wt, ignore & archive verified"
