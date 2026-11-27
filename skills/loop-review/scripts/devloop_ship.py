#!/usr/bin/env python3
"""Ship receipt, archive retro metadata, and shared release/archive checks. Stdlib only."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from datetime import datetime, timezone

from devloop_docs import docs_fingerprint, verify_doc_attestation
from devloop_git import (current_branch, head_sha, is_ancestor, is_tracked, is_worktree_clean,
                         repo_prefix, resolve_commit_sha, run_git, same_git_repo)
from devloop_preflight import resolve_review_roots
from devloop_review_io import acquire_review_lock, atomic_write_json, verify_review_paths_ignored
from devloop_review_store import (list_active_scope_keys, list_scope_records, make_scope_key,
                                  normalize_version, parse_handoff_meta, version_matches_tag)

DEFAULT_ALLOW_DIFF = frozenset({".loop/handoff.md"})
SHIP_COMMON_KEYS = ("mode", "kind", "scope_key", "version", "branch", "reviewed_sha", "shipped_at")
SHIP_OWN_KEYS = ("tag", "tag_sha", "release_sha", "target_branch", "push_status")
RETRO_COMMON_KEYS = ("scope_key", "version", "branch", "reviewed_sha", "retro_head_sha", "archived_at", "close_reason", "ship_kind")


def ship_receipt_path(reviews_root: pathlib.Path, scope_key: str) -> pathlib.Path:
    return reviews_root / f"{scope_key}.ship.json"


def retro_meta_path(archive_root: pathlib.Path, scope_key: str) -> pathlib.Path:
    return archive_root / f"{scope_key}.retro.json"


def meta_binding_equal(a: dict, b: dict, ignore: frozenset[str]) -> bool:
    return {k: v for k, v in a.items() if k not in ignore} == {k: v for k, v in b.items() if k not in ignore}


def _str_fields_ok(data: dict, keys: tuple[str, ...]) -> bool:
    return all(isinstance(data.get(k), str) and bool(data[k].strip()) for k in keys)


def check_same_repo_and_clean(roots: dict) -> list[str]:
    errors: list[str] = []
    if roots["state_git"] != roots["cwd_git"] and not same_git_repo(roots["cwd_git"], roots["state_git"]):
        errors.append("[STATE_ROOT_REPO_MISMATCH] state-root belongs to a different git repository")
    pairs = [("cwd", roots["cwd_git"])]
    if roots["state_git"] != roots["cwd_git"]:
        pairs.append(("state_root", roots["state_git"]))
    for label, groot in pairs:
        clean, first = is_worktree_clean(groot)
        if clean is None:
            errors.append(f"[GIT_ERROR] git status failed on {label}")
        elif not clean:
            errors.append(f"[DIRTY_WORKTREE] {label} has uncommitted changes: {first}")
    return errors


def check_external_leaks_and_tmp(roots: dict) -> list[str]:
    errors: list[str] = []
    if roots["mode"] == "external":
        c_pref = repo_prefix(roots["cwd"], roots["cwd_git"])
        if is_tracked(roots["cwd_git"], f"{c_pref}.loop"):
            errors.append("[EXTERNAL_LEAK] .loop/ is tracked in external mode")
        if roots["state_git"] != roots["cwd_git"]:
            s_pref = repo_prefix(roots["state"], roots["state_git"])
            if is_tracked(roots["state_git"], f"{s_pref}.loop"):
                errors.append("[EXTERNAL_LEAK] state_root .loop/ is tracked in external mode")
    if (roots["cwd"] / ".tmp").exists():
        errors.append("[TMP_RESIDUAL] .tmp directory present at repository root")
    if roots["state"] != roots["cwd"] and (roots["state"] / ".tmp").exists():
        errors.append("[TMP_RESIDUAL] .tmp directory present at state root")
    return errors


def check_reviewed_diff_between(
    git_root: pathlib.Path, reviewed_sha: str, target_ref: str = "HEAD",
    allow_diff: frozenset[str] = DEFAULT_ALLOW_DIFF,
) -> list[str]:
    if not is_ancestor(git_root, reviewed_sha, target_ref):
        return [f"[NOT_ANCESTOR] reviewed_sha {reviewed_sha} is absent from {target_ref} ancestry"]
    diff_proc = run_git(["diff", "--name-only", f"{reviewed_sha}..{target_ref}"], git_root)
    if diff_proc is None or diff_proc.returncode != 0:
        return [f"[DIFF_ERROR] git diff failed for {reviewed_sha}..{target_ref}"]
    blocked = [p for p in (line.strip() for line in diff_proc.stdout.splitlines()) if p and p not in allow_diff]
    return [f"[UNREVIEWED_DIFF] {p}: outside release diff whitelist" for p in blocked]


def _load_json_dict(path: pathlib.Path, code: str) -> tuple[dict | None, str]:
    if not path.exists() and not path.is_symlink():
        return None, ""
    if path.is_symlink() or not path.is_file():
        return None, f"[{code}] {path.name}: invalid metadata path"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return None, f"[{code}] {path.name}: corrupt JSON ({exc})"
    if not isinstance(loaded, dict):
        return None, f"[{code}] {path.name}: JSON object required"
    return loaded, ""


def read_ship_receipt(reviews_root: pathlib.Path, scope_key: str) -> tuple[dict | None, str]:
    path = ship_receipt_path(reviews_root, scope_key)
    data, err = _load_json_dict(path, "SHIP_RECEIPT_CORRUPT")
    if err or data is None:
        return data, err
    if not _str_fields_ok(data, SHIP_COMMON_KEYS) or data.get("scope_key") != scope_key:
        return None, f"[SHIP_RECEIPT_CORRUPT] {path.name}: missing or mismatched fields"
    mode, kind = data.get("mode"), data.get("kind")
    if mode == "own" and kind == "release_tag":
        ok = _str_fields_ok(data, SHIP_OWN_KEYS) and version_matches_tag(data["version"], data["tag"])
        return (data, "") if ok else (None, f"[SHIP_RECEIPT_CORRUPT] {path.name}: invalid own release receipt")
    if mode == "external" and kind == "pr_handoff":
        return data, ""
    return None, f"[SHIP_RECEIPT_CORRUPT] {path.name}: invalid mode/kind ({mode!r}/{kind!r})"


def read_retro_meta(archive_root: pathlib.Path, scope_key: str) -> tuple[dict | None, str]:
    path = retro_meta_path(archive_root, scope_key)
    data, err = _load_json_dict(path, "ARCHIVE_META_CORRUPT")
    if err or data is None:
        return data, err
    if not _str_fields_ok(data, RETRO_COMMON_KEYS) or data.get("scope_key") != scope_key:
        return None, f"[ARCHIVE_META_CORRUPT] {path.name}: missing or mismatched fields"
    reason, kind = data.get("close_reason"), data.get("ship_kind")
    if reason not in ("shipped", "abandoned") or kind not in ("release_tag", "pr_handoff", "abandoned"):
        return None, f"[ARCHIVE_META_CORRUPT] {path.name}: invalid close_reason or ship_kind"
    if reason == "abandoned" and kind != "abandoned":
        return None, f"[ARCHIVE_META_CORRUPT] {path.name}: abandoned close requires ship_kind=abandoned"
    if kind == "release_tag" and (not _str_fields_ok(data, ("tag", "tag_sha")) or not version_matches_tag(data["version"], data["tag"])):
        return None, f"[ARCHIVE_META_CORRUPT] {path.name}: invalid release_tag metadata"
    return data, ""


def scan_corrupt_meta(reviews_root: pathlib.Path, archive_root: pathlib.Path) -> list[str]:
    errs: list[str] = []
    if reviews_root.is_dir() and not reviews_root.is_symlink():
        errs += [err for p in sorted(reviews_root.glob("*.ship.json"), key=lambda x: x.name) if (err := read_ship_receipt(reviews_root, p.name[:-10])[1])]
    if archive_root.is_dir() and not archive_root.is_symlink():
        errs += [err for p in sorted(archive_root.glob("*.retro.json"), key=lambda x: x.name) if (err := read_retro_meta(archive_root, p.name[:-11])[1])]
    return errs


def _branch_suffix(branch: str) -> str:
    br_clean = branch.strip().strip("`").strip()
    br_slug = re.sub(r"[^a-z0-9._-]+", "-", br_clean.lower()).strip("-")[:40] or "branch"
    return f"__{br_slug}-{hashlib.sha256(br_clean.encode('utf-8')).hexdigest()[:8]}"


def locate_active_scope(reviews_root: pathlib.Path, eff_ver: str, branch: str, ho_br: str, cwd_br: str) -> tuple[str, str]:
    br_list = [branch.strip()] if branch.strip() else [b for b in (ho_br, cwd_br) if b]
    active = list_active_scope_keys(reviews_root)
    for br in dict.fromkeys(br_list):
        try:
            direct = make_scope_key(eff_ver, br)
        except ValueError as exc:
            return "", f"[REVIEW_SCOPE_INVALID] {exc}"
        if (reviews_root / direct).is_dir():
            return direct, ""
        alias = [k for k in active if k.endswith(_branch_suffix(br)) and version_matches_tag(k.split("__", 1)[0], eff_ver)]
        if len(alias) == 1:
            return alias[0], ""
    if branch.strip():
        return make_scope_key(eff_ver, branch.strip()), ""
    matches = [k for k in active if version_matches_tag(k.split("__", 1)[0], eff_ver)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return "", f"[SHIP_SCOPE_AMBIGUOUS] multiple active scopes match {eff_ver}: {matches}"
    return "", f"[REVIEW_RECORD_MISSING] no active review scope found for {eff_ver}"


def _resolve_ship_version(ho_ver: str, version: str, tag: str) -> tuple[str, str]:
    if tag.strip() and version.strip() and not version_matches_tag(version, tag):
        return "", f"[VERSION_CONFLICT] --version {version} conflicts with --tag {tag}"
    raw = version.strip() or (ho_ver if (ho_ver and (not tag.strip() or version_matches_tag(ho_ver, tag))) else tag.strip())
    if not raw:
        return "", "[VERSION_MISSING] --version, --tag, or handoff version required"
    try:
        return normalize_version(raw), ""
    except ValueError as exc:
        return "", f"[VERSION_INVALID] {exc}"


def _build_own_receipt(
    roots: dict, scope_key: str, latest: dict, cur_head: str, tag: str, target_branch: str, push_status: str, now: str,
) -> tuple[dict | None, list[str]]:
    eff_tag = tag.strip()
    if not eff_tag:
        return None, ["[TAG_REQUIRED] --tag required for own mode ship-record"]
    errs = [] if version_matches_tag(latest["version"], eff_tag) else [f"[VERSION_MISMATCH] review version={latest['version']} vs tag={eff_tag}"]
    verify = run_git(["rev-parse", "-q", "--verify", f"refs/tags/{eff_tag}"], roots["cwd"])
    tag_sha = resolve_commit_sha(roots["cwd"], f"refs/tags/{eff_tag}") if (verify and verify.returncode == 0) else ""
    if not tag_sha:
        errs.append(f"[TAG_MISSING] tag {eff_tag} missing")
    elif tag_sha != cur_head:
        errs.append(f"[TAG_HEAD_MISMATCH] {eff_tag} points to {tag_sha[:12]} vs HEAD {cur_head[:12]}")
    eff_target = target_branch.strip() or "main"
    if current_branch(roots["cwd"]) != eff_target:
        errs.append(f"[BRANCH_MISMATCH] current branch differs from {eff_target}")
    if tag_sha:
        errs += check_reviewed_diff_between(roots["cwd_git"], latest["reviewed_sha"], tag_sha)
    return {
        "mode": "own", "kind": "release_tag", "scope_key": scope_key, "version": latest["version"],
        "branch": latest["branch"], "reviewed_sha": latest["reviewed_sha"], "tag": eff_tag,
        "tag_sha": tag_sha, "release_sha": cur_head, "target_branch": eff_target,
        "push_status": push_status.strip() or "local_only", "shipped_at": now,
    }, errs


def _build_ship_receipt(roots: dict, scope_key: str, latest: dict, tag: str, target_branch: str, push_status: str) -> tuple[dict | None, list[str]]:
    cur_head = head_sha(roots["cwd"])
    if not cur_head:
        return None, ["[HEAD_MISSING] repository has no HEAD commit"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if roots["mode"] == "external":
        errs = ["[TAG_FORBIDDEN_EXTERNAL] --tag is not allowed in external mode"] if tag.strip() else []
        errs += check_reviewed_diff_between(roots["cwd_git"], latest["reviewed_sha"], cur_head)
        return {"mode": "external", "kind": "pr_handoff", "scope_key": scope_key, "version": latest["version"], "branch": latest["branch"], "reviewed_sha": latest["reviewed_sha"], "shipped_at": now}, errs
    return _build_own_receipt(roots, scope_key, latest, cur_head, tag, target_branch, push_status, now)


def _validate_pass_for_ship(roots: dict, scope_key: str, reviewed_sha: str) -> tuple[dict | None, list[str]]:
    records, errs = list_scope_records(roots["state"] / ".loop" / "reviews" / scope_key)
    if errs or not records:
        return None, errs or [f"[REVIEW_RECORD_MISSING] no review records under .loop/reviews/{scope_key}"]
    latest = records[-1]
    if latest["verdict"] != "pass":
        return None, [f"[REVIEW_NOT_PASSED] latest record {latest['filename']} has verdict={latest['verdict']}"]
    v_errs: list[str] = []
    if reviewed_sha.strip() and resolve_commit_sha(roots["cwd"], reviewed_sha.strip()) != latest["reviewed_sha"]:
        v_errs.append("[REVIEW_SHA_MISMATCH] --reviewed-sha differs from latest pass record")
    return latest, v_errs + verify_doc_attestation(docs_fingerprint(roots["cwd"], roots["mode"], roots["docs"]), latest)


def _commit_ship_receipt(reviews_root: pathlib.Path, scope_key: str, receipt: dict) -> tuple[int, dict]:
    path = ship_receipt_path(reviews_root, scope_key)
    existing, ex_err = read_ship_receipt(reviews_root, scope_key)
    if ex_err:
        return 1, {"status": "FAILED", "violations": [ex_err]}
    if existing is not None:
        if meta_binding_equal(existing, receipt, frozenset({"shipped_at"})):
            return 0, {"status": "PASSED", "idempotent": True, "scope_key": scope_key, "receipt_path": str(path), "receipt": existing}
        return 1, {"status": "FAILED", "violations": [f"[SHIP_RECEIPT_CONFLICT] existing ship receipt differs for {scope_key}"]}
    atomic_write_json(path, receipt)
    return 0, {"status": "PASSED", "idempotent": False, "scope_key": scope_key, "receipt_path": str(path), "receipt": receipt}


def run_ship_record(
    cwd: pathlib.Path, state_root: str = "", docs_root: str = "", mode: str = "",
    version: str = "", branch: str = "", reviewed_sha: str = "", tag: str = "",
    target_branch: str = "main", push_status: str = "local_only",
) -> tuple[int, dict]:
    roots, root_errs = resolve_review_roots(cwd, state_root, docs_root, mode)
    if root_errs or roots is None:
        return 2, {"status": "FAILED", "violations": root_errs}
    reviews_root = roots["state"] / ".loop" / "reviews"
    with acquire_review_lock(roots["state"]):
        _, ign_errs = verify_review_paths_ignored(roots["state"], (".loop/reviews", ".loop/archive"))
        errs = check_same_repo_and_clean(roots) + ign_errs + check_external_leaks_and_tmp(roots)
        ho_meta = parse_handoff_meta(roots["state"] / ".loop" / "handoff.md")
        eff_ver, ver_err = _resolve_ship_version(ho_meta.get("version", ""), version, tag)
        if ver_err:
            return 1, {"status": "FAILED", "violations": errs + [ver_err]}
        scope_key, scope_err = locate_active_scope(reviews_root, eff_ver, branch, ho_meta.get("branch", ""), current_branch(roots["cwd"]))
        if scope_err:
            return 1, {"status": "FAILED", "violations": errs + [scope_err]}
        latest, pass_errs = _validate_pass_for_ship(roots, scope_key, reviewed_sha)
        receipt, rec_errs = _build_ship_receipt(roots, scope_key, latest, tag, target_branch, push_status) if latest else (None, [])
        if errs + pass_errs + rec_errs or receipt is None:
            return 1, {"status": "FAILED", "violations": errs + pass_errs + rec_errs}
        return _commit_ship_receipt(reviews_root, scope_key, receipt)
