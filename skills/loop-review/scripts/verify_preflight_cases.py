#!/usr/bin/env python3
"""Isolated self-tests for devloop read-only preflight, status routing, and strict handoff/doc guards."""

from __future__ import annotations

import json, pathlib, subprocess, sys

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


def _handoff_text(version: str, branch: str, baseline: str, extra_top: str = "", evidence: str = "`pytest -> exit 0`",
                  dec_row: str = "| `DECISIONS.md` | `已更新` | 新增条目 |", feat_row: str = "| `FEATURE_MAP.md` | `已更新` | 新增功能行 |", extra_sec: str = "") -> str:
    ev_row = f"| 自测与返工验证证据 | {evidence} |\n" if evidence else ""
    base_row = f"| 基线 SHA | `{baseline}` |\n" if baseline else ""
    return (
        f"# 交接\n\n| 字段 | 内容 |\n| --- | --- |\n| 版本 / 主题 | `{version} / preflight` |\n"
        f"| 分支 | `{branch}` |\n{base_row}| 改动范围 | `app.py` |\n{ev_row}{extra_top}\n"
        f"{extra_sec}## 文档同步声明\n\n| 文档 | 状态 | 摘要 |\n| --- | --- | --- |\n{dec_row}\n{feat_row}\n"
    )


def _seed_repo(repo: pathlib.Path, branch: str = "feat/v0.1-pf") -> tuple[str, str]:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@users.noreply.github.com")
    _write(repo / "app.py", "def run():\n    return 1\n")
    _write(repo / "old_mod.py", "def legacy():\n    return 0\n")
    assert _git(repo, "add", ".").returncode == 0 and _git(repo, "commit", "-m", "init").returncode == 0
    assert _ops("init", "--cwd", str(repo), "--mode", "own", cwd=repo)[0] == 0
    assert _git(repo, "add", ".").returncode == 0 and _git(repo, "commit", "-m", "init devloop").returncode == 0
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, "checkout", "-b", branch).returncode == 0 and _git(repo, "mv", "old_mod.py", "new_mod.py").returncode == 0
    _write(repo / "DECISIONS.md", "# DECISIONS\n\n2026-11-26 | [active] | 选择：A | 放弃：B | 触发：C\n")
    _write(repo / "FEATURE_MAP.md", "# FEATURE_MAP\n\n| 版本 | 功能 | 入口 | 验证 |\n| --- | --- | --- | --- |\n| v0.1 | X | `x` | `t` |\n")
    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", branch, base_sha))
    assert _git(repo, "add", ".").returncode == 0 and _git(repo, "commit", "-m", "candidate").returncode == 0
    return base_sha, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _check_empty_and_first_pass(work: pathlib.Path) -> tuple[pathlib.Path, str, str]:
    empty = work / "empty-repo"
    empty.mkdir(parents=True, exist_ok=True)
    assert _git(empty, "init", "-b", "main").returncode == 0 and _ops("init", "--cwd", str(empty), "--mode", "own", cwd=empty)[0] == 0
    code, pf = _ops("preflight", "--cwd", str(empty), cwd=empty)
    assert code == 1 and any("[HEAD_MISSING]" in v for v in pf["violations"]), pf
    repo = work / "pf-main"
    base_sha, cand_sha = _seed_repo(repo)
    code, pf = _ops("preflight", "--cwd", str(repo), cwd=repo)
    assert code == 0 and pf["status"] == "PASSED" and pf["candidate_sha"] == cand_sha and pf["baseline_sha"] == base_sha, pf
    assert pf["scope_key"] and pf["handoff_sha256"] and pf["doc_fingerprints"]["decisions_sha256"], pf
    assert any(d.get("status", "").startswith("R") and d.get("old_path") == "old_mod.py" and d.get("path") == "new_mod.py" for d in pf["diff_files"]), pf
    return repo, base_sha, cand_sha


