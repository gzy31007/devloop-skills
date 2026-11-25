#!/usr/bin/env python3
"""Isolated self-test for the devloop scripts and skill files.

Every case builds its own tree under a temporary directory and removes it
afterwards. Covered: Python gate (lines, complexity, cycles, clean pass),
TS/JS gate (lines, complexity, pseudo-branches in comments and strings, cycles,
clean pass), a mixed `.loop/config.json`, the cross-platform reaper (timeout,
environment stripping, serialized lock, `--status`) and the six SKILL.md
frontmatter plus banned-phrase scan. Standard library only.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPTS = pathlib.Path(__file__).resolve().parent
SKILLS = SCRIPTS.parents[1]
ARCH_GATE = SCRIPTS / "arch_gate.py"
REAPER = SCRIPTS / "reap_and_run.py"
BANNED = ("不是", "不能", "不把", "不等于")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def gate(target, *extra):
    proc = subprocess.run([sys.executable, str(ARCH_GATE), "--json", str(target), *extra],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, check=False)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}


def config(target, **values):
    write(target / ".loop" / "config.json", json.dumps(values, ensure_ascii=False))


def check_python_gate(work):
    root = work / "python-case"
    write(root / "good.py", "def add(a, b):\n    return a + b\n")
    config(root, max_lines=8, max_complexity=3)
    code, report = gate(root)
    assert code == 0 and report["status"] == "PASSED", report
    write(root / "long.py", "".join(f"value_{index} = {index}\n" for index in range(12)))
    code, report = gate(root)
    assert code == 1 and any("[LINE_LIMIT] long.py" in item for item in report["violations"]), report
    (root / "long.py").unlink()
    write(root / "complex.py", "def f(x):\n" + "".join(f"    if x > {i}:\n        x -= {i}\n" for i in range(5)) + "    return x\n")
    code, report = gate(root)
    assert code == 1 and any("[COMPLEXITY] complex.py" in item for item in report["violations"]), report
    (root / "complex.py").unlink()
    write(root / "a.py", "import b\n\ndef fa():\n    return 1\n")
    write(root / "b.py", "import a\n\ndef fb():\n    return 1\n")
    code, report = gate(root)
    assert code == 1 and report["cycles"] == 1, report
    write(root / "b.py", "def fb():\n    return 1\n")
    code, report = gate(root)
    assert code == 0 and report["cycles"] == 0, report
    empty = work / "python-empty"
    empty.mkdir(parents=True, exist_ok=True)
    code, report = gate(empty)
    assert code == 1 and report["violations"], report
    code, report = gate(empty, "--allow-empty")
    assert code == 0, report
    return "python: pass/lines/complexity/cycle/empty (allow-empty accepted)"


def check_ts_gate(work):
    root = work / "ts-case"
    write(root / "util.ts", "export const twice = (x: number): number => x * 2;\n")
    write(root / "clean.ts", 'import { twice } from "./util";\n\nexport function f(a: number): number {\n  if (a > 0) {\n    return twice(a);\n  }\n  return 0;\n}\n')
    config(root, max_lines=20, max_complexity=4)
    code, report = gate(root)
    assert code == 0 and report["status"] == "PASSED", report
    fake = ("// if (a) { for (;;) {} } while (b) {}\n"
            'const text = "if (x) { for (y) {} } catch (e) { case 1: }";\n'
            "export function quiet(a: number): number {\n  return a + 1;\n}\n")
    write(root / "fake.ts", fake)
    code, report = gate(root)
    assert code == 0 and report["max_complexity"] <= 2, report
    (root / "fake.ts").unlink()
    write(root / "heavy.ts", "export function heavy(a: number): number {\n" + "".join(
        f"  if (a === {index}) {{ a += {index}; }}\n" for index in range(6)) + "  return a;\n}\n")
    code, report = gate(root)
    assert code == 1 and any("[COMPLEXITY] heavy.ts" in item for item in report["violations"]), report
    (root / "heavy.ts").unlink()
    write(root / "long.js", "".join(f"const v{index} = {index};\n" for index in range(25)))
    code, report = gate(root)
    assert code == 1 and any("[LINE_LIMIT] long.js" in item for item in report["violations"]), report
    (root / "long.js").unlink()
    write(root / "x.ts", 'import { y } from "./y";\nexport const x = (): number => y + 1;\n')
    write(root / "y.ts", 'import { x } from "./x";\nexport const y = 2;\n')
    code, report = gate(root)
    assert code == 1 and report["cycles"] == 1, report
    write(root / "y.ts", "export const y = 2;\n")
    code, report = gate(root)
    assert code == 0 and report["cycles"] == 0, report
    return "ts/js: pass/lines/complexity/pseudo-branch immunity/cycle/clean"


def check_language_families(work):
    root = work / "family-case"
    for suffix, family in ((".tsx", "ts"), (".mts", "ts"), (".cts", "ts"),
                           (".jsx", "js"), (".mjs", "js"), (".cjs", "js")):
        case = root / f"case-{suffix[1:]}"
        write(case / f"view{suffix}", "export const View = () => null;\n")
        config(case, languages=[family])
        code, report = gate(case)
        assert code == 0 and report["languages"] == [family], (suffix, report)
    return "six suffixes map to the ts/js families and pass a matching languages list"


def check_js_methods(work):
    root = work / "method-case"
    body = "".join(f"    if (a === {index}) {{ a += {index}; }}\n" for index in range(6))
    class_source = "export class S {\n  run(a: number): number {\n" + body + "    return a;\n  }\n}\n"
    object_source = "export const handler = {\n  run(a: number): number {\n" + body + "    return a;\n  },\n};\n"
    for label, source in (("class", class_source), ("object", object_source)):
        write(root / "svc.ts", source)
        config(root, max_complexity=4)
        code, report = gate(root)
        assert code == 1 and report["max_complexity"] == 7, (label, report)
    write(root / "svc.ts", "export function f(o: any): any {\n  return o.case;\n}\n")
    config(root, max_complexity=1)
    code, report = gate(root)
    assert code == 0 and report["max_complexity"] == 1, report
    (root / "svc.ts").unlink()
    write(root / "view.tsx", "export const View = () => <p>if you like it else stay</p>;\n")
    code, report = gate(root)
    assert code == 0 and report["max_complexity"] == 1, report
    return "class and object methods score 7 > 4; .case property and JSX text stay at 1"


def check_mixed_config(work):
    root = work / "mixed-case"
    write(root / "mod.py", "def f():\n    return 1\n")
    write(root / "mod.ts", "export const f = (): number => 1;\n")
    config(root, languages=["python", "ts"], max_lines=20, max_complexity=5)
    code, report = gate(root)
    assert code == 0 and report["languages"] == ["python", "ts"], report
    assert report["files"] == 2, report
    config(root, languages=["python", "ts", "js"])
    code, report = gate(root)
    assert code == 1 and any("LANGUAGE_MISSING" in item for item in report["violations"]), report
    return "mixed .loop/config.json: python+ts scanned, missing language reported"


def _reaper_env(lock):
    env = os.environ.copy()
    env.pop("OMB_REAPER_LOCK_HELD", None)
    env["OMB_REAPER_LOCK_FILE"] = str(lock)
    return env


def _reaper(args, env, timeout=120):
    return subprocess.run([sys.executable, str(REAPER), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False, env=env)


def check_reaper(work):
    lock = work / "reaper.lock"
    env = _reaper_env(lock)
    status = _reaper(["--status"], env)
    assert status.returncode == 0 and "REAPER_PORTS_STATUS:" in status.stdout, status.stdout
    started = time.monotonic()
    timed = _reaper(["--cwd", str(work), "--timeout", "2", sys.executable, "-c", "import time; time.sleep(20)"], env)
    elapsed = time.monotonic() - started
    assert timed.returncode == 124, (timed.returncode, timed.stderr)
    assert elapsed < 15, elapsed
    probe = work / "env-probe.py"
    write(probe, "import os\nprint(os.environ.get('PYTHONIOENCODING', ''), os.environ.get('PYTHONUTF8', ''))\n")
    stripped = _reaper(["--cwd", str(work), sys.executable, str(probe)],
                       {**env, "PYTHONIOENCODING": "gbk", "PYTHONUTF8": "1"})
    assert stripped.returncode == 0, stripped.stderr
    assert stripped.stdout.strip() == "", repr(stripped.stdout)
    log = work / "serial.log"
    child = work / "serial-child.py"
    write(child, "import sys, time\nhandle = open(sys.argv[1], 'a', encoding='utf-8')\n"
                 "handle.write('S')\nhandle.flush()\ntime.sleep(0.5)\n"
                 "handle.write('E')\nhandle.flush()\n")
    command = [sys.executable, str(REAPER), "--cwd", str(work), sys.executable, str(child), str(log)]
    first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first.communicate(timeout=120)
    second.communicate(timeout=120)
    assert log.read_text(encoding="utf-8") == "SESE", log.read_text(encoding="utf-8")
    return "reaper: --status, timeout=124, env stripped, lock serialized (SESE)"


def check_skills():
    expected = {"hakimi", "loop-init", "loop-plan", "loop-build", "loop-review", "loop-retro"}
    found = {path.parent.name for path in SKILLS.glob("*/SKILL.md")}
    assert found == expected, (sorted(found), sorted(expected))
    for name in sorted(expected):
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        match = FRONTMATTER.match(text)
        assert match, f"{name}: frontmatter missing"
        fields = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
        assert fields.get("name", "").strip() == name, (name, fields.get("name"))
        assert fields.get("description", "").strip(), f"{name}: description empty"
    documents = list(SKILLS.rglob("*.md"))
    for path in documents:
        text = path.read_text(encoding="utf-8")
        hits = [token for token in BANNED if token in text]
        assert not hits, (path.name, hits)
    decisions = (SKILLS / "loop-review" / "references" / "decisions.template.md").read_text(encoding="utf-8")
    assert "可推翻" in decisions and "superseded" in decisions, "decisions template header"
    assert "可推翻" in decisions.split("---")[0] or decisions.index("可推翻") < 600, "header must come first"
    return f"skills: 6 frontmatter names, {len(documents)} md files free of banned phrasing"


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    work = pathlib.Path(tempfile.mkdtemp(prefix="devloop-verify-"))
    checks = [("python_gate", lambda: check_python_gate(work)),
              ("ts_js_gate", lambda: check_ts_gate(work)),
              ("language_families", lambda: check_language_families(work)),
              ("js_methods", lambda: check_js_methods(work)),
              ("mixed_config", lambda: check_mixed_config(work)),
              ("reaper", lambda: check_reaper(work)),
              ("skills", check_skills)]
    failures = 0
    print(f"verify_scripts.py | python {sys.version.split()[0]} | scripts {SCRIPTS}")
    try:
        for name, check in checks:
            try:
                print(f"[PASS] {name} - {check()}")
            except Exception as exc:  # noqa: BLE001 - one readable line per failure
                failures += 1
                print(f"[FAIL] {name} - {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        print(f"VERIFY_SCRIPTS FAILED: {failures}/{len(checks)}")
        return 1
    print(f"VERIFY_SCRIPTS PASSED: {len(checks)}/{len(checks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
