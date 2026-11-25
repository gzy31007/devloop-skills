#!/usr/bin/env python3
"""Isolated self-tests for review attestation, two-phase release-gate, and publish_devloop."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import socket
import subprocess
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent
SKILLS = SCRIPTS.parents[1]
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


def _init_git_repo(repo: pathlib.Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@users.noreply.github.com")
    _write(repo / "app.py", "def run():\n    return 1\n")
    assert _git(repo, "add", "app.py").returncode == 0
    assert _git(repo, "commit", "-m", "init").returncode == 0


def _ops(*args: str, cwd: pathlib.Path, stdin_text: str | None = None) -> tuple[int, dict]:
    proc = _run([sys.executable, str(DEVLOOP_OPS), *args, "--json"], cwd, stdin_text=stdin_text)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_handoff(repo: pathlib.Path, version: str, branch: str) -> None:
    text = (
        f"# 交接\n\n| 字段 | 内容 |\n| --- | --- |\n"
        f"| 版本 / 主题 | `{version} / gate` |\n| 分支 | `{branch}` |\n| 改动范围 | `app.py` |\n"
    )
    _write(repo / ".loop" / "handoff.md", text)


def _check_doc_attestation_edges(work: pathlib.Path, repo: pathlib.Path) -> None:
    _seed_handoff(repo, "v0.1", "main")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-m", "seed handoff").returncode == 0

    code, rep = _ops(
        "review-record", "--cwd", str(repo), "--verdict", "pass",
        "--decisions-status", "updated", "--decisions-summary", "claimed updated on raw template",
        "--feature-map-status", "updated", "--feature-map-summary", "claimed updated on raw template",
        cwd=repo, stdin_text="# 结论\n全绿放行。\n",
    )
    assert code == 1 and any("[DOCS_TEMPLATE_ONLY]" in v for v in rep["violations"]), rep

    snap = work / "blind-snap"
    _write(snap / ".loop" / "DECISIONS.md", "# DECISIONS\n\n2026-11-25 | [active] | 选择：A | 放弃：B | 触发：C\n")
    _write(snap / ".loop" / "FEATURE_MAP.md", "# F\n\n| 版本 | 功能 | 入口 | 验证 |\n| --- | --- | --- | --- |\n| v0.1 | X | `x` | `t` |\n")
    code, fp = _ops("docs-fingerprint", "--cwd", str(repo), "--mode", "external", "--docs-root", str(snap), cwd=repo)
    assert code == 0 and not fp["decisions_is_template"] and not fp["feature_map_is_template"], fp

    crlf_snap = work / "blind-snap-crlf"
    (crlf_snap / ".loop").mkdir(parents=True, exist_ok=True)
    (crlf_snap / ".loop" / "DECISIONS.md").write_bytes(b"\xef\xbb\xbf" + (snap / ".loop" / "DECISIONS.md").read_bytes().replace(b"\n", b"\r\n"))
    (crlf_snap / ".loop" / "FEATURE_MAP.md").write_bytes((snap / ".loop" / "FEATURE_MAP.md").read_bytes().replace(b"\n", b"\r\n"))
    code_c, fp_c = _ops("docs-fingerprint", "--cwd", str(repo), "--mode", "external", "--docs-root", str(crlf_snap), cwd=repo)
    assert code_c == 0 and fp_c["decisions_sha256"] == fp["decisions_sha256"] and fp_c["feature_map_sha256"] == fp["feature_map_sha256"]

    code, rep = _ops(
        "review-record", "--cwd", str(repo), "--mode", "external", "--docs-root", str(snap),
        "--verdict", "pass", "--decisions-status", "updated", "--decisions-summary", "声称已更新但指纹未变",
        "--feature-map-status", "updated", "--feature-map-summary", "新增条目",
        "--baseline-decisions-sha", fp["decisions_sha256"],
        cwd=repo, stdin_text="# 结论\n测试基线未变拦截。\n",
    )
    assert code == 1 and any("[DOC_UNCHANGED]" in v for v in rep["violations"]), rep


def _check_reject_and_strict_whitelist(repo: pathlib.Path, port: str) -> None:
    code, rep = _ops(
        "review-record", "--cwd", str(repo), "--verdict", "reject", "--category", "method",
        cwd=repo, stdin_text="# 打回\n追加 reject 阻断已旧 pass。\n",
    )
    assert code == 0 and rep["record"]["round"] == 2, rep
    code, rep = _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--tag", "v0.1", "--ports", port, cwd=repo)
    assert code == 1 and any("[REVIEW_NOT_PASSED]" in v for v in rep["violations"]), rep

    code, rep = _ops(
        "review-record", "--cwd", str(repo), "--verdict", "pass",
        "--decisions-status", "updated", "--decisions-summary", "新增 SQLite 决策条目",
        "--feature-map-status", "updated", "--feature-map-summary", "新增 v0.1 CLI 启动行",
        cwd=repo, stdin_text="# 放行\n重新放行 r03。\n",
    )
    assert code == 0 and rep["record"]["round"] == 3, rep
    _write(repo / "package.json", json.dumps({"name": "demo", "version": "0.1.0", "extra": True}) + "\n")
    assert _git(repo, "add", "package.json").returncode == 0
    assert _git(repo, "commit", "-m", "unreviewed manifest edit").returncode == 0
    code, rep = _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--tag", "v0.1", "--ports", port, cwd=repo)
    assert code == 1 and any("[UNREVIEWED_DIFF] package.json" in v for v in rep["violations"]), rep


def check_ops_review_and_release_gate(work: pathlib.Path) -> str:
    repo = work / "gate-repo"
    _init_git_repo(repo)
    assert _ops("init", "--cwd", str(repo), "--mode", "own", cwd=repo)[0] == 0
    _write(repo / "package.json", json.dumps({"name": "demo", "version": "0.1.0"}) + "\n")
    _check_doc_attestation_edges(work, repo)

    assert _git(repo, "checkout", "-b", "feat/v0.1-gate").returncode == 0
    _write(repo / "DECISIONS.md", "# DECISIONS\n\n2026-11-25 | [active] | 选择：SQLite | 放弃：JSON | 触发：并发上升\n")
    _write(repo / "FEATURE_MAP.md", "# FEATURE_MAP\n\n| 版本 | 功能 | 入口 | 验证 |\n| --- | --- | --- | --- |\n| v0.1 | CLI 启动 | `app.py` | `pytest` |\n")
    _seed_handoff(repo, "v0.1", "feat/v0.1-gate")
    assert _git(repo, "add", "DECISIONS.md", "FEATURE_MAP.md", ".loop/handoff.md").returncode == 0
    assert _git(repo, "commit", "-m", "feat: v0.1 docs").returncode == 0
    cand_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    code, rep = _ops(
        "review-record", "--cwd", str(repo), "--verdict", "pass", "--reviewed-sha", cand_sha,
        "--decisions-status", "updated", "--decisions-summary", "新增 SQLite 决策条目",
        "--feature-map-status", "updated", "--feature-map-summary", "新增 v0.1 CLI 启动行",
        cwd=repo, stdin_text="# 全绿放行\n核验通过。\n",
    )
    assert code == 0 and rep["record"]["reviewed_sha"] == cand_sha, rep
    _write(repo / ".loop" / "handoff.md", (repo / ".loop" / "handoff.md").read_text(encoding="utf-8") + "\n# retro note\n")
    assert _git(repo, "add", ".loop/handoff.md").returncode == 0
    assert _git(repo, "commit", "-m", "docs: retro handoff").returncode == 0

    port = str(_free_port())
    code, rep = _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--tag", "v0.1", "--ports", port, cwd=repo)
    assert code == 0 and rep["status"] == "PASSED", rep

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", int(port)))
        busy.listen(1)
        code, rep = _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--tag", "v0.1", "--ports", port, cwd=repo)
        assert code == 1 and any("[PORTS_ACTIVE]" in v for v in rep["violations"]), rep

    code, rep = _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--tag", "v0.2", "--ports", port, cwd=repo)
    assert code == 1 and any("[VERSION_MISMATCH]" in v for v in rep["violations"]), rep

    assert _git(repo, "tag", "-a", "v0.1", "-m", "release v0.1").returncode == 0
    code, rep = _ops("release-gate", "--cwd", str(repo), "--phase", "post-tag", "--tag", "v0.1", "--branch", "feat/v0.1-gate", cwd=repo)
    assert code == 0 and rep["status"] == "PASSED", rep

    _check_reject_and_strict_whitelist(repo, port)

    _write(repo / "DECISIONS.md", (repo / "DECISIONS.md").read_text(encoding="utf-8") + "\n2026-11-25 | [active] | tampered |\n")
    _write(repo / "app.py", "def run():\n    return 2\n")
    assert _git(repo, "add", "DECISIONS.md", "app.py").returncode == 0
    assert _git(repo, "commit", "-m", "unreviewed code and doc drift").returncode == 0
    code, rep = _ops("release-gate", "--cwd", str(repo), "--phase", "pre-merge", "--tag", "v0.1", "--ports", port, cwd=repo)
    assert code == 1 and any("[UNREVIEWED_DIFF]" in v for v in rep["violations"]), rep
    assert any("[DOC_FINGERPRINT_MISMATCH]" in v for v in rep["violations"]), rep
    code, rep = _ops("release-gate", "--cwd", str(repo), "--phase", "post-tag", "--tag", "v0.1", cwd=repo)
    assert code == 1 and any("[TAG_HEAD_MISMATCH]" in v for v in rep["violations"]), rep
    return "review-record pass/reject-after-pass, narrowed whitelist & two-phase release-gate verified"


def _seed_publish_pair(work: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    bare = work / "remote.git"
    assert _run(["git", "init", "--bare", str(bare)], work).returncode == 0
    local = work / "pub-local"
    assert _run(["git", "init", "-b", "main", str(local)], work).returncode == 0
    _git(local, "config", "user.name", "Test Bot")
    _git(local, "config", "user.email", "test@users.noreply.github.com")
    _git(local, "remote", "add", "origin", str(bare))
    _write(local / "README.md", "# seed\n")
    _write(local / "skills" / "obsolete.md", "old\n")
    assert _git(local, "add", ".").returncode == 0
    assert _git(local, "commit", "-m", "seed").returncode == 0
    assert _git(local, "push", "-u", "origin", "main").returncode == 0
    return bare, local


def check_publish_devloop_unit(work: pathlib.Path) -> str:
    pub_script = SKILLS.parents[2] / "scripts" / "publish_devloop.py"
    if not pub_script.is_file():
        return "publish_devloop: skipped (standalone kit checkout)"
    spec = importlib.util.spec_from_file_location("publish_devloop_test", pub_script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    synthetic_tok = "ghp" + "_SYNTHETICTESTVALUE123456"
    secret_file = _write(work / "leak.txt", f"token = {synthetic_tok}\n")
    hits = mod.scan_file_secrets(secret_file, "leak.txt")
    assert hits == ["leak.txt:1: token_prefix"] and "SYNTHETICTESTVALUE" not in hits[0], hits

    long_md = _write(work / "long_para.md", "# Title\n\nline1\nline2\nline3\nline4\nline5\n\n```text\na\nb\nc\nd\ne\n```\n")
    p_hits = mod.scan_file_phrases(long_md, "long_para.md", check_style=True)
    assert p_hits == ["long_para.md:7: paragraph_too_long"], p_hits
    ok_md = _write(work / "ok_para.md", "# Title\n\nline1\nline2\nline3\nline4\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n| 7 | 8 |\n")
    assert mod.scan_file_phrases(ok_md, "ok_para.md", check_style=True) == []

    bare, local = _seed_publish_pair(work)
    allowed = frozenset({str(bare)})
    assert mod.publish(dry_run=False, kit_source=SKILLS, publish_repo=local, allowed_urls=frozenset({"https://example.invalid/x.git"}), skip_fetch=True) == 2
    assert mod.publish(dry_run=True, kit_source=SKILLS, publish_repo=local, allowed_urls=allowed, skip_fetch=True) == 0
    assert _git(local, "checkout", "--", ".").returncode == 0
    shutil.rmtree(local / "skills", ignore_errors=True)
    assert _git(local, "checkout", "--", "skills").returncode == 0
    assert mod.publish(dry_run=False, kit_source=SKILLS, publish_repo=local, allowed_urls=allowed, skip_fetch=True) == 0
    assert not (local / "skills" / "obsolete.md").exists()
    assert _git(local, "rev-parse", "HEAD").stdout.strip() == _git(bare, "rev-parse", "refs/heads/main").stdout.strip()

    assert _git(local, "commit", "--allow-empty", "-m", "simulate unpushed local commit").returncode == 0
    assert mod.publish(dry_run=False, kit_source=SKILLS, publish_repo=local, allowed_urls=allowed, skip_fetch=True) == 0
    assert _git(local, "rev-parse", "HEAD").stdout.strip() == _git(bare, "rev-parse", "refs/heads/main").stdout.strip()
    return "publish_devloop: redacted scan, 4-line paragraph guard, URL guard, scoped sync, ahead-retry verified"
