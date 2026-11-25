#!/usr/bin/env python3
"""Architecture gate for one tree: Python via `ast`, TS/JS via a lexical scan
that ignores comments and strings. Config: `--config`, `.loop/config.json`, or
the shadow index `~/.devloop/projects/<repo-id>/config.json`. Stdlib only."""

import argparse
import ast
import json
import pathlib
import re
import sys

DEFAULTS = {"max_lines": 300, "max_complexity": 12, "check_cycles": True, "languages": "auto",
            "ignore_dirs": [".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                            ".hypothesis", ".tmp", "dist", "build", ".loop"], "allow_empty": False}
PY_SUFFIXES = frozenset({".py"})
JS_FAMILY = {".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts", ".js": "js", ".jsx": "js",
             ".mjs": "js", ".cjs": "js"}
JS_SUFFIXES = frozenset(JS_FAMILY)
CONTROL = frozenset({"if", "for", "while", "catch", "switch", "with", "do", "else", "try", "finally",
                     "return", "typeof", "new", "function", "await", "yield", "in", "of", "case"})
BRANCH_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.With, ast.AsyncWith,
                ast.IfExp, ast.match_case)
JS_BRANCH = re.compile(r"\b(?:if|for|while)\s*\(|\bcatch\s*[({]|(?<!\.)\bcase\b[^\n:]*:|"
                       r"\?\?|&&|\|\||\?(?![?:.])")
JS_FUNCTION = re.compile(r"\bfunction\b\s*\*?\s*([A-Za-z_$][\w$]*)?\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^{;]*)?\{")
JS_METHOD = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^{;]*)?\{")
JS_ARROW = re.compile(r"([A-Za-z_$][\w$]*)?\s*=>\s*")
JS_IMPORT = re.compile(r"(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*|\bimport\s+)([\"'])([^\"']+)\1")


def configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_config(target, explicit):
    """Defaults, then the shadow index, then the project file (explicit wins)."""
    repo = re.sub(r"[^A-Za-z0-9._-]+", "-", target.name.strip().lower()).strip("-") or "project"
    shadow = pathlib.Path.home() / ".devloop" / "projects" / repo / "config.json"
    candidates = [pathlib.Path(explicit)] if explicit else [shadow, target / ".loop" / "config.json"]
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
    """Every gateable file under `target`, skipping ignored dirs and symlinks."""
    for path in sorted(target.rglob("*")):
        rel = path.relative_to(target)
        if not path.is_file() or path.is_symlink() or any(part in ignore_dirs for part in rel.parts):
            continue
        if path.suffix.lower() in PY_SUFFIXES | JS_SUFFIXES:
            yield path, rel.as_posix()

def _skip_string(text, plain, start):
    quote, index, size = text[start], start, len(text)
    while index < size:
        char = text[index]
        if char == "\\":
            plain[index] = " "
            if index + 1 < size:
                plain[index + 1] = " "
            index += 2
            continue
        plain[index] = "\n" if char == "\n" else " "
        if char == quote:
            return index + 1
        if char == "\n" and quote != "`":
            return index
        index += 1
    return size

def mask_js(text):
    """Return (comments blanked, comments and strings blanked); newlines kept."""
    code, plain, index, size = list(text), list(text), 0, len(text)
    while index < size:
        char = text[index]
        if char in "\"'`":
            index = _skip_string(text, plain, index)
        elif text.startswith("//", index) or text.startswith("/*", index):
            block = text.startswith("/*", index)
            stop = text.find("*/" if block else "\n", index + 2)
            stop = size if stop == -1 else stop + (2 if block else 0)
            for position in range(index, stop):
                if text[position] != "\n":
                    code[position] = plain[position] = " "
            index = stop
        else:
            index += 1
    return "".join(code), "".join(plain)

def brace_end(text, start):
    depth, index, size = 0, start, len(text)
    while index < size:
        depth += text[index] == "{"
        if text[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return size

def _arrow_span(text, match):
    cursor = match.end()
    while cursor < len(text) and text[cursor] in " \t\r\n":
        cursor += 1
    if cursor < len(text) and text[cursor] == "{":
        return (cursor, brace_end(text, cursor), match.group(1) or "arrow")
    ends = [index for index in (text.find(";", cursor), text.find("\n", cursor)) if index != -1]
    return (cursor, min(ends) if ends else len(text), match.group(1) or "arrow")

def js_bodies(text):
    """(label, body) for functions, arrows and methods, outermost spans only."""
    spans = []
    for match in JS_FUNCTION.finditer(text):
        start = match.end() - 1
        spans.append((start, brace_end(text, start), match.group(1) or "anonymous"))
    for match in JS_METHOD.finditer(text):
        if match.group(1) not in CONTROL:
            start = match.end() - 1
            spans.append((start, brace_end(text, start), match.group(1)))
    for match in JS_ARROW.finditer(text):
        spans.append(_arrow_span(text, match))
    kept = []
    for span in sorted(spans, key=lambda item: (item[0], -item[1])):
        if not any(outer[0] <= span[0] and span[1] <= outer[1] for outer in kept):
            kept.append(span)
    return [(label, text[start:end]) for start, end, label in kept]

def js_complexity(text):
    best, target = 0, ""
    for label, body in js_bodies(text):
        score = 1 + len(JS_BRANCH.findall(body))
        if score > best:
            best, target = score, label
    return best, target

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

def _resolve_js(spec, base, root):
    if not spec.startswith("."):
        return None
    stem = (base / spec).resolve()
    options = [stem, *[stem.with_name(stem.name + suffix) for suffix in sorted(JS_SUFFIXES)],
               *[stem / f"index{suffix}" for suffix in sorted(JS_SUFFIXES)]]
    for option in options:
        if option.is_file() and option.is_relative_to(root):
            return module_of(option, root)
    return None

def js_metrics(path, root):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    code, plain = mask_js(text)
    best, target = js_complexity(plain)
    deps = {name for name in (_resolve_js(m.group(2), path.parent, root) for m in JS_IMPORT.finditer(code)) if name}
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
    """Walk the tree once: stats, violations, language families and the import graph."""
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
            lines, score, where, deps, problem = js_metrics(path, target)
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
    return {"status": "FAILED" if violations else "PASSED", "target": str(target), "config": config,
            "languages": sorted(languages), "files": stats["files"], "max_file_lines": stats["lines"],
            "max_lines_file": stats["lines_file"], "max_complexity": stats["complexity"],
            "max_complexity_target": stats["complexity_target"], "cycles": len(cycles), "violations": violations}


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
        print(f"{report['status']} files={report['files']} max_lines={report['max_file_lines']} "
              f"max_complexity={report['max_complexity']} cycles={report['cycles']}")
        for line in report["violations"]:
            print(f"  {line}")
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
