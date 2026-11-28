#!/usr/bin/env python3
"""Two-phase mechanical release gate (`pre-merge` and `post-tag`) for devloop. Stdlib only."""

from __future__ import annotations

import json
import pathlib
import re

from devloop_git import current_branch, is_ancestor, resolve_commit_sha, run_git
from devloop_ports import is_none_ports, resolve_ports
from devloop_preflight import resolve_review_roots
from devloop_review_store import list_scope_records, normalize_version, parse_handoff_meta, version_matches_tag
from devloop_reviews import verify_review_pass_for_gate
from devloop_ship import (
    DEFAULT_ALLOW_DIFF,
    check_external_leaks_and_tmp,
    check_reviewed_diff_between,
    check_same_repo_and_clean,
    locate_active_scope,
)
from reap_and_run import probe_ports


def _check_manifest_versions(cwd: pathlib.Path, tag: str) -> list[str]:
    if not tag:
        return []
    violations: list[str] = []
    pkg = cwd / "package.json"
    if pkg.is_file():
        try:
            ver = str(json.loads(pkg.read_text(encoding="utf-8-sig")).get("version", ""))
            if ver and not version_matches_tag(ver, tag):
                violations.append(f"[VERSION_MISMATCH] package.json version={ver} vs tag={tag}")
        except (OSError, ValueError) as exc:
            violations.append(f"[MANIFEST_ERROR] package.json unreadable: {exc}")
    pyproj = cwd / "pyproject.toml"
    if pyproj.is_file():
        match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', pyproj.read_text(encoding="utf-8-sig"))
        if match and not version_matches_tag(match.group(1), tag):
            violations.append(f"[VERSION_MISMATCH] pyproject.toml version={match.group(1)} vs tag={tag}")
    return violations


def _check_ports(roots: dict, ports: str) -> list[str]:
    eff_ports, _ = resolve_ports(roots["cwd"], ports, fallback_cwd=roots["state"])
    if is_none_ports(eff_ports):
        return []
    try:
        active = probe_ports(eff_ports)
        return [f"[PORTS_ACTIVE] active listeners on {eff_ports}: {active}"] if active else []
    except (ValueError, RuntimeError) as exc:
        return [f"[PORT_PROBE_ERROR] {exc}"]


def _run_pre_merge(
    roots: dict, tag: str, version: str, branch: str, reviewed_sha: str,
    state_root: str, docs_root: str, ports: str, extra_allow: list[str],
) -> dict:
    cwd, mode = roots["cwd"], roots["mode"]
    violations = check_same_repo_and_clean(roots) + check_external_leaks_and_tmp(roots) + _check_ports(roots, ports)
    violations += _check_manifest_versions(cwd, tag)
    for item in extra_allow:
        norm = item.strip().replace("\\", "/")
        if norm and norm not in DEFAULT_ALLOW_DIFF:
            violations.append(f"[ALLOW_DIFF_FORBIDDEN] {item}: only .loop/handoff.md is permitted in release diff whitelist")
    eff_sha, review_errs = verify_review_pass_for_gate(
        cwd, state_root, mode, tag, version, branch, reviewed_sha, docs_root,
    )
    violations += review_errs
    if eff_sha:
        violations += check_reviewed_diff_between(roots["cwd_git"], eff_sha, "HEAD", DEFAULT_ALLOW_DIFF)
    elif not review_errs:
        violations.append("[REVIEW_SHA_MISSING] reviewed_sha required from active pass record")
    return {
        "status": "FAILED" if violations else "PASSED",
        "phase": "pre-merge",
        "mode": mode,
        "reviewed_sha": eff_sha,
        "violations": violations,
    }


def _check_tag_target(git_root: pathlib.Path, tag: str) -> tuple[str, list[str]]:
    verify = run_git(["rev-parse", "-q", "--verify", f"refs/tags/{tag}"], git_root)
    if verify is None or verify.returncode != 0:
        return "", [f"[TAG_MISSING] tag {tag} missing"]
    tag_commit = run_git(["rev-parse", f"{tag}^{{commit}}"], git_root)
    head_commit = run_git(["rev-parse", "HEAD"], git_root)
    if not (tag_commit and head_commit and tag_commit.returncode == 0 and head_commit.returncode == 0):
        return "", [f"[TAG_RESOLVE_ERROR] failed to dereference {tag} or HEAD"]
    tag_sha, head_s = tag_commit.stdout.strip(), head_commit.stdout.strip()
    if tag_sha != head_s:
        return tag_sha, [f"[TAG_HEAD_MISMATCH] {tag} points to {tag_sha[:12]} vs HEAD {head_s[:12]}"]
    return tag_sha, []


