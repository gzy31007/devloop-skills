#!/usr/bin/env python3
"""Scope-key derivation, handoff metadata parsing, and review record serialization. Stdlib only."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

from devloop_docs import split_md_row
from devloop_review_io import normalize_body

META_PREFIX = "<!-- devloop:review-meta "
META_SUFFIX = " -->"
RECORD_NAME_RE = re.compile(r"^r(\d{2,})-(reject|pending|pass)\.md$")
COMMON_TEXT_FIELDS = ("version", "branch", "verdict", "reviewed_sha", "reviewer", "created_at", "body_sha256", "summary")
PASS_FIELDS = (
    "mode", "decisions_sha256", "decisions_status", "decisions_summary",
    "feature_map_sha256", "feature_map_status", "feature_map_summary",
)
PLACEHOLDER_TEXTS = frozenset({"", "待填", "无", "none", "tbd", "todo", "...", "n/a", "-"})
HANDOFF_FIELD_KEYS = {
    "版本 / 主题": "version", "版本/主题": "version", "版本": "version",
    "分支": "branch", "源分支": "branch",
    "基线 SHA": "baseline_sha", "基线": "baseline_sha",
    "改动范围": "has_evidence", "自测与返工验证证据": "has_evidence", "自测命令与退出码": "has_evidence",
}


def version_matches_tag(version: str, tag: str) -> bool:
    v_norm, t_norm = version.strip().lstrip("vV"), tag.strip().lstrip("vV")
    if not v_norm or not t_norm:
        return False
    if v_norm == t_norm:
        return True
    v_core, v_pre = (v_norm.split("-", 1) + [""])[:2]
    t_core, t_pre = (t_norm.split("-", 1) + [""])[:2]
    if v_pre != t_pre:
        return False
    v_tok, t_tok = v_core.split("."), t_core.split(".")
    if not all(x.isdigit() for x in v_tok) or not all(x.isdigit() for x in t_tok):
        return False
    v_parts, t_parts = [int(x) for x in v_tok], [int(x) for x in t_tok]
    size = max(len(v_parts), len(t_parts))
    return (v_parts + [0] * (size - len(v_parts))) == (t_parts + [0] * (size - len(t_parts)))


def normalize_version(raw: str) -> str:
    cleaned = raw.strip().strip("`").strip()
    if not cleaned or "<" in cleaned or ">" in cleaned or cleaned.lower() in PLACEHOLDER_TEXTS:
        raise ValueError("missing or placeholder version")
    low = cleaned.lower()
    if low[0].isdigit():
        low = f"v{low}"
    slug = re.sub(r"[^a-z0-9._-]+", "-", low).strip("-")
    if not slug:
        raise ValueError(f"invalid version: {raw!r}")
    return slug


def make_scope_key(version: str, branch: str) -> str:
    ver_slug = normalize_version(version)
    br_clean = branch.strip().strip("`").strip()
    if not br_clean or "<" in br_clean or ">" in br_clean:
        raise ValueError("missing or invalid branch")
    br_slug = re.sub(r"[^a-z0-9._-]+", "-", br_clean.lower()).strip("-")[:40] or "branch"
    br_hash = hashlib.sha256(br_clean.encode("utf-8")).hexdigest()[:8]
    return f"{ver_slug}__{br_slug}-{br_hash}"


def _clean_cell(val: str) -> str:
    text = val.strip().strip("`").strip()
    return "" if "<" in text or ">" in text or text.lower() in PLACEHOLDER_TEXTS else text


def parse_handoff_meta(handoff_path: pathlib.Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    if not handoff_path.is_file():
        return meta
    for line in handoff_path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = split_md_row(stripped)
        field = HANDOFF_FIELD_KEYS.get(cells[0].strip("*` ")) if len(cells) >= 2 else None
        val = _clean_cell(cells[1]) if field else ""
        if not field or not val:
            continue
        if field == "version":
            val = _clean_cell(val.split("/", 1)[0])
        elif field == "has_evidence":
            val = "1"
        if val:
            meta[field] = val
    return meta


def body_sha256(normalized_body: str) -> str:
    return hashlib.sha256(normalized_body.encode("utf-8")).hexdigest()


def derive_summary(explicit: str, body: str) -> str:
    if explicit.strip():
        return re.sub(r"\s+", " ", explicit.strip())[:160]
    for line in body.splitlines():
        cleaned = re.sub(r"\s+", " ", line.strip().lstrip("#*> -`")).strip()
        if cleaned:
            return cleaned[:160]
    return ""


def format_review_doc(meta: dict, normalized_body: str) -> str:
    encoded = json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("-->", "--\\u003e")
    return f"{META_PREFIX}{encoded}{META_SUFFIX}\n\n{normalized_body}"


def _missing_str_key(meta: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        val = meta.get(key)
        if not isinstance(val, str) or not val.strip():
            return key
    return ""


def _validate_meta_fields(meta: object, exp_round: int, exp_verdict: str, name: str) -> str:
    if not isinstance(meta, dict):
        return f"[REVIEW_RECORD_CORRUPT] {name}: metadata must be JSON object"
    if not isinstance(meta.get("round"), int) or meta.get("round") != exp_round:
        return f"[REVIEW_RECORD_CORRUPT] {name}: invalid round {meta.get('round')!r}"
    missing = _missing_str_key(meta, COMMON_TEXT_FIELDS)
    if missing:
        return f"[REVIEW_RECORD_CORRUPT] {name}: missing {missing}"
    if meta.get("verdict") != exp_verdict:
        return f"[REVIEW_RECORD_CORRUPT] {name}: verdict mismatch"
    if exp_verdict == "reject" and meta.get("category") not in ("method", "env"):
        return f"[REVIEW_RECORD_CORRUPT] {name}: invalid reject category"
    if exp_verdict == "pass":
        p_miss = _missing_str_key(meta, PASS_FIELDS)
        if p_miss:
            return f"[REVIEW_RECORD_CORRUPT] {name}: missing pass attestation {p_miss}"
    return ""


def parse_review_file(path: pathlib.Path) -> tuple[dict | None, str]:
    match = RECORD_NAME_RE.match(path.name)
    if not match:
        return None, f"[REVIEW_RECORD_INVALID_FILE] unexpected filename: {path.name}"
    exp_round, exp_verdict = int(match.group(1)), match.group(2)
    try:
        raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except OSError as exc:
        return None, f"[REVIEW_RECORD_CORRUPT] {path.name}: {exc}"
    lines = raw.split("\n")
    header = lines[0] if lines else ""
    if not header.startswith(META_PREFIX) or not header.endswith(META_SUFFIX):
        return None, f"[REVIEW_RECORD_CORRUPT] {path.name}: missing review-meta comment"
    try:
        meta = json.loads(header[len(META_PREFIX):-len(META_SUFFIX)])
    except ValueError as exc:
        return None, f"[REVIEW_RECORD_CORRUPT] {path.name}: invalid JSON metadata ({exc})"
    err = _validate_meta_fields(meta, exp_round, exp_verdict, path.name)
    if err:
        return None, err
    body_lines = lines[2:] if len(lines) > 1 and lines[1] == "" else lines[1:]
    norm = normalize_body("\n".join(body_lines))
    if not norm.strip() or body_sha256(norm) != meta["body_sha256"]:
        return None, f"[REVIEW_RECORD_CORRUPT] {path.name}: body checksum mismatch"
    return {**meta, "filename": path.name, "path": str(path)}, ""


def list_scope_records(scope_dir: pathlib.Path) -> tuple[list[dict], list[str]]:
    if not scope_dir.exists():
        return [], []
    if scope_dir.is_symlink() or not scope_dir.is_dir():
        return [], [f"[REVIEW_SCOPE_INVALID] {scope_dir} is not a regular directory"]
    records, errors, seen_rounds = [], [], set()
    for item in sorted(scope_dir.iterdir(), key=lambda p: p.name):
        if item.is_symlink() or not item.is_file():
            errors.append(f"[REVIEW_RECORD_INVALID_FILE] non-file entry: {item.name}")
            continue
        rec, err = parse_review_file(item)
        if err or rec is None:
            errors.append(err)
            continue
        if rec["round"] in seen_rounds:
            errors.append(f"[REVIEW_RECORD_DUPLICATE_ROUND] duplicate round {rec['round']}: {item.name}")
            continue
        seen_rounds.add(rec["round"])
        records.append(rec)
    records.sort(key=lambda item: item["round"])
    return records, errors


def list_active_scope_keys(reviews_root: pathlib.Path) -> list[str]:
    if not reviews_root.is_dir() or reviews_root.is_symlink():
        return []
    keys = []
    for item in sorted(reviews_root.iterdir(), key=lambda p: p.name):
        if item.is_dir() and not item.is_symlink() and any(item.iterdir()):
            keys.append(item.name)
    return keys


def count_rejects(records: list[dict]) -> dict:
    rejects = [r for r in records if r["verdict"] == "reject"]
    method_n = sum(1 for r in rejects if r.get("category") == "method")
    env_n = sum(1 for r in rejects if r.get("category") == "env")
    pending_n = sum(1 for r in records if r["verdict"] == "pending")
    return {
        "reject_count": len(rejects),
        "method_reject_count": method_n,
        "env_reject_count": env_n,
        "pending_count": pending_n,
        "cross_model_recommended": len(rejects) >= 2,
        "circuit_breaker_triggered": len(rejects) >= 3,
    }


def find_archived_pass_records(archive_root: pathlib.Path, version: str) -> list[tuple[str, dict]]:
    if not version.strip():
        return []
    found: list[tuple[str, dict]] = []
    for key in list_active_scope_keys(archive_root):
        if not version_matches_tag(key.split("__", 1)[0], version):
            continue
        recs, errs = list_scope_records(archive_root / key)
        if not errs and recs and recs[-1]["verdict"] == "pass" and version_matches_tag(recs[-1]["version"], version):
            found.append((key, recs[-1]))
    return found
