#!/usr/bin/env python3
"""Review verdict recording and release-gate attestation verification. Stdlib only."""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone

from devloop_docs import build_doc_attestation, docs_fingerprint, resolve_mode, verify_doc_attestation
from devloop_git import (
    current_branch,
    head_sha,
    is_worktree_clean,
    resolve_commit_sha,
    resolve_git_root,
)
from devloop_review_io import acquire_review_lock, atomic_write_new, read_body_outside_repo, verify_review_paths_ignored
from devloop_review_store import (
    body_sha256,
    count_rejects,
    derive_summary,
    format_review_doc,
    list_scope_records,
    make_scope_key,
    normalize_version,
    parse_handoff_meta,
    version_matches_tag,
)


def resolve_review_roots(
    cwd: pathlib.Path, state_root_arg: str = "", docs_root_arg: str = "", mode: str = "",
) -> tuple[dict | None, list[str]]:
    eff_cwd = cwd.expanduser().resolve()
    st_c, cwd_git = resolve_git_root(eff_cwd)
    if st_c != "ok" or cwd_git is None:
        return None, [f"[NOT_GIT] cwd must be inside a git repository ({st_c})"]
    eff_state = pathlib.Path(state_root_arg).expanduser().resolve() if state_root_arg else eff_cwd
    st_s, state_git = resolve_git_root(eff_state)
    if st_s != "ok" or state_git is None:
        return None, [f"[NOT_GIT] state-root must be inside a git repository ({st_s})"]
    eff_mode = resolve_mode(eff_cwd, mode, state_root=eff_state)
    eff_docs = pathlib.Path(docs_root_arg).expanduser().resolve() if docs_root_arg else (eff_state if eff_mode == "external" else eff_cwd)
    return {
        "cwd": eff_cwd, "cwd_git": cwd_git,
        "state": eff_state, "state_git": state_git,
        "mode": eff_mode, "docs": eff_docs,
    }, []


def _validate_clean_and_head(roots: dict, reviewed_sha: str) -> tuple[str, list[str]]:
    errors: list[str] = []
    for label, groot in (("cwd", roots["cwd_git"]), ("state_root", roots["state_git"])):
        if label == "state_root" and groot == roots["cwd_git"]:
            continue
        clean, first = is_worktree_clean(groot)
        if clean is None:
            errors.append(f"[GIT_ERROR] git status failed on {label}")
        elif not clean:
            errors.append(f"[DIRTY_WORKTREE] {label} has uncommitted changes: {first}")
    cur_head = head_sha(roots["cwd"])
    if not cur_head:
        errors.append("[HEAD_MISSING] unable to resolve current HEAD commit")
    elif reviewed_sha.strip():
        target = resolve_commit_sha(roots["cwd"], reviewed_sha.strip())
        if not target or target != cur_head:
            errors.append(f"[CANDIDATE_HEAD_MISMATCH] reviewed_sha differs from HEAD ({cur_head[:12]})")
    return cur_head, errors


def _validate_branch_consistency(roots: dict, cur_head: str, ho_br: str, eff_br: str) -> list[str]:
    errors: list[str] = []
    cwd_br = current_branch(roots["cwd"])
    if not eff_br:
        return ["[BRANCH_MISSING] branch required via --branch, git branch, or .loop/handoff.md"]
    if cwd_br and cwd_br != eff_br:
        errors.append(f"[BRANCH_MISMATCH] current branch {cwd_br} differs from target branch {eff_br}")
    if ho_br and ho_br != eff_br:
        errors.append(f"[HANDOFF_BRANCH_MISMATCH] handoff branch {ho_br} differs from {eff_br}")
    if cur_head:
        tip = resolve_commit_sha(roots["state"], f"refs/heads/{eff_br}")
        if tip and tip != cur_head:
            errors.append(f"[BRANCH_HEAD_MISMATCH] branch {eff_br} tip {tip[:12]} differs from HEAD {cur_head[:12]}")
    return errors


