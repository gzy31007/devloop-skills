#!/usr/bin/env python3
"""Read-only status queries and atomic archival of completed devloop review scopes. Stdlib only."""

from __future__ import annotations

import os
import pathlib
import shutil

from devloop_docs import docs_fingerprint
from devloop_git import current_branch, head_sha, is_worktree_clean, resolve_git_root, run_git, sha256_file
from devloop_markers import INDEX_START
from devloop_review_io import acquire_review_lock, verify_review_paths_ignored
from devloop_review_store import (
    count_rejects,
    list_active_scope_keys,
    list_scope_records,
    make_scope_key,
    normalize_version,
    parse_handoff_meta,
    version_matches_tag,
)
from devloop_reviews import resolve_review_roots


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


def _pass_is_fresh(cwd_git: pathlib.Path, latest_sha: str, cur_head: str) -> bool:
    if not latest_sha or not cur_head:
        return False
    if latest_sha == cur_head:
        return True
    diff = run_git(["diff", "--name-only", f"{latest_sha}..HEAD"], cwd_git)
    if diff is None or diff.returncode != 0:
        return False
    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    return all(path == ".loop/handoff.md" for path in changed)


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


def _recommend_next(
    initialized: bool, corrupt: list[str], records: list[dict], counts: dict,
    roots: dict, cur_head: str, clean: bool, ho_meta: dict,
) -> tuple[str, str, bool]:
    if not initialized:
        return "/loop-init", "missing .loop/config.json or AGENTS.md marker", False
    if corrupt:
        return "fix_corrupt_records", corrupt[0], False
    if not records:
        return _recommend_for_no_records(roots, ho_meta)
    latest = records[-1]
    verdict = latest["verdict"]
    if verdict == "reject":
        return _recommend_for_reject(latest, counts, cur_head, clean)
    if verdict == "pending":
        return "manual_browser_verify", "latest verdict pending manual browser verification", False
    if _pass_is_fresh(roots["cwd_git"], latest["reviewed_sha"], cur_head):
        return "/loop-retro", f"latest round r{latest['round']:02d} passed for current candidate", False
    return "/loop-review", "code or docs changed since latest pass record", True


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


def _status_for_git_repo(roots: dict, version: str, branch: str) -> tuple[int, dict]:
    cur_head, cwd_br = head_sha(roots["cwd"]), current_branch(roots["cwd"])
    clean, _ = is_worktree_clean(roots["cwd_git"])
    ho_meta = parse_handoff_meta(roots["state"] / ".loop" / "handoff.md")
    eff_ver = version.strip() or ho_meta.get("version", "")
    eff_br = branch.strip() or cwd_br or ho_meta.get("branch", "")
    scope_key = _safe_scope_key(eff_ver, eff_br)
    reviews_root = roots["state"] / ".loop" / "reviews"
    records, corrupt = list_scope_records(reviews_root / scope_key) if scope_key else ([], [])
    counts = count_rejects(records)
    initialized = _is_initialized(roots)
    nxt, reason, pass_stale = _recommend_next(initialized, corrupt, records, counts, roots, cur_head, bool(clean), ho_meta)
    latest = records[-1] if records else None
    payload = {
        "status": "FAILED" if corrupt else "PASSED", "is_git": True,
        "git_root": str(roots["cwd_git"]), "mode": roots["mode"], "initialized": initialized,
        "head_sha": cur_head, "current_branch": cwd_br, "worktree_clean": bool(clean),
        "version": eff_ver, "branch": eff_br, "scope_key": scope_key,
        "scope_dir": str(reviews_root / scope_key) if scope_key else "",
        "active_records": records, "latest_record": latest,
        "latest_verdict": latest["verdict"] if latest else "none",
        "latest_reviewed_sha": latest["reviewed_sha"] if latest else "",
        "pass_stale": pass_stale, "corrupt_records": corrupt,
        "pending_archive_scopes": _pending_archive_scopes(reviews_root, scope_key),
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
            "status": "PASSED", "is_git": False, "initialized": False,
            "recommended_next": "/loop-init", "recommended_reason": root_errs[0],
            "pending_archive_scopes": [], "active_records": [],
        }
    return _status_for_git_repo(roots, version, branch)


def _resolve_archive_version(tag: str, version: str, ho_meta: dict) -> tuple[str, str]:
    if tag.strip() and version.strip() and not version_matches_tag(version, tag):
        return "", f"[VERSION_CONFLICT] --version {version} conflicts with --tag {tag}"
    raw = version.strip() or tag.strip() or ho_meta.get("version", "")
    if not raw:
        return "", "[VERSION_MISSING] --tag, --version, or handoff version required"
    try:
        return normalize_version(raw), ""
    except ValueError as exc:
        return "", f"[VERSION_INVALID] {exc}"


