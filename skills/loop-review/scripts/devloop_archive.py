#!/usr/bin/env python3
"""Crash-resilient atomic archival of completed devloop review scopes. Stdlib only."""

from __future__ import annotations

import os
import pathlib
import shutil
from datetime import datetime, timezone

from devloop_git import current_branch, head_sha, is_ancestor, resolve_commit_sha, sha256_file
from devloop_preflight import resolve_review_roots
from devloop_review_io import acquire_review_lock, atomic_write_json, verify_review_paths_ignored
from devloop_review_store import list_active_scope_keys, list_scope_records, make_scope_key, normalize_version, parse_handoff_meta, version_matches_tag
from devloop_ship import check_same_repo_and_clean, meta_binding_equal, read_retro_meta, read_ship_receipt, retro_meta_path, ship_receipt_path


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


def _lookup_existing_scope(reviews_root: pathlib.Path, archive_root: pathlib.Path, eff_ver: str, ho_meta: dict, cur_br: str) -> str:
    ho_ver = ho_meta.get("version", "")
    ver_choices = [eff_ver] + ([ho_ver] if ho_ver and version_matches_tag(ho_ver, eff_ver) else [])
    for ver_opt in ver_choices:
        for br in [b for b in (ho_meta.get("branch", ""), cur_br) if b]:
            key = make_scope_key(ver_opt, br)
            if (reviews_root / key).is_dir() or (archive_root / key).is_dir():
                return key
    return ""


def _resolve_archive_scope(
    reviews_root: pathlib.Path, archive_root: pathlib.Path, eff_ver: str, branch: str, ho_meta: dict, cur_br: str,
) -> tuple[str, str]:
    if branch.strip():
        return make_scope_key(eff_ver, branch.strip()), ""
    direct = _lookup_existing_scope(reviews_root, archive_root, eff_ver, ho_meta, cur_br)
    if direct:
        return direct, ""
    for root in (reviews_root, archive_root):
        found, err = _match_scope_in_dir(root, eff_ver, ho_meta.get("version", ""))
        if err or found:
            return found, err
    return "", f"[ARCHIVE_SOURCE_MISSING] no active review scope found for {eff_ver}"


def _dir_file_hashes(directory: pathlib.Path) -> dict[str, str]:
    return {p.name: sha256_file(p) for p in sorted(directory.iterdir()) if p.is_file()}


def _check_retro_head(cwd: pathlib.Path, cur_head: str, anchor_sha: str, retro_head: str) -> str:
    if retro_head.strip():
        resolved = resolve_commit_sha(cwd, retro_head.strip())
        return "" if resolved == cur_head else f"[RETRO_HEAD_MISMATCH] --retro-head differs from HEAD ({cur_head[:12]})"
    if cur_head != anchor_sha:
        return "[RETRO_HEAD_REQUIRED] --retro-head required when HEAD advanced past shipped commit"
    return ""


def _make_retro_meta(
    scope_key: str, latest: dict, cur_head: str, now: str, reason: str, kind: str,
    tag: str = "", tag_sha: str = "", sampling_note: str = "",
) -> dict:
    meta = {
        "scope_key": scope_key, "version": latest["version"], "branch": latest["branch"],
        "reviewed_sha": latest["reviewed_sha"], "retro_head_sha": cur_head, "archived_at": now,
        "close_reason": reason, "ship_kind": kind, "tag": tag, "tag_sha": tag_sha,
    }
    if sampling_note:
        meta["sampling_note"] = sampling_note
    return meta


def _authorize_pr_handoff(cwd: pathlib.Path, scope_key: str, latest: dict, cur_head: str, tag: str, retro_head: str, now: str, sampling_note: str = "") -> tuple[dict | None, list[str]]:
    if tag.strip():
        return None, ["[ARCHIVE_META_CONFLICT] --tag forbidden for pr_handoff archive"]
    if not is_ancestor(cwd, latest["reviewed_sha"], cur_head):
        return None, [f"[NOT_ANCESTOR] reviewed_sha {latest['reviewed_sha']} absent from HEAD"]
    rh_err = _check_retro_head(cwd, cur_head, latest["reviewed_sha"], retro_head)
    return (None, [rh_err]) if rh_err else (_make_retro_meta(scope_key, latest, cur_head, now, "shipped", "pr_handoff", sampling_note=sampling_note), [])


