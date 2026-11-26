#!/usr/bin/env python3
"""Read-only preflight validation and candidate diff resolution for devloop reviews. Stdlib only."""

from __future__ import annotations

import pathlib
import re

from devloop_docs import build_doc_attestation, bytes_sha256, docs_fingerprint, resolve_mode
from devloop_git import (
    current_branch,
    git_diff_name_status,
    git_show_bytes,
    head_sha,
    is_ancestor,
    is_tracked,
    is_worktree_clean,
    repo_prefix,
    resolve_commit_sha,
    resolve_git_root,
    same_git_repo,
)
from devloop_review_io import decode_text_bytes, verify_review_paths_ignored
from devloop_review_store import make_scope_key, normalize_version, parse_handoff_meta_text

FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")
BRANCH_BAD_CHARS = set("`()（）[]<>\"'*?:\\")


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
    default_docs = eff_state if eff_mode == "external" else eff_cwd
    eff_docs = pathlib.Path(docs_root_arg).expanduser().resolve() if docs_root_arg else default_docs
    return {
        "cwd": eff_cwd, "cwd_git": cwd_git,
        "state": eff_state, "state_git": state_git,
        "mode": eff_mode, "docs": eff_docs,
    }, []


def _check_worktrees_and_head(roots: dict, reviewed_sha: str) -> tuple[str, list[str]]:
    errors: list[str] = []
    if roots["state_git"] != roots["cwd_git"] and not same_git_repo(roots["cwd_git"], roots["state_git"]):
        errors.append("[STATE_ROOT_REPO_MISMATCH] state-root belongs to a different git repository")
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
        errors.append("[HEAD_MISSING] repository has no HEAD commit; create initial baseline commit first")
    elif reviewed_sha.strip():
        target = resolve_commit_sha(roots["cwd"], reviewed_sha.strip())
        if not target or target != cur_head:
            errors.append(f"[CANDIDATE_HEAD_MISMATCH] reviewed_sha differs from HEAD ({cur_head[:12]})")
    return cur_head, errors


def _check_review_dirs_and_mode(roots: dict) -> list[str]:
    _, errs = verify_review_paths_ignored(roots["state"], (".loop/reviews", ".loop/archive"))
    if roots["mode"] == "external":
        c_pref = repo_prefix(roots["cwd"], roots["cwd_git"])
        if is_tracked(roots["cwd_git"], f"{c_pref}.loop"):
            errs.append("[EXTERNAL_LEAK] .loop/ is tracked in external mode")
        elif roots["state_git"] != roots["cwd_git"]:
            s_pref = repo_prefix(roots["state"], roots["state_git"])
            if is_tracked(roots["state_git"], f"{s_pref}.loop"):
                errs.append("[EXTERNAL_LEAK] state_root .loop/ is tracked in external mode")
    return errs


def load_handoff_content(roots: dict, cur_head: str) -> tuple[str, str, list[str]]:
    if roots["mode"] == "own" and cur_head:
        pref = repo_prefix(roots["state"], roots["state_git"])
        raw = git_show_bytes(roots["cwd_git"], f"HEAD:{pref}.loop/handoff.md")
        if raw is None:
            return "", "", ["[HANDOFF_UNTRACKED] .loop/handoff.md is not committed in candidate HEAD"]
        return decode_text_bytes(raw), bytes_sha256(raw), []
    path = roots["state"] / ".loop" / "handoff.md"
    if path.is_symlink() or not path.is_file():
        return "", "", [f"[HANDOFF_MISSING] missing {path}"]
    raw = path.read_bytes()
    return decode_text_bytes(raw), bytes_sha256(raw), []


def _is_pure_branch(name: str) -> bool:
    if not name or any(ch.isspace() or ch in BRANCH_BAD_CHARS for ch in name):
        return False
    if ".." in name or "@{" in name:
        return False
    return not name.startswith(("/", ".")) and not name.endswith(("/", ".", ".lock"))


def _validate_version(ho_meta: dict[str, str], version: str) -> tuple[str, list[str]]:
    errs: list[str] = []
    ho_raw, cli_raw = ho_meta.get("version", ""), version.strip()
    ho_ver, cli_ver = "", ""
    if ho_raw:
        try:
            ho_ver = normalize_version(ho_raw)
        except ValueError:
            errs.append(f"[HANDOFF_VERSION_INVALID] invalid version in handoff.md: {ho_raw!r}")
    else:
        errs.append("[HANDOFF_VERSION_MISSING] version missing in .loop/handoff.md")
    if cli_raw:
        try:
            cli_ver = normalize_version(cli_raw)
        except ValueError:
            errs.append(f"[VERSION_INVALID] invalid --version: {cli_raw!r}")
    if ho_ver and cli_ver and ho_ver != cli_ver:
        errs.append(f"[VERSION_MISMATCH] --version {cli_ver} differs from handoff {ho_ver}")
    return ho_ver or cli_ver, errs


