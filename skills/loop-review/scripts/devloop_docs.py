#!/usr/bin/env python3
"""Document-sync fingerprint attestation and template-placeholder checks. Stdlib only."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

PLACEHOLDER_TEXTS = frozenset({"", "待填", "无", "无变更", "none", "tbd", "todo", "...", "n/a", "-"})
STATUS_MAP = {
    "updated": "updated",
    "已更新": "updated",
    "no_applicable_change": "no_applicable_change",
    "本轮无适用变更": "no_applicable_change",
}


DOC_SYNC_HEADING_RE = re.compile(r"^#{1,6}\s+文档同步声明\b")
ANY_HEADING_RE = re.compile(r"^#{1,6}\s+")


def ref_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "references"


def bytes_sha256(data: bytes) -> str:
    """Compute SHA-256 after stripping UTF-8 BOM and normalizing CRLF to LF for cross-worktree stability."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _doc_sha256(path: pathlib.Path) -> str:
    return bytes_sha256(path.read_bytes())


def resolve_mode(cwd: pathlib.Path, explicit_mode: str, state_root: pathlib.Path | None = None) -> str:
    if explicit_mode in ("own", "external"):
        return explicit_mode
    roots = [state_root, cwd] if state_root else [cwd]
    for root in roots:
        cfg = root / ".loop" / "config.json"
        if not cfg.is_file():
            continue
        try:
            loaded = json.loads(cfg.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict) and loaded.get("mode") in ("own", "external"):
            return str(loaded["mode"])
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


def split_md_row(line: str) -> list[str]:
    """Split a Markdown table row on unescaped `|` delimiters and unescape `\\|`."""
    text = line.strip()
    if not text:
        return []
    cells, buf, pos, last_delim_end = [], [], 0, -1
    for match in re.finditer(r"\\(\||\\)|(\|)", text):
        buf.append(text[pos:match.start()])
        esc, pipe = match.groups()
        if pipe:
            cells.append("".join(buf).strip())
            buf, last_delim_end = [], match.end()
        else:
            buf.append("|" if esc == "|" else "\\\\")
        pos = match.end()
    buf.append(text[pos:])
    if last_delim_end != len(text):
        cells.append("".join(buf).strip())
    return cells[1:] if text.startswith("|") else cells


def _is_placeholder_cell(cell: str) -> bool:
    s = cell.strip().strip("`").strip()
    return not s or (s.startswith("<") and s.endswith(">")) or s.lower() in PLACEHOLDER_TEXTS


def _is_separator_or_header(cells: list[str]) -> bool:
    if all(set(c) <= set("-: ") for c in cells):
        return True
    return cells[0].strip("*` ") in ("版本", "功能", "编号")


def _is_feature_map_template(path: pathlib.Path) -> bool:
    raw = _norm_text(path.read_text(encoding="utf-8-sig"))
    tmpl = _norm_text((ref_dir() / "feature_map.template.md").read_text(encoding="utf-8-sig"))
    if not raw or raw == tmpl:
        return True
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = split_md_row(stripped)
        if len(cells) >= 3 and not _is_separator_or_header(cells) and not all(_is_placeholder_cell(c) for c in cells):
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


def _scoped_sync_lines(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines) if DOC_SYNC_HEADING_RE.match(line.strip())]
    if len(heads) > 1:
        return [], ["[DOC_SYNC_DUPLICATE] multiple 文档同步声明 sections in handoff"]
    if not heads:
        return [ln.strip() for ln in lines if ln.strip().startswith("|")], []
    collected, in_table = [], False
    for raw in lines[heads[0] + 1:]:
        s = raw.strip()
        if s.startswith("|"):
            in_table = True
            collected.append(s)
        elif in_table or ANY_HEADING_RE.match(s) or s.startswith("<!-- devloop:"):
            break
    return collected, []


def parse_handoff_doc_sync_text(text: str) -> tuple[dict[str, tuple[str, str]], list[str]]:
    lines, errors = _scoped_sync_lines(text)
    found: dict[str, tuple[str, str]] = {}
    targets = {
        "DECISIONS.md": ("decisions", "DECISIONS.md"),
        ".loop/DECISIONS.md": ("decisions", "DECISIONS.md"),
        "FEATURE_MAP.md": ("feature_map", "FEATURE_MAP.md"),
        ".loop/FEATURE_MAP.md": ("feature_map", "FEATURE_MAP.md"),
    }
    for s in lines:
        cells = split_md_row(s)
        hit = targets.get(cells[0].strip().strip("`* ")) if len(cells) >= 3 else None
        if not hit or all(set(c) <= set("-: ") for c in cells):
            continue
        key, label = hit
        if key in found:
            errors.append(f"[DOC_SYNC_DUPLICATE] duplicate {label} row in handoff")
            continue
        found[key] = (STATUS_MAP.get(cells[1].strip().strip("`"), ""), cells[2].strip())
    return found, errors


def parse_handoff_doc_sync(handoff_path: pathlib.Path) -> dict[str, tuple[str, str]]:
    if not handoff_path.is_file():
        return {}
    return parse_handoff_doc_sync_text(handoff_path.read_text(encoding="utf-8-sig"))[0]


