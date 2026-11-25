#!/usr/bin/env python3
"""Isolated self-tests for arch_gate generated_paths and devloop_ops init/resolve-kit."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent
SKILLS = SCRIPTS.parents[1]
ARCH_GATE = SCRIPTS / "arch_gate.py"
DEVLOOP_OPS = SCRIPTS / "devloop_ops.py"


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _run(cmd: list[str], cwd: pathlib.Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False, env=env,
    )


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd)


def _init_git_repo(repo: pathlib.Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@users.noreply.github.com")
    _write(repo / "app.py", "def run():\n    return 1\n")
    assert _git(repo, "add", "app.py").returncode == 0
    assert _git(repo, "commit", "-m", "init").returncode == 0
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _gate(target: pathlib.Path, *extra: str, env: dict | None = None) -> tuple[int, dict]:
    proc = _run([sys.executable, str(ARCH_GATE), "--json", str(target), *extra], target, env=env)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}


def _ops(*args: str, cwd: pathlib.Path, env: dict | None = None) -> tuple[int, dict]:
    proc = _run([sys.executable, str(DEVLOOP_OPS), *args, "--json"], cwd, env=env)
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}


def check_generated_paths(work: pathlib.Path) -> str:
    non_git = work / "gp-nongit"
    _write(non_git / "ok.py", "x = 1\n")
    _write(non_git / ".loop" / "config.json", json.dumps({"generated_paths": []}))
    code, rep = _gate(non_git)
    assert code == 0 and rep["generated_paths_checked"] is False, rep
    _write(non_git / ".loop" / "config.json", json.dumps({"generated_paths": ["board.md"]}))
    code, rep = _gate(non_git)
    assert code == 1 and any("[GENERATED_PATH_NOT_GIT]" in v for v in rep["violations"]), rep

    repo = work / "gp-repo"
    _init_git_repo(repo)
    _write(repo / ".loop" / "config.json", json.dumps({"generated_paths": ["board.md"]}))
    code, rep = _gate(repo)
    assert code == 1 and any("[GENERATED_PATH_UNIGNORED] board.md" in v for v in rep["violations"]), rep
    _write(repo / ".gitignore", "board.md\nout/\n")
    code, rep = _gate(repo)
    assert code == 0 and rep["generated_paths_checked"] is True, rep

    _write(repo / "board.md", "# tracked despite ignore\n")
    assert _git(repo, "add", "-f", "board.md").returncode == 0
    code, rep = _gate(repo)
    assert code == 1 and any("[GENERATED_PATH_TRACKED] board.md" in v for v in rep["violations"]), rep
    assert _git(repo, "rm", "--cached", "board.md").returncode == 0

    _write(repo / "out" / "nested.txt", "data\n")
    assert _git(repo, "add", "-f", "out/nested.txt").returncode == 0
    _write(repo / ".loop" / "config.json", json.dumps({"generated_paths": ["out/"]}))
    code, rep = _gate(repo)
    assert code == 1 and any("[GENERATED_PATH_TRACKED] out" in v for v in rep["violations"]), rep
    assert _git(repo, "rm", "--cached", "out/nested.txt").returncode == 0

    sub = repo / "sub"
    _write(sub / "mod.py", "y = 2\n")
    _write(sub / ".loop" / "config.json", json.dumps({"generated_paths": ["../board.md"]}))
    code, rep = _gate(sub)
    assert code == 0 and rep["generated_paths_checked"] is True, rep

    _write(repo / ".loop" / "config.json", json.dumps({"generated_paths": ["*.md"]}))
    code, rep = _gate(repo)
    assert code == 1 and any("[GENERATED_PATH_INVALID]" in v for v in rep["violations"]), rep

    no_git_env = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
    no_git_env["PATH"] = ""
    _write(repo / ".loop" / "config.json", json.dumps({"generated_paths": ["board.md"]}))
    code, rep = _gate(repo, env=no_git_env)
    assert code == 1 and any("[GENERATED_PATH_GIT_MISSING]" in v for v in rep["violations"]), rep
    return "generated_paths: board.md/tracked+ignored/dir-descendant/subdir/invalid/no-git verified"


def _check_init_e1_and_negation(work: pathlib.Path) -> pathlib.Path:
    non_git = work / "ext-nongit"
    non_git.mkdir(parents=True, exist_ok=True)
    code, rep = _ops("init", "--cwd", str(non_git), "--mode", "external", cwd=non_git)
    assert code == 2, rep
    assert not (non_git / ".loop").exists() and not (non_git / "AGENTS.md").exists()

    e1 = work / "dup-a" / "same-name"
    _init_git_repo(e1)
    code, rep = _ops("init", "--cwd", str(e1), "--mode", "external", cwd=e1)
    assert code == 0 and rep["agents_md"] == "created" and rep["exclude_verified"] is True, rep
    assert _git(e1, "status", "--porcelain").stdout.strip() == ""

    e1_b = work / "dup-b" / "same-name"
    _init_git_repo(e1_b)
    from devloop_git import shadow_config_path
    assert shadow_config_path(e1) != shadow_config_path(e1_b)

    neg = work / "ext-negation"
    _init_git_repo(neg)
    _write(neg / ".gitignore", "!AGENTS.md\n")
    code, rep = _ops("init", "--cwd", str(neg), "--mode", "external", cwd=neg)
    assert code == 2, rep
    assert not (neg / ".loop").exists() and not (neg / "AGENTS.md").exists()
    return e1


def _check_init_e2_and_transition(work: pathlib.Path, e1: pathlib.Path) -> None:
    e2 = work / "ext-e2"
    _init_git_repo(e2)
    upstream_agents = "# Upstream Agents\nKeep verbatim.\n"
    _write(e2 / "AGENTS.md", upstream_agents)
    assert _git(e2, "add", "AGENTS.md").returncode == 0
    assert _git(e2, "commit", "-m", "track agents").returncode == 0
    code, rep = _ops("init", "--cwd", str(e2), "--mode", "external", cwd=e2)
    assert code == 0 and rep["agents_md"] == "skipped_tracked_upstream", rep
    assert rep["docs_index_mode"] == "handoff-fallback", rep
    assert (e2 / "AGENTS.md").read_text(encoding="utf-8") == upstream_agents
    assert _git(e2, "status", "--porcelain").stdout.strip() == ""

    assert _git(e1, "add", "-f", "AGENTS.md").returncode == 0
    assert _git(e1, "commit", "-m", "upstream adopts").returncode == 0
    before_bytes = (e1 / "AGENTS.md").read_bytes()
    code, rep = _ops("init", "--cwd", str(e1), cwd=e1)
    assert code == 0 and rep["agents_md"] == "skipped_tracked_upstream", rep
    assert (e1 / "AGENTS.md").read_bytes() == before_bytes
    assert "handoff-fallback" in (e1 / ".loop" / "handoff.md").read_text(encoding="utf-8")


def _check_init_preflight_rejections(work: pathlib.Path, own: pathlib.Path) -> None:
    assert _ops("init", "--cwd", str(own), "--mode", "external", cwd=own)[0] == 2
    bad_json = work / "bad-json"
    _write(bad_json / ".loop" / "config.json", "{invalid")
    assert _ops("init", "--cwd", str(bad_json), "--mode", "own", cwd=bad_json)[0] == 2
    assert not (bad_json / "AGENTS.md").exists()
    bad_mark = work / "bad-marker"
    _write(bad_mark / "AGENTS.md", "<!-- devloop:index:start -->\norphan\n")
    assert _ops("init", "--cwd", str(bad_mark), "--mode", "own", cwd=bad_mark)[0] == 2
    assert not (bad_mark / ".loop").exists()


def check_ops_init_and_resolve(work: pathlib.Path) -> str:
    env = {**os.environ, "DEVLOOP_KIT": str(SKILLS)}
    code, rep = _ops("resolve-kit", "--cwd", str(work), cwd=work, env=env)
    assert code == 0 and rep["source"] == "env:DEVLOOP_KIT", rep
    from devloop_git import is_valid_kit
    fake_kit = work / "partial-kit"
    _write(fake_kit / "loop-review" / "scripts" / "arch_gate.py", "# partial\n")
    assert not is_valid_kit(fake_kit)

    own = work / "own-repo"
    _init_git_repo(own)
    code, rep = _ops("init", "--cwd", str(own), "--mode", "own", cwd=own)
    assert code == 0 and rep["mode"] == "own" and rep["agents_md"] == "created", rep
    orig_agents = (own / "AGENTS.md").read_text(encoding="utf-8")
    _write(own / "AGENTS.md", "# Custom Header\n\n" + orig_agents + "\n# Custom Footer\n")
    cfg = json.loads((own / ".loop" / "config.json").read_text(encoding="utf-8"))
    cfg["max_lines"] = 250
    _write(own / ".loop" / "config.json", json.dumps(cfg))
    _write(own / "DECISIONS.md", (own / "DECISIONS.md").read_text(encoding="utf-8") + "\n2026-11-25 | [active] | custom |\n")
    code, rep = _ops("init", "--cwd", str(own), cwd=own)
    assert code == 0 and rep["agents_md"] == "updated", rep
    after_agents = (own / "AGENTS.md").read_text(encoding="utf-8")
    assert after_agents.startswith("# Custom Header\n\n") and after_agents.endswith("# Custom Footer\n")
    assert after_agents.count("<!-- devloop:index:start -->") == 1
    assert json.loads((own / ".loop" / "config.json").read_text(encoding="utf-8"))["max_lines"] == 250

    _check_init_preflight_rejections(work, own)
    e1 = _check_init_e1_and_negation(work)
    _check_init_e2_and_transition(work, e1)
    return "devloop_ops: resolve-kit, own idempotency, atomic preflight, E1/E2/E1->E2, path-hash shadow verified"
