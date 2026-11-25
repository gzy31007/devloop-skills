#!/usr/bin/env python3
"""Markdown marker-block rendering and byte-preserving upsert helpers. Stdlib only."""

from __future__ import annotations

import pathlib

INDEX_START = "<!-- devloop:index:start -->"
INDEX_END = "<!-- devloop:index:end -->"
STATUS_START = "<!-- devloop:init-status:start -->"
STATUS_END = "<!-- devloop:init-status:end -->"


def render_agents_block(mode: str) -> str:
    prefix = ".loop/" if mode == "external" else ""
    decisions = f"{prefix}DECISIONS.md"
    feature_map = f"{prefix}FEATURE_MAP.md"
    lines = [
        INDEX_START,
        "## DevLoop 工作区索引",
        "",
        "本仓库启用 `devloop` 闭环，由 `/hakimi` 判定阶段并按 `/loop-init -> /loop-plan -> /loop-build -> /loop-review -> /loop-retro` 推进。",
        "",
        "| 文档 | 读取时机 | 写入时机 |",
        "| --- | --- | --- |",
        f"| `{decisions}` | 规划与编码前核验现行前提 | 新增、改道或推翻技术取舍时追加或标记 `[superseded by ...]` |",
        f"| `{feature_map}` | 规划、审查与发版前核对行为 | 新增、变更或下线外部可观察行为时同步更新版本行 |",
        "| `.loop/config.json` | 跑门禁与调整配置前 | 调整语言栈、阈值、忽略目录或 `generated_paths` 时 |",
        "| `.loop/handoff.md` | 接手、审查与发版前 | 每阶段结束写入目标、范围、自检证据、文档同步声明与打回记录 |",
        "",
        "脚本定位：优先从已加载的 `loop-review` 技能目录拼接 `scripts/<脚本名>`；CI 与夹具优先读取 `DEVLOOP_KIT`。",
        INDEX_END,
    ]
    return "\n".join(lines) + "\n"


def render_status_block(mode: str, index_mode: str, dec_rel: str, feat_rel: str, exclude_ok: bool) -> str:
    lines = [
        STATUS_START,
        "## 初始化状态",
        "",
        "| 字段 | 当前值 |",
        "| --- | --- |",
        f"| 接入模式 | `{mode}` |",
        f"| 文档索引方式 | `{index_mode}` |",
        f"| 决策账本 | `{dec_rel}` |",
        f"| 功能清单 | `{feat_rel}` |",
        f"| 忽略规则校验 | `{'verified' if exclude_ok else 'n/a'}` |",
        STATUS_END,
    ]
    return "\n".join(lines) + "\n"


def validate_marker_pair(text: str, start: str, end: str, label: str) -> tuple[str, str]:
    s_count, e_count = text.count(start), text.count(end)
    if s_count == 0 and e_count == 0:
        return "none", ""
    if s_count != 1 or e_count != 1:
        return "error", f"{label}: invalid marker count (start={s_count}, end={e_count})"
    if text.index(start) >= text.index(end):
        return "error", f"{label}: reversed marker pair"
    return "replace", ""


def upsert_marker_block(path: pathlib.Path, start: str, end: str, block: str) -> str:
    """Create, append, or replace a marker block while preserving surrounding bytes."""
    if not path.exists():
        path.write_text(block, encoding="utf-8", newline="\n")
        return "created"
    raw = path.read_text(encoding="utf-8")
    if start in raw and end in raw:
        s_idx = raw.index(start)
        e_idx = raw.index(end) + len(end)
        trailing = "\n" if e_idx < len(raw) and raw[e_idx] == "\n" else ""
        updated = raw[:s_idx] + block.rstrip("\n") + trailing + raw[e_idx + len(trailing):]
        path.write_text(updated, encoding="utf-8", newline="\n")
        return "updated"
    sep = "" if not raw else ("\n" if raw.endswith("\n\n") else "\n\n")
    path.write_text(raw + sep + block, encoding="utf-8", newline="\n")
    return "appended"
