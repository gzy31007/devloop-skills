#!/usr/bin/env python3
"""Locking, atomic file creation, body input, and Git ignore checks for devloop reviews. Stdlib only."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import time

from devloop_git import is_ignored, is_tracked, repo_prefix, resolve_git_root

MAX_SAMPLING_NOTE_LEN = 500


def normalize_sampling_note(raw: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", (raw or "").strip())
    if not cleaned:
        return "", ""
    if len(cleaned) > MAX_SAMPLING_NOTE_LEN:
        return "", f"[SAMPLING_NOTE_TOO_LONG] --sampling-note exceeds {MAX_SAMPLING_NOTE_LEN} characters"
    return cleaned, ""


def optional_note_ok(data: dict) -> bool:
    if "sampling_note" not in data:
        return True
    val = data.get("sampling_note")
    return isinstance(val, str) and bool(val.strip()) and len(val) <= MAX_SAMPLING_NOTE_LEN


def decode_text_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def normalize_body(text: str) -> str:
    cleaned = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return f"{cleaned}\n" if cleaned else ""


def read_body_outside_repo(
    body_file: str, body_text: str, eff_cwd: pathlib.Path, cwd_git: pathlib.Path, state_git: pathlib.Path,
) -> tuple[str, str]:
    if body_file:
        raw_path = pathlib.Path(body_file).expanduser()
        bf = raw_path if raw_path.is_absolute() else (eff_cwd / raw_path)
        if bf.is_symlink() or not bf.is_file():
            return "", f"[BODY_FILE_INVALID] missing or non-regular --body-file: {body_file}"
        bf_abs, bf_res = pathlib.Path(os.path.abspath(str(bf))), bf.resolve()
        for groot in (cwd_git, state_git):
            if bf_abs.is_relative_to(groot) or bf_res.is_relative_to(groot):
                return "", "[BODY_FILE_INSIDE_REPO] --body-file must reside outside git repository"
        raw = decode_text_bytes(bf_res.read_bytes())
    elif body_text:
        raw = body_text
    else:
        raw = decode_text_bytes(sys.stdin.buffer.read())
    norm = normalize_body(raw)
    return (norm, "") if norm.strip() else ("", "[REVIEW_BODY_EMPTY] review verdict body is empty")


def _lock_handle(handle, wait_sec: float, path: pathlib.Path) -> None:
    deadline = time.monotonic() + max(5.0, float(wait_sec))
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for lock {path}")
            time.sleep(0.05)


def _unlock_handle(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def acquire_review_lock(state_root: pathlib.Path, wait_sec: float = 15.0):
    canonical = str(state_root.resolve())
    if os.name == "nt":
        canonical = canonical.lower()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    lock_path = pathlib.Path(tempfile.gettempdir()) / f"devloop-review-{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        _lock_handle(handle, wait_sec, lock_path)
        yield
    finally:
        _unlock_handle(handle)
        handle.close()


def atomic_write_new(target: pathlib.Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-review-", suffix=".md", dir=str(target.parent))
    tmp_path = pathlib.Path(tmp_name)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing review record: {target}")
        os.replace(tmp_path, target)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(target: pathlib.Path, payload: dict) -> None:
    if target.is_symlink():
        raise OSError(f"refusing symlink target: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-meta-", suffix=".json", dir=str(target.parent))
    tmp_path = pathlib.Path(tmp_name)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, target)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _check_one_review_dir(state_root: pathlib.Path, git_root: pathlib.Path, prefix: str, rel: str) -> str:
    if (state_root / rel).is_symlink():
        return f"[REVIEW_PATH_SYMLINK] symlink rejected: {state_root / rel}"
    full_rel = f"{prefix}{rel}".rstrip("/")
    tracked = is_tracked(git_root, full_rel)
    if tracked is None:
        return f"[GIT_ERROR] git ls-files failed for {full_rel}"
    if tracked:
        return f"[REVIEW_PATH_TRACKED] {full_rel} is tracked; untrack and run devloop_ops.py init"
    dir_ign = is_ignored(git_root, full_rel, is_dir=True)
    probe_ign = is_ignored(git_root, f"{full_rel}/_probe/r01-pass.md", is_dir=False)
    if dir_ign is not True or probe_ign is not True:
        return f"[REVIEW_PATH_UNIGNORED] {full_rel} is not git-ignored; run devloop_ops.py init and check .gitignore negations"
    return ""


def verify_review_paths_ignored(state_root: pathlib.Path, rel_dirs: tuple[str, ...]) -> tuple[pathlib.Path | None, list[str]]:
    status, git_root = resolve_git_root(state_root)
    if status != "ok" or git_root is None:
        return None, [f"[NOT_GIT] state root must be inside a git repository ({status})"]
    if (state_root / ".loop").is_symlink():
        return git_root, [f"[REVIEW_PATH_SYMLINK] symlink rejected: {state_root / '.loop'}"]
    prefix = repo_prefix(state_root, git_root)
    errors = [msg for rel in rel_dirs if (msg := _check_one_review_dir(state_root, git_root, prefix, rel))]
    return git_root, errors
