#!/usr/bin/env python3
"""Document-sync fingerprint attestation and template-placeholder checks. Stdlib only."""

from __future__ import annotations

import hashlib
import json
import pathlib

from devloop_git import run_git

PLACEHOLDER_TEXTS = frozenset({"", "待填", "无", "无变更", "none", "tbd", "todo", "...", "n/a", "-"})
STATUS_MAP = {
    "updated": "updated",
    "已更新": "updated",
    "no_applicable_change": "no_applicable_change",
    "本轮无适用变更": "no_applicable_change",
}


def ref_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "references"


def _doc_sha256(path: pathlib.Path) -> str:
    """Compute SHA-256 after stripping UTF-8 BOM and normalizing CRLF to LF for cross-worktree stability."""
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def resolve_mode(cwd: pathlib.Path, explicit_mode: str) -> str:
    if explicit_mode in ("own", "external"):
        return explicit_mode
    cfg = cwd / ".loop" / "config.json"
    if cfg.is_file():
        try:
            loaded = json.loads(cfg.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict) and loaded.get("mode") in ("own", "external"):
                return str(loaded["mode"])
        except (OSError, ValueError):
            pass
    return "own"


def doc_paths(cwd: pathlib.Path, mode: str, docs_root: pathlib.Path | None = None) -> tuple[pathlib.Path, pathlib.Path]:
    base = docs_root.resolve() if docs_root else cwd
    sub = (base / ".loop") if mode == "external" else base
    return sub / "DECISIONS.md", sub / "FEATURE_MAP.md"


def _norm_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines())


def _is_decisions_template(path: pathlib.Path) -> bool:
    raw = _norm_text(path.read_text(encoding="utf-8-sig"))
    tmpl = _norm_text((ref_dir() / "decisions.template.md").read_text(encoding="utf-8-sig"))
    if not raw or raw == tmpl:
        return True
    return not any("| [active] |" in line or "| [superseded" in line for line in raw.splitlines())


def _is_feature_map_template(path: pathlib.Path) -> bool:
    raw = _norm_text(path.read_text(encoding="utf-8-sig"))
    tmpl = _norm_text((ref_dir() / "feature_map.template.md").read_text(encoding="utf-8-sig"))
    if not raw or raw == tmpl:
        return True
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("| ---"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) >= 3 and cells[0] not in ("版本", "功能") and not all(c.startswith("<") or not c for c in cells):
            return False
    return True


def docs_fingerprint(cwd: pathlib.Path, mode: str = "", docs_root: pathlib.Path | None = None) -> dict:
    cwd = cwd.expanduser().resolve()
    eff_mode = resolve_mode(cwd, mode)
    dec_path, feat_path = doc_paths(cwd, eff_mode, docs_root)
    dec_ok, feat_ok = dec_path.is_file(), feat_path.is_file()
    return {
        "mode": eff_mode,
        "decisions_path": str(dec_path),
        "decisions_exists": dec_ok,
        "decisions_sha256": _doc_sha256(dec_path) if dec_ok else "",
        "decisions_is_template": _is_decisions_template(dec_path) if dec_ok else True,
        "feature_map_path": str(feat_path),
        "feature_map_exists": feat_ok,
        "feature_map_sha256": _doc_sha256(feat_path) if feat_ok else "",
        "feature_map_is_template": _is_feature_map_template(feat_path) if feat_ok else True,
    }


def _valid_note(note: str) -> bool:
    cleaned = note.strip().strip("`")
    return len(cleaned) >= 4 and cleaned.lower() not in PLACEHOLDER_TEXTS


def parse_handoff_doc_sync(handoff_path: pathlib.Path) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    if not handoff_path.is_file():
        return found
    for line in handoff_path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        status = STATUS_MAP.get(cells[1].strip().strip("`"), "")
        if not status:
            continue
        if "DECISIONS.md" in cells[0]:
            found["decisions"] = (status, cells[2].strip())
        elif "FEATURE_MAP.md" in cells[0]:
            found["feature_map"] = (status, cells[2].strip())
    return found


def validate_doc_claim(label: str, status: str, note: str, is_tmpl: bool, cur_sha: str, base_sha: str) -> list[str]:
    errs: list[str] = []
    if status not in ("updated", "no_applicable_change"):
        return [f"[DOC_SYNC_INVALID] {label}: status must be updated or no_applicable_change"]
    if not _valid_note(note):
        errs.append(f"[DOC_SYNC_EMPTY] {label}: concrete summary or no-change reason required")
    if is_tmpl and status != "no_applicable_change":
        errs.append(f"[DOCS_TEMPLATE_ONLY] {label}: still matches initial template")
    if base_sha and status == "updated" and cur_sha == base_sha:
        errs.append(f"[DOC_UNCHANGED] {label}: declared updated while SHA matches baseline")
    return errs


def _resolve_claim(cli_status: str, cli_note: str, handoff_pair: tuple[str, str]) -> tuple[str, str]:
    status = STATUS_MAP.get(cli_status.strip(), "") or handoff_pair[0]
    note = cli_note.strip() or handoff_pair[1]
    return status, note