def _resolve_scope_identity(roots: dict, cur_head: str, version: str, branch: str) -> tuple[str, str, str, list[str]]:
    ho_meta = parse_handoff_meta(roots["state"] / ".loop" / "handoff.md")
    raw_ver = version.strip() or ho_meta.get("version", "")
    eff_ver, errors = "", []
    try:
        eff_ver = normalize_version(raw_ver)
    except ValueError:
        errors.append("[VERSION_MISSING] version required via --version or .loop/handoff.md")
    eff_br = branch.strip() or current_branch(roots["cwd"]) or ho_meta.get("branch", "")
    errors += _validate_branch_consistency(roots, cur_head, ho_meta.get("branch", ""), eff_br)
    scope_key = make_scope_key(eff_ver, eff_br) if not errors else ""
    return eff_ver, eff_br, scope_key, errors


def _same_attempt(latest: dict, verdict: str, sha: str, b_sha: str, category: str, attest: dict | None) -> bool:
    if latest["verdict"] != verdict or latest["reviewed_sha"] != sha or latest["body_sha256"] != b_sha:
        return False
    if verdict == "reject":
        return latest.get("category") == category
    if verdict == "pass" and attest:
        keys = ("decisions_sha256", "feature_map_sha256", "decisions_status", "feature_map_status")
        return all(latest.get(k) == attest.get(k) for k in keys)
    return True


def _build_record_meta(
    verdict: str, category: str, eff_ver: str, eff_br: str, next_round: int,
    cur_head: str, reviewer: str, b_sha: str, summary: str, attest: dict | None,
) -> dict:
    meta: dict = {
        "version": eff_ver,
        "branch": eff_br,
        "round": next_round,
        "verdict": verdict,
        "reviewed_sha": cur_head,
        "reviewer": reviewer.strip() or "loop-review",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "body_sha256": b_sha,
        "summary": summary,
    }
    if verdict == "reject":
        meta["category"] = category
    if verdict == "pass" and attest:
        meta.update(attest)
    return meta


def _write_locked_record(
    roots: dict, scope_key: str, verdict: str, category: str, eff_ver: str, eff_br: str,
    cur_head: str, reviewer: str, norm_body: str, summary: str, attest: dict | None,
) -> tuple[int, dict]:
    b_sha = body_sha256(norm_body)
    eff_sum = derive_summary(summary, norm_body)
    scope_dir = roots["state"] / ".loop" / "reviews" / scope_key
    with acquire_review_lock(roots["state"]):
        records, rec_errs = list_scope_records(scope_dir)
        if rec_errs:
            return 1, {"status": "FAILED", "violations": rec_errs}
        if records and _same_attempt(records[-1], verdict, cur_head, b_sha, category, attest):
            return 0, {"status": "PASSED", "idempotent": True, "scope_key": scope_key, "record": records[-1], **count_rejects(records)}
        next_round = (records[-1]["round"] + 1) if records else 1
        meta = _build_record_meta(verdict, category, eff_ver, eff_br, next_round, cur_head, reviewer, b_sha, eff_sum, attest)
        target = scope_dir / f"r{next_round:02d}-{verdict}.md"
        atomic_write_new(target, format_review_doc(meta, norm_body))
        saved = {**meta, "filename": target.name, "path": str(target)}
        return 0, {"status": "PASSED", "idempotent": False, "scope_key": scope_key, "record": saved, **count_rejects(records + [saved])}


