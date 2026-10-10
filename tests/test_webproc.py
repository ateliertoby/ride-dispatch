import os
import signal
from datetime import datetime

import pytest

from ride_dispatch import webproc

PID = 4321
DB = "/data/orders.db"


def stat_line(pid, start, comm="python"):
    # Field 22 is the start time; the command, field 2, may hold spaces and
    # brackets of its own.
    fields = ["S", "1"] + ["0"] * 17 + [str(start)] + ["0"] * 10
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


@pytest.fixture
def proc(tmp_path):
    """A /proc with one process in it, built by make(...)."""
    root = tmp_path / "proc"

    def make(pid=PID, start=777, comm="python", fds=(), threads=1, rss="52000 kB", limit=1024):
        d = root / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "task").mkdir()
        (d / "stat").write_text(stat_line(pid, start, comm))
        (d / "status").write_text(f"Name:\tpython\nVmRSS:\t{rss}\nThreads:\t{threads}\n")
        (d / "limits").write_text(
            "Limit                     Soft Limit           Hard Limit           Units     \n"
            f"Max open files            {limit}                 524288               files     \n")
        for n, target in enumerate(fds):
            os.symlink(target, d / "fd" / str(n))
        for n in range(threads):
            (d / "task" / str(pid + n)).mkdir()
        return str(root)

    return make


def test_the_start_time_is_read_past_a_command_with_brackets_in_it(proc):
    root = proc(start=991, comm="py (web) x")
    assert webproc.start_ticks(PID, root) == "991"
    assert webproc.start_ticks(PID + 1, root) is None


def test_a_recorded_process_is_named_only_while_it_is_the_same_process(proc, tmp_path):
    """A pid is reused once its process has gone. The start time recorded
    beside it is what says the pid still means the process that wrote it."""
    root = proc(start=777)
    path = str(tmp_path / "web.pid")
    webproc.write_pid(path, pid=PID, proc=root)
    assert webproc.read_pid(path, root) == PID
    # The same pid, now another process.
    with open(os.path.join(root, str(PID), "stat"), "w") as f:
        f.write(stat_line(PID, 778))
    assert webproc.read_pid(path, root) is None


def test_no_process_is_named_without_a_record_or_without_a_start_time(proc, tmp_path):
    root = proc()
    assert webproc.read_pid(str(tmp_path / "absent.pid"), root) is None
    path = str(tmp_path / "web.pid")
    # Where there is no /proc to read a start time from, the record carries
    # none, and a pid alone names nothing.
    webproc.write_pid(path, pid=PID, proc=str(tmp_path / "no-proc"))
    assert webproc.read_pid(path, root) is None
    (tmp_path / "web.pid").write_text("not a pid\n")
    assert webproc.read_pid(path, root) is None


def test_a_snapshot_counts_what_the_process_holds_and_shows_none_of_it(proc):
    root = proc(fds=[DB] * 3 + ["socket:[11]", "socket:[12]", "pipe:[5]", "/dev/null", "/var/x.log"],
                threads=4, rss="81234 kB", limit=1024)
    lines = webproc.snapshot(PID, DB, root)
    text = "\n".join(lines)
    assert "open files: 8 of 1024" in text
    assert "database: 3" in text
    assert "socket: 2" in text
    assert "pipe: 1" in text
    assert "other: 2" in text
    assert "threads: 4" in text
    assert "memory: 81234 kB" in text
    assert "/var/x.log" not in text and "socket:[" not in text


def test_a_snapshot_of_a_process_that_cannot_be_read_says_so(tmp_path):
    assert webproc.snapshot(PID, DB, str(tmp_path / "no-proc")) == ["process state could not be read"]


def test_an_incident_is_written_under_its_time(tmp_path):
    path = webproc.write_incident(str(tmp_path / "incidents"), datetime(2026, 10, 10, 9, 5, 7),
                                  ["fault: HTTP 500", "threads: 4"])
    assert os.path.basename(path) == "web-20261010-090507.txt"
    assert open(path).read() == "fault: HTTP 500\nthreads: 4\n"


def test_stopping_asks_first_and_forces_only_a_process_that_stays(proc):
    root = proc()
    sent = []

    def leaves_when_asked(pid, sig):
        sent.append(sig)
        os.rename(os.path.join(root, str(PID)), os.path.join(root, "gone"))

    assert webproc.stop(PID, root, kill=leaves_when_asked, sleep=lambda s: None)
    assert sent == [signal.SIGTERM]


def test_a_process_that_ignores_the_request_is_forced(proc):
    """A stopped or stuck process does not act on SIGTERM."""
    root = proc()
    sent = []

    def only_force_works(pid, sig):
        sent.append(sig)
        if sig == signal.SIGKILL:
            os.rename(os.path.join(root, str(PID)), os.path.join(root, "gone"))

    assert webproc.stop(PID, root, kill=only_force_works, sleep=lambda s: None)
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_stopping_reports_a_process_that_outlives_both(proc):
    root = proc()
    assert not webproc.stop(PID, root, kill=lambda pid, sig: None, sleep=lambda s: None)


def test_a_pid_taken_by_another_process_mid_stop_is_not_forced(proc):
    root = proc(start=777)
    sent = []

    def replaced(pid, sig):
        sent.append(sig)
        with open(os.path.join(root, str(PID), "stat"), "w") as f:
            f.write(stat_line(PID, 999))

    assert webproc.stop(PID, root, kill=replaced, sleep=lambda s: None)
    assert sent == [signal.SIGTERM]
