#!/usr/bin/env python3
"""Architecture gate for Python and TS/JS trees.

Checks file line counts, function cyclomatic complexity, circular imports,
declared languages, and optional Git ignore rules for `generated_paths`.
Config precedence: `--config`, `.loop/config.json`, or hashed shadow config.
"""

import argparse
import ast
import json
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from arch_js import JS_FAMILY, JS_SUFFIXES, js_metrics
from devloop_git import check_generated_paths, configure_stdio, shadow_config_path

DEFAULTS = {
    "max_lines": 300,
    "max_complexity": 12,
    "check_cycles": True,
    "languages": "auto",
    "ignore_dirs": [
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        ".pytest_cache", ".hypothesis", ".tmp", "dist", "build", ".loop",
        ".opencode", ".agents", ".claude", ".codex",
    ],
    "allow_empty": False,
    "generated_paths": [],
}
PY_SUFFIXES = frozenset({".py"})
BRANCH_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.With, ast.AsyncWith, ast.IfExp, ast.match_case,
)


def load_config(target, explicit):
    """Merge defaults, hashed shadow config, and project `.loop/config.json`."""
    candidates = [pathlib.Path(explicit)] if explicit else [
        shadow_config_path(target),
        target / ".loop" / "config.json",
    ]
    merged = dict(DEFAULTS)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            merged.update({key: value for key, value in loaded.items() if key in DEFAULTS})
    return merged


def iter_files(target, ignore_dirs):
    """Yield gateable Python and TS/JS files under `target`, skipping ignored dirs and symlinks."""
    for path in sorted(target.rglob("*")):
        rel = path.relative_to(target)
        if not path.is_file() or path.is_symlink() or any(part in ignore_dirs for part in rel.parts):
            continue
        if path.suffix.lower() in PY_SUFFIXES | JS_SUFFIXES:
            yield path, rel.as_posix()


def module_of(path, root):
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


def python_deps(node, module, is_pkg, known):
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    else:
        base = node.module or ""
        if node.level:
            parts = module.split(".") if module else []
            keep = max(0, len(parts) - node.level + (1 if is_pkg else 0))
            base = ".".join(parts[:keep] + ([base] if base else []))
        names = [base] + [f"{base}.{alias.name}".strip(".") for alias in node.names]
    found = set()
    for candidate in names:
        parts = candidate.split(".")
        for size in range(len(parts), 0, -1):
            prefix = ".".join(parts[:size])
            if prefix in known and prefix != module:
                found.add(prefix)
                break
    return found


def python_metrics(path, root, known):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return len(text.splitlines()), 0, "", set(), f"[SYNTAX_ERROR] {path.name}:{exc.lineno}: {exc.msg}"
    best, target, deps, module = 0, "", set(), module_of(path, root)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            score = 1 + sum(1 for child in ast.walk(node) if isinstance(child, BRANCH_NODES))
            score += sum(len(child.values) - 1 for child in ast.walk(node) if isinstance(child, ast.BoolOp))
            if score > best:
                best, target = score, f"{node.lineno}:{node.name}"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            deps |= python_deps(node, module, path.name == "__init__.py", known)
    return len(text.splitlines()), best, target, deps, ""


def find_cycles(graph):
    seen, stack, cycles = set(), [], []

    def walk(node):
        seen.add(node)
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt in stack:
                cycles.append(" -> ".join(stack[stack.index(nxt):] + [nxt]))
            elif nxt not in seen:
                walk(nxt)
        stack.pop()

    for node in sorted(graph):
        if node not in seen:
            walk(node)
    return cycles


def scan_tree(target, config):
    """Collect file metrics, violations, detected languages, and local import graph."""
    ignore = set(config["ignore_dirs"])
    known = {module_of(p, target) for p, _r in iter_files(target, ignore) if p.suffix.lower() in PY_SUFFIXES}
    graph, violations, languages = {}, [], set()
    stats = {"files": 0, "lines": 0, "lines_file": "", "complexity": 0, "complexity_target": ""}
    for path, rel in iter_files(target, ignore):
        suffix = path.suffix.lower()
        languages.add("python" if suffix in PY_SUFFIXES else JS_FAMILY.get(suffix, suffix.lstrip(".")))
        if suffix in PY_SUFFIXES:
            lines, score, where, deps, problem = python_metrics(path, target, known)
        else:
            lines, score, where, deps, problem = js_metrics(path, target, module_of)
        if lines > config["max_lines"]:
            violations.append(f"[LINE_LIMIT] {rel}: {lines} lines > {config['max_lines']}")
        if score > config["max_complexity"]:
            violations.append(f"[COMPLEXITY] {rel}: {where} complexity {score} > {config['max_complexity']}")
        if problem:
            violations.append(problem)
        stats["files"] += 1
        if lines > stats["lines"]:
            stats["lines"], stats["lines_file"] = lines, rel
        if score > stats["complexity"]:
            stats["complexity"], stats["complexity_target"] = score, f"{rel}:{where}"
        graph[module_of(path, target)] = deps
    return stats, graph, violations, languages


def evaluate(target, config):
    target = target.resolve()
    stats, graph, violations, languages = scan_tree(target, config)
    if not stats["files"] and not config["allow_empty"]:
        violations.append(f"[EMPTY_SCAN] 0 python or ts/js files found in {target}")
    cycles = find_cycles(graph) if config["check_cycles"] else []
    violations += [f"[CIRCULAR_IMPORT] {cycle}" for cycle in cycles]
    wanted = config["languages"]
    if isinstance(wanted, list) and wanted:
        violations += [f"[LANGUAGE_MISSING] no {name} sources found" for name in sorted(set(wanted) - languages)]
    gen_checked, gen_violations = check_generated_paths(target, config.get("generated_paths"))
    violations += gen_violations
    detected = sorted(languages)
    return {
        "status": "FAILED" if violations else "PASSED",
        "target": str(target),
        "config": config,
        "languages": detected,
        "languages_declared": wanted,
        "languages_detected": detected,
        "generated_paths_checked": gen_checked,
        "files": stats["files"],
        "max_file_lines": stats["lines"],
        "max_lines_file": stats["lines_file"],
        "max_complexity": stats["complexity"],
        "max_complexity_target": stats["complexity_target"],
        "cycles": len(cycles),
        "violations": violations,
    }


def main(argv=None):
    configure_stdio()
    parser = argparse.ArgumentParser(description="Architecture gate: lines, complexity, import cycles.")
    parser.add_argument("target", nargs="?", default=".", help="project directory to inspect")
    parser.add_argument("--config", default="", help="explicit config JSON path")
    parser.add_argument("--json", action="store_true", help="print the structured report")
    parser.add_argument("--allow-empty", action="store_true", help="accept a tree with no sources")
    args = parser.parse_args(argv)
    target = pathlib.Path(args.target).expanduser()
    if not target.is_dir():
        print(f"ERROR: target directory not found: {target}")
        return 2
    config = load_config(target.resolve(), args.config)
    if args.allow_empty:
        config["allow_empty"] = True
    report = evaluate(target, config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"{report['status']} files={report['files']} max_lines={report['max_file_lines']} "
            f"max_complexity={report['max_complexity']} cycles={report['cycles']}"
        )
        for line in report["violations"]:
            print(f"  {line}")
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
