#!/usr/bin/env python3
"""Read-only status queries and preflight-coupled routing for devloop. Stdlib only."""

from __future__ import annotations

import pathlib

from devloop_docs import docs_fingerprint
from devloop_git import current_branch, head_sha, is_ancestor, is_worktree_clean, run_git, tags_at_head
from devloop_markers import INDEX_START
from devloop_preflight import resolve_review_roots, run_preflight
from devloop_review_store import (
    count_rejects,
    find_archived_pass_records,
    list_active_scope_keys,
    list_scope_records,
    make_scope_key,
    parse_handoff_meta,
    version_matches_tag,
)


def _pending_archive_scopes(reviews_root: pathlib.Path, current_scope: str) -> list[dict]:
    pending = []
    for key in list_active_scope_keys(reviews_root):
        if key == current_scope:
            continue
        recs, errs = list_scope_records(reviews_root / key)
        latest = recs[-1] if recs else {}
        pending.append({
            "scope_key": key,
            "version": latest.get("version", key.split("__", 1)[0]),
            "branch": latest.get("branch", ""),
            "latest_verdict": latest.get("verdict", "corrupt" if errs else "none"),
            "record_count": len(recs),
            "corrupt": bool(errs),
        })
    return pending


def _pass_is_fresh(roots: dict, latest: dict, cur_head: str) -> bool:
    latest_sha = latest.get("reviewed_sha", "")
    if not latest_sha or not cur_head:
        return False
    if latest_sha != cur_head:
        diff = run_git(["diff", "--name-only", f"{latest_sha}..HEAD"], roots["cwd_git"])
        if diff is None or diff.returncode != 0:
            return False
        changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
        if not all(path == ".loop/handoff.md" for path in changed):
            return False
    fp = docs_fingerprint(roots["cwd"], roots["mode"], roots["docs"])
    if not fp["decisions_exists"] or not fp["feature_map_exists"]:
        return False
    return (
        latest.get("decisions_sha256") == fp["decisions_sha256"]
        and latest.get("feature_map_sha256") == fp["feature_map_sha256"]
    )


def _recommend_for_no_records(roots: dict, ho_meta: dict) -> tuple[str, str, bool]:
    fp = docs_fingerprint(roots["cwd"], roots["mode"], roots["docs"])
    if fp["decisions_is_template"] or fp["feature_map_is_template"] or not ho_meta.get("version"):
        return "/loop-plan", "docs still template or version missing in handoff", False
    if ho_meta.get("has_evidence"):
        return "/loop-review", "handoff evidence present with no active review records", False
    return "/loop-build", "plan ready with no candidate handoff evidence yet", False


def _recommend_for_reject(latest: dict, counts: dict, cur_head: str, clean: bool) -> tuple[str, str, bool]:
    if counts["circuit_breaker_triggered"]:
        return "manual_decision", "circuit breaker triggered after 3+ rejections", False
    if latest["reviewed_sha"] != cur_head and clean:
        return "/loop-review", f"new candidate {cur_head[:12]} committed after r{latest['round']:02d} reject", False
    return "/loop-build", f"latest round r{latest['round']:02d} rejected ({latest.get('category', '')})", False


def _released_for_head(roots: dict, eff_ver: str, cur_head: str, pending: list[dict]) -> tuple[str, str, bool]:
    if not eff_ver.strip() or not cur_head:
        return "", "", False
    matched = [t for t in tags_at_head(roots["cwd"]) if version_matches_tag(eff_ver, t)]
    if not matched:
        return "", "", False
    rel_tag = eff_ver if eff_ver in matched else matched[0]
    same_ver = [item for item in pending if version_matches_tag(item["version"], eff_ver)]
    if same_ver:
        ok = any(item["latest_verdict"] == "pass" and not item["corrupt"] for item in same_ver)
        return (rel_tag, "", True) if ok else ("", "", False)
    for arch_key, rec in find_archived_pass_records(roots["state"] / ".loop" / "archive", eff_ver):
        if is_ancestor(roots["cwd"], rec["reviewed_sha"], cur_head):
            return rel_tag, arch_key, False
    return "", "", False


def _recommend_next(
    initialized: bool, corrupt: list[str], records: list[dict], counts: dict,
    roots: dict, cur_head: str, clean: bool, ho_meta: dict, eff_ver: str, pending: list[dict],
) -> tuple[str, str, bool, str, str]:
    if not initialized:
        return "/loop-init", "missing .loop/config.json or AGENTS.md marker", False, "", ""
    if corrupt:
        return "fix_corrupt_records", corrupt[0], False, "", ""
    if not records:
        rel_tag, arch_scope, need_arch = _released_for_head(roots, eff_ver, cur_head, pending)
        if need_arch:
            return "/loop-retro", f"tag {rel_tag} matches HEAD with unarchived pass scope; run review-archive", False, rel_tag, ""
        if arch_scope:
            return "/loop-plan", f"version {eff_ver} released at tag {rel_tag} and archived ({arch_scope}); start next version", False, rel_tag, arch_scope
        return (*_recommend_for_no_records(roots, ho_meta), "", "")
    latest = records[-1]
    verdict = latest["verdict"]
    if verdict == "reject":
        return (*_recommend_for_reject(latest, counts, cur_head, clean), "", "")
    if verdict == "pending":
        return "manual_browser_verify", "latest verdict pending manual browser verification", False, "", ""
    if _pass_is_fresh(roots, latest, cur_head):
        return "/loop-retro", f"latest round r{latest['round']:02d} passed for current candidate", False, "", ""
    return "/loop-review", "code or docs changed since latest pass record", True, "", ""