def _check_baseline_and_branch_errors(repo: pathlib.Path, base_sha: str, cand_sha: str) -> str:
    assert _ops("preflight", "--cwd", str(repo), "--version", "v0.2", cwd=repo)[0] == 1
    code, mm = _ops("preflight", "--cwd", str(repo), "--baseline", cand_sha, cwd=repo)
    assert code == 1 and any("[BASELINE_MISMATCH]" in v for v in mm["violations"]), mm

    assert _git(repo, "checkout", "-b", "side", base_sha).returncode == 0
    _write(repo / "side.txt", "x\n")
    assert _git(repo, "add", "side.txt").returncode == 0 and _git(repo, "commit", "-m", "side").returncode == 0
    side_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, "checkout", "feat/v0.1-pf").returncode == 0

    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf", ""))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0 and _git(repo, "commit", "-m", "no baseline in handoff").returncode == 0
    head_now = _git(repo, "rev-parse", "HEAD").stdout.strip()
    for b_args, exp_tag in (
        ([], "[BASELINE_MISSING]"), (["--baseline", base_sha[:8]], "[BASELINE_INVALID]"),
        (["--baseline", head_now], "[BASELINE_EQUALS_HEAD]"), (["--baseline", side_sha], "[BASELINE_NOT_ANCESTOR]"),
    ):
        c_b, r_b = _ops("preflight", "--cwd", str(repo), *b_args, cwd=repo)
        assert c_b == 1 and any(exp_tag in v for v in r_b["violations"]), (exp_tag, r_b)

    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf (base old)", base_sha))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0 and _git(repo, "commit", "-m", "remark branch").returncode == 0
    st = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st["preflight_checked"] and not st["preflight_passed"] and st["recommended_next"] == "/loop-build", st
    assert not list((repo / ".loop" / "reviews").rglob("*.md")), "preflight failure wrote review records"
    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf", base_sha))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0 and _git(repo, "commit", "-m", "restore clean handoff").returncode == 0
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _check_detached_and_archive_guards(work: pathlib.Path, repo: pathlib.Path, cand_sha: str) -> None:
    wt = work / "pf-wt"
    assert _git(repo, "worktree", "add", "--detach", str(wt), cand_sha).returncode == 0
    assert any("[BRANCH_REQUIRED_DETACHED]" in v for v in _ops("preflight", "--cwd", str(wt), "--state-root", str(repo), cwd=wt)[1]["violations"])
    assert any("[BRANCH_REF_MISSING]" in v for v in _ops("preflight", "--cwd", str(wt), "--state-root", str(repo), "--branch", "feat/missing", cwd=wt)[1]["violations"])
    other = work / "other-repo"
    _seed_repo(other)
    assert any("[STATE_ROOT_REPO_MISMATCH]" in v for v in _ops("preflight", "--cwd", str(wt), "--state-root", str(other), "--branch", "feat/v0.1-pf", cwd=wt)[1]["violations"])
    _write(repo / "dirty_state.txt", "dirty\n")
    assert any("[DIRTY_WORKTREE]" in v for v in _ops("preflight", "--cwd", str(wt), "--state-root", str(repo), "--branch", "feat/v0.1-pf", cwd=wt)[1]["violations"])
    (repo / "dirty_state.txt").unlink()
    code, pf_ok = _ops("preflight", "--cwd", str(wt), "--state-root", str(repo), "--branch", "feat/v0.1-pf", cwd=wt)
    assert code == 0 and pf_ok["status"] == "PASSED" and _git(repo, "worktree", "remove", "--force", str(wt)).returncode == 0
    from devloop_git import resolve_git_exclude
    exc = resolve_git_exclude(other)
    assert exc is not None
    exc.write_text("/.loop/reviews/\n", encoding="utf-8")
    assert any("[REVIEW_PATH_UNIGNORED]" in v for v in _ops("preflight", "--cwd", str(other), cwd=other)[1]["violations"])
    _write(other / ".loop" / "archive" / "keep.txt", "x\n")
    assert _git(other, "add", "-f", ".loop/archive/keep.txt").returncode == 0 and _git(other, "commit", "-m", "track archive").returncode == 0
    assert any("[REVIEW_PATH_TRACKED]" in v for v in _ops("preflight", "--cwd", str(other), cwd=other)[1]["violations"])


