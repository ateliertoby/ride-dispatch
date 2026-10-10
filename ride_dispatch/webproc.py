"""The running web process, as the bot needs to know it: which process it is,
what it holds, and how to end it so that its unit starts a fresh one.

A pid alone does not name a process, because the system hands the number to
another process once the first has gone.  The web app records its pid together
with the time the kernel says that pid started, and the pair is trusted only
while /proc still reports the same start time.  Where there is no /proc there
is no start time, nothing is ever named, and nothing is ever stopped.
"""
import os
import signal
import time
from collections import Counter

PROC = "/proc"
STOP_WAIT = 5
_STEP = 0.25


def pid_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "web.pid")


def incident_dir(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "incidents")


def start_ticks(pid: int, proc: str = PROC) -> str | None:
    """When the process started, in the kernel's clock ticks since boot."""
    try:
        with open(os.path.join(proc, str(pid), "stat")) as f:
            stat = f.read()
    except OSError:
        return None
    # The command sits in brackets and may itself hold brackets and spaces;
    # the numbered fields begin after the last closing one.  Start time is
    # field 22, the 20th after the command.
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def write_pid(path: str, pid: int | None = None, proc: str = PROC) -> None:
    pid = os.getpid() if pid is None else pid
    with open(path, "w") as f:
        f.write(f"{pid} {start_ticks(pid, proc) or ''}\n")


def read_pid(path: str, proc: str = PROC) -> int | None:
    """The recorded pid, if it is still the process that recorded it."""
    try:
        with open(path) as f:
            parts = f.read().split()
        pid, ticks = int(parts[0]), parts[1]
    except (OSError, ValueError, IndexError):
        return None
    return pid if start_ticks(pid, proc) == ticks else None


def _kind(target: str, db_path: str) -> str:
    if target == os.path.abspath(db_path):
        return "database"
    if target.startswith("socket:"):
        return "socket"
    if target.startswith("pipe:"):
        return "pipe"
    return "other"


def _field(path: str, name: str) -> str | None:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(name):
                    return line[len(name):].strip()
    except OSError:
        pass
    return None


def snapshot(pid: int, db_path: str, proc: str = PROC) -> list[str]:
    """What the process holds, as counts.  Taken before the process is ended,
    because none of it can be read afterwards.  It says how many files of
    each kind are open and never which, so a snapshot holds no data."""
    base = os.path.join(proc, str(pid))
    try:
        fds = os.listdir(os.path.join(base, "fd"))
    except OSError:
        return ["process state could not be read"]
    kinds = Counter()
    for fd in fds:
        try:
            kinds[_kind(os.readlink(os.path.join(base, "fd", fd)), db_path)] += 1
        except OSError:
            kinds["other"] += 1
    limit = (_field(os.path.join(base, "limits"), "Max open files") or "?").split()[0]
    lines = [f"open files: {len(fds)} of {limit}"]
    lines += [f"  {kind}: {kinds[kind]}" for kind in ("database", "socket", "pipe", "other") if kinds[kind]]
    try:
        lines.append(f"threads: {len(os.listdir(os.path.join(base, 'task')))}")
    except OSError:
        pass
    rss = _field(os.path.join(base, "status"), "VmRSS:")
    if rss:
        lines.append(f"memory: {rss}")
    return lines


def write_incident(directory: str, now, lines: list[str]) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, now.strftime("web-%Y%m%d-%H%M%S.txt"))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def stop(pid: int, proc: str = PROC, kill=os.kill, sleep=time.sleep) -> bool:
    """End the process; True once it has gone.  It is asked first and forced
    if it stays, since a process that is stopped or stuck acts on no request.
    Each signal is sent only while the pid is still the process it was."""
    ticks = start_ticks(pid, proc)
    if ticks is None:
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if start_ticks(pid, proc) != ticks:
            return True
        try:
            kill(pid, sig)
        except ProcessLookupError:
            return True
        for _ in range(int(STOP_WAIT / _STEP)):
            if start_ticks(pid, proc) != ticks:
                return True
            sleep(_STEP)
    return start_ticks(pid, proc) != ticks