def _validate_detached_branch(roots: dict, cur_head: str, ho_br: str, cli_br: str) -> tuple[str, list[str]]:
    if not cli_br:
        return "", ["[BRANCH_REQUIRED_DETACHED] --branch required in detached HEAD worktree"]
    errs: list[str] = []
    if not _is_pure_branch(cli_br):
        return "", [f"[BRANCH_INVALID] pure branch name required, got {cli_br!r}"]
    if ho_br and ho_br != cli_br:
        errs.append(f"[HANDOFF_BRANCH_MISMATCH] handoff branch {ho_br} differs from --branch {cli_br}")
    tip = resolve_commit_sha(roots["cwd"], f"refs/heads/{cli_br}")
    if not tip:
        errs.append(f"[BRANCH_REF_MISSING] refs/heads/{cli_br} does not exist")
    elif tip != cur_head:
        errs.append(f"[BRANCH_HEAD_MISMATCH] branch {cli_br} tip {tip[:12]} differs from HEAD {cur_head[:12]}")
    return cli_br, errs


def _validate_branch(roots: dict, cur_head: str, ho_meta: dict[str, str], branch: str) -> tuple[str, list[str]]:
    cwd_br = current_branch(roots["cwd"])
    ho_br = ho_meta.get("branch", "").strip()
    cli_br = branch.strip()
    errs: list[str] = []
    if not ho_br:
        errs.append("[HANDOFF_BRANCH_MISSING] branch missing in .loop/handoff.md")
    elif not _is_pure_branch(ho_br):
        errs.append(f"[HANDOFF_BRANCH_INVALID] pure branch name required in handoff.md, got {ho_br!r}")
    if cwd_br:
        if cli_br and cli_br != cwd_br:
            errs.append(f"[BRANCH_MISMATCH] current branch {cwd_br} differs from --branch {cli_br}")
        if ho_br and ho_br != cwd_br:
            errs.append(f"[HANDOFF_BRANCH_MISMATCH] handoff branch {ho_br} differs from {cwd_br}")
        return cwd_br, errs
    eff_br, det_errs = _validate_detached_branch(roots, cur_head, ho_br, cli_br)
    return eff_br, errs + det_errs


def _validate_baseline_and_diff(
    roots: dict, cur_head: str, ho_meta: dict[str, str], baseline: str,
) -> tuple[str, list[dict], list[str]]:
    cli_base = baseline.strip().lower()
    ho_base = ho_meta.get("baseline_sha", "").strip().lower()
    if cli_base and ho_base and cli_base != ho_base:
        return "", [], ["[BASELINE_MISMATCH] --baseline differs from handoff 基线 SHA"]
    raw_base = cli_base or ho_base
    if not raw_base:
        return "", [], ["[BASELINE_MISSING] full baseline SHA required via --baseline or handoff.md"]
    if not FULL_SHA_RE.match(raw_base):
        return "", [], [f"[BASELINE_INVALID] full commit SHA required, got {raw_base!r}"]
    resolved = resolve_commit_sha(roots["cwd"], raw_base)
    if not resolved:
        return "", [], [f"[BASELINE_UNRESOLVED] baseline commit not found: {raw_base}"]
    if resolved == cur_head:
        return "", [], [f"[BASELINE_EQUALS_HEAD] baseline {resolved[:12]} must be a strict ancestor of HEAD"]
    if not is_ancestor(roots["cwd"], resolved, cur_head):
        return "", [], [f"[BASELINE_NOT_ANCESTOR] baseline {resolved[:12]} is not an ancestor of HEAD"]
    entries, diff_err = git_diff_name_status(roots["cwd_git"], resolved, cur_head)
    return (resolved, entries or [], [diff_err] if diff_err else [])


def _baseline_doc_sha(roots: dict, resolved_base: str, fname: str, explicit_sha: str) -> str:
    if explicit_sha.strip():
        return explicit_sha.strip()
    if resolved_base and roots["mode"] == "own" and roots["docs"] == roots["cwd"]:
        raw = git_show_bytes(roots["cwd_git"], f"{resolved_base}:{repo_prefix(roots['cwd'], roots['cwd_git'])}{fname}")
        return bytes_sha256(raw) if raw is not None else ""
    return ""


