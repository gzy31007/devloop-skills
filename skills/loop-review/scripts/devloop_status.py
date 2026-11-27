#!/usr/bin/env python3
"""Read-only status queries and preflight-coupled routing for devloop. Stdlib only."""

from __future__ import annotations

import pathlib

from devloop_docs import docs_fingerprint
from devloop_git import current_branch, head_sha, is_ancestor, is_worktree_clean, resolve_commit_sha, run_git, tags_at_head
from devloop_markers import INDEX_START
from devloop_preflight import load_handoff_content, resolve_review_roots, run_preflight
from devloop_review_store import (count_rejects, find_archived_pass_records, list_active_scope_keys,
                                  list_scope_records, make_scope_key, parse_handoff_meta, version_matches_tag)
from devloop_ship import read_retro_meta, read_ship_receipt, scan_corrupt_meta


def _pending_archive_scopes(reviews_root: pathlib.Path, current_scope: str) -> list[dict]:
    pending = []
    for key in list_active_scope_keys(reviews_root):
        if key == current_scope:
            continue
        recs, errs = list_scope_records(reviews_root / key)
        latest = recs[-1] if recs else {}
        pending.append({
            "scope_key": key, "version": latest.get("version", key.split("__", 1)[0]),
            "branch": latest.get("branch", ""), "latest_verdict": latest.get("verdict", "corrupt" if errs else "none"),
            "latest_reviewed_sha": latest.get("reviewed_sha", ""), "record_count": len(recs), "corrupt": bool(errs),
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
    return latest.get("decisions_sha256") == fp["decisions_sha256"] and latest.get("feature_map_sha256") == fp["feature_map_sha256"]


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


def _pending_inputs_drifted(roots: dict, latest: dict, cur_head: str) -> bool:
    keys = ("handoff_sha256", "decisions_sha256", "feature_map_sha256")
    if not any(latest.get(k) for k in keys):
        return False
    _, ho_sha, ho_errs = load_handoff_content(roots, cur_head)
    fp = docs_fingerprint(roots["cwd"], roots["mode"], roots["docs"])
    cur = {"handoff_sha256": "" if ho_errs else ho_sha, "decisions_sha256": fp["decisions_sha256"], "feature_map_sha256": fp["feature_map_sha256"]}
    return any(latest.get(k) and latest.get(k) != cur[k] for k in keys)


def _recommend_for_pending(roots: dict, latest: dict, counts: dict, cur_head: str, clean: bool) -> tuple[str, str, bool]:
    if counts["circuit_breaker_triggered"]:
        return "manual_decision", "circuit breaker triggered after 3+ rejections", False
    if not clean:
        return "/loop-build", "worktree dirty after pending verdict; commit or clean first", False
    if latest["reviewed_sha"] != cur_head:
        return "/loop-review", f"new candidate {cur_head[:12]} committed after r{latest['round']:02d} pending", False
    if _pending_inputs_drifted(roots, latest, cur_head):
        return "/loop-review", f"handoff or docs changed after r{latest['round']:02d} pending", False
    return "manual_browser_verify", "latest verdict pending manual browser verification", False


def _valid_ship_for_head(cwd: pathlib.Path, reviews_root: pathlib.Path, scope_key: str, reviewed_sha: str, cur_head: str) -> tuple[str, str, str] | None:
    if not scope_key or not reviewed_sha or not cur_head:
        return None
    ship, err = read_ship_receipt(reviews_root, scope_key)
    if err or ship is None or ship["reviewed_sha"] != reviewed_sha:
        return None
    if ship["kind"] == "release_tag":
        tag_sha = resolve_commit_sha(cwd, f"refs/tags/{ship['tag']}")
        if tag_sha == ship["tag_sha"] and is_ancestor(cwd, reviewed_sha, tag_sha) and is_ancestor(cwd, tag_sha, cur_head):
            return ship["tag"], scope_key, "release_tag"
        return None
    if is_ancestor(cwd, reviewed_sha, cur_head):
        return "", scope_key, "pr_handoff"
    return None


def _shipped_candidates(scope_key: str, records: list[dict], pending: list[dict], eff_ver: str) -> list[tuple[str, str, str]]:
    cands: list[tuple[str, str, str]] = []
    if records and records[-1]["verdict"] == "pass":
        cands.append((scope_key, records[-1]["version"], records[-1]["reviewed_sha"]))
    for item in pending:
        if not item["corrupt"] and item["latest_verdict"] == "pass":
            if not eff_ver or version_matches_tag(item["version"], eff_ver):
                cands.append((item["scope_key"], item["version"], item["latest_reviewed_sha"]))
    return cands


def _find_shipped_scope(
    roots: dict, reviews_root: pathlib.Path, scope_key: str, records: list[dict],
    pending: list[dict], eff_ver: str, cur_head: str,
) -> tuple[str, str, str]:
    cands = _shipped_candidates(scope_key, records, pending, eff_ver)
    for key, _, rev_sha in cands:
        hit = _valid_ship_for_head(roots["cwd"], reviews_root, key, rev_sha, cur_head)
        if hit:
            return hit
    if not eff_ver.strip() or not cur_head:
        return "", "", ""
    matched = [t for t in tags_at_head(roots["cwd"]) if version_matches_tag(eff_ver, t)]
    if not matched:
        return "", "", ""
    rel_tag = eff_ver if eff_ver in matched else matched[0]
    if resolve_commit_sha(roots["cwd"], f"refs/tags/{rel_tag}") != cur_head:
        return "", "", ""
    for key, ver, rev_sha in cands:
        if version_matches_tag(ver, eff_ver) and rev_sha and is_ancestor(roots["cwd"], rev_sha, cur_head):
            return rel_tag, key, "release_tag"
    return "", "", ""


def _match_archived_entry(
    cwd: pathlib.Path, arch_key: str, rec: dict, rmeta: dict | None,
    eff_ver: str, cur_head: str, matched_tags: list[str],
) -> tuple[str, str, str] | None:
    if rmeta is None:
        if matched_tags and is_ancestor(cwd, rec["reviewed_sha"], cur_head):
            rel_tag = eff_ver if eff_ver in matched_tags else matched_tags[0]
            return rel_tag, arch_key, "release_tag"
        return None
    if rmeta["close_reason"] == "abandoned" or rmeta["reviewed_sha"] != rec["reviewed_sha"]:
        return None
    if rmeta["ship_kind"] == "pr_handoff":
        return ("", arch_key, "pr_handoff") if cur_head in (rmeta["reviewed_sha"], rmeta["retro_head_sha"]) else None
    tag_sha = resolve_commit_sha(cwd, f"refs/tags/{rmeta['tag']}")
    anc_ok = tag_sha == rmeta["tag_sha"] and is_ancestor(cwd, rec["reviewed_sha"], tag_sha)
    if anc_ok and cur_head in (tag_sha, rmeta["retro_head_sha"]):
        return rmeta["tag"], arch_key, "release_tag"
    return None


def _find_archived_scope(roots: dict, archive_root: pathlib.Path, eff_ver: str, cur_head: str) -> tuple[str, str, str]:
    if not eff_ver.strip() or not cur_head:
        return "", "", ""
    matched_tags = [t for t in tags_at_head(roots["cwd"]) if version_matches_tag(eff_ver, t)]
    for arch_key, rec in find_archived_pass_records(archive_root, eff_ver):
        rmeta, err = read_retro_meta(archive_root, arch_key)
        if err:
            continue
        hit = _match_archived_entry(roots["cwd"], arch_key, rec, rmeta, eff_ver, cur_head, matched_tags)
        if hit:
            return hit
    return "", "", ""


def _recommend_next(
    initialized: bool, corrupt: list[str], records: list[dict], counts: dict,
    roots: dict, scope_key: str, cur_head: str, clean: bool, ho_meta: dict, eff_ver: str, pending: list[dict],
) -> tuple[str, str, bool, str, str, str, str]:
    if not initialized:
        return "/loop-init", "missing .loop/config.json or AGENTS.md marker", False, "", "", "", ""
    if corrupt:
        return "fix_corrupt_records", corrupt[0], False, "", "", "", ""
    reviews_root, archive_root = roots["state"] / ".loop" / "reviews", roots["state"] / ".loop" / "archive"
    rel_tag, ship_scope, ship_kind = _find_shipped_scope(roots, reviews_root, scope_key, records, pending, eff_ver, cur_head)
    if ship_scope:
        return "/loop-retro", f"shipped scope {ship_scope} ({ship_kind}) awaiting /loop-retro", False, rel_tag, "", ship_scope, ship_kind
    if not records:
        a_tag, a_scope, a_kind = _find_archived_scope(roots, archive_root, eff_ver, cur_head)
        if a_scope:
            return "/loop-plan", f"version {eff_ver} archived ({a_scope}, {a_kind}); start next version", False, a_tag, a_scope, "", a_kind
        nxt, reason, stale = _recommend_for_no_records(roots, ho_meta)
        return nxt, reason, stale, "", "", "", ""
    latest = records[-1]
    if latest["verdict"] == "reject":
        return (*_recommend_for_reject(latest, counts, cur_head, clean), "", "", "", "")
    if latest["verdict"] == "pending":
        return (*_recommend_for_pending(roots, latest, counts, cur_head, clean), "", "", "", "")
    if _pass_is_fresh(roots, latest, cur_head):
        return "/loop-ship", f"latest round r{latest['round']:02d} passed; ready for /loop-ship", False, "", "", "", ""
    return "/loop-review", "code or docs changed since latest pass record", True, "", "", "", ""


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


def _apply_preflight_gate(roots: dict, state_root: str, docs_root: str, version: str, branch: str, nxt: str, reason: str) -> tuple[str, str, bool, bool, list[str]]:
    if nxt != "/loop-review":
        return nxt, reason, False, False, []
    pf_code, pf = run_preflight(roots["cwd"], state_root=state_root, docs_root=docs_root, mode=roots["mode"], version=version, branch=branch)
    pf_violations = list(pf.get("violations", []))
    if pf_code == 0:
        return nxt, reason, True, True, []
    return "/loop-build", f"preflight failed ({len(pf_violations)} issue(s)); fix before /loop-review", True, False, pf_violations


def _status_for_git_repo(roots: dict, state_root: str, docs_root: str, version: str, branch: str) -> tuple[int, dict]:
    cur_head, cwd_br = head_sha(roots["cwd"]), current_branch(roots["cwd"])
    clean, _ = is_worktree_clean(roots["cwd_git"])
    ho_meta = parse_handoff_meta(roots["state"] / ".loop" / "handoff.md")
    eff_ver, eff_br = version.strip() or ho_meta.get("version", ""), branch.strip() or cwd_br or ho_meta.get("branch", "")
    scope_key = _safe_scope_key(eff_ver, eff_br)
    reviews_root, archive_root = roots["state"] / ".loop" / "reviews", roots["state"] / ".loop" / "archive"
    records, corrupt = list_scope_records(reviews_root / scope_key) if scope_key else ([], [])
    corrupt += scan_corrupt_meta(reviews_root, archive_root)
    counts, pending = count_rejects(records), _pending_archive_scopes(reviews_root, scope_key)
    nxt, reason, pass_stale, rel_tag, arch_scope, ship_scope, ship_kind = _recommend_next(
        _is_initialized(roots), corrupt, records, counts, roots, scope_key, cur_head, bool(clean), ho_meta, eff_ver, pending,
    )
    nxt, reason, pf_checked, pf_passed, pf_violations = _apply_preflight_gate(roots, state_root, docs_root, version, branch, nxt, reason)
    latest = records[-1] if records else None
    payload = {
        "status": "FAILED" if corrupt else "PASSED", "is_git": True, "git_root": str(roots["cwd_git"]), "mode": roots["mode"],
        "initialized": _is_initialized(roots), "head_sha": cur_head, "current_branch": cwd_br, "worktree_clean": bool(clean),
        "version": eff_ver, "branch": eff_br, "scope_key": scope_key, "scope_dir": str(reviews_root / scope_key) if scope_key else "",
        "active_records": records, "latest_record": latest, "latest_verdict": latest["verdict"] if latest else "none",
        "latest_reviewed_sha": latest["reviewed_sha"] if latest else "", "pass_stale": pass_stale,
        "released_tag": rel_tag, "archived_scope": arch_scope, "shipped_scope": ship_scope, "ship_kind": ship_kind,
        "preflight_checked": pf_checked, "preflight_passed": pf_passed, "preflight_violations": pf_violations,
        "corrupt_records": corrupt, "pending_archive_scopes": pending, "recommended_next": nxt, "recommended_reason": reason, **counts,
    }
    return (1 if corrupt else 0), payload


def run_review_status(
    cwd: pathlib.Path, state_root: str = "", docs_root: str = "", mode: str = "", version: str = "", branch: str = "",
) -> tuple[int, dict]:
    eff_cwd = cwd.expanduser().resolve()
    if not eff_cwd.is_dir():
        return 2, {"status": "FAILED", "error": f"target directory missing: {eff_cwd}"}
    roots, root_errs = resolve_review_roots(eff_cwd, state_root, docs_root, mode)
    if root_errs or roots is None:
        return 0, {
            "status": "PASSED", "is_git": False, "initialized": False, "released_tag": "", "archived_scope": "",
            "shipped_scope": "", "ship_kind": "", "preflight_checked": False, "preflight_passed": False,
            "preflight_violations": [], "recommended_next": "/loop-init", "recommended_reason": root_errs[0],
            "pending_archive_scopes": [], "active_records": [],
        }
    return _status_for_git_repo(roots, state_root, docs_root, version, branch)
