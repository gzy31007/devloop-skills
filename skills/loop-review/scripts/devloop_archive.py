#!/usr/bin/env python3
"""Atomic archival of completed devloop review scopes. Stdlib only."""

from __future__ import annotations

import os
import pathlib
import shutil

from devloop_git import current_branch, resolve_git_root, sha256_file
from devloop_review_io import acquire_review_lock, verify_review_paths_ignored
from devloop_review_store import (
    list_active_scope_keys,
    list_scope_records,
    make_scope_key,
    normalize_version,
    parse_handoff_meta,
    version_matches_tag,
)


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


def _lookup_existing_scope(
    reviews_root: pathlib.Path, archive_root: pathlib.Path, eff_ver: str, ho_meta: dict, cur_branch: str,
) -> str:
    ho_ver = ho_meta.get("version", "")
    ver_choices = [eff_ver] + ([ho_ver] if ho_ver and version_matches_tag(ho_ver, eff_ver) else [])
    branches = [b for b in (ho_meta.get("branch", ""), cur_branch) if b]
    for ver_opt in ver_choices:
        for br in branches:
            key = make_scope_key(ver_opt, br)
            if (reviews_root / key).is_dir() or (archive_root / key).is_dir():
                return key
    return ""


def _resolve_archive_scope(
    reviews_root: pathlib.Path, archive_root: pathlib.Path,
    eff_ver: str, branch: str, ho_meta: dict, cur_branch: str,
) -> tuple[str, str]:
    if branch.strip():
        return make_scope_key(eff_ver, branch.strip()), ""
    direct = _lookup_existing_scope(reviews_root, archive_root, eff_ver, ho_meta, cur_branch)
    if direct:
        return direct, ""
    ho_ver = ho_meta.get("version", "")
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