def _is_initialized(roots: dict) -> bool:
    if not (roots["state"] / ".loop" / "config.json").is_file():
        return False
    if roots["mode"] == "own":
        agents = roots["cwd"] / "AGENTS.md"
        return agents.is_file() and INDEX_START in agents.read_text(encoding="utf-8-sig", errors="replace")
    return True


def _safe_scope_key(eff_ver: str, eff_br: str) -> str:
    if not eff_ver or not eff_br:
        return ""
    try:
        return make_scope_key(eff_ver, eff_br)
    except ValueError:
        return ""


def _apply_preflight_gate(
    roots: dict, state_root: str, docs_root: str, version: str, branch: str, nxt: str, reason: str,
) -> tuple[str, str, bool, bool, list[str]]:
    if nxt != "/loop-review":
        return nxt, reason, False, False, []
    pf_code, pf = run_preflight(
        roots["cwd"], state_root=state_root, docs_root=docs_root,
        mode=roots["mode"], version=version, branch=branch,
    )
    pf_passed = pf_code == 0
    pf_violations = list(pf.get("violations", []))
    if pf_passed:
        return nxt, reason, True, True, []
    return "/loop-build", f"preflight failed ({len(pf_violations)} issue(s)); fix before /loop-review", True, False, pf_violations


def _status_for_git_repo(
    roots: dict, state_root: str, docs_root: str, version: str, branch: str,
) -> tuple[int, dict]:
    cur_head, cwd_br = head_sha(roots["cwd"]), current_branch(roots["cwd"])
    clean, _ = is_worktree_clean(roots["cwd_git"])
    ho_meta = parse_handoff_meta(roots["state"] / ".loop" / "handoff.md")
    eff_ver = version.strip() or ho_meta.get("version", "")
    eff_br = branch.strip() or cwd_br or ho_meta.get("branch", "")
    scope_key = _safe_scope_key(eff_ver, eff_br)
    reviews_root = roots["state"] / ".loop" / "reviews"
    records, corrupt = list_scope_records(reviews_root / scope_key) if scope_key else ([], [])
    counts = count_rejects(records)
    pending = _pending_archive_scopes(reviews_root, scope_key)
    nxt, reason, pass_stale, rel_tag, arch_scope = _recommend_next(
        _is_initialized(roots), corrupt, records, counts, roots, cur_head, bool(clean), ho_meta, eff_ver, pending,
    )
    nxt, reason, pf_checked, pf_passed, pf_violations = _apply_preflight_gate(
        roots, state_root, docs_root, version, branch, nxt, reason,
    )
    latest = records[-1] if records else None
    payload = {
        "status": "FAILED" if corrupt else "PASSED", "is_git": True,
        "git_root": str(roots["cwd_git"]), "mode": roots["mode"], "initialized": _is_initialized(roots),
        "head_sha": cur_head, "current_branch": cwd_br, "worktree_clean": bool(clean),
        "version": eff_ver, "branch": eff_br, "scope_key": scope_key,
        "scope_dir": str(reviews_root / scope_key) if scope_key else "",
        "active_records": records, "latest_record": latest,
        "latest_verdict": latest["verdict"] if latest else "none",
        "latest_reviewed_sha": latest["reviewed_sha"] if latest else "",
        "pass_stale": pass_stale, "released_tag": rel_tag, "archived_scope": arch_scope,
        "preflight_checked": pf_checked, "preflight_passed": pf_passed,
        "preflight_violations": pf_violations,
        "corrupt_records": corrupt, "pending_archive_scopes": pending,
        "recommended_next": nxt, "recommended_reason": reason, **counts,
    }
    return (1 if corrupt else 0), payload


def run_review_status(
    cwd: pathlib.Path, state_root: str = "", docs_root: str = "", mode: str = "",
    version: str = "", branch: str = "",
) -> tuple[int, dict]:
    eff_cwd = cwd.expanduser().resolve()
    if not eff_cwd.is_dir():
        return 2, {"status": "FAILED", "error": f"target directory missing: {eff_cwd}"}
    roots, root_errs = resolve_review_roots(eff_cwd, state_root, docs_root, mode)
    if root_errs or roots is None:
        return 0, {
            "status": "PASSED", "is_git": False, "initialized": False, "released_tag": "", "archived_scope": "",
            "preflight_checked": False, "preflight_passed": False, "preflight_violations": [],
            "recommended_next": "/loop-init", "recommended_reason": root_errs[0],
            "pending_archive_scopes": [], "active_records": [],
        }
    return _status_for_git_repo(roots, state_root, docs_root, version, branch)