def _head_sha(cwd: pathlib.Path) -> str:
    proc = run_git(["rev-parse", "HEAD"], cwd)
    return proc.stdout.strip() if proc and proc.returncode == 0 else ""


def _collect_missing_docs(fp: dict) -> list[str]:
    errs: list[str] = []
    if not fp["decisions_exists"]:
        errs.append(f"[DOCS_MISSING] missing {fp['decisions_path']}")
    if not fp["feature_map_exists"]:
        errs.append(f"[DOCS_MISSING] missing {fp['feature_map_path']}")
    return errs


def write_review_stamp(
    cwd: pathlib.Path, mode: str = "", reviewed_sha: str = "", docs_root: pathlib.Path | None = None,
    dec_status: str = "", dec_summary: str = "", feat_status: str = "", feat_summary: str = "",
    base_dec_sha: str = "", base_feat_sha: str = "", out_path: pathlib.Path | None = None,
) -> tuple[int, dict]:
    cwd = cwd.expanduser().resolve()
    fp = docs_fingerprint(cwd, mode, docs_root)
    errors = _collect_missing_docs(fp)
    sha = reviewed_sha.strip() or _head_sha(cwd)
    if not sha:
        errors.append("[REVIEW_SHA_MISSING] unable to resolve candidate SHA")
    hs = parse_handoff_doc_sync(cwd / ".loop" / "handoff.md")
    d_stat, d_note = _resolve_claim(dec_status, dec_summary, hs.get("decisions", ("", "")))
    f_stat, f_note = _resolve_claim(feat_status, feat_summary, hs.get("feature_map", ("", "")))
    if fp["decisions_exists"]:
        errors += validate_doc_claim("DECISIONS.md", d_stat, d_note, fp["decisions_is_template"], fp["decisions_sha256"], base_dec_sha)
    if fp["feature_map_exists"]:
        errors += validate_doc_claim("FEATURE_MAP.md", f_stat, f_note, fp["feature_map_is_template"], fp["feature_map_sha256"], base_feat_sha)
    if errors:
        return 1, {"status": "FAILED", "violations": errors}
    stamp = {
        "reviewed_sha": sha,
        "mode": fp["mode"],
        "decisions_sha256": fp["decisions_sha256"],
        "decisions_status": d_stat,
        "decisions_summary": d_note,
        "feature_map_sha256": fp["feature_map_sha256"],
        "feature_map_status": f_stat,
        "feature_map_summary": f_note,
    }
    target = out_path if out_path else (cwd / ".loop" / "review-stamp.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(stamp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0, {"status": "PASSED", "stamp_path": str(target), "stamp": stamp}


def verify_stamp_and_docs(cwd: pathlib.Path, mode: str, stamp_path: pathlib.Path, reviewed_sha: str) -> tuple[str, list[str]]:
    fp = docs_fingerprint(cwd, mode)
    violations = _collect_missing_docs(fp)
    if not stamp_path.is_file():
        return reviewed_sha, violations + [f"[STAMP_MISSING] review stamp missing: {stamp_path}"]
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return reviewed_sha, violations + [f"[STAMP_INVALID] {stamp_path}: {exc}"]
    if not isinstance(stamp, dict):
        return reviewed_sha, violations + [f"[STAMP_INVALID] {stamp_path}: JSON object expected"]
    eff_sha = reviewed_sha or str(stamp.get("reviewed_sha", "")).strip()
    violations += _compare_stamp_sha(cwd, reviewed_sha, str(stamp.get("reviewed_sha", "")))
    violations += _compare_doc_fingerprints(fp, stamp)
    return eff_sha, violations


def _compare_stamp_sha(cwd: pathlib.Path, cli_sha: str, stamp_sha: str) -> list[str]:
    if not (cli_sha and stamp_sha):
        return []
    r1 = run_git(["rev-parse", cli_sha], cwd)
    r2 = run_git(["rev-parse", stamp_sha], cwd)
    if r1 and r2 and r1.returncode == 0 and r2.returncode == 0 and r1.stdout.strip() == r2.stdout.strip():
        return []
    return ["[STAMP_SHA_MISMATCH] --reviewed-sha differs from review stamp"]


def _compare_doc_fingerprints(fp: dict, stamp: dict) -> list[str]:
    errs: list[str] = []
    pairs = (
        ("DECISIONS.md", "decisions_exists", "decisions_sha256", "decisions_is_template", "decisions_status", "decisions_summary"),
        ("FEATURE_MAP.md", "feature_map_exists", "feature_map_sha256", "feature_map_is_template", "feature_map_status", "feature_map_summary"),
    )
    for label, ex_k, sha_k, tmpl_k, st_k, note_k in pairs:
        if not fp[ex_k]:
            continue
        if stamp.get(sha_k) != fp[sha_k]:
            errs.append(f"[DOC_FINGERPRINT_MISMATCH] {label} changed since review stamp")
        errs += validate_doc_claim(label, str(stamp.get(st_k, "")), str(stamp.get(note_k, "")), fp[tmpl_k], fp[sha_k], "")
    return errs