def _match_scope_in_dir(base_dir: pathlib.Path, eff_ver: str, ho_ver: str) -> tuple[str, str]:
    keys = list_active_scope_keys(base_dir)
    if not keys:
        return "", ""
    matches = [k for k in keys if version_matches_tag(k.split("__", 1)[0], eff_ver)]
    if not matches and ho_ver and version_matches_tag(ho_ver, eff_ver):
        matches = [k for k in keys if version_matches_tag(k.split("__", 1)[0], ho_ver)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return "", f"[ARCHIVE_SCOPE_AMBIGUOUS] multiple scopes match {eff_ver}: {matches}"
    return "", ""


def _resolve_archive_scope(
    reviews_root: pathlib.Path, archive_root: pathlib.Path,
    eff_ver: str, branch: str, ho_meta: dict, cur_branch: str,
) -> tuple[str, str]:
    if branch.strip():
        return make_scope_key(eff_ver, branch.strip()), ""
    ho_ver = ho_meta.get("version", "")
    ver_choices = [eff_ver] + ([ho_ver] if ho_ver and version_matches_tag(ho_ver, eff_ver) else [])
    for ver_opt in ver_choices:
        for br in (ho_meta.get("branch", ""), cur_branch):
            if br and ((reviews_root / (key := make_scope_key(ver_opt, br))).is_dir() or (archive_root / key).is_dir()):
                return key, ""
    for root in (reviews_root, archive_root):
        found, err = _match_scope_in_dir(root, eff_ver, ho_ver)
        if err or found:
            return found, err
    return "", f"[ARCHIVE_SOURCE_MISSING] no active review scope found for {eff_ver}"


def _dir_file_hashes(directory: pathlib.Path) -> dict[str, str]:
    return {p.name: sha256_file(p) for p in sorted(directory.iterdir()) if p.is_file()}


def _move_to_empty_dest(src: pathlib.Path, dst: pathlib.Path, scope_key: str) -> tuple[int, dict]:
    records, errs = list_scope_records(src)
    if errs:
        return 1, {"status": "FAILED", "violations": errs}
    if not records:
        return 1, {"status": "FAILED", "violations": [f"[ARCHIVE_SOURCE_EMPTY] {src} contains no records"]}
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    return 0, {
        "status": "PASSED", "archived": True, "idempotent": False,
        "scope_key": scope_key, "source": str(src), "destination": str(dst), "records_count": len(records),
    }


def _reconcile_existing_dest(src: pathlib.Path, dst: pathlib.Path, scope_key: str) -> tuple[int, dict]:
    records, errs = list_scope_records(dst)
    if errs or not records:
        return 1, {"status": "FAILED", "violations": errs or [f"[ARCHIVE_DEST_CORRUPT] {dst} is empty"]}
    if src.is_dir() and _dir_file_hashes(src) != _dir_file_hashes(dst):
        return 1, {"status": "FAILED", "violations": [f"[ARCHIVE_CONFLICT] destination {dst} exists with different contents"]}
    if src.is_dir():
        shutil.rmtree(src)
    return 0, {
        "status": "PASSED", "archived": True, "idempotent": True,
        "scope_key": scope_key, "source": str(src), "destination": str(dst), "records_count": len(records),
    }


def run_review_archive(
    cwd: pathlib.Path, state_root_arg: str = "", tag: str = "", version: str = "", branch: str = "",
) -> tuple[int, dict]:
    eff_cwd = cwd.expanduser().resolve()
    st, _ = resolve_git_root(eff_cwd)
    if st != "ok":
        return 2, {"status": "FAILED", "violations": [f"[NOT_GIT] cwd must be inside a git repository ({st})"]}
    eff_state = pathlib.Path(state_root_arg).expanduser().resolve() if state_root_arg else eff_cwd
    _, ign_errs = verify_review_paths_ignored(eff_state, (".loop/reviews", ".loop/archive"))
    if ign_errs:
        return 1, {"status": "FAILED", "violations": ign_errs}
    ho_meta = parse_handoff_meta(eff_state / ".loop" / "handoff.md")
    eff_ver, ver_err = _resolve_archive_version(tag, version, ho_meta)
    if ver_err:
        return 1, {"status": "FAILED", "violations": [ver_err]}
    reviews_root, archive_root = eff_state / ".loop" / "reviews", eff_state / ".loop" / "archive"
    scope_key, scope_err = _resolve_archive_scope(reviews_root, archive_root, eff_ver, branch, ho_meta, current_branch(eff_cwd))
    if scope_err:
        return 1, {"status": "FAILED", "violations": [scope_err]}
    with acquire_review_lock(eff_state):
        src, dst = reviews_root / scope_key, archive_root / scope_key
        if dst.is_dir():
            return _reconcile_existing_dest(src, dst, scope_key)
        if src.is_dir():
            return _move_to_empty_dest(src, dst, scope_key)
        return 1, {"status": "FAILED", "violations": [f"[ARCHIVE_SOURCE_MISSING] missing {src}"]}