def _authorize_release_tag(cwd: pathlib.Path, scope_key: str, latest: dict, ship: dict, cur_head: str, tag: str, retro_head: str, now: str, sampling_note: str = "") -> tuple[dict | None, list[str]]:
    if tag.strip() and tag.strip() != ship["tag"]:
        return None, [f"[ARCHIVE_META_CONFLICT] --tag {tag} conflicts with ship receipt {ship['tag']}"]
    tag_sha = resolve_commit_sha(cwd, f"refs/tags/{ship['tag']}")
    if tag_sha != ship["tag_sha"]:
        return None, [f"[TAG_MISMATCH] refs/tags/{ship['tag']} differs from ship receipt"]
    if not is_ancestor(cwd, latest["reviewed_sha"], tag_sha) or not is_ancestor(cwd, tag_sha, cur_head):
        return None, ["[NOT_ANCESTOR] shipped tag ancestry broken"]
    rh_err = _check_retro_head(cwd, cur_head, tag_sha, retro_head)
    return (None, [rh_err]) if rh_err else (_make_retro_meta(scope_key, latest, cur_head, now, "shipped", "release_tag", ship["tag"], tag_sha, sampling_note), [])


def _authorize_with_ship(cwd: pathlib.Path, reviews_root: pathlib.Path, scope_key: str, latest: dict, cur_head: str, tag: str, retro_head: str, now: str) -> tuple[dict | None, list[str]]:
    ship, err = read_ship_receipt(reviews_root, scope_key)
    if err or ship is None:
        return None, [err or "[SCOPE_NOT_SHIPPED] unreadable ship receipt"]
    if latest["verdict"] != "pass":
        return None, [f"[REVIEW_NOT_PASSED] latest record {latest['filename']} has verdict={latest['verdict']}"]
    if (latest["version"], latest["branch"], latest["reviewed_sha"]) != (ship["version"], ship["branch"], ship["reviewed_sha"]):
        return None, ["[ARCHIVE_META_CONFLICT] active pass record differs from .ship.json"]
    note = ship.get("sampling_note", "")
    if ship["kind"] == "pr_handoff":
        return _authorize_pr_handoff(cwd, scope_key, latest, cur_head, tag, retro_head, now, note)
    return _authorize_release_tag(cwd, scope_key, latest, ship, cur_head, tag, retro_head, now, note)


def _authorize_abandoned(cwd: pathlib.Path, ship_path: pathlib.Path, scope_key: str, latest: dict, cur_head: str, tag: str, retro_head: str, now: str) -> tuple[dict | None, list[str]]:
    if tag.strip() or ship_path.exists() or ship_path.is_symlink():
        return None, ["[ARCHIVE_META_CONFLICT] abandoned close conflicts with --tag or .ship.json"]
    if retro_head.strip() and resolve_commit_sha(cwd, retro_head.strip()) != cur_head:
        return None, [f"[RETRO_HEAD_MISMATCH] --retro-head differs from HEAD ({cur_head[:12]})"]
    return _make_retro_meta(scope_key, latest, cur_head, now, "abandoned", "abandoned"), []


def _authorize_compat_tag(cwd: pathlib.Path, scope_key: str, latest: dict, cur_head: str, tag: str, retro_head: str, now: str) -> tuple[dict | None, list[str]]:
    eff_tag = tag.strip()
    tag_sha = resolve_commit_sha(cwd, f"refs/tags/{eff_tag}") if eff_tag else ""
    if latest["verdict"] != "pass" or not eff_tag or not version_matches_tag(latest["version"], eff_tag) or not tag_sha:
        return None, ["[SCOPE_NOT_SHIPPED] missing .ship.json or matching release tag; run /loop-ship or pass --close-reason abandoned"]
    if not is_ancestor(cwd, latest["reviewed_sha"], tag_sha) or not is_ancestor(cwd, tag_sha, cur_head):
        return None, ["[SCOPE_NOT_SHIPPED] release tag ancestry does not cover candidate and HEAD"]
    rh_err = _check_retro_head(cwd, cur_head, tag_sha, retro_head)
    return (None, [rh_err]) if rh_err else (_make_retro_meta(scope_key, latest, cur_head, now, "shipped", "release_tag", eff_tag, tag_sha), [])


