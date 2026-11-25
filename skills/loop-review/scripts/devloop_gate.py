#!/usr/bin/env python3
"""Two-phase mechanical release gate (`pre-merge` and `post-tag`) for devloop. Stdlib only."""

from __future__ import annotations

import json
import pathlib
import re

from devloop_docs import resolve_mode, verify_stamp_and_docs
from devloop_git import is_tracked, resolve_git_root, run_git
from reap_and_run import DEFAULT_PORTS_SPEC, probe_ports

DEFAULT_ALLOW_DIFF = frozenset({
    "DECISIONS.md", "FEATURE_MAP.md", ".loop/handoff.md",
    ".loop/review-stamp.json", "package.json", "pyproject.toml",
})


def _resolve_ports(cwd: pathlib.Path, cli_ports: str) -> str:
    if cli_ports.strip():
        return cli_ports.strip()
    cfg = cwd / ".loop" / "config.json"
    if cfg.is_file():
        try:
            loaded = json.loads(cfg.read_text(encoding="utf-8-sig"))
            val = loaded.get("ports") if isinstance(loaded, dict) else ""
            if isinstance(val, str) and val.strip():
                return val.strip()
        except (OSError, ValueError):
            pass
    return DEFAULT_PORTS_SPEC


def _version_matches_tag(version: str, tag: str) -> bool:
    v_norm = version.strip().lstrip("v")
    t_norm = tag.strip().lstrip("v")
    if v_norm == t_norm:
        return True
    v_core, v_pre = (v_norm.split("-", 1) + [""])[:2]
    t_core, t_pre = (t_norm.split("-", 1) + [""])[:2]
    if v_pre != t_pre:
        return False
    v_tokens, t_tokens = v_core.split("."), t_core.split(".")
    if not all(x.isdigit() for x in v_tokens) or not all(x.isdigit() for x in t_tokens):
        return False
    v_parts, t_parts = [int(x) for x in v_tokens], [int(x) for x in t_tokens]
    size = max(len(v_parts), len(t_parts))
    v_parts += [0] * (size - len(v_parts))
    t_parts += [0] * (size - len(t_parts))
    return v_parts == t_parts


def _check_manifest_versions(cwd: pathlib.Path, tag: str) -> list[str]:
    if not tag:
        return []
    violations: list[str] = []
    pkg = cwd / "package.json"
    if pkg.is_file():
        try:
            ver = str(json.loads(pkg.read_text(encoding="utf-8-sig")).get("version", ""))
            if ver and not _version_matches_tag(ver, tag):
                violations.append(f"[VERSION_MISMATCH] package.json version={ver} vs tag={tag}")
        except (OSError, ValueError) as exc:
            violations.append(f"[MANIFEST_ERROR] package.json unreadable: {exc}")
    pyproj = cwd / "pyproject.toml"
    if pyproj.is_file():
        match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', pyproj.read_text(encoding="utf-8-sig"))
        if match and not _version_matches_tag(match.group(1), tag):
            violations.append(f"[VERSION_MISMATCH] pyproject.toml version={match.group(1)} vs tag={tag}")
    return violations


def _check_reviewed_diff(git_root: pathlib.Path, reviewed_sha: str, allow_diff: set[str]) -> list[str]:
    proc = run_git(["merge-base", "--is-ancestor", reviewed_sha, "HEAD"], git_root)
    if proc is None or proc.returncode != 0:
        return [f"[NOT_ANCESTOR] reviewed_sha {reviewed_sha} is absent from HEAD ancestry"]
    diff_proc = run_git(["diff", "--name-only", f"{reviewed_sha}..HEAD"], git_root)
    if diff_proc is None or diff_proc.returncode != 0:
        return [f"[DIFF_ERROR] git diff failed for {reviewed_sha}..HEAD"]
    blocked = [p for p in (line.strip() for line in diff_proc.stdout.splitlines()) if p and p not in allow_diff]
    return [f"[UNREVIEWED_DIFF] {p}: outside release diff whitelist" for p in blocked]


