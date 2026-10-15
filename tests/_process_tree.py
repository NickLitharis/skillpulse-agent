"""Process-tree helpers for the M5 MCP tests.

The HTTP server and the MCP Inspector are launched as subprocesses. On Windows a venv
``python.exe`` is a launcher that starts the real interpreter as a *child*, so killing
only the launcher leaves the server alive; the Inspector likewise fans out to node and
the server. These helpers snapshot the whole descendant tree, kill it as a tree, and
verify every pid seen before the kill is gone afterwards.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from typing import Any

_WAIT_S = 10.0
_POLL_S = 0.2


def spawn_kwargs() -> dict[str, Any]:
    """Extra ``Popen`` kwargs so :func:`stop_tree` can kill the whole child group.

    On POSIX the child must lead its own session/group, otherwise ``killpg`` would
    signal this test process's group and take pytest down with it. Windows kills the
    tree with ``taskkill /T`` instead, so it needs nothing extra.
    """
    return {} if sys.platform == "win32" else {"start_new_session": True}


def _process_table() -> dict[int, list[int]]:
    """Map every live parent pid to its child pids."""
    if sys.platform == "win32":
        script = (
            "Get-CimInstance Win32_Process | "
            'ForEach-Object { "$($_.ProcessId) $($_.ParentProcessId)" }'
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    else:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=False
        ).stdout
    table: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        table.setdefault(int(parts[1]), []).append(int(parts[0]))
    return table


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True
    return True


def tree_pids(root: int) -> set[int]:
    """Return ``root`` and every live descendant pid."""
    if not _alive(root):
        return set()
    table = _process_table()
    found: set[int] = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        stack.extend(table.get(pid, []))
    return found


def stop_tree(process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> set[int]:
    """Kill ``process`` and its descendants; return any pid still alive afterwards."""
    pid = process.pid
    before = tree_pids(pid)
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            pgid = os.getpgid(pid)
            if pgid == pid:
                os.killpg(pgid, signal.SIGTERM)
            else:  # pragma: no cover - caller forgot spawn_kwargs: never signal our group
                process.terminate()
    try:
        process.wait(timeout=_WAIT_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - escalation path
        process.kill()
        process.wait(timeout=_WAIT_S)

    deadline = time.time() + _WAIT_S
    survivors = {p for p in before if _alive(p)}
    while survivors and time.time() < deadline:
        time.sleep(_POLL_S)
        survivors = {p for p in before if _alive(p)}
    if survivors:  # pragma: no cover - last-resort force kill
        for survivor in survivors:
            command = (
                ["taskkill", "/PID", str(survivor), "/T", "/F"]
                if sys.platform == "win32"
                else ["kill", "-9", str(survivor)]
            )
            subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            )
        time.sleep(_POLL_S)
        survivors = {p for p in before if _alive(p)}
    return survivors


def free_port() -> int:
    """Reserve and release a loopback TCP port for a test HTTP server."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
