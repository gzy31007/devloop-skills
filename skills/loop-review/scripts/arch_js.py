#!/usr/bin/env python3
"""Lexical scanner for TypeScript and JavaScript complexity and local imports."""

import re

JS_FAMILY = {
    ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
}
JS_SUFFIXES = frozenset(JS_FAMILY)
CONTROL = frozenset({
    "if", "for", "while", "catch", "switch", "with", "do", "else", "try", "finally",
    "return", "typeof", "new", "function", "await", "yield", "in", "of", "case",
})
JS_BRANCH = re.compile(
    r"\b(?:if|for|while)\s*\(|\bcatch\s*[({]|(?<!\.)\bcase\b[^\n:]*:|"
    r"\?\?|&&|\|\||\?(?![?:.])"
)
JS_FUNCTION = re.compile(
    r"\bfunction\b\s*\*?\s*([A-Za-z_$][\w$]*)?\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^{;]*)?\{"
)
JS_METHOD = re.compile(
    r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::[^{;]*)?\{"
)
JS_ARROW = re.compile(r"([A-Za-z_$][\w$]*)?\s*=>\s*")
JS_IMPORT = re.compile(r"(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*|\bimport\s+)([\"'])([^\"']+)\1")


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
    """Return (comments blanked, comments and strings blanked); newlines preserved."""
    code, plain, index, size = list(text), list(text), 0, len(text)
    while index < size:
        char = text[index]
        if char in "\"'`":
            index = _skip_string(text, plain, index)
        elif text.startswith("//", index) or text.startswith("/*", index):
            block = text.startswith("/*", index)
            stop = text.find("*/" if block else "\n", index + 2)
            stop = size if stop == -1 else stop + (2 if block else 0)
            for pos in range(index, stop):
                if text[pos] != "\n":
                    code[pos] = plain[pos] = " "
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
    ends = [idx for idx in (text.find(";", cursor), text.find("\n", cursor)) if idx != -1]
    return (cursor, min(ends) if ends else len(text), match.group(1) or "arrow")


def js_bodies(text):
    """Return (label, body) for outermost functions, arrows, and methods."""
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


def _resolve_js(spec, base, root, module_of):
    if not spec.startswith("."):
        return None
    stem = (base / spec).resolve()
    options = [
        stem,
        *[stem.with_name(stem.name + suffix) for suffix in sorted(JS_SUFFIXES)],
        *[stem / f"index{suffix}" for suffix in sorted(JS_SUFFIXES)],
    ]
    for option in options:
        if option.is_file() and option.is_relative_to(root):
            return module_of(option, root)
    return None


def js_metrics(path, root, module_of):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    code, plain = mask_js(text)
    best, target = js_complexity(plain)
    deps = {
        name
        for name in (_resolve_js(m.group(2), path.parent, root, module_of) for m in JS_IMPORT.finditer(code))
        if name
    }
    return len(text.splitlines()), best, target, deps, ""
