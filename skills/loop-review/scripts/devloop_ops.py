#!/usr/bin/env python3
"""Unified CLI for devloop kit discovery, idempotent init, review records, and release gates."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from devloop_archive import run_review_archive, run_review_status
from devloop_docs import docs_fingerprint
from devloop_gate import run_release_gate
from devloop_git import configure_stdio, is_valid_kit
from devloop_init import run_init
from devloop_reviews import run_review_record

LOCAL_SKILL_DIRS = (".opencode/skills", ".agents/skills", ".claude/skills", "skills", "kits/devloop/skills")
GLOBAL_SKILL_DIRS = (
    ".agents/skills", ".config/opencode/skills", ".claude/skills",
    ".codex/skills", ".opencode/skills",
)


def resolve_kit(cwd: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """Locate the devloop skills root by strict precedence without global write side effects."""
    env_kit = os.environ.get("DEVLOOP_KIT", "").strip()
    if env_kit:
        cand = pathlib.Path(env_kit).expanduser().resolve()
        if is_valid_kit(cand):
            return cand, "env:DEVLOOP_KIT"
    self_kit = pathlib.Path(__file__).resolve().parents[2]
    if is_valid_kit(self_kit):
        return self_kit, "self:script_root"
    cur = cwd.expanduser().resolve()
    for base in (cur, *cur.parents):
        for rel in LOCAL_SKILL_DIRS:
            cand = base / rel
            if is_valid_kit(cand):
                return cand.resolve(), f"walk:{rel}"
    home = pathlib.Path.home()
    for rel in GLOBAL_SKILL_DIRS:
        cand = home / rel
        if is_valid_kit(cand):
            return cand.resolve(), f"global:{rel}"
    return None, "missing"


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    status = payload.get("status", "OK")
    print(f"{status}: {json.dumps(payload, ensure_ascii=False)}")


def _cmd_resolve_kit(args: argparse.Namespace) -> int:
    kit, source = resolve_kit(pathlib.Path(args.cwd))
    if kit is None:
        _emit({"status": "FAILED", "error": "devloop kit unavailable", "source": source}, args.json)
        return 1
    payload = {
        "status": "PASSED",
        "kit_root": str(kit),
        "scripts_dir": str(kit / "loop-review" / "scripts"),
        "source": source,
    }
    _emit(payload, args.json)
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    code, payload = run_init(
        pathlib.Path(args.cwd),
        mode=args.mode,
        languages=args.languages,
        switch_mode=args.switch_mode,
        migrate_shadow=args.migrate_shadow,
    )
    _emit(payload, args.json)
    return code


def _cmd_docs_fingerprint(args: argparse.Namespace) -> int:
    docs_root = pathlib.Path(args.docs_root) if args.docs_root else None
    payload = docs_fingerprint(pathlib.Path(args.cwd), mode=args.mode, docs_root=docs_root)
    ok = payload["decisions_exists"] and payload["feature_map_exists"]
    payload["status"] = "PASSED" if ok else "FAILED"
    _emit(payload, args.json)
    return 0 if ok else 1


def _cmd_review_record(args: argparse.Namespace) -> int:
    code, payload = run_review_record(
        pathlib.Path(args.cwd), verdict=args.verdict, category=args.category,
        state_root=args.state_root, docs_root=args.docs_root, mode=args.mode,
        version=args.version, branch=args.branch, reviewed_sha=args.reviewed_sha,
        reviewer=args.reviewer, summary=args.summary, body_file=args.body_file,
        body_text=args.body, dec_status=args.decisions_status, dec_summary=args.decisions_summary,
        feat_status=args.feature_map_status, feat_summary=args.feature_map_summary,
        base_dec_sha=args.baseline_decisions_sha, base_feat_sha=args.baseline_feature_map_sha,
    )
    _emit(payload, args.json)
    return code


def _cmd_review_status(args: argparse.Namespace) -> int:
    code, payload = run_review_status(
        pathlib.Path(args.cwd), state_root=args.state_root, docs_root=args.docs_root,
        mode=args.mode, version=args.version, branch=args.branch,
    )
    _emit(payload, args.json)
    return code


def _cmd_review_archive(args: argparse.Namespace) -> int:
    code, payload = run_review_archive(
        pathlib.Path(args.cwd), state_root_arg=args.state_root,
        tag=args.tag, version=args.version, branch=args.branch,
    )
    _emit(payload, args.json)
    return code


def _cmd_release_gate(args: argparse.Namespace) -> int:
    code, payload = run_release_gate(
        pathlib.Path(args.cwd), phase=args.phase, mode=args.mode,
        tag=args.tag, version=args.version, branch=args.branch,
        reviewed_sha=args.reviewed_sha, state_root=args.state_root,
        docs_root=args.docs_root, ports=args.ports, allow_diff=args.allow_diff,
    )
    _emit(payload, args.json)
    return code


def _add_review_parsers(sub) -> None:
    p_rec = sub.add_parser("review-record", help="Append immutable review verdict record under .loop/reviews/.")
    p_rec.add_argument("--cwd", default=".")
    p_rec.add_argument("--state-root", default="")
    p_rec.add_argument("--docs-root", default="")
    p_rec.add_argument("--mode", default="", choices=["", "own", "external"])
    p_rec.add_argument("--verdict", required=True, choices=["reject", "pending", "pass"])
    p_rec.add_argument("--category", default="", choices=["", "method", "env"])
    p_rec.add_argument("--version", default="")
    p_rec.add_argument("--branch", default="")
    p_rec.add_argument("--reviewed-sha", default="")
    p_rec.add_argument("--reviewer", default="loop-review")
    p_rec.add_argument("--summary", default="")
    p_rec.add_argument("--body-file", default="")
    p_rec.add_argument("--body", default="")
    p_rec.add_argument("--decisions-status", default="")
    p_rec.add_argument("--decisions-summary", default="")
    p_rec.add_argument("--feature-map-status", default="")
    p_rec.add_argument("--feature-map-summary", default="")
    p_rec.add_argument("--baseline-decisions-sha", default="")
    p_rec.add_argument("--baseline-feature-map-sha", default="")
    p_rec.add_argument("--json", action="store_true")
    p_rec.set_defaults(handler=_cmd_review_record)

    p_st = sub.add_parser("review-status", help="Read active review state and routing advice.")
    p_st.add_argument("--cwd", default=".")
    p_st.add_argument("--state-root", default="")
    p_st.add_argument("--docs-root", default="")
    p_st.add_argument("--mode", default="", choices=["", "own", "external"])
    p_st.add_argument("--version", default="")
    p_st.add_argument("--branch", default="")
    p_st.add_argument("--json", action="store_true")
    p_st.set_defaults(handler=_cmd_review_status)

    p_ar = sub.add_parser("review-archive", help="Move completed review scope into .loop/archive/.")
    p_ar.add_argument("--cwd", default=".")
    p_ar.add_argument("--state-root", default="")
    p_ar.add_argument("--tag", default="")
    p_ar.add_argument("--version", default="")
    p_ar.add_argument("--branch", default="")
    p_ar.add_argument("--json", action="store_true")
    p_ar.set_defaults(handler=_cmd_review_archive)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DevLoop workspace operations CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_res = sub.add_parser("resolve-kit", help="Resolve and validate devloop skills root.")
    p_res.add_argument("--cwd", default=".")
    p_res.add_argument("--json", action="store_true")
    p_res.set_defaults(handler=_cmd_resolve_kit)

    p_init = sub.add_parser("init", help="Idempotently initialize devloop workspace files.")
    p_init.add_argument("--cwd", default=".")
    p_init.add_argument("--mode", default="", choices=["", "own", "external"])
    p_init.add_argument("--languages", default="auto")
    p_init.add_argument("--switch-mode", action="store_true")
    p_init.add_argument("--migrate-shadow", action="store_true")
    p_init.add_argument("--json", action="store_true")
    p_init.set_defaults(handler=_cmd_init)

    p_fp = sub.add_parser("docs-fingerprint", help="Print SHA-256 fingerprints for DECISIONS and FEATURE_MAP.")
    p_fp.add_argument("--cwd", default=".")
    p_fp.add_argument("--mode", default="", choices=["", "own", "external"])
    p_fp.add_argument("--docs-root", default="")
    p_fp.add_argument("--json", action="store_true")
    p_fp.set_defaults(handler=_cmd_docs_fingerprint)

    _add_review_parsers(sub)

    p_gate = sub.add_parser("release-gate", help="Run pre-merge or post-tag mechanical release gate.")
    p_gate.add_argument("--cwd", default=".")
    p_gate.add_argument("--phase", required=True, choices=["pre-merge", "post-tag"])
    p_gate.add_argument("--mode", default="", choices=["", "own", "external"])
    p_gate.add_argument("--tag", default="")
    p_gate.add_argument("--version", default="")
    p_gate.add_argument("--branch", default="")
    p_gate.add_argument("--reviewed-sha", default="")
    p_gate.add_argument("--state-root", default="")
    p_gate.add_argument("--docs-root", default="")
    p_gate.add_argument("--ports", default="")
    p_gate.add_argument("--allow-diff", action="append", default=[])
    p_gate.add_argument("--json", action="store_true")
    p_gate.set_defaults(handler=_cmd_release_gate)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
