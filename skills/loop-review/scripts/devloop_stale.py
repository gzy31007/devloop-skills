#!/usr/bin/env python3
"""Read-only detection of stale handoff.md referencing an already-archived scope. Stdlib only."""

from __future__ import annotations

import pathlib

from devloop_git import is_ancestor, resolve_commit_sha, run_git
from devloop_review_store import list_active_scope_keys, list_scope_records, version_matches_tag
from devloop_ship import read_retro_meta


def _merged_tags(cwd: pathlib.Path, target: str) -> list[str]:
    proc = run_git(["tag", "--merged", target], cwd)
    if proc is None or proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _match_shipped_retro(cwd: pathlib.Path, latest: dict, rmeta: dict, cur_head: str) -> str:
    if latest["verdict"] != "pass" or rmeta["reviewed_sha"] != latest["reviewed_sha"]:
        return ""
    if rmeta["ship_kind"] == "pr_handoff":
        return "pr_handoff" if is_ancestor(cwd, latest["reviewed_sha"], cur_head) else ""
    tag_sha = resolve_commit_sha(cwd, f"refs/tags/{rmeta['tag']}")
    if tag_sha == rmeta["tag_sha"] and is_ancestor(cwd, latest["reviewed_sha"], tag_sha) and is_ancestor(cwd, tag_sha, cur_head):
        return "release_tag"
    return ""


def _match_legacy_archive(cwd: pathlib.Path, latest: dict, ho_ver: str, cur_head: str, m_tags: list[str]) -> bool:
    if latest["verdict"] != "pass":
        return False
    for tag in m_tags:
        if not version_matches_tag(ho_ver, tag):
            continue
        tag_sha = resolve_commit_sha(cwd, f"refs/tags/{tag}")
        if tag_sha and is_ancestor(cwd, latest["reviewed_sha"], tag_sha) and is_ancestor(cwd, tag_sha, cur_head):
            return True
    return False


def _match_abandoned_retro(cwd: pathlib.Path, latest: dict, rmeta: dict, ho_ver: str, ho_br: str, cur_head: str) -> bool:
    if rmeta["branch"] != ho_br or not version_matches_tag(rmeta["version"], ho_ver):
        return False
    return rmeta["reviewed_sha"] == latest["reviewed_sha"] and is_ancestor(cwd, rmeta["retro_head_sha"], cur_head)


def _match_scope_entry(
    cwd: pathlib.Path, archive_root: pathlib.Path, key: str, ho_ver: str, ho_br: str, cur_head: str, m_tags_box: list[list[str]],
) -> tuple[str, str, bool]:
    records, errs = list_scope_records(archive_root / key)
    if errs or not records:
        return "", "", False
    latest = records[-1]
    if not version_matches_tag(latest["version"], ho_ver) or latest["branch"] != ho_br:
        return "", "", False
    rmeta, r_err = read_retro_meta(archive_root, key)
    if r_err:
        return "", "", False
    if rmeta is not None:
        if rmeta["close_reason"] == "abandoned":
            return (key, "abandoned", False) if _match_abandoned_retro(cwd, latest, rmeta, ho_ver, ho_br, cur_head) else ("", "", False)
        kind = _match_shipped_retro(cwd, latest, rmeta, cur_head)
        return (key, kind, False) if kind else ("", "", False)
    if not m_tags_box:
        m_tags_box.append(_merged_tags(cwd, cur_head))
    return (key, "release_tag", True) if _match_legacy_archive(cwd, latest, ho_ver, cur_head, m_tags_box[0]) else ("", "", False)


def find_stale_archived_handoff(
    cwd: pathlib.Path, archive_root: pathlib.Path, ho_meta: dict, eff_ver: str, cur_head: str,
) -> tuple[str, str, bool]:
    """Return (scope_key, kind, is_legacy) if handoff.md still points to an archived scope."""
    ho_ver = ho_meta.get("version", "").strip()
    ho_br = ho_meta.get("branch", "").strip()
    if not ho_ver or not ho_br or not cur_head or not version_matches_tag(ho_ver, eff_ver):
        return "", "", False
    m_tags_box: list[list[str]] = []
    for key in list_active_scope_keys(archive_root):
        if not version_matches_tag(key.split("__", 1)[0], ho_ver):
            continue
        hit = _match_scope_entry(cwd, archive_root, key, ho_ver, ho_br, cur_head, m_tags_box)
        if hit[0]:
            return hit
    return "", "", False