def run_review_record(
    cwd: pathlib.Path, verdict: str, category: str = "", state_root: str = "", docs_root: str = "",
    mode: str = "", version: str = "", branch: str = "", reviewed_sha: str = "", reviewer: str = "loop-review",
    summary: str = "", body_file: str = "", body_text: str = "", dec_status: str = "", dec_summary: str = "",
    feat_status: str = "", feat_summary: str = "", base_dec_sha: str = "", base_feat_sha: str = "",
) -> tuple[int, dict]:
    if verdict not in ("reject", "pending", "pass"):
        return 2, {"status": "FAILED", "violations": [f"[INVALID_VERDICT] unsupported verdict: {verdict!r}"]}
    if verdict == "reject" and category not in ("method", "env"):
        return 2, {"status": "FAILED", "violations": ["[CATEGORY_REQUIRED] --category method|env required for reject"]}
    roots, root_errs = resolve_review_roots(cwd, state_root, docs_root, mode)
    if root_errs or roots is None:
        return 2, {"status": "FAILED", "violations": root_errs}
    cur_head, errors = _validate_clean_and_head(roots, reviewed_sha)
    eff_ver, eff_br, scope_key, id_errs = _resolve_scope_identity(roots, cur_head, version, branch)
    errors += id_errs
    _, ign_errs = verify_review_paths_ignored(roots["state"], (".loop/reviews",))
    errors += ign_errs
    norm_body, body_err = read_body_outside_repo(body_file, body_text, roots["cwd"], roots["cwd_git"], roots["state_git"])
    if body_err:
        errors.append(body_err)
    attest: dict | None = None
    if verdict == "pass":
        attest, att_errs = build_doc_attestation(
            roots["cwd"], roots["state"], roots["mode"], roots["docs"],
            dec_status, dec_summary, feat_status, feat_summary, base_dec_sha, base_feat_sha,
        )
        errors += att_errs
    if errors:
        return 1, {"status": "FAILED", "violations": errors}
    return _write_locked_record(roots, scope_key, verdict, category, eff_ver, eff_br, cur_head, reviewer, norm_body, summary, attest)


def _resolve_gate_scope(roots: dict, tag: str, version: str, branch: str) -> tuple[str, str, str]:
    ho_meta = parse_handoff_meta(roots["state"] / ".loop" / "handoff.md")
    ho_ver = ho_meta.get("version", "")
    raw_ver = version.strip()
    if not raw_ver:
        raw_ver = ho_ver if (ho_ver and (not tag or version_matches_tag(ho_ver, tag))) else tag.strip()
    eff_br = branch.strip() or current_branch(roots["cwd"]) or ho_meta.get("branch", "")
    if not raw_ver or not eff_br:
        return "", "", "[REVIEW_SCOPE_MISSING] unable to resolve version and branch for release gate"
    try:
        return make_scope_key(raw_ver, eff_br), eff_br, ""
    except ValueError as exc:
        return "", "", f"[REVIEW_SCOPE_INVALID] {exc}"


def _validate_pass_meta(roots: dict, latest: dict, tag: str, eff_br: str, reviewed_sha: str) -> list[str]:
    violations: list[str] = []
    if tag and not version_matches_tag(latest["version"], tag):
        violations.append(f"[VERSION_MISMATCH] review version={latest['version']} vs tag={tag}")
    if latest["branch"] != eff_br:
        violations.append(f"[BRANCH_MISMATCH] review branch={latest['branch']} vs {eff_br}")
    if reviewed_sha.strip() and resolve_commit_sha(roots["cwd"], reviewed_sha.strip()) != latest["reviewed_sha"]:
        violations.append("[REVIEW_SHA_MISMATCH] --reviewed-sha differs from latest pass record")
    fp = docs_fingerprint(roots["cwd"], roots["mode"], roots["docs"])
    violations += verify_doc_attestation(fp, latest)
    return violations


def verify_review_pass_for_gate(
    cwd: pathlib.Path, state_root_arg: str = "", mode: str = "", tag: str = "",
    version: str = "", branch: str = "", reviewed_sha: str = "", docs_root_arg: str = "",
) -> tuple[str, list[str]]:
    roots, root_errs = resolve_review_roots(cwd, state_root_arg, docs_root_arg, mode)
    if root_errs or roots is None:
        return "", root_errs
    scope_key, eff_br, scope_err = _resolve_gate_scope(roots, tag, version, branch)
    if scope_err:
        return "", [scope_err]
    records, errs = list_scope_records(roots["state"] / ".loop" / "reviews" / scope_key)
    if errs:
        return "", errs
    if not records:
        return "", [f"[REVIEW_RECORD_MISSING] no review records under .loop/reviews/{scope_key}"]
    latest = records[-1]
    if latest["verdict"] != "pass":
        return "", [f"[REVIEW_NOT_PASSED] latest record {latest['filename']} has verdict={latest['verdict']}"]
    return latest["reviewed_sha"], _validate_pass_meta(roots, latest, tag, eff_br, reviewed_sha)
