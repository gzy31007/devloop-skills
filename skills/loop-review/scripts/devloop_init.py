#!/usr/bin/env python3
"""Idempotent devloop workspace initializer with atomic preflight checks. Stdlib only."""

from __future__ import annotations

import json
import pathlib

from devloop_docs import ref_dir
from devloop_git import (
    is_ignored,
    is_tracked,
    legacy_shadow_config_path,
    resolve_git_exclude,
    resolve_git_root,
    shadow_config_path,
)
from devloop_markers import (
    INDEX_END,
    INDEX_START,
    STATUS_END,
    STATUS_START,
    render_agents_block,
    render_status_block,
    upsert_marker_block,
    validate_marker_pair,
)

VALID_MODES = ("own", "external")
REQUIRED_TEMPLATES = ("config.example.json", "decisions.template.md", "feature_map.template.md", "handoff.template.md")


def detect_languages(cwd: pathlib.Path) -> list[str] | str:
    found: set[str] = set()
    if (cwd / "pyproject.toml").is_file() or (cwd / "setup.py").is_file() or any(cwd.glob("*.py")):
        found.add("python")
    if (cwd / "tsconfig.json").is_file() or any(cwd.glob("*.ts")) or any(cwd.glob("*.tsx")):
        found.add("ts")
    if (cwd / "package.json").is_file() or any(cwd.glob("*.js")) or any(cwd.glob("*.jsx")):
        found.add("js")
    return sorted(found) if found else "auto"


def _load_existing_config(cfg_path: pathlib.Path) -> tuple[dict | None, str]:
    if not cfg_path.exists():
        return None, ""
    if cfg_path.is_symlink() or not cfg_path.is_file():
        return None, f"invalid config path: {cfg_path}"
    try:
        loaded = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return None, f"corrupt JSON in {cfg_path}: {exc}"
    if not isinstance(loaded, dict):
        return None, f"config root must be JSON object: {cfg_path}"
    return loaded, ""


def _resolve_mode(requested: str, existing: dict | None, switch_mode: bool) -> tuple[str, str]:
    prev = str(existing.get("mode", "")).strip() if existing else ""
    if prev and prev not in VALID_MODES:
        return "", f"invalid existing mode in config: {prev!r}"
    if not requested:
        return prev or "own", ""
    if requested not in VALID_MODES:
        return "", f"unsupported mode: {requested!r}"
    if prev and prev != requested and not switch_mode:
        return "", f"mode conflict (existing={prev}, requested={requested}); pass --switch-mode"
    return requested, ""


def _preflight_external(cwd: pathlib.Path) -> tuple[dict | None, str]:
    status, git_root = resolve_git_root(cwd)
    if status != "ok" or git_root is None:
        return None, f"external mode requires a git repository (status={status})"
    exclude_path = resolve_git_exclude(git_root)
    if exclude_path is None:
        return None, "failed to resolve git info/exclude path"
    rel_base = cwd.relative_to(git_root).as_posix()
    prefix = "" if rel_base in ("", ".") else f"{rel_base}/"
    loop_tracked = is_tracked(git_root, f"{prefix}.loop")
    if loop_tracked is None or loop_tracked:
        return None, f"external mode refuses tracked or unreadable {prefix}.loop/"
    agents_tracked = is_tracked(git_root, f"{prefix}AGENTS.md")
    if agents_tracked is None:
        return None, "git ls-files failed while inspecting AGENTS.md"
    return {"git_root": git_root, "exclude_path": exclude_path, "prefix": prefix, "agents_tracked": agents_tracked}, ""


def _preflight_targets(cwd: pathlib.Path, mode: str, agents_tracked: bool) -> str:
    doc_dir = (cwd / ".loop") if mode == "external" else cwd
    targets = [
        cwd / ".loop", cwd / ".loop" / "config.json", cwd / ".loop" / "handoff.md",
        doc_dir / "DECISIONS.md", doc_dir / "FEATURE_MAP.md",
    ]
    if not agents_tracked:
        targets.append(cwd / "AGENTS.md")
    for path in targets:
        if path.is_symlink():
            return f"symlink rejected: {path}"
    if not agents_tracked and (cwd / "AGENTS.md").is_file():
        _, err = validate_marker_pair((cwd / "AGENTS.md").read_text(encoding="utf-8"), INDEX_START, INDEX_END, "AGENTS.md")
        if err:
            return err
    if (cwd / ".loop" / "handoff.md").is_file():
        text = (cwd / ".loop" / "handoff.md").read_text(encoding="utf-8")
        _, err = validate_marker_pair(text, STATUS_START, STATUS_END, ".loop/handoff.md")
        if err:
            return err
    return ""


def _preflight_all(cwd: pathlib.Path, req_mode: str, switch_mode: bool) -> tuple[dict | None, str]:
    if not cwd.is_dir():
        return None, f"target directory missing: {cwd}"
    refs = ref_dir()
    missing = [name for name in REQUIRED_TEMPLATES if not (refs / name).is_file()]
    if missing:
        return None, f"kit template missing: {missing[0]}"
    existing_cfg, err = _load_existing_config(cwd / ".loop" / "config.json")
    if err:
        return None, err
    mode, err = _resolve_mode(req_mode, existing_cfg, switch_mode)
    if err:
        return None, err
    ext = None
    if mode == "external":
        ext, err = _preflight_external(cwd)
        if err:
            return None, err
    err = _preflight_targets(cwd, mode, bool(ext and ext["agents_tracked"]))
    if err:
        return None, err
    return {"mode": mode, "existing_cfg": existing_cfg, "ext": ext, "refs": refs}, ""