def _check_docs(
    roots: dict, cur_head: str, resolved_base: str, handoff_text: str,
    dec_status: str, dec_summary: str, feat_status: str, feat_summary: str,
    base_dec_sha: str, base_feat_sha: str,
) -> tuple[dict, dict | None, list[str]]:
    errs: list[str] = []
    if roots["mode"] == "own" and roots["docs"] == roots["cwd"] and cur_head:
        pref = repo_prefix(roots["cwd"], roots["cwd_git"])
        for fname in ("DECISIONS.md", "FEATURE_MAP.md"):
            if git_show_bytes(roots["cwd_git"], f"HEAD:{pref}{fname}") is None:
                errs.append(f"[DOCS_UNTRACKED] {fname} must be committed in candidate HEAD")
    b_dec = _baseline_doc_sha(roots, resolved_base, "DECISIONS.md", base_dec_sha)
    b_feat = _baseline_doc_sha(roots, resolved_base, "FEATURE_MAP.md", base_feat_sha)
    fp = docs_fingerprint(roots["cwd"], roots["mode"], roots["docs"])
    attest, att_errs = build_doc_attestation(
        roots["cwd"], roots["state"], roots["mode"], roots["docs"],
        dec_status, dec_summary, feat_status, feat_summary, b_dec, b_feat, handoff_text=handoff_text,
    )
    return fp, attest, errs + att_errs


def _preflight_payload(
    mode: str, fp: dict, errors: list[str], cur_head: str = "", base_sha: str = "",
    eff_ver: str = "", eff_br: str = "", scope_key: str = "",
    diff_files: list[dict] | None = None, ho_sha: str = "",
) -> dict:
    return {
        "status": "FAILED" if errors else "PASSED", "mode": mode, "candidate_sha": cur_head,
        "baseline_sha": base_sha, "version": eff_ver, "branch": eff_br, "scope_key": scope_key,
        "diff_files": diff_files or [], "doc_fingerprints": fp, "handoff_sha256": ho_sha, "violations": errors,
    }


def _eval_handoff_identity(
    roots: dict, cur_head: str, ho_text: str, ho_errs: list[str], version: str, branch: str,
) -> tuple[dict, str, str, str, list[str]]:
    ho_meta, meta_errs = parse_handoff_meta_text(ho_text) if ho_text else ({}, [])
    errs = list(ho_errs) + meta_errs
    if not ho_errs and not ho_meta.get("has_evidence"):
        errs.append("[HANDOFF_EVIDENCE_MISSING] handoff.md missing 自测与返工验证证据")
    if ho_errs:
        return ho_meta, "", "", "", errs
    eff_ver, ver_errs = _validate_version(ho_meta, version)
    eff_br, br_errs = _validate_branch(roots, cur_head, ho_meta, branch)
    errs += ver_errs + br_errs
    scope = make_scope_key(eff_ver, eff_br) if (eff_ver and eff_br and not ver_errs and not br_errs) else ""
    return ho_meta, eff_ver, eff_br, scope, errs


def evaluate_preflight(
    cwd: pathlib.Path, state_root: str = "", docs_root: str = "", mode: str = "",
    version: str = "", branch: str = "", baseline: str = "", reviewed_sha: str = "",
    dec_status: str = "", dec_summary: str = "", feat_status: str = "", feat_summary: str = "",
    base_dec_sha: str = "", base_feat_sha: str = "",
) -> tuple[int, dict, dict | None, dict | None]:
    roots, root_errs = resolve_review_roots(cwd, state_root, docs_root, mode)
    if root_errs or roots is None:
        return 2, {"status": "FAILED", "violations": root_errs}, None, None
    cur_head, errors = _check_worktrees_and_head(roots, reviewed_sha)
    errors += _check_review_dirs_and_mode(roots)
    fp = docs_fingerprint(roots["cwd"], roots["mode"], roots["docs"])
    if not cur_head:
        return 1, _preflight_payload(roots["mode"], fp, errors), roots, None
    ho_text, ho_sha, ho_errs = load_handoff_content(roots, cur_head)
    ho_meta, eff_ver, eff_br, scope_key, id_errs = _eval_handoff_identity(roots, cur_head, ho_text, ho_errs, version, branch)
    base_sha, diff_files, diff_errs = _validate_baseline_and_diff(roots, cur_head, ho_meta, baseline)
    fp, attest, doc_errs = _check_docs(
        roots, cur_head, base_sha, ho_text, dec_status, dec_summary, feat_status, feat_summary, base_dec_sha, base_feat_sha,
    )
    errors += id_errs + diff_errs + doc_errs
    payload = _preflight_payload(roots["mode"], fp, errors, cur_head, base_sha, eff_ver, eff_br, scope_key, diff_files, ho_sha)
    return (0 if not errors else 1), payload, roots, attest


def run_preflight(
    cwd: pathlib.Path, state_root: str = "", docs_root: str = "", mode: str = "",
    version: str = "", branch: str = "", baseline: str = "", reviewed_sha: str = "",
) -> tuple[int, dict]:
    return evaluate_preflight(cwd, state_root, docs_root, mode, version, branch, baseline, reviewed_sha)[:2]