def _check_handoff_and_template_edges(work: pathlib.Path, repo: pathlib.Path, base_sha: str) -> None:
    from devloop_docs import _is_feature_map_template

    fmap = work / "tmpl_with_inv.md"
    _write(fmap, "# 功能清单\n\n自定义导语。\n\n| 版本 | 功能 | 入口 | 验证 |\n| --- | --- | --- | --- |\n| `<v0.1>` | `<待填>` | `<x>` | `<t>` |\n\n## 不变量\n\n| 编号 | 不变量描述 | 覆盖输入域 | 验证命令 |\n| --- | --- | --- | --- |\n| `<INV-1>` | `<待填>` | `<x>` | `<t>` |\n")
    assert _is_feature_map_template(fmap), "FEATURE_MAP with 编号 header falsely marked non-template"

    t_repo = work / "pf-tmpl"
    _seed_repo(t_repo)
    _write(t_repo / "FEATURE_MAP.md", fmap.read_text(encoding="utf-8"))
    _write(t_repo / ".loop" / "handoff.md", _handoff_text(
        "v0.1", "feat/v0.1-pf", _git(t_repo, "rev-parse", "main").stdout.strip(),
        feat_row="| `FEATURE_MAP.md` | `本轮无适用变更` | 仍保留初始占位行 |",
    ))
    assert _git(t_repo, "add", ".").returncode == 0 and _git(t_repo, "commit", "-m", "template bypass attempt").returncode == 0
    code, rep = _ops("preflight", "--cwd", str(t_repo), cwd=t_repo)
    assert code == 1 and any("[DOCS_TEMPLATE_ONLY]" in v for v in rep["violations"]), rep

    extra_sec = "## 调研与边界\n\n| 分支 | 备注 |\n| --- | --- |\n| `wrong/branch` | 表外引用 |\n\n| `DECISIONS.md` | `已更新` | 表外引用 |\n\n"
    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf", base_sha, extra_sec=extra_sec))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0 and _git(repo, "commit", "-m", "lower table non-polluting").returncode == 0
    assert _ops("preflight", "--cwd", str(repo), cwd=repo)[0] == 0

    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf", base_sha, extra_top="| 分支 | `feat/v0.1-pf` |\n", evidence=""))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0 and _git(repo, "commit", "-m", "dup meta and no evidence").returncode == 0
    st = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st["recommended_next"] == "/loop-build", st
    pf_bad = _ops("preflight", "--cwd", str(repo), cwd=repo)[1]
    assert any("[HANDOFF_DUPLICATE_FIELD]" in v for v in pf_bad["violations"]) and any("[HANDOFF_EVIDENCE_MISSING]" in v for v in pf_bad["violations"]), pf_bad

    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf", base_sha, dec_row="| `DECISIONS.md` | `已更新` | A |\n| `DECISIONS.md` | `已更新` | B |"))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0 and _git(repo, "commit", "-m", "dup doc sync").returncode == 0
    assert any("[DOC_SYNC_DUPLICATE]" in v for v in _ops("preflight", "--cwd", str(repo), cwd=repo)[1]["violations"])

    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf", base_sha))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0 and _git(repo, "commit", "-m", "clean again").returncode == 0
    code, rec_conf = _ops("review-record", "--cwd", str(repo), "--verdict", "pass", "--decisions-status", "no_applicable_change", cwd=repo, stdin_text="# fail\n")
    assert code == 1 and any("[DOC_SYNC_CONFLICT]" in v for v in rec_conf["violations"]), rec_conf


