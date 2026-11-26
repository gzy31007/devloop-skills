#!/usr/bin/env python3
"""Review verdict recording and release-gate attestation verification. Stdlib only."""

from __future__ import annotations

import pathlib
from datetime import datetime, timezone

from devloop_docs import docs_fingerprint, verify_doc_attestation
from devloop_git import current_branch, head_sha, is_worktree_clean, resolve_commit_sha
from devloop_preflight import evaluate_preflight, load_handoff_content, resolve_review_roots
from devloop_review_io import acquire_review_lock, atomic_write_new, read_body_outside_repo
from devloop_review_store import (
    body_sha256,
    count_rejects,
    derive_summary,
    format_review_doc,
    list_scope_records,
    make_scope_key,
    parse_handoff_meta,
    version_matches_tag,
)


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


def _verify_lock_drift(roots: dict, pf: dict) -> list[str]:
    errs: list[str] = []
    cand_sha = pf["candidate_sha"]
    if head_sha(roots["cwd"]) != cand_sha:
        errs.append("[DRIFT_HEAD_CHANGED] candidate HEAD changed before review lock write")
    for label, groot in (("cwd", roots["cwd_git"]), ("state_root", roots["state_git"])):
        if label == "state_root" and groot == roots["cwd_git"]:
            continue
        clean, first = is_worktree_clean(groot)
        if clean is False:
            errs.append(f"[DIRTY_WORKTREE] {label} changed before lock write: {first}")
    _, ho_sha, ho_errs = load_handoff_content(roots, cand_sha)
    errs += ho_errs
    if not ho_errs and ho_sha != pf["handoff_sha256"]:
        errs.append("[DRIFT_HANDOFF_CHANGED] handoff.md changed before review lock write")
    fp_now = docs_fingerprint(roots["cwd"], roots["mode"], roots["docs"])
    fp_prev = pf["doc_fingerprints"]
    if (
        fp_now["decisions_sha256"] != fp_prev["decisions_sha256"]
        or fp_now["feature_map_sha256"] != fp_prev["feature_map_sha256"]
    ):
        errs.append("[DRIFT_DOCS_CHANGED] DECISIONS.md or FEATURE_MAP.md changed before lock write")
    return errs


def _write_locked_record(
    roots: dict, pf: dict, verdict: str, category: str, reviewer: str,
    norm_body: str, summary: str, attest: dict | None,
) -> tuple[int, dict]:
    scope_key, eff_ver, eff_br, cur_head = pf["scope_key"], pf["version"], pf["branch"], pf["candidate_sha"]
    b_sha = body_sha256(norm_body)
    eff_sum = derive_summary(summary, norm_body)
    scope_dir = roots["state"] / ".loop" / "reviews" / scope_key
    with acquire_review_lock(roots["state"]):
        drift_errs = _verify_lock_drift(roots, pf)
        if drift_errs:
            return 1, {"status": "FAILED", "violations": drift_errs}
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
    mode: str = "", version: str = "", branch: str = "", baseline: str = "", reviewed_sha: str = "",
    reviewer: str = "loop-review", summary: str = "", body_file: str = "", body_text: str = "",
    dec_status: str = "", dec_summary: str = "", feat_status: str = "", feat_summary: str = "",
    base_dec_sha: str = "", base_feat_sha: str = "",
) -> tuple[int, dict]:
    if verdict not in ("reject", "pending", "pass"):
        return 2, {"status": "FAILED", "violations": [f"[INVALID_VERDICT] unsupported verdict: {verdict!r}"]}
    if verdict == "reject" and category not in ("method", "env"):
        return 2, {"status": "FAILED", "violations": ["[CATEGORY_REQUIRED] --category method|env required for reject"]}
    pf_code, pf, roots, attest = evaluate_preflight(
        cwd, state_root=state_root, docs_root=docs_root, mode=mode,
        version=version, branch=branch, baseline=baseline, reviewed_sha=reviewed_sha,
        dec_status=dec_status, dec_summary=dec_summary, feat_status=feat_status, feat_summary=feat_summary,
        base_dec_sha=base_dec_sha, base_feat_sha=base_feat_sha,
    )
    if pf_code == 2 or roots is None:
        return 2, {"status": "FAILED", "violations": pf["violations"]}
    errors = list(pf["violations"])
    norm_body, body_err = read_body_outside_repo(body_file, body_text, roots["cwd"], roots["cwd_git"], roots["state_git"])
    if body_err:
        errors.append(body_err)
    if errors:
        return 1, {"status": "FAILED", "violations": errors}
    return _write_locked_record(
        roots, pf, verdict, category, reviewer, norm_body, summary, attest if verdict == "pass" else None,
    )


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