def validate_doc_claim(label: str, status: str, note: str, is_tmpl: bool, cur_sha: str, base_sha: str) -> list[str]:
    errs: list[str] = []
    if status not in ("updated", "no_applicable_change"):
        return [f"[DOC_SYNC_INVALID] {label}: status must be updated or no_applicable_change"]
    if not _valid_note(note):
        errs.append(f"[DOC_SYNC_EMPTY] {label}: concrete summary or no-change reason required")
    if is_tmpl:
        errs.append(f"[DOCS_TEMPLATE_ONLY] {label}: still matches initial template")
    if base_sha and status == "updated" and cur_sha == base_sha:
        errs.append(f"[DOC_UNCHANGED] {label}: declared updated while SHA matches baseline")
    return errs


def _resolve_claim(label: str, cli_status: str, cli_note: str, handoff_pair: tuple[str, str]) -> tuple[str, str, list[str]]:
    errs: list[str] = []
    raw_cli = cli_status.strip()
    mapped = STATUS_MAP.get(raw_cli, "") if raw_cli else ""
    if raw_cli and not mapped:
        errs.append(f"[DOC_SYNC_INVALID] {label}: invalid CLI status {raw_cli!r}")
    elif mapped and handoff_pair[0] and mapped != handoff_pair[0]:
        errs.append(f"[DOC_SYNC_CONFLICT] {label}: CLI status {mapped} conflicts with handoff status {handoff_pair[0]}")
    return (mapped or handoff_pair[0]), (cli_note.strip() or handoff_pair[1]), errs


def collect_missing_docs(fp: dict) -> list[str]:
    errs: list[str] = []
    if not fp["decisions_exists"]:
        errs.append(f"[DOCS_MISSING] missing {fp['decisions_path']}")
    if not fp["feature_map_exists"]:
        errs.append(f"[DOCS_MISSING] missing {fp['feature_map_path']}")
    return errs


def build_doc_attestation(
    cwd: pathlib.Path, state_root: pathlib.Path, mode: str = "", docs_root: pathlib.Path | None = None,
    dec_status: str = "", dec_summary: str = "", feat_status: str = "", feat_summary: str = "",
    base_dec_sha: str = "", base_feat_sha: str = "", handoff_text: str | None = None,
) -> tuple[dict | None, list[str]]:
    cwd = cwd.expanduser().resolve()
    eff_mode = resolve_mode(cwd, mode, state_root=state_root)
    eff_docs = docs_root if docs_root else (state_root if eff_mode == "external" else cwd)
    fp = docs_fingerprint(cwd, eff_mode, eff_docs)
    errors = collect_missing_docs(fp)
    if handoff_text is not None:
        hs, sync_errs = parse_handoff_doc_sync_text(handoff_text)
    else:
        ho = state_root / ".loop" / "handoff.md"
        hs, sync_errs = parse_handoff_doc_sync_text(ho.read_text(encoding="utf-8-sig")) if ho.is_file() else ({}, [])
    errors += sync_errs
    d_stat, d_note, d_errs = _resolve_claim("DECISIONS.md", dec_status, dec_summary, hs.get("decisions", ("", "")))
    f_stat, f_note, f_errs = _resolve_claim("FEATURE_MAP.md", feat_status, feat_summary, hs.get("feature_map", ("", "")))
    errors += d_errs + f_errs
    if fp["decisions_exists"] and not d_errs:
        errors += validate_doc_claim("DECISIONS.md", d_stat, d_note, fp["decisions_is_template"], fp["decisions_sha256"], base_dec_sha)
    if fp["feature_map_exists"] and not f_errs:
        errors += validate_doc_claim("FEATURE_MAP.md", f_stat, f_note, fp["feature_map_is_template"], fp["feature_map_sha256"], base_feat_sha)
    if errors:
        return None, errors
    return {
        "mode": fp["mode"],
        "decisions_sha256": fp["decisions_sha256"],
        "decisions_status": d_stat,
        "decisions_summary": d_note,
        "feature_map_sha256": fp["feature_map_sha256"],
        "feature_map_status": f_stat,
        "feature_map_summary": f_note,
    }, []


def verify_doc_attestation(fp: dict, meta: dict) -> list[str]:
    errs = collect_missing_docs(fp)
    pairs = (
        ("DECISIONS.md", "decisions_exists", "decisions_sha256", "decisions_is_template", "decisions_status", "decisions_summary"),
        ("FEATURE_MAP.md", "feature_map_exists", "feature_map_sha256", "feature_map_is_template", "feature_map_status", "feature_map_summary"),
    )
    for label, ex_k, sha_k, tmpl_k, st_k, note_k in pairs:
        if not fp[ex_k]:
            continue
        if meta.get(sha_k) != fp[sha_k]:
            errs.append(f"[DOC_FINGERPRINT_MISMATCH] {label} changed since review pass")
        errs += validate_doc_claim(label, str(meta.get(st_k, "")), str(meta.get(note_k, "")), fp[tmpl_k], fp[sha_k], "")
    return errs
