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

DEFAULT_PORTS_SPEC = "18700-18799"
LOCK_ENV = "OMB_REAPER_LOCK_HELD"
LOCK_FILE_ENV = "OMB_REAPER_LOCK_FILE"
ALLOWED_ROOT_ENV = "OMB_REAPER_ALLOWED_ROOT"
DEFAULT_LOCK = pathlib.Path(tempfile.gettempdir()) / "devloop-reaper.lock"


def configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def parse_ports(spec):
    """Parse '18700-18799' or '18750,18760-18762' into sorted ints."""
    ports = set()
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
    ports = parse_ports(spec)
    return _probe_windows_ports(set(ports)) if os.name == "nt" else _probe_posix_ports(ports)


def port_pids(spec=DEFAULT_PORTS_SPEC):
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


def run_with_reaper(cmd, timeout_sec, cwd, spec=DEFAULT_PORTS_SPEC):
    work_dir, err = _check_work_dir(cwd)
    if err or work_dir is None:
        print(err, file=sys.stderr)
        return 126
    try:
        with acquire_lock(timeout_sec):
            return _execute(cmd, timeout_sec, work_dir, spec)
    except TimeoutError as exc:
        print(f"REAPER_LOCK_TIMEOUT: {exc}", file=sys.stderr)
        return 124
    except RuntimeError as exc:
        print(f"REAPER_PROBE_ERROR: {exc}", file=sys.stderr)
        return 125


def _execute(cmd, timeout_sec, work_dir, spec):
    baseline = flatten(port_pids(spec))
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
        killed, surviving = reap_new(baseline, spec)
        if killed:
            print(f"REAPER_PORT_CLEANUP: terminated run PIDs={killed}")
        if surviving:
            print(f"REAPER_CLEANUP_FAILED: surviving PIDs={surviving}", file=sys.stderr)
            return 125
    return 124 if timed_out else code


def _handle_status(ports_spec, as_json):
    try:
        listening = probe_ports(ports_spec)
    except (ValueError, RuntimeError) as exc:
        print(f"REAPER_PROBE_ERROR: {exc}", file=sys.stderr)
        return 2
    if as_json:
        payload = {"checked_ports": ports_spec, "listening": {str(k): v for k, v in listening.items()}}
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"REAPER_PORTS_STATUS: {listening}")
    return 0


def main(argv=None):
    configure_stdio()
    parser = argparse.ArgumentParser(description="Run one command under a process/port reaper.")
    parser.add_argument("--cwd", default=".", help="working directory for the command")
    parser.add_argument("--timeout", type=int, default=60, help="seconds before the tree is killed")
    parser.add_argument("--ports", default=DEFAULT_PORTS_SPEC, help="port range or comma list, e.g. 18700-18799")
    parser.add_argument("--status", action="store_true", help="print the current sandbox-port map")
    parser.add_argument("--json", action="store_true", help="print structured JSON with --status")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments")
    args = parser.parse_args(argv)
    if args.status:
        return _handle_status(args.ports, args.json)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("REAPER_USAGE_ERROR: missing command", file=sys.stderr)
        return 2
    return run_with_reaper(command, args.timeout, args.cwd, args.ports)


if __name__ == "__main__":
    sys.exit(main())
