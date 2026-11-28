#!/usr/bin/env python3
"""Port specification parsing and hierarchical config resolution for devloop. Stdlib only."""

from __future__ import annotations

import json
import os
import pathlib

DEFAULT_PORTS_SPEC = "18700-18799"


def is_none_ports(spec: str | None) -> bool:
    """Return True when port specification explicitly disables port probing/cleanup."""
    return str(spec or "").strip().lower() == "none"


def parse_ports(spec: str) -> list[int]:
    """Parse '18700-18799' or '18750,18760-18762' into sorted ints."""
    ports: set[int] = set()
    for token in (part.strip() for part in str(spec).split(",")):
        if not token:
            continue
        if "-" in token:
            start, end = (int(x) for x in token.split("-", 1))
            if not (1 <= start <= end <= 65535):
                raise ValueError(f"invalid port range: {token}")
            ports.update(range(start, end + 1))
        else:
            port = int(token)
            if not (1 <= port <= 65535):
                raise ValueError(f"invalid port: {token}")
            ports.add(port)
    if not ports:
        raise ValueError(f"empty port spec: {spec!r}")
    return sorted(ports)


def _read_config_ports(directory: pathlib.Path | str | None) -> str:
    if not directory:
        return ""
    try:
        base = pathlib.Path(directory).expanduser().resolve()
        if not base.is_dir():
            return ""
        cfg = base / ".loop" / "config.json"
        if cfg.is_symlink() or not cfg.is_file():
            return ""
        loaded = json.loads(cfg.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    val = loaded.get("ports")
    return val.strip() if isinstance(val, str) and val.strip() else ""


def _normalize_resolved(raw: str, source: str) -> tuple[str, str]:
    return ("none", source) if is_none_ports(raw) else (raw, source)


def resolve_ports(
    cwd: pathlib.Path | str = ".",
    cli_ports: str = "",
    fallback_cwd: pathlib.Path | str | None = None,
) -> tuple[str, str]:
    """Resolve port spec by precedence: CLI > cwd .loop/config.json > fallback_cwd > default."""
    raw_cli = (cli_ports or "").strip()
    if raw_cli:
        return _normalize_resolved(raw_cli, "cli")
    cwd_val = _read_config_ports(cwd or ".")
    if cwd_val:
        return _normalize_resolved(cwd_val, "config")
    if fallback_cwd:
        try:
            same = os.path.normcase(str(pathlib.Path(fallback_cwd).expanduser().resolve())) == os.path.normcase(
                str(pathlib.Path(cwd or ".").expanduser().resolve())
            )
        except OSError:
            same = False
        if not same:
            fb_val = _read_config_ports(fallback_cwd)
            if fb_val:
                return _normalize_resolved(fb_val, "state_config")
    return DEFAULT_PORTS_SPEC, "default"
