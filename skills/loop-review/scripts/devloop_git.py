#!/usr/bin/env python3
"""Shared Git, path-hash, and generated-path helpers for devloop scripts. Stdlib only."""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import sys

GLOB_CHARS = frozenset("*?[")
KIT_SKILLS = ("agent-docs", "hakimi", "loop-init", "loop-plan", "loop-build", "loop-review", "loop-retro")
KIT_SCRIPTS = (
    "arch_gate.py", "arch_js.py", "reap_and_run.py", "devloop_git.py", "devloop_markers.py",
    "devloop_docs.py", "devloop_init.py", "devloop_gate.py", "devloop_ops.py",
    "verify_scripts.py", "verify_ops_cases.py", "verify_gate_cases.py",
)
KIT_REFERENCES = (
    "config.example.json", "decisions.template.md", "feature_map.template.md",
    "handoff.template.md", "review-checklist.md",
)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def run_git(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return None


def resolve_git_root(cwd: pathlib.Path) -> tuple[str, pathlib.Path | None]:
    """Return ('ok', root), ('git_missing', None), ('not_git', None), or ('git_error', None)."""
    proc = run_git(["rev-parse", "--show-toplevel"], cwd)
    if proc is None:
        return "git_missing", None
    if proc.returncode != 0:
        err = (proc.stderr or "").lower()
        return ("not_git", None) if "not a git repository" in err else ("git_error", None)
    raw = proc.stdout.strip()
    return ("ok", pathlib.Path(raw).resolve()) if raw else ("git_error", None)


def resolve_git_exclude(git_root: pathlib.Path) -> pathlib.Path | None:
    proc = run_git(["rev-parse", "--path-format=absolute", "--git-path", "info/exclude"], git_root)
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    return pathlib.Path(proc.stdout.strip())


def is_tracked(git_root: pathlib.Path, repo_rel: str) -> bool | None:
    """Return True if file or any descendant under directory is tracked, None on Git failure."""
    proc = run_git(["ls-files", "--", repo_rel.rstrip("/")], git_root)
    if proc is None or proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def is_ignored(git_root: pathlib.Path, repo_rel: str, is_dir: bool = False) -> bool | None:
    """Return True if ignored by Git rules (--no-index), False if unignored, None on error."""
    arg = repo_rel.rstrip("/") + ("/" if is_dir else "")
    proc = run_git(["check-ignore", "-q", "--no-index", "--", arg], git_root)
    if proc is None:
        return None
    if proc.returncode == 0:
        return True
    return False if proc.returncode == 1 else None


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_slug(repo_root: pathlib.Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", repo_root.name.strip().lower()).strip("-") or "project"


def legacy_shadow_config_path(repo_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path.home() / ".devloop" / "projects" / _repo_slug(repo_root) / "config.json"


def shadow_config_path(repo_root: pathlib.Path) -> pathlib.Path:
    """Return a path-hash-isolated shadow config path to prevent same-basename collisions."""
    resolved = str(repo_root.resolve())
    canonical = resolved.lower() if os.name == "nt" else resolved
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return pathlib.Path.home() / ".devloop" / "projects" / f"{_repo_slug(repo_root)}-{digest}" / "config.json"


def is_valid_kit(root: pathlib.Path) -> bool:
    """Verify every required skill, script, and template exists under `root`."""
    if not root.is_dir():
        return False
    review_dir = root / "loop-review"
    skills_ok = all((root / name / "SKILL.md").is_file() for name in KIT_SKILLS)
    scripts_ok = all((review_dir / "scripts" / name).is_file() for name in KIT_SCRIPTS)
    refs_ok = all((review_dir / "references" / name).is_file() for name in KIT_REFERENCES)
    return skills_ok and scripts_ok and refs_ok


def _validate_literal_path(target: pathlib.Path, git_root: pathlib.Path, raw: str) -> tuple[str | None, bool, str]:
    if not isinstance(raw, str) or not raw.strip():
        return None, False, f"[GENERATED_PATH_INVALID] empty path entry: {raw!r}"
    text = raw.strip()
    if any(ch in GLOB_CHARS for ch in text) or pathlib.Path(text).is_absolute():
        return None, False, f"[GENERATED_PATH_INVALID] {text}: literal repo-relative path required"
    is_dir_hint = text.endswith("/") or text.endswith("\\")
    candidate = target / text.rstrip("/\\")
    if candidate.is_symlink():
        return None, False, f"[GENERATED_PATH_INVALID] {text}: symlink rejected"
    resolved = candidate.resolve()
    if not resolved.is_relative_to(git_root):
        return None, False, f"[GENERATED_PATH_INVALID] {text}: escapes repository root"
    rel = resolved.relative_to(git_root).as_posix()
    if rel in ("", "."):
        return None, False, f"[GENERATED_PATH_INVALID] {text}: repository root rejected"
    return rel, is_dir_hint or resolved.is_dir(), ""


def _check_one_generated_path(target: pathlib.Path, git_root: pathlib.Path, raw: str) -> str:
    rel, is_dir, err = _validate_literal_path(target, git_root, raw)
    if err or rel is None:
        return err
    tracked = is_tracked(git_root, rel)
    if tracked is None:
        return f"[GENERATED_PATH_GIT_ERROR] git ls-files failed for {rel}"
    if tracked:
        return f"[GENERATED_PATH_TRACKED] {rel}: tracked in git index"
    ignored = is_ignored(git_root, rel, is_dir=is_dir)
    if ignored is None:
        return f"[GENERATED_PATH_GIT_ERROR] git check-ignore failed for {rel}"
    return "" if ignored else f"[GENERATED_PATH_UNIGNORED] {rel}: missing ignore rule"


def check_generated_paths(target: pathlib.Path, paths: list[str] | None) -> tuple[bool, list[str]]:
    """Verify declared runtime-generated paths are untracked and ignored by Git."""
    if not paths:
        return False, []
    status, git_root = resolve_git_root(target)
    if status == "git_missing":
        return False, ["[GENERATED_PATH_GIT_MISSING] git executable unavailable for generated_paths check"]
    if status == "not_git":
        return False, [f"[GENERATED_PATH_NOT_GIT] {target} is outside a git repository"]
    if status != "ok" or git_root is None:
        return False, [f"[GENERATED_PATH_GIT_ERROR] failed to resolve git root for {target}"]
    violations = [msg for raw in paths if (msg := _check_one_generated_path(target, git_root, raw))]
    return True, violations
