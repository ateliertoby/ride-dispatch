import asyncio
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import ride_dispatch.bot as bot
from ride_dispatch import webproc

CHAT = 4242
N = bot.WEB_HEALTH_FAILURES
DOWN = ["HTTP 500"] * N


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(bot, "_web_failures", 0)
    monkeypatch.setattr(bot, "_web_outage", False)
    monkeypatch.setattr(bot, "_web_restarted", False)
    monkeypatch.setattr(bot, "_web_stuck", False)
    monkeypatch.setattr(bot, "_web_restart_at", None)
    monkeypatch.setattr(bot, "_web_notices", [])
    monkeypatch.setattr(bot, "_notify_chat_id", lambda: CHAT)


@pytest.fixture
def watch(state, monkeypatch):
    """The bot's check of the web app, with the web app's answers scripted:
    None for a healthy answer, a string for what went wrong. What the bot does
    about an outage is scripted too: watch.restarts says whether it restarts."""
    answers = []
    monkeypatch.setattr(bot, "_web_fault", AsyncMock(side_effect=lambda: answers.pop(0)))
    ctx = MagicMock()
    ctx.application.bot.send_message = AsyncMock()

    def run(*script):
        answers.extend(script)
        for _ in script:
            asyncio.run(bot._web_health_tick(ctx))
        return [call.kwargs["text"] for call in ctx.application.bot.send_message.call_args_list]

    async def act(fault, failures, now):
        run.acted.append(fault)
        return f"down {fault}", run.restarts

    run.acted = []
    run.restarts = True
    run.send = ctx.application.bot.send_message
    monkeypatch.setattr(bot, "_web_act", act)
    return run


def test_a_healthy_web_app_is_never_mentioned(watch):
    assert watch(None, None, None, None) == []
    assert watch.acted == []


def test_a_restart_is_not_an_outage(watch):
    """A deploy stops the web app for a few seconds. Fewer failures in a row
    than the threshold say nothing and do nothing."""
    assert watch(*["ConnectError"] * (N - 1), None) == []
    assert watch.acted == []


def test_failures_must_be_in_a_row(watch):
    assert watch("HTTP 500", "HTTP 500", None, "HTTP 500", "HTTP 500", None) == []


def test_an_outage_is_acted_on_once_and_told_once(watch):
    watch.restarts = False
    assert watch(*DOWN, *["HTTP 500"] * 5) == ["down HTTP 500"]
    assert watch.acted == ["HTTP 500"]


def test_the_end_of_an_outage_is_told_once(watch):
    said = watch(*DOWN, None, None)
    assert len(said) == 2 and said[0] == "down HTTP 500"
    assert "HTTP 500" not in said[1]


def test_a_second_outage_is_acted_on_again(watch):
    assert len(watch(*DOWN, None, *DOWN)) == 3
    assert len(watch.acted) == 2


def test_a_restart_that_did_not_help_is_told_once_and_not_repeated(watch):
    """The count starts again against the new process. When that one fails
    as often, a restart is not the cure, and another is not tried."""
    said = watch(*DOWN, *DOWN, *["HTTP 500"] * 5)
    assert len(said) == 2
    assert "HTTP 500" in said[1]
    assert watch.acted == ["HTTP 500"]


def test_nothing_is_said_about_a_restart_that_was_never_made(watch):
    watch.restarts = False
    assert len(watch(*DOWN, *DOWN, *DOWN)) == 1


def test_a_notice_that_could_not_be_pushed_is_pushed_on_the_next_check(watch):
    """An outage nobody was told about must not count as told, and telling it
    again must not act on it again."""
    watch.send.side_effect = [RuntimeError("telegram down"), None]
    watch(*DOWN, "HTTP 500")
    assert [c.kwargs["text"] for c in watch.send.call_args_list] == ["down HTTP 500"] * 2
    assert watch.acted == ["HTTP 500"]
    assert bot._web_notices == []


def test_nothing_is_checked_without_a_chat_to_tell(watch, monkeypatch):
    """A restart nobody hears about is the one thing this must not do."""
    monkeypatch.setattr(bot, "_notify_chat_id", lambda: 0)
    assert watch(*DOWN, "HTTP 500") == []
    assert watch.acted == []


# ---- what is done about an outage ----

NOW = datetime(2026, 10, 10, 9, 5, 7)


