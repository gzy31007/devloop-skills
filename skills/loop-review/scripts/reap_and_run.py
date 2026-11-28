#!/usr/bin/env python3
"""Cross-platform process-tree timer and loopback port cleaner. Stdlib only."""

import argparse
import contextlib
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from devloop_ports import DEFAULT_PORTS_SPEC, is_none_ports, parse_ports, resolve_ports

LOCK_ENV = "OMB_REAPER_LOCK_HELD"
LOCK_FILE_ENV = "OMB_REAPER_LOCK_FILE"
ALLOWED_ROOT_ENV = "OMB_REAPER_ALLOWED_ROOT"
DEFAULT_LOCK = pathlib.Path(tempfile.gettempdir()) / "devloop-reaper.lock"


def configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def sandbox_env():
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env[LOCK_ENV] = "1"
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    return env


def lock_file():
    override = os.environ.get(LOCK_FILE_ENV)
    return pathlib.Path(override).expanduser() if override else DEFAULT_LOCK


@contextlib.contextmanager
def acquire_lock(wait_sec):
    if os.environ.get(LOCK_ENV) == "1":
        yield
        return
    path = lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        _lock(handle, wait_sec, path)
        yield
    finally:
        _unlock(handle)
        handle.close()


def _lock(handle, wait_sec, path):
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
                raise TimeoutError(f"timed out waiting for {path}")
            time.sleep(0.1)


def _unlock(handle):
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


def _probe_windows_ports(port_set):
    proc = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"netstat failed with exit code {proc.returncode}: {proc.stderr.strip()}")
    found = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING" or ":" not in parts[1]:
            continue
        port_text = parts[1].rsplit(":", 1)[1]
        if port_text.isdigit() and parts[4].isdigit():
            port, pid = int(port_text), int(parts[4])
            if port in port_set and pid > 0:
                found.setdefault(port, set()).add(pid)
    return {port: sorted(pids) for port, pids in sorted(found.items())}


def _probe_posix_ports(ports):
    found = {}
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                found[port] = []
    return found


def probe_ports(spec=DEFAULT_PORTS_SPEC):
    """Return {port: [pids...]} for active listeners within `spec`."""
    if is_none_ports(spec):
        return {}
    ports = parse_ports(spec)
    return _probe_windows_ports(set(ports)) if os.name == "nt" else _probe_posix_ports(ports)


def port_pids(spec=DEFAULT_PORTS_SPEC):
    if is_none_ports(spec):
        return {}
    return {port: set(pids) for port, pids in probe_ports(spec).items()} if os.name == "nt" else {}


def flatten(mapping):
    return {pid for group in mapping.values() for pid in group}


def kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def reap_new(baseline, spec=DEFAULT_PORTS_SPEC):
    if is_none_ports(spec):
        return [], []
    new_pids = sorted(flatten(port_pids(spec)) - baseline)
    for pid in new_pids:
        kill_tree(pid)
    if new_pids:
        time.sleep(0.2)
    return new_pids, sorted(flatten(port_pids(spec)) - baseline)


def _spawn(cmd, cwd):
    options = {"cwd": str(cwd), "env": sandbox_env()}
    if os.name != "nt":
        options["start_new_session"] = True
    return subprocess.Popen(cmd, **options)


def _check_work_dir(cwd):
    work_dir = pathlib.Path(cwd).expanduser().resolve()
    if not work_dir.is_dir():
        return None, f"REAPER_REFUSED: cwd {work_dir} is missing"
    allowed = os.environ.get(ALLOWED_ROOT_ENV, "").strip()
    if allowed:
        root = pathlib.Path(allowed).expanduser().resolve()
        if not work_dir.is_relative_to(root):
            return None, f"REAPER_REFUSED: cwd {work_dir} outside allowed root {root}"
    return work_dir, ""


def _validate_port_spec(work_dir, spec, fallback_cwd):
    eff_spec = spec.strip() if str(spec or "").strip() else resolve_ports(work_dir, "", fallback_cwd=fallback_cwd or None)[0]
    if is_none_ports(eff_spec):
        return "none", 0
    try:
        parse_ports(eff_spec)
        return eff_spec, 0
    except ValueError as exc:
        print(f"REAPER_PROBE_ERROR: {exc}", file=sys.stderr)
        return "", 2


def run_with_reaper(cmd, timeout_sec, cwd, spec="", fallback_cwd=""):
    work_dir, err = _check_work_dir(cwd)
    if err or work_dir is None:
        print(err, file=sys.stderr)
        return 126
    eff_spec, bad_code = _validate_port_spec(work_dir, spec, fallback_cwd)
    if bad_code:
        return bad_code
    try:
        with acquire_lock(timeout_sec):
            return _execute(cmd, timeout_sec, work_dir, eff_spec)
    except TimeoutError as exc:
        print(f"REAPER_LOCK_TIMEOUT: {exc}", file=sys.stderr)
        return 124
    except RuntimeError as exc:
        print(f"REAPER_PROBE_ERROR: {exc}", file=sys.stderr)
        return 125


def _execute(cmd, timeout_sec, work_dir, spec):
    skip_ports = is_none_ports(spec)
    baseline = set() if skip_ports else flatten(port_pids(spec))
    proc = _spawn(cmd, work_dir)
    timed_out = False
    try:
        code = proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc.pid)
        print(f"REAPER_TIMEOUT: killed process tree PID={proc.pid} after {timeout_sec}s", file=sys.stderr)
        code = 124
    finally:
        kill_tree(proc.pid)
        if not skip_ports:
            killed, surviving = reap_new(baseline, spec)
            if killed:
                print(f"REAPER_PORT_CLEANUP: terminated run PIDs={killed}")
            if surviving:
                print(f"REAPER_CLEANUP_FAILED: surviving PIDs={surviving}", file=sys.stderr)
                return 125
    return 124 if timed_out else code


def _handle_status(ports_spec, as_json, ports_source="default"):
    try:
        listening = {} if is_none_ports(ports_spec) else probe_ports(ports_spec)
    except (ValueError, RuntimeError) as exc:
        print(f"REAPER_PROBE_ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        payload = {"checked_ports": ports_spec, "ports_source": ports_source, "listening": {str(k): v for k, v in listening.items()}}
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"REAPER_PORTS_STATUS: {listening}")
    return 0


def main(argv=None):
    configure_stdio()
    parser = argparse.ArgumentParser(description="Run one command under a process/port reaper.")
    parser.add_argument("--cwd", default=".", help="working directory for the command")
    parser.add_argument("--state-root", default="", help="fallback root for .loop/config.json ports")
    parser.add_argument("--timeout", type=int, default=60, help="seconds before the tree is killed")
    parser.add_argument("--ports", default="", help="port range, comma list, or 'none'")
    parser.add_argument("--status", action="store_true", help="print the current sandbox-port map")
    parser.add_argument("--json", action="store_true", help="print structured JSON with --status")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments")
    args = parser.parse_args(argv)
    ports_spec, ports_source = resolve_ports(args.cwd, args.ports, fallback_cwd=args.state_root or None)
    if args.status:
        return _handle_status(ports_spec, args.json, ports_source)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("REAPER_USAGE_ERROR: missing command", file=sys.stderr)
        return 2
    return run_with_reaper(command, args.timeout, args.cwd, ports_spec, fallback_cwd=args.state_root)


if __name__ == "__main__":
    sys.exit(main())