def _locate_pass_sha_for_post_tag(roots: dict, tag: str, version: str, branch: str) -> str:
    ho_meta = parse_handoff_meta(roots["state"] / ".loop" / "handoff.md")
    ho_ver = ho_meta.get("version", "")
    raw_ver = version.strip() or (ho_ver if (ho_ver and version_matches_tag(ho_ver, tag)) else tag.strip())
    try:
        eff_ver = normalize_version(raw_ver)
    except ValueError:
        return ""
    reviews_root = roots["state"] / ".loop" / "reviews"
    scope_key, _ = locate_active_scope(reviews_root, eff_ver, "", ho_meta.get("branch", ""), branch.strip() or current_branch(roots["cwd"]))
    if not scope_key:
        return ""
    records, errs = list_scope_records(reviews_root / scope_key)
    if errs or not records or records[-1]["verdict"] != "pass":
        return ""
    return records[-1]["reviewed_sha"] if version_matches_tag(records[-1]["version"], tag) else ""


def _resolve_post_tag_sha(roots: dict, tag: str, version: str, branch: str, reviewed_sha: str) -> tuple[str, list[str]]:
    if reviewed_sha.strip():
        resolved = resolve_commit_sha(roots["cwd"], reviewed_sha.strip())
        if not resolved:
            return "", [f"[REVIEW_SHA_INVALID] invalid --reviewed-sha: {reviewed_sha}"]
        return resolved, []
    return _locate_pass_sha_for_post_tag(roots, tag, version, branch), []


def _run_post_tag(
    roots: dict, tag: str, version: str, branch: str, reviewed_sha: str,
) -> dict:
    if not tag.strip():
        return {"status": "FAILED", "phase": "post-tag", "violations": ["[TAG_REQUIRED] --tag required for post-tag"]}
    tag_sha, violations = _check_tag_target(roots["cwd_git"], tag.strip())
    violations += check_same_repo_and_clean(roots)
    if branch.strip():
        br = run_git(["rev-parse", "--abbrev-ref", "HEAD"], roots["cwd_git"])
        if br is None or br.returncode != 0 or br.stdout.strip() != branch.strip():
            violations.append(f"[BRANCH_MISMATCH] current branch differs from {branch}")
    eff_sha, sha_errs = _resolve_post_tag_sha(roots, tag.strip(), version, branch, reviewed_sha)
    violations += sha_errs
    if eff_sha:
        if tag_sha and not is_ancestor(roots["cwd_git"], eff_sha, tag_sha):
            violations.append(f"[NOT_ANCESTOR] reviewed_sha {eff_sha} is absent from tag {tag}")
        violations += check_reviewed_diff_between(roots["cwd_git"], eff_sha, "HEAD", DEFAULT_ALLOW_DIFF)
    return {
        "status": "FAILED" if violations else "PASSED",
        "phase": "post-tag",
        "tag": tag,
        "reviewed_sha": eff_sha,
        "violations": violations,
    }


def run_release_gate(
    cwd: pathlib.Path, phase: str, mode: str = "", tag: str = "", version: str = "", branch: str = "",
    reviewed_sha: str = "", state_root: str = "", docs_root: str = "",
    ports: str = "", allow_diff: list[str] | None = None,
) -> tuple[int, dict]:
    if phase not in ("pre-merge", "post-tag"):
        return 2, {"status": "FAILED", "phase": phase, "violations": [f"[INVALID_PHASE] {phase}"]}
    roots, root_errs = resolve_review_roots(cwd, state_root, docs_root, mode)
    if root_errs or roots is None:
        return 2, {"status": "FAILED", "phase": phase, "violations": root_errs}
    if phase == "pre-merge":
        report = _run_pre_merge(
            roots, tag, version, branch, reviewed_sha, state_root, docs_root, ports, allow_diff or [],
        )
    else:
        report = _run_post_tag(roots, tag, version, branch, reviewed_sha)
    return (0 if report["status"] == "PASSED" else 1), report
