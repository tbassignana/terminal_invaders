"""Finding the audio helper and its players from a test, the same way on Linux and macOS (pgrep and ps)."""

import os
import subprocess
import sys
from pathlib import Path


def alive(pgid: int) -> list[int]:
    """The live members of a process group. Zombies count as gone: an orphan's zombie is its new parent's to reap,
    and some containers' init never does."""
    if sys.platform != "linux":
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):  # macOS: EPERM for a group of zombies
            return []
        listed = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True).stdout
        rows = [row.split() for row in listed.splitlines()]
        return [int(pid) for pid, group, stat in rows if int(group) == pgid and "Z" not in stat]
    members = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                stat = Path("/proc", entry, "stat").read_text()
            except OSError:
                continue
            state, _ppid, group = stat.rsplit(")", 1)[1].split()[:3]
            if int(group) == pgid and state != "Z":
                members.append(int(entry))
    return members


def children(pid: int) -> list[int]:
    found = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(child) for child in found.split()]


def state(pid: int) -> str:
    """ps's STAT for pid: T is stopped, Z a zombie; empty once it is gone."""
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