def _ensure_excludes(ext: dict) -> tuple[bool, list[str], str]:
    git_root, exclude_path, prefix = ext["git_root"], ext["exclude_path"], ext["prefix"]
    probes = [(f"/{prefix}.loop/", f"{prefix}.loop/config.json")]
    if not ext["agents_tracked"]:
        probes.append((f"/{prefix}AGENTS.md", f"{prefix}AGENTS.md"))
    orig_exists = exclude_path.exists()
    orig_text = exclude_path.read_text(encoding="utf-8") if orig_exists else ""
    lines = orig_text.splitlines()
    existing_set = {line.rstrip() for line in lines}
    added = []
    for rule, probe in probes:
        if is_ignored(git_root, probe) is True:
            continue
        if rule not in existing_set:
            lines.append(rule)
            existing_set.add(rule)
            added.append(rule)
    if added:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    for rule, probe in probes:
        if is_ignored(git_root, probe) is not True:
            _restore_file(exclude_path, orig_exists, orig_text, bool(added))
            return False, [], f"git check-ignore failed for {probe} after writing {rule}"
    return True, added, ""


def _restore_file(path: pathlib.Path, existed: bool, orig_text: str, changed: bool) -> None:
    if not changed:
        return
    if existed:
        path.write_text(orig_text, encoding="utf-8", newline="\n")
    else:
        path.unlink(missing_ok=True)


def _copy_if_missing(src: pathlib.Path, dst: pathlib.Path) -> str:
    if dst.exists():
        return "kept_existing"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return "created"


def _write_config(cwd: pathlib.Path, state: dict, languages: str, migrate_shadow: bool) -> dict:
    defaults = json.loads((state["refs"] / "config.example.json").read_text(encoding="utf-8-sig"))
    merged = dict(defaults)
    mode = state["mode"]
    if mode == "external" and migrate_shadow:
        legacy = legacy_shadow_config_path(cwd)
        if legacy.is_file():
            try:
                old = json.loads(legacy.read_text(encoding="utf-8-sig"))
                if isinstance(old, dict):
                    merged.update(old)
            except (OSError, ValueError):
                pass
    if state["existing_cfg"]:
        merged.update(state["existing_cfg"])
    elif languages == "auto":
        merged["languages"] = detect_languages(cwd)
    else:
        merged["languages"] = [item.strip() for item in languages.split(",") if item.strip()]
    merged["mode"] = mode
    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    cfg_path = cwd / ".loop" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(text, encoding="utf-8", newline="\n")
    if mode == "external":
        shadow = shadow_config_path(cwd)
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_text(text, encoding="utf-8", newline="\n")
    return merged


def run_init(cwd: pathlib.Path, mode: str = "", languages: str = "auto",
             switch_mode: bool = False, migrate_shadow: bool = False) -> tuple[int, dict]:
    cwd = cwd.expanduser().resolve()
    state, err = _preflight_all(cwd, mode, switch_mode)
    if err or state is None:
        return 2, {"status": "FAILED", "error": err}
    eff_mode = state["mode"]
    exclude_ok, exclude_added = False, []
    if eff_mode == "external":
        exclude_ok, exclude_added, err = _ensure_excludes(state["ext"])
        if err:
            return 2, {"status": "FAILED", "error": err}
    cfg = _write_config(cwd, state, languages, migrate_shadow)
    doc_dir = (cwd / ".loop") if eff_mode == "external" else cwd
    dec_rel = ".loop/DECISIONS.md" if eff_mode == "external" else "DECISIONS.md"
    feat_rel = ".loop/FEATURE_MAP.md" if eff_mode == "external" else "FEATURE_MAP.md"
    dec_state = _copy_if_missing(state["refs"] / "decisions.template.md", doc_dir / "DECISIONS.md")
    feat_state = _copy_if_missing(state["refs"] / "feature_map.template.md", doc_dir / "FEATURE_MAP.md")
    agents_tracked = bool(state["ext"] and state["ext"]["agents_tracked"])
    if agents_tracked:
        agents_state, index_mode = "skipped_tracked_upstream", "handoff-fallback"
    else:
        agents_state = upsert_marker_block(cwd / "AGENTS.md", INDEX_START, INDEX_END, render_agents_block(eff_mode))
        index_mode = "agents-block"
    handoff_path = cwd / ".loop" / "handoff.md"
    handoff_init = _copy_if_missing(state["refs"] / "handoff.template.md", handoff_path)
    status_block = render_status_block(eff_mode, index_mode, dec_rel, feat_rel, exclude_ok)
    upsert_marker_block(handoff_path, STATUS_START, STATUS_END, status_block)
    return 0, {
        "status": "PASSED",
        "cwd": str(cwd),
        "mode": eff_mode,
        "docs_index_mode": index_mode,
        "agents_md": agents_state,
        "decisions": {"path": dec_rel, "action": dec_state},
        "feature_map": {"path": feat_rel, "action": feat_state},
        "handoff": {"path": ".loop/handoff.md", "action": handoff_init},
        "exclude_added": exclude_added,
        "exclude_verified": exclude_ok,
        "languages": cfg["languages"],
    }