def _authorize_archive(roots: dict, reviews_root: pathlib.Path, scope_key: str, latest: dict, tag: str, retro_head: str, close_reason: str) -> tuple[dict | None, list[str]]:
    cwd, cur_head = roots["cwd"], head_sha(roots["cwd"])
    if not cur_head:
        return None, ["[HEAD_MISSING] repository has no HEAD commit"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ship_path = ship_receipt_path(reviews_root, scope_key)
    if close_reason == "abandoned":
        return _authorize_abandoned(cwd, ship_path, scope_key, latest, cur_head, tag, retro_head, now)
    if ship_path.exists() or ship_path.is_symlink():
        return _authorize_with_ship(cwd, reviews_root, scope_key, latest, cur_head, tag, retro_head, now)
    return _authorize_compat_tag(cwd, scope_key, latest, cur_head, tag, retro_head, now)


def _write_or_verify_retro_meta(archive_root: pathlib.Path, scope_key: str, expected_meta: dict) -> tuple[dict | None, str]:
    meta_path = retro_meta_path(archive_root, scope_key)
    if meta_path.exists() or meta_path.is_symlink():
        existing, r_err = read_retro_meta(archive_root, scope_key)
        if r_err or existing is None:
            return None, r_err or "[ARCHIVE_META_CORRUPT]"
        if not meta_binding_equal(existing, expected_meta, frozenset({"archived_at"})):
            return None, f"[ARCHIVE_META_CONFLICT] {meta_path.name} conflicts with pending archive"
        return existing, ""
    atomic_write_json(meta_path, expected_meta)
    return expected_meta, ""


def _move_to_empty_dest(
    roots: dict, reviews_root: pathlib.Path, archive_root: pathlib.Path, src: pathlib.Path, dst: pathlib.Path,
    scope_key: str, tag: str, retro_head: str, close_reason: str,
) -> tuple[int, dict]:
    records, errs = list_scope_records(src)
    if errs or not records:
        return 1, {"status": "FAILED", "violations": errs or [f"[ARCHIVE_SOURCE_EMPTY] {src} contains no records"]}
    expected_meta, auth_errs = _authorize_archive(roots, reviews_root, scope_key, records[-1], tag, retro_head, close_reason)
    if auth_errs or expected_meta is None:
        return 1, {"status": "FAILED", "violations": auth_errs}
    saved_meta, meta_err = _write_or_verify_retro_meta(archive_root, scope_key, expected_meta)
    if meta_err or saved_meta is None:
        return 1, {"status": "FAILED", "violations": [meta_err]}
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    ship_receipt_path(reviews_root, scope_key).unlink(missing_ok=True)
    return 0, {"status": "PASSED", "archived": True, "idempotent": False, "scope_key": scope_key, "source": str(src), "destination": str(dst), "records_count": len(records), "retro_meta": saved_meta}


def _check_residual_ship_note(reviews_root: pathlib.Path, scope_key: str, rmeta: dict) -> str:
    ship_path = ship_receipt_path(reviews_root, scope_key)
    if not ship_path.exists() and not ship_path.is_symlink():
        return ""
    ship, s_err = read_ship_receipt(reviews_root, scope_key)
    if s_err or ship is None:
        return s_err or "[SHIP_RECEIPT_CORRUPT] unreadable residual ship receipt"
    if ship.get("sampling_note", "") != rmeta.get("sampling_note", ""):
        return "[ARCHIVE_META_CONFLICT] .ship.json sampling_note differs from .retro.json"
    return ""


def _validate_existing_retro(
    cwd: pathlib.Path, reviews_root: pathlib.Path, scope_key: str,
    rmeta: dict, latest: dict, tag: str, retro_head: str, close_reason: str,
) -> str:
    if (rmeta["version"], rmeta["branch"], rmeta["reviewed_sha"]) != (latest["version"], latest["branch"], latest["reviewed_sha"]):
        return "[ARCHIVE_META_CONFLICT] .retro.json disagrees with archived records"
    if tag.strip() and tag.strip() != rmeta["tag"]:
        return "[ARCHIVE_META_CONFLICT] --tag conflicts with existing .retro.json"
    if close_reason and close_reason != rmeta["close_reason"]:
        return "[ARCHIVE_META_CONFLICT] --close-reason conflicts with existing .retro.json"
    if retro_head.strip() and resolve_commit_sha(cwd, retro_head.strip()) != rmeta["retro_head_sha"]:
        return "[ARCHIVE_META_CONFLICT] --retro-head conflicts with existing .retro.json"
    return _check_residual_ship_note(reviews_root, scope_key, rmeta)


def _reconcile_existing_dest(
    roots: dict, reviews_root: pathlib.Path, archive_root: pathlib.Path, src: pathlib.Path, dst: pathlib.Path,
    scope_key: str, tag: str, retro_head: str, close_reason: str,
) -> tuple[int, dict]:
    records, errs = list_scope_records(dst)
    if errs or not records:
        return 1, {"status": "FAILED", "violations": errs or [f"[ARCHIVE_DEST_CORRUPT] {dst} is empty"]}
    if src.is_dir() and _dir_file_hashes(src) != _dir_file_hashes(dst):
        return 1, {"status": "FAILED", "violations": [f"[ARCHIVE_CONFLICT] destination {dst} exists with different contents"]}
    rmeta, r_err = read_retro_meta(archive_root, scope_key)
    if r_err or rmeta is None:
        return 1, {"status": "FAILED", "violations": [r_err or f"[ARCHIVE_META_MISSING] missing {scope_key}.retro.json"]}
    conflict = _validate_existing_retro(roots["cwd"], reviews_root, scope_key, rmeta, records[-1], tag, retro_head, close_reason)
    if conflict:
        return 1, {"status": "FAILED", "violations": [conflict]}
    if src.is_dir():
        shutil.rmtree(src)
    ship_receipt_path(reviews_root, scope_key).unlink(missing_ok=True)
    return 0, {"status": "PASSED", "archived": True, "idempotent": True, "scope_key": scope_key, "source": str(src), "destination": str(dst), "records_count": len(records), "retro_meta": rmeta}


def run_review_archive(
    cwd: pathlib.Path, state_root_arg: str = "", tag: str = "", version: str = "", branch: str = "",
    retro_head: str = "", close_reason: str = "",
) -> tuple[int, dict]:
    if close_reason not in ("", "abandoned"):
        return 2, {"status": "FAILED", "violations": [f"[INVALID_CLOSE_REASON] {close_reason!r}"]}
    roots, root_errs = resolve_review_roots(cwd, state_root_arg, "", "")
    if root_errs or roots is None:
        return 2, {"status": "FAILED", "violations": root_errs}
    _, ign_errs = verify_review_paths_ignored(roots["state"], (".loop/reviews", ".loop/archive"))
    if ign_errs:
        return 1, {"status": "FAILED", "violations": ign_errs}
    ho_meta = parse_handoff_meta(roots["state"] / ".loop" / "handoff.md")
    eff_ver, ver_err = _resolve_archive_version(tag, version, ho_meta)
    if ver_err:
        return 1, {"status": "FAILED", "violations": [ver_err]}
    reviews_root, archive_root = roots["state"] / ".loop" / "reviews", roots["state"] / ".loop" / "archive"
    scope_key, scope_err = _resolve_archive_scope(reviews_root, archive_root, eff_ver, branch, ho_meta, current_branch(roots["cwd"]))
    if scope_err:
        return 1, {"status": "FAILED", "violations": [scope_err]}
    with acquire_review_lock(roots["state"]):
        dirty_errs = check_same_repo_and_clean(roots)
        if dirty_errs:
            return 1, {"status": "FAILED", "violations": dirty_errs}
        src, dst = reviews_root / scope_key, archive_root / scope_key
        if dst.is_dir():
            return _reconcile_existing_dest(roots, reviews_root, archive_root, src, dst, scope_key, tag, retro_head, close_reason)
        if src.is_dir():
            return _move_to_empty_dest(roots, reviews_root, archive_root, src, dst, scope_key, tag, retro_head, close_reason)
        return 1, {"status": "FAILED", "violations": [f"[ARCHIVE_SOURCE_MISSING] missing {src}"]}