def _check_status_pass_stale_and_allow_diff(work: pathlib.Path, repo: pathlib.Path) -> None:
    from devloop_git import sha256_file
    code, r1 = _ops("review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "method", cwd=repo, stdin_text="# r1\nbug.\n")
    assert code == 0 and r1["reject_count"] == 1, r1
    r1_path = pathlib.Path(r1["record"]["path"])
    r1_hash = sha256_file(r1_path)

    _write(repo / "app.py", "def run():\n    return 2\n")
    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf (base old)", _git(repo, "rev-parse", "main").stdout.strip()))
    assert _git(repo, "add", "app.py", ".loop/handoff.md").returncode == 0
    assert _git(repo, "commit", "-m", "fix with bad branch remark").returncode == 0

    pf_d, st_d = _ops("preflight", "--cwd", str(repo), cwd=repo)[1], _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    rec_d = _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# fail\n")[1]
    for rep in (pf_d, rec_d):
        assert any("[HANDOFF_BRANCH_MISMATCH]" in v for v in rep["violations"]), rep
    assert not st_d["preflight_passed"] and st_d["recommended_next"] == "/loop-build", st_d
    assert [p.name for p in r1_path.parent.iterdir()] == ["r01-reject.md"] and sha256_file(r1_path) == r1_hash
    _check_post_pass_drift(repo)


def _check_post_pass_drift(repo: pathlib.Path) -> None:
    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf", _git(repo, "rev-parse", "main").stdout.strip()))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0
    assert _git(repo, "commit", "-m", "fix clean handoff").returncode == 0
    assert _ops("review-record", "--cwd", str(repo), "--verdict", "pass", cwd=repo, stdin_text="# r2\nok.\n")[0] == 0

    _write(repo / ".loop" / "handoff.md", (repo / ".loop" / "handoff.md").read_text(encoding="utf-8") + "\n# retro note\n")
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0
    assert _git(repo, "commit", "-m", "handoff only post pass").returncode == 0
    assert _ops("review-status", "--cwd", str(repo), cwd=repo)[1]["recommended_next"] == "/loop-ship"

    code, gate_forbid = _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--allow-diff", "app.py", "--ports", "none", cwd=repo)
    assert code == 1 and any("[ALLOW_DIFF_FORBIDDEN]" in v for v in gate_forbid["violations"]), gate_forbid

    _write(repo / "DECISIONS.md", (repo / "DECISIONS.md").read_text(encoding="utf-8") + "\n2026-11-26 | [active] | 选择：B | 放弃：A | 触发：C\n")
    st_drift = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st_drift["pass_stale"] is True and st_drift["recommended_next"] == "/loop-build", st_drift
    assert _git(repo, "add", "DECISIONS.md").returncode == 0
    assert _git(repo, "commit", "-m", "commit doc drift").returncode == 0
    st_committed = _ops("review-status", "--cwd", str(repo), cwd=repo)[1]
    assert st_committed["recommended_next"] == "/loop-review" and st_committed["preflight_passed"], st_committed


def _check_external_pending_and_legacy(work: pathlib.Path, ext_repo: pathlib.Path, snap: pathlib.Path, base0: str) -> None:
    from devloop_review_io import normalize_body
    from devloop_review_store import body_sha256, format_review_doc

    _write(ext_repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-ext", base0))
    assert _ops("review-record", "--cwd", str(ext_repo), "--mode", "external", "--docs-root", str(snap), "--verdict", "pending", cwd=ext_repo, stdin_text="# pending\nwait.\n")[0] == 0
    assert _ops("review-record", "--cwd", str(ext_repo), "--mode", "external", "--docs-root", str(snap), "--verdict", "pending", cwd=ext_repo, stdin_text="# pending\nwait.\n")[1]["idempotent"] is True
    assert _ops("review-status", "--cwd", str(ext_repo), "--mode", "external", "--docs-root", str(snap), cwd=ext_repo)[1]["recommended_next"] == "manual_browser_verify"

    _write(snap / ".loop" / "DECISIONS.md", "# DECISIONS\n\n2026-11-26 | [active] | 选择：E3 | 放弃：B | 触发：C\n")
    st_doc = _ops("review-status", "--cwd", str(ext_repo), "--mode", "external", "--docs-root", str(snap), cwd=ext_repo)[1]
    assert st_doc["recommended_next"] == "/loop-review" and st_doc["preflight_passed"] is True, st_doc
    c_p2, r_p2 = _ops("review-record", "--cwd", str(ext_repo), "--mode", "external", "--docs-root", str(snap), "--verdict", "pending", cwd=ext_repo, stdin_text="# pending\nwait.\n")
    assert c_p2 == 0 and r_p2["idempotent"] is False and r_p2["record"]["round"] == 3, r_p2

    _write(ext_repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-ext", base0, evidence="`npm test -> exit 0`"))
    assert _ops("review-status", "--cwd", str(ext_repo), "--mode", "external", "--docs-root", str(snap), cwd=ext_repo)[1]["recommended_next"] == "/loop-review"

    leg = work / "legacy-pending"
    _seed_repo(leg, branch="feat/v0.1-leg")
    leg_head, st_leg0 = _git(leg, "rev-parse", "HEAD").stdout.strip(), _ops("review-status", "--cwd", str(leg), cwd=leg)[1]
    norm = normalize_body("# legacy pending\nmanual check.\n")
    meta = {"version": "v0.1", "branch": "feat/v0.1-leg", "round": 1, "verdict": "pending", "reviewed_sha": leg_head, "reviewer": "loop-review", "created_at": "2026-11-26T00:00:00Z", "body_sha256": body_sha256(norm), "summary": "legacy"}
    _write(pathlib.Path(st_leg0["scope_dir"]) / "r01-pending.md", format_review_doc(meta, norm))
    c_leg, st_leg = _ops("review-status", "--cwd", str(leg), cwd=leg)
    assert c_leg == 0 and st_leg["corrupt_records"] == [] and st_leg["recommended_next"] == "manual_browser_verify", st_leg


def _check_doc_unchanged_and_external_drift(work: pathlib.Path, repo: pathlib.Path) -> None:
    unch_base = _git(repo, "rev-parse", "HEAD~1").stdout.strip()
    _write(repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-pf", unch_base))
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0 and _git(repo, "commit", "-m", "unchanged baseline doc test").returncode == 0
    code_u, rep_u = _ops("preflight", "--cwd", str(repo), cwd=repo)
    assert code_u == 1 and any("[DOC_UNCHANGED]" in v for v in rep_u["violations"]), rep_u

    ext_repo, snap = work / "pf-ext-repo", work / "pf-ext-snap"
    ext_repo.mkdir(parents=True, exist_ok=True)
    assert _git(ext_repo, "init", "-b", "main").returncode == 0
    _git(ext_repo, "config", "user.name", "Test Bot")
    _git(ext_repo, "config", "user.email", "test@users.noreply.github.com")
    _write(ext_repo / "app.py", "x = 1\n")
    assert _git(ext_repo, "add", "app.py").returncode == 0 and _git(ext_repo, "commit", "-m", "init").returncode == 0
    b0 = _git(ext_repo, "rev-parse", "HEAD").stdout.strip()
    assert _ops("init", "--cwd", str(ext_repo), "--mode", "external", cwd=ext_repo)[0] == 0
    assert _git(ext_repo, "checkout", "-b", "feat/v0.1-ext").returncode == 0
    _write(ext_repo / "app.py", "x = 2\n")
    assert _git(ext_repo, "add", "app.py").returncode == 0 and _git(ext_repo, "commit", "-m", "cand").returncode == 0
    _write(snap / ".loop" / "DECISIONS.md", "# DECISIONS\n\n2026-11-26 | [active] | 选择：E | 放弃：B | 触发：C\n")
    _write(snap / ".loop" / "FEATURE_MAP.md", "# F\n\n| 版本 | 功能 | 入口 | 验证 |\n| --- | --- | --- | --- |\n| v0.1 | E | `e` | `t` |\n")
    _write(ext_repo / ".loop" / "handoff.md", _handoff_text("v0.1", "feat/v0.1-ext", b0))
    assert _ops("review-record", "--cwd", str(ext_repo), "--mode", "external", "--docs-root", str(snap), "--verdict", "pass", cwd=ext_repo, stdin_text="# ext\nok.\n")[0] == 0
    _write(snap / ".loop" / "DECISIONS.md", "# DECISIONS\n\n2026-11-26 | [active] | 选择：E2 | 放弃：B | 触发：C\n")
    st_ext = _ops("review-status", "--cwd", str(ext_repo), "--mode", "external", "--docs-root", str(snap), cwd=ext_repo)[1]
    assert st_ext["pass_stale"] is True and st_ext["recommended_next"] == "/loop-review", st_ext
    _check_external_pending_and_legacy(work, ext_repo, snap, b0)


def check_preflight_and_status(work: pathlib.Path) -> str:
    repo, base_sha, cand_sha = _check_empty_and_first_pass(work)
    cand_sha = _check_baseline_and_branch_errors(repo, base_sha, cand_sha)
    _check_detached_and_archive_guards(work, repo, cand_sha)
    _check_handoff_and_template_edges(work, repo, base_sha)
    _check_status_pass_stale_and_allow_diff(work, repo)
    _check_doc_unchanged_and_external_drift(work, repo)
    return "preflight: empty/baseline/rename/detached/templates/duplicates/pass_stale/allow_diff verified"
