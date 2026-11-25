#!/usr/bin/env python3
r"""Cross-platform process-tree timer and loopback port cleaner.

Windows: `msvcrt` byte-range lock, `taskkill /F /T /PID`, `netstat` listener
scan on 127.0.0.1:18700-18799. POSIX: `fcntl.flock`, `start_new_session` and
`os.killpg` on the child's process group. Only processes started by this run
are killed; a listener that already existed stays untouched. Standard library
only, and both platform branches import their modules lazily.
"""

import argparse
import contextlib
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time

PORT_MIN, PORT_MAX = 18700, 18799
LOCK_ENV = "OMB_REAPER_LOCK_HELD"
LOCK_FILE_ENV = "OMB_REAPER_LOCK_FILE"
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


def port_pids():
    """Listening PIDs inside 18700-18799; Windows parses netstat, POSIX has none."""
    if os.name != "nt":
        return {}
    proc = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
    found = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or ":" not in parts[1]:
            continue
        port_text = parts[1].rsplit(":", 1)[1]
        if port_text.isdigit() and parts[4].isdigit():
            port = int(port_text)
            if PORT_MIN <= port <= PORT_MAX and int(parts[4]) > 0:
                found.setdefault(port, set()).add(int(parts[4]))
    return found


def flatten(mapping):
    pids = set()
    for group in mapping.values():
        pids.update(group)
    return pids


def kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def reap_new(baseline):
    current = flatten(port_pids())
    new_pids = sorted(current - baseline)
    for pid in new_pids:
        kill_tree(pid)
    if new_pids:
        time.sleep(0.2)
    return new_pids, sorted(flatten(port_pids()) - baseline)


def _spawn(cmd, cwd):
    options = {"cwd": str(cwd), "env": sandbox_env()}
    if os.name != "nt":
        options["start_new_session"] = True
    return subprocess.Popen(cmd, **options)


def run_with_reaper(cmd, timeout_sec, cwd):
    work_dir = pathlib.Path(cwd).expanduser().resolve()
    if not work_dir.is_dir():
        print(f"REAPER_REFUSED: cwd {work_dir} is missing", file=sys.stderr)
        return 126
    try:
        with acquire_lock(timeout_sec):
            return _execute(cmd, timeout_sec, work_dir)
    except TimeoutError as exc:
        print(f"REAPER_LOCK_TIMEOUT: {exc}", file=sys.stderr)
        return 124


def _execute(cmd, timeout_sec, work_dir):
    baseline = flatten(port_pids())
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
        killed, surviving = reap_new(baseline)
        if killed:
            print(f"REAPER_PORT_CLEANUP: terminated run PIDs={killed}")
        if surviving:
            print(f"REAPER_CLEANUP_FAILED: surviving PIDs={surviving}", file=sys.stderr)
            return 125
    return 124 if timed_out else code


def main(argv=None):
    configure_stdio()
    parser = argparse.ArgumentParser(description="Run one command under a process/port reaper.")
    parser.add_argument("--cwd", default=".", help="working directory for the command")
    parser.add_argument("--timeout", type=int, default=60, help="seconds before the tree is killed")
    parser.add_argument("--status", action="store_true", help="print the current sandbox-port map")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments")
    args = parser.parse_args(argv)
    if args.status:
        print(f"REAPER_PORTS_STATUS: { {port: sorted(pids) for port, pids in sorted(port_pids().items())} }")
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("REAPER_USAGE_ERROR: missing command", file=sys.stderr)
        return 2
    return run_with_reaper(command, args.timeout, args.cwd)


if __name__ == "__main__":
    sys.exit(main())