def _check_worktree_and_ports(cwd: pathlib.Path, git_root: pathlib.Path, mode: str, ports: str) -> list[str]:
    violations: list[str] = []
    status_proc = run_git(["status", "--porcelain"], git_root)
    if status_proc is None or status_proc.returncode != 0:
        violations.append("[GIT_ERROR] git status failed")
    elif status_proc.stdout.strip():
        first = status_proc.stdout.strip().splitlines()[0]
        violations.append(f"[DIRTY_WORKTREE] uncommitted files present: {first}")
    if mode == "external" and is_tracked(git_root, ".loop"):
        violations.append("[EXTERNAL_LEAK] .loop/ is tracked in external mode")
    if (cwd / ".tmp").exists():
        violations.append("[TMP_RESIDUAL] .tmp directory present at repository root")
    eff_ports = _resolve_ports(cwd, ports)
    if eff_ports.lower() != "none":
        try:
            active = probe_ports(eff_ports)
            if active:
                violations.append(f"[PORTS_ACTIVE] active listeners on {eff_ports}: {active}")
        except (ValueError, RuntimeError) as exc:
            violations.append(f"[PORT_PROBE_ERROR] {exc}")
    return violations


def _run_pre_merge(cwd: pathlib.Path, git_root: pathlib.Path, mode: str, tag: str,
                   reviewed_sha: str, stamp_path: pathlib.Path, ports: str, extra_allow: list[str]) -> dict:
    violations = _check_worktree_and_ports(cwd, git_root, mode, ports)
    violations += _check_manifest_versions(cwd, tag)
    eff_sha, stamp_errs = verify_stamp_and_docs(cwd, mode, stamp_path, reviewed_sha)
    violations += stamp_errs
    if eff_sha:
        violations += _check_reviewed_diff(git_root, eff_sha, set(DEFAULT_ALLOW_DIFF) | set(extra_allow))
    else:
        violations.append("[REVIEW_SHA_MISSING] reviewed_sha required via --reviewed-sha or review stamp")
    return {
        "status": "FAILED" if violations else "PASSED",
        "phase": "pre-merge",
        "mode": mode,
        "violations": violations,
    }


def _check_tag_target(git_root: pathlib.Path, tag: str) -> list[str]:
    verify = run_git(["rev-parse", "-q", "--verify", f"refs/tags/{tag}"], git_root)
    if verify is None or verify.returncode != 0:
        return [f"[TAG_MISSING] tag {tag} missing"]
    tag_commit = run_git(["rev-parse", f"{tag}^{{commit}}"], git_root)
    head_commit = run_git(["rev-parse", "HEAD"], git_root)
    if not (tag_commit and head_commit and tag_commit.returncode == 0 and head_commit.returncode == 0):
        return [f"[TAG_RESOLVE_ERROR] failed to dereference {tag} or HEAD"]
    if tag_commit.stdout.strip() != head_commit.stdout.strip():
        return [
            f"[TAG_HEAD_MISMATCH] {tag} points to {tag_commit.stdout.strip()[:12]} "
            f"vs HEAD {head_commit.stdout.strip()[:12]}"
        ]
    return []


def _run_post_tag(git_root: pathlib.Path, tag: str, branch: str) -> dict:
    if not tag:
        return {"status": "FAILED", "phase": "post-tag", "violations": ["[TAG_REQUIRED] --tag required for post-tag"]}
    violations = _check_tag_target(git_root, tag)
    st = run_git(["status", "--porcelain"], git_root)
    if st is None or st.returncode != 0 or st.stdout.strip():
        violations.append("[DIRTY_WORKTREE] working tree must be clean after tagging")
    if branch:
        br = run_git(["rev-parse", "--abbrev-ref", "HEAD"], git_root)
        if br is None or br.returncode != 0 or br.stdout.strip() != branch:
            violations.append(f"[BRANCH_MISMATCH] current branch differs from {branch}")
    return {"status": "FAILED" if violations else "PASSED", "phase": "post-tag", "tag": tag, "violations": violations}


def run_release_gate(
    cwd: pathlib.Path, phase: str, mode: str = "", tag: str = "", reviewed_sha: str = "",
    stamp: str = "", ports: str = "", allow_diff: list[str] | None = None, branch: str = "",
) -> tuple[int, dict]:
    cwd = cwd.expanduser().resolve()
    status, git_root = resolve_git_root(cwd)
    if status != "ok" or git_root is None:
        return 2, {"status": "FAILED", "phase": phase, "violations": [f"[NOT_GIT] git root unavailable ({status})"]}
    eff_mode = resolve_mode(cwd, mode)
    stamp_path = pathlib.Path(stamp).expanduser().resolve() if stamp else (cwd / ".loop" / "review-stamp.json")
    if phase == "pre-merge":
        report = _run_pre_merge(cwd, git_root, eff_mode, tag, reviewed_sha, stamp_path, ports, allow_diff or [])
    elif phase == "post-tag":
        report = _run_post_tag(git_root, tag, branch)
    else:
        return 2, {"status": "FAILED", "phase": phase, "violations": [f"[INVALID_PHASE] {phase}"]}
    return (0 if report["status"] == "PASSED" else 1), report
