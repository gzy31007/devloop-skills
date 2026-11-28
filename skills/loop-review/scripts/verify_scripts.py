#!/usr/bin/env python3
"""Isolated self-test suite for devloop scripts, templates, and skill files. Stdlib only."""

import importlib.util, json, os, pathlib, re, shutil, subprocess, sys, tempfile, time

SCRIPTS = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from devloop_git import KIT_SCRIPTS, KIT_SKILLS
from verify_gate_cases import check_ops_review_and_release_gate, check_publish_devloop_unit
from verify_ops_cases import check_generated_paths, check_ops_init_and_resolve
from verify_preflight_cases import check_preflight_and_status
from verify_review_cases import check_review_handoff_and_archive
from verify_ship_retro_cases import check_ship_and_retro_flows
from verify_v017_cases import check_v017_upgrades

SKILLS = SCRIPTS.parents[1]
ARCH_GATE = SCRIPTS / "arch_gate.py"
REAPER = SCRIPTS / "reap_and_run.py"
BANNED = ("不是", "不能", "不把", "不等于", "不能看作")
STYLE_BANNED = ("不再", "原先", "旧版", "已废弃", "实测", "已经", "口径", "红线", "踩坑")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def gate(target, *extra):
    proc = subprocess.run([sys.executable, str(ARCH_GATE), "--json", str(target), *extra],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
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
    assert gate(empty)[0] == 1 and gate(empty, "--allow-empty")[0] == 0
    return "python: pass/lines/complexity/cycle/empty (allow-empty accepted)"


def check_ts_gate(work):
    root = work / "ts-case"
    write(root / "util.ts", "export const twice = (x: number): number => x * 2;\n")
    write(root / "clean.ts", 'import { twice } from "./util";\n\nexport function f(a: number): number {\n  if (a > 0) {\n    return twice(a);\n  }\n  return 0;\n}\n')
    config(root, max_lines=20, max_complexity=4)
    code, report = gate(root)
    assert code == 0 and report["status"] == "PASSED", report
    fake = ('// if (a) { for (;;) {} } while (b) {}\nconst text = "if (x) { for (y) {} } catch (e) { case 1: }";\n'
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
    for suffix, family in ((".tsx", "ts"), (".mts", "ts"), (".cts", "ts"), (".jsx", "js"), (".mjs", "js"), (".cjs", "js")):
        case = root / f"case-{suffix[1:]}"
        write(case / f"view{suffix}", "export const View = () => null;\n")
        config(case, languages=[family])
        code, report = gate(case)
        assert code == 0 and report["languages"] == [family], (suffix, report)
        assert report["languages_declared"] == [family] and report["languages_detected"] == [family]
    return "six suffixes map to ts/js families with declared/detected split"


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
    assert gate(root)[0] == 0
    (root / "svc.ts").unlink()
    write(root / "view.tsx", "export const View = () => <p>if you like it else stay</p>;\n")
    code, report = gate(root)
    assert code == 0 and report["max_complexity"] == 1, report
    return "class/object methods score 7 > 4; .case property and JSX text stay at 1"


def check_mixed_config(work):
    root = work / "mixed-case"
    write(root / "mod.py", "def f():\n    return 1\n")
    write(root / "mod.ts", "export const f = (): number => 1;\n")
    config(root, languages=["python", "ts"], max_lines=20, max_complexity=5)
    code, report = gate(root)
    assert code == 0 and report["languages"] == ["python", "ts"], report
    assert report["languages_declared"] == ["python", "ts"] and report["languages_detected"] == ["python", "ts"]
    config(root, languages=["python", "ts", "js"])
    code, report = gate(root)
    assert code == 1 and any("LANGUAGE_MISSING" in item for item in report["violations"]), report
    return "mixed .loop/config.json: python+ts scanned, declared/detected split, missing language reported"


def _reaper(args, env, timeout=120):
    return subprocess.run([sys.executable, str(REAPER), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=False, env=env)


def check_reaper(work):
    env = os.environ.copy()
    env.pop("OMB_REAPER_LOCK_HELD", None)
    env["OMB_REAPER_LOCK_FILE"] = str(work / "reaper.lock")
    status = _reaper(["--status"], env)
    assert status.returncode == 0 and "REAPER_PORTS_STATUS:" in status.stdout, status.stdout
    s_json = _reaper(["--status", "--ports", "18750-18755", "--json"], env)
    assert s_json.returncode == 0, s_json.stderr
    payload = json.loads(s_json.stdout)
    assert payload["checked_ports"] == "18750-18755" and isinstance(payload["listening"], dict), payload
    assert _reaper(["--status", "--ports", "99999"], env).returncode == 2
    started = time.monotonic()
    timed = _reaper(["--cwd", str(work), "--timeout", "2", sys.executable, "-c", "import time; time.sleep(20)"], env)
    assert timed.returncode == 124 and (time.monotonic() - started) < 15, (timed.returncode, timed.stderr)
    probe = write(work / "env-probe.py", "import os\nprint(os.environ.get('PYTHONIOENCODING', ''), os.environ.get('PYTHONUTF8', ''))\n")
    stripped = _reaper(["--cwd", str(work), sys.executable, str(probe)], {**env, "PYTHONIOENCODING": "gbk", "PYTHONUTF8": "1"})
    assert stripped.returncode == 0 and stripped.stdout.strip() == "", stripped.stdout
    log = work / "serial.log"
    child = write(work / "serial-child.py", "import sys, time\nf = open(sys.argv[1], 'a', encoding='utf-8')\nf.write('S'); f.flush(); time.sleep(0.5); f.write('E'); f.flush()\n")
    cmd = [sys.executable, str(REAPER), "--cwd", str(work), sys.executable, str(child), str(log)]
    p1 = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p1.communicate(timeout=120)
    p2.communicate(timeout=120)
    assert log.read_text(encoding="utf-8") == "SESE", log.read_text(encoding="utf-8")
    return "reaper: --status compat, --status --ports --json, timeout=124, env stripped, lock serialized (SESE)"


def _check_paragraphs(path, text):
    in_code, run_lines = False, 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code, run_lines = not in_code, 0
            continue
        if in_code or not s or s.startswith(("#", "|", "-", ">", "1.", "2.", "3.")):
            run_lines = 0
            continue
        run_lines += 1
        assert run_lines <= 4, f"{path.name}: paragraph exceeds 4 lines"


def _check_publish_sync():
    pub_script = SKILLS.parents[2] / "scripts" / "publish_devloop.py"
    if not pub_script.is_file():
        return
    spec = importlib.util.spec_from_file_location("publish_devloop_sync", pub_script)
    assert spec and spec.loader
    pub_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pub_mod)
    assert tuple(pub_mod.EXPECTED_SKILLS) == tuple(KIT_SKILLS), pub_mod.EXPECTED_SKILLS
    assert tuple(pub_mod.EXPECTED_SCRIPTS) == tuple(KIT_SCRIPTS), pub_mod.EXPECTED_SCRIPTS


def check_skills():
    assert len(KIT_SKILLS) == 8 and "loop-ship" in KIT_SKILLS, KIT_SKILLS
    _check_publish_sync()
    expected = set(KIT_SKILLS)
    found = {path.parent.name for path in SKILLS.glob("*/SKILL.md")}
    assert found == expected, (sorted(found), sorted(expected))
    for name in sorted(expected):
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        match = FRONTMATTER.match(text)
        assert match, f"{name}: frontmatter missing"
        fields = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
        assert fields.get("name", "").strip() == name and fields.get("description", "").strip(), name
    documents = list(SKILLS.rglob("*.md"))
    for path in documents:
        rel = path.relative_to(SKILLS).as_posix()
        text = path.read_text(encoding="utf-8")
        assert not [t for t in BANNED if t in text], (rel, [t for t in BANNED if t in text])
        if rel != "agent-docs/SKILL.md":
            assert not [w for w in STYLE_BANNED if w in text], (rel, [w for w in STYLE_BANNED if w in text])
            assert not EMOJI_RE.search(text), f"{rel}: emoji found"
            _check_paragraphs(path, text)
    refs = SKILLS / "loop-review" / "references"
    fmap = (refs / "feature_map.template.md").read_text(encoding="utf-8")
    assert "| 版本 |" in fmap and "## 不变量" in fmap
    handoff = (refs / "handoff.template.md").read_text(encoding="utf-8")
    assert "源头防错自检表" in handoff and "文档同步声明" in handoff
    assert "本版打回次数" not in handoff and "## 打回记录" not in handoff
    decisions = (refs / "decisions.template.md").read_text(encoding="utf-8")
    assert "可推翻" in decisions and "superseded" in decisions and decisions.index("可推翻") < 600
    return f"skills: {len(expected)} frontmatter names, {len(documents)} md files pass banned/style/emoji/paragraph checks"


def check_self_arch_gate():
    code, rep = gate(SCRIPTS)
    assert code == 0 and rep["status"] == "PASSED", rep
    assert rep["max_file_lines"] <= 280 and rep["max_complexity"] <= 12 and rep["cycles"] == 0, rep
    return f"self arch_gate: PASSED files={rep['files']} max_lines={rep['max_file_lines']} max_complexity={rep['max_complexity']}"


def main():
    for stream in (sys.stdout, sys.stderr):
        hasattr(stream, "reconfigure") and stream.reconfigure(encoding="utf-8", errors="replace")
    work = pathlib.Path(tempfile.mkdtemp(prefix="devloop-verify-"))
    checks = [
        ("python_gate", lambda: check_python_gate(work)), ("ts_js_gate", lambda: check_ts_gate(work)),
        ("language_families", lambda: check_language_families(work)), ("js_methods", lambda: check_js_methods(work)),
        ("mixed_config", lambda: check_mixed_config(work)), ("generated_paths", lambda: check_generated_paths(work)),
        ("reaper", lambda: check_reaper(work)), ("ops_init_resolve", lambda: check_ops_init_and_resolve(work)),
        ("ops_preflight_status", lambda: check_preflight_and_status(work)), ("ops_review_handoff", lambda: check_review_handoff_and_archive(work)),
        ("ops_review_release", lambda: check_ops_review_and_release_gate(work)), ("ops_ship_retro", lambda: check_ship_and_retro_flows(work)),
        ("v017_upgrades", lambda: check_v017_upgrades(work)), ("publish_devloop", lambda: check_publish_devloop_unit(work)),
        ("skills", check_skills), ("self_arch_gate", check_self_arch_gate),
    ]
    failures = 0
    print(f"verify_scripts.py | python {sys.version.split()[0]} | scripts {SCRIPTS}")
    try:
        for name, check in checks:
            try:
                print(f"[PASS] {name} - {check()}")
            except Exception as exc:
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