@pytest.fixture
def scene(state, monkeypatch, tmp_path):
    """A web process the bot can name, snapshot and stop, all recorded."""
    calls = []
    monkeypatch.setattr(bot, "DB_PATH", str(tmp_path / "orders.db"))
    monkeypatch.setattr(webproc, "read_pid", lambda path: calls.append(("read", path)) or 777)
    monkeypatch.setattr(webproc, "snapshot",
                        lambda pid, db: calls.append(("snapshot", pid)) or ["open files: 1023 of 1024"])
    monkeypatch.setattr(webproc, "stop", lambda pid: calls.append(("stop", pid)) or True)
    return calls, tmp_path


def _incident(tmp_path):
    (name,) = os.listdir(tmp_path / "incidents")
    return (tmp_path / "incidents" / name).read_text()


def test_the_process_is_recorded_before_it_is_stopped(scene):
    calls, tmp_path = scene
    text, restarted = asyncio.run(bot._web_act("HTTP 500", N, NOW))
    assert restarted
    assert [c[0] for c in calls] == ["read", "snapshot", "stop"]
    assert calls[0] == ("read", str(tmp_path / "web.pid"))
    record = _incident(tmp_path)
    assert "HTTP 500" in record and "open files: 1023 of 1024" in record
    assert "HTTP 500" in text and "web-20261010-090507.txt" in text


def test_a_second_restart_is_not_made_within_the_gap(scene, monkeypatch):
    calls, tmp_path = scene
    monkeypatch.setattr(bot, "_web_restart_at", NOW - timedelta(seconds=bot.WEB_RESTART_GAP - 1))
    text, restarted = asyncio.run(bot._web_act("HTTP 500", N, NOW))
    assert not restarted
    assert "stop" not in [c[0] for c in calls]
    # Still recorded: the state of a process left running is worth as much.
    assert "open files: 1023 of 1024" in _incident(tmp_path)
    monkeypatch.setattr(bot, "_web_restart_at", NOW - timedelta(seconds=bot.WEB_RESTART_GAP))
    assert asyncio.run(bot._web_act("HTTP 500", N, NOW + timedelta(seconds=1)))[1]


def test_a_process_that_cannot_be_named_is_left_alone(scene, monkeypatch):
    calls, tmp_path = scene
    monkeypatch.setattr(webproc, "read_pid", lambda path: None)
    text, restarted = asyncio.run(bot._web_act("ConnectError", N, NOW))
    assert not restarted
    assert calls == []
    assert "ConnectError" in _incident(tmp_path)


def test_a_process_that_would_not_stop_is_not_called_restarted(scene, monkeypatch):
    calls, tmp_path = scene
    monkeypatch.setattr(webproc, "stop", lambda pid: False)
    assert not asyncio.run(bot._web_act("HTTP 500", N, NOW))[1]


def test_a_record_that_cannot_be_written_does_not_stop_the_restart(scene, monkeypatch):
    calls, tmp_path = scene

    def full(*a):
        raise OSError("disk full")

    monkeypatch.setattr(webproc, "write_incident", full)
    text, restarted = asyncio.run(bot._web_act("HTTP 500", N, NOW))
    assert restarted and ("stop", 777) in calls


def test_the_restart_time_is_kept_across_outages(watch, monkeypatch):
    watch(*DOWN)
    assert bot._web_restart_at is not None
    kept = bot._web_restart_at
    watch(None)
    assert bot._web_restart_at == kept


# ---- asking the web app ----

def test_the_fault_names_the_status_or_the_error(monkeypatch):
    import httpx

    def answering(handler):
        monkeypatch.setattr(bot, "_web_health_transport", httpx.MockTransport(handler))
        return asyncio.run(bot._web_fault())

    def refuse(request):
        raise httpx.ConnectError("refused")

    seen = []
    assert answering(lambda r: seen.append(str(r.url)) or httpx.Response(200, json={"ok": True})) is None
    assert seen == [f"http://127.0.0.1:{bot._web_port()}/api/health"]
    assert answering(lambda r: httpx.Response(500)) == "HTTP 500"
    assert answering(refuse) == "ConnectError"


def test_the_bot_asks_the_port_the_web_app_listens_on(monkeypatch):
    monkeypatch.setenv("RIDE_WEB_PORT", "3999")
    assert bot._web_port() == 3999
    monkeypatch.setenv("RIDE_WEB_PORT", "")
    assert bot._web_port() == 3200
