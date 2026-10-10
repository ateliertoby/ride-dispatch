import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import ride_dispatch.bot as bot

CHAT = 4242


@pytest.fixture
def watch(monkeypatch):
    """The bot's check of the web app, with the web app's answers scripted:
    None for a healthy answer, a string for what went wrong."""
    answers = []
    monkeypatch.setattr(bot, "_web_failures", 0)
    monkeypatch.setattr(bot, "_web_down_told", False)
    monkeypatch.setattr(bot, "_notify_chat_id", lambda: CHAT)
    monkeypatch.setattr(bot, "_web_fault", AsyncMock(side_effect=lambda: answers.pop(0)))
    ctx = MagicMock()
    ctx.application.bot.send_message = AsyncMock()

    def run(*script):
        answers.extend(script)
        for _ in script:
            asyncio.run(bot._web_health_tick(ctx))
        return [call.kwargs["text"] for call in ctx.application.bot.send_message.call_args_list]

    return run


def test_a_healthy_web_app_is_never_mentioned(watch):
    assert watch(None, None, None, None) == []


def test_a_restart_is_not_an_outage(watch):
    """A deploy stops the web app for a few seconds. Fewer failures in a row
    than the threshold say nothing."""
    assert watch(*["ConnectError"] * (bot.WEB_HEALTH_FAILURES - 1), None) == []


def test_an_outage_is_told_once_with_what_the_app_answered(watch):
    said = watch(*["HTTP 500"] * (bot.WEB_HEALTH_FAILURES + 5))
    assert len(said) == 1
    assert "HTTP 500" in said[0]


def test_the_end_of_an_outage_is_told_once(watch):
    said = watch(*["HTTP 500"] * bot.WEB_HEALTH_FAILURES, None, None)
    assert len(said) == 2
    assert "HTTP 500" not in said[1]


def test_a_second_outage_is_told_again(watch):
    down = ["HTTP 500"] * bot.WEB_HEALTH_FAILURES
    assert len(watch(*down, None, *down)) == 3


def test_failures_must_be_in_a_row(watch):
    assert watch("HTTP 500", "HTTP 500", None, "HTTP 500", "HTTP 500", None) == []


def test_a_failed_push_is_tried_again_on_the_next_tick(watch, monkeypatch):
    """An outage nobody was told about must not count as told."""
    ctx_send = AsyncMock(side_effect=[RuntimeError("telegram down"), None])
    ctx = MagicMock()
    ctx.application.bot.send_message = ctx_send
    monkeypatch.setattr(bot, "_web_fault", AsyncMock(return_value="HTTP 500"))
    for _ in range(bot.WEB_HEALTH_FAILURES + 1):
        asyncio.run(bot._web_health_tick(ctx))
    assert ctx_send.call_count == 2
    assert bot._web_down_told


def test_nothing_is_checked_without_a_chat_to_tell(watch, monkeypatch):
    monkeypatch.setattr(bot, "_notify_chat_id", lambda: 0)
    assert watch(*["HTTP 500"] * (bot.WEB_HEALTH_FAILURES + 1)) == []


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
