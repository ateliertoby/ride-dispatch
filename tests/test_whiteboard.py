import asyncio
import logging
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from ride_dispatch.whiteboard import (
    build_prompt,
    cache_path,
    cache_store,
    qualifies_for_prompt,
    sanitize_name,
    _build_data_uri,
    _build_payload,
    generate,
    WhiteboardError,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path):
    """Keep every test's whiteboard cache off the real runtime directory."""
    cache_dir = tmp_path / "whiteboard"
    with patch("ride_dispatch.whiteboard.CACHE_DIR", cache_dir):
        yield cache_dir


# ---- prompt construction ----


def test_prompt_contains_name_and_flight():
    prompt = build_prompt("PIYA DEJKONG", "TG607")
    assert "'PIYA DEJKONG'" in prompt
    assert "'TG607'" in prompt


def test_prompt_two_lines_structure():
    prompt = build_prompt("WATANABE/YUKI", "NH811")
    assert "First line:" in prompt
    assert "Second line:" in prompt
    assert "Third line" not in prompt


def test_prompt_exact_wording_fragments():
    prompt = build_prompt("X", "Y")
    assert "Change ONLY the handwritten text on the whiteboard" in prompt
    assert "pixel-identical" in prompt
    assert "messy thick black marker" in prompt
    assert "wobbly baselines" in prompt
    assert "do not translate, transliterate, or change any text" in prompt


def test_prompt_preserves_chinese_name():
    prompt = build_prompt("張大文", "CX889")
    assert "'張大文'" in prompt


# ---- data URI ----


def test_build_data_uri():
    uri = _build_data_uri(b"\x89PNG\r\n")
    assert uri.startswith("data:image/png;base64,")


# ---- payload construction ----


def test_build_payload_structure():
    payload = _build_payload("PIYA DEJKONG", "TG607", "data:image/png;base64,abc")
    assert payload["image_urls"] == ["data:image/png;base64,abc"]
    assert payload["image_size"] == {"width": 1024, "height": 768}
    assert payload["quality"] == "low"
    assert payload["output_format"] == "png"
    assert payload["num_images"] == 1
    assert "PIYA DEJKONG" in payload["prompt"]


# ---- VIP marker sanitization ----


def test_sanitize_removes_halfwidth_vip_marker():
    assert sanitize_name("PIYA DEJKONG(重要贵宾)") == "PIYA DEJKONG"


def test_sanitize_removes_fullwidth_vip_marker():
    assert sanitize_name("PIYA DEJKONG（重要贵宾）") == "PIYA DEJKONG"


def test_sanitize_removes_bare_vip_marker():
    assert sanitize_name("张大文(贵宾)") == "张大文"


def test_sanitize_removes_traditional_vip_marker():
    assert sanitize_name("張大文(貴賓)") == "張大文"


def test_sanitize_leaves_clean_name_unchanged():
    assert sanitize_name("PIYA DEJKONG") == "PIYA DEJKONG"


def test_sanitize_keeps_non_vip_parenthetical():
    # Only the 贵宾 family is platform metadata; other parentheses may be part
    # of the real name and must survive.
    assert sanitize_name("WONG TAI MAN (JR)") == "WONG TAI MAN (JR)"


def test_sanitize_marker_not_at_end():
    assert sanitize_name("LEE(重要贵宾) MING") == "LEE MING"


def test_sanitize_strips_surrounding_whitespace():
    assert sanitize_name("  PIYA DEJKONG  (重要贵宾)  ") == "PIYA DEJKONG"


def test_sanitize_empty_string():
    assert sanitize_name("") == ""


def test_generate_sanitizes_name_before_prompting():
    """The API must never be asked to write the VIP marker onto the board."""
    fake_submit = {"status_url": "https://q.fal.run/status/123", "response_url": "https://q.fal.run/result/123"}

    mock_client = AsyncMock()
    submit_resp = MagicMock(status_code=200)
    submit_resp.json.return_value = fake_submit
    poll_resp = MagicMock(status_code=200)
    poll_resp.json.return_value = {"status": "COMPLETED"}
    result_resp = MagicMock(status_code=200)
    result_resp.json.return_value = {"images": [{"url": "https://cdn.fal.run/img.png"}]}
    img_resp = MagicMock(status_code=200)
    img_resp.content = b"img"

    mock_client.post.return_value = submit_resp
    mock_client.get.side_effect = [poll_resp, result_resp, img_resp]

    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"), \
         patch("ride_dispatch.whiteboard.httpx.AsyncClient", return_value=_mock_client_ctx(mock_client)), \
         patch("ride_dispatch.whiteboard.POLL_INTERVAL", 0):
        asyncio.run(generate("PIYA DEJKONG(重要贵宾)", "TG607"))

    prompt = mock_client.post.call_args[1]["json"]["prompt"]
    assert "重要贵宾" not in prompt
    assert "'PIYA DEJKONG'" in prompt


# ---- qualification logic ----


def test_qualifies_pickup_with_banner():
    order = {
        "service_type": "接机",
        "additional_services": "举牌接机",
        "reminders_sent": "",
    }
    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
        assert qualifies_for_prompt(order) is True


def test_qualifies_bare_banner_flag():
    # 同程 orders carry no 附加服务 field; the parser sets this bare literal.
    order = {
        "service_type": "接机",
        "additional_services": "举牌",
        "reminders_sent": "",
    }
    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
        assert qualifies_for_prompt(order) is True


def test_not_qualifies_no_banner():
    order = {
        "service_type": "接机",
        "additional_services": "",
        "reminders_sent": "",
    }
    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
        assert qualifies_for_prompt(order) is False


def test_not_qualifies_not_pickup():
    order = {
        "service_type": "送机",
        "additional_services": "举牌接机",
        "reminders_sent": "",
    }
    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
        assert qualifies_for_prompt(order) is False


def test_not_qualifies_already_sent():
    order = {
        "service_type": "接机",
        "additional_services": "举牌接机",
        "reminders_sent": "svc,whiteboard",
    }
    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
        assert qualifies_for_prompt(order) is False


def test_not_qualifies_no_fal_key():
    order = {
        "service_type": "接机",
        "additional_services": "举牌接机",
        "reminders_sent": "",
    }
    with patch("ride_dispatch.whiteboard.FAL_KEY", ""):
        assert qualifies_for_prompt(order) is False


def test_not_qualifies_none_additional_services():
    order = {
        "service_type": "接机",
        "additional_services": None,
        "reminders_sent": "",
    }
    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
        assert qualifies_for_prompt(order) is False


# ---- async generate: happy path ----


def _mock_client_ctx(mock_client):
    """Build a patched httpx.AsyncClient context manager returning mock_client."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def test_generate_happy_path():
    fake_submit = {"status_url": "https://q.fal.run/status/123", "response_url": "https://q.fal.run/result/123"}
    fake_poll_done = {"status": "COMPLETED"}
    fake_result = {"images": [{"url": "https://cdn.fal.run/img.png"}]}
    fake_image_bytes = b"\x89PNG fake image"

    mock_client = AsyncMock()
    submit_resp = MagicMock(status_code=200)
    submit_resp.json.return_value = fake_submit
    poll_resp = MagicMock(status_code=200)
    poll_resp.json.return_value = fake_poll_done
    result_resp = MagicMock(status_code=200)
    result_resp.json.return_value = fake_result
    img_resp = MagicMock(status_code=200)
    img_resp.content = fake_image_bytes

    mock_client.post.return_value = submit_resp
    mock_client.get.side_effect = [poll_resp, result_resp, img_resp]

    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"), \
         patch("ride_dispatch.whiteboard.httpx.AsyncClient", return_value=_mock_client_ctx(mock_client)), \
         patch("ride_dispatch.whiteboard.POLL_INTERVAL", 0):
        result = asyncio.run(generate("PIYA DEJKONG", "TG607"))
        assert result == fake_image_bytes

    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "https://queue.fal.run/fal-ai/gpt-image-2/edit"


def test_generate_polls_through_in_progress():
    fake_submit = {"status_url": "https://q.fal.run/status/123", "response_url": "https://q.fal.run/result/123"}
    fake_image_bytes = b"img"

    mock_client = AsyncMock()
    submit_resp = MagicMock(status_code=200)
    submit_resp.json.return_value = fake_submit

    poll_q = MagicMock(status_code=202)
    poll_q.json.return_value = {"status": "IN_QUEUE"}
    poll_p = MagicMock(status_code=202)
    poll_p.json.return_value = {"status": "IN_PROGRESS"}
    poll_done = MagicMock(status_code=200)
    poll_done.json.return_value = {"status": "COMPLETED"}
    result_resp = MagicMock(status_code=200)
    result_resp.json.return_value = {"images": [{"url": "https://cdn.fal.run/img.png"}]}
    img_resp = MagicMock(status_code=200)
    img_resp.content = fake_image_bytes

    mock_client.post.return_value = submit_resp
    mock_client.get.side_effect = [poll_q, poll_p, poll_done, result_resp, img_resp]

    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"), \
         patch("ride_dispatch.whiteboard.httpx.AsyncClient", return_value=_mock_client_ctx(mock_client)), \
         patch("ride_dispatch.whiteboard.POLL_INTERVAL", 0):
        result = asyncio.run(generate("TEST/USER", "CX100"))
        assert result == fake_image_bytes

    # 3 polls + result fetch + image download = 5 GETs
    assert mock_client.get.call_count == 5


def test_generate_timeout():
    fake_submit = {"status_url": "https://q.fal.run/status/123", "response_url": "https://q.fal.run/result/123"}

    mock_client = AsyncMock()
    submit_resp = MagicMock(status_code=200)
    submit_resp.json.return_value = fake_submit
    poll_resp = MagicMock(status_code=202)
    poll_resp.json.return_value = {"status": "IN_PROGRESS"}

    mock_client.post.return_value = submit_resp
    mock_client.get.return_value = poll_resp

    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"), \
         patch("ride_dispatch.whiteboard.httpx.AsyncClient", return_value=_mock_client_ctx(mock_client)), \
         patch("ride_dispatch.whiteboard.POLL_INTERVAL", 0), \
         patch("ride_dispatch.whiteboard.POLL_TIMEOUT", 0):
        with pytest.raises(WhiteboardError, match="timed out"):
            asyncio.run(generate("TEST/USER", "CX100"))


def test_generate_terminal_status_raises():
    fake_submit = {"status_url": "https://q.fal.run/status/123", "response_url": "https://q.fal.run/result/123"}

    mock_client = AsyncMock()
    submit_resp = MagicMock(status_code=200)
    submit_resp.json.return_value = fake_submit
    poll_resp = MagicMock(status_code=200)
    poll_resp.json.return_value = {"status": "FAILED"}

    mock_client.post.return_value = submit_resp
    mock_client.get.return_value = poll_resp

    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"), \
         patch("ride_dispatch.whiteboard.httpx.AsyncClient", return_value=_mock_client_ctx(mock_client)), \
         patch("ride_dispatch.whiteboard.POLL_INTERVAL", 0):
        with pytest.raises(WhiteboardError, match="terminal status"):
            asyncio.run(generate("TEST/USER", "CX100"))


def test_generate_202_polls_then_200_completes():
    """Real fal.ai flow: pending polls return 202, completed returns 200."""
    fake_submit = {"status_url": "https://q.fal.run/status/123", "response_url": "https://q.fal.run/result/123"}
    fake_image_bytes = b"\x89PNG 202 flow"

    mock_client = AsyncMock()
    submit_resp = MagicMock(status_code=200)
    submit_resp.json.return_value = fake_submit

    poll_202_queue = MagicMock(status_code=202)
    poll_202_queue.json.return_value = {"status": "IN_QUEUE"}
    poll_202_progress = MagicMock(status_code=202)
    poll_202_progress.json.return_value = {"status": "IN_PROGRESS"}
    poll_202_bare = MagicMock(status_code=202)
    poll_202_bare.json.return_value = {}  # no recognizable status
    poll_200_done = MagicMock(status_code=200)
    poll_200_done.json.return_value = {"status": "COMPLETED"}
    result_resp = MagicMock(status_code=200)
    result_resp.json.return_value = {"images": [{"url": "https://cdn.fal.run/img.png"}]}
    img_resp = MagicMock(status_code=200)
    img_resp.content = fake_image_bytes

    mock_client.post.return_value = submit_resp
    mock_client.get.side_effect = [
        poll_202_queue, poll_202_progress, poll_202_bare, poll_200_done,
        result_resp, img_resp,
    ]

    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"), \
         patch("ride_dispatch.whiteboard.httpx.AsyncClient", return_value=_mock_client_ctx(mock_client)), \
         patch("ride_dispatch.whiteboard.POLL_INTERVAL", 0):
        result = asyncio.run(generate("TEST/USER", "CX100"))
        assert result == fake_image_bytes

    # 4 polls + result fetch + image download = 6 GETs
    assert mock_client.get.call_count == 6


def test_generate_4xx_raises_immediately():
    """Client errors (e.g. 401) on poll raise immediately, not after timeout."""
    fake_submit = {"status_url": "https://q.fal.run/status/123", "response_url": "https://q.fal.run/result/123"}

    mock_client = AsyncMock()
    submit_resp = MagicMock(status_code=200)
    submit_resp.json.return_value = fake_submit
    poll_401 = MagicMock(status_code=401, text="Unauthorized")

    mock_client.post.return_value = submit_resp
    mock_client.get.return_value = poll_401

    with patch("ride_dispatch.whiteboard.FAL_KEY", "bad-key"), \
         patch("ride_dispatch.whiteboard.httpx.AsyncClient", return_value=_mock_client_ctx(mock_client)), \
         patch("ride_dispatch.whiteboard.POLL_INTERVAL", 0):
        with pytest.raises(WhiteboardError, match="client error: 401"):
            asyncio.run(generate("TEST/USER", "CX100"))

    # Only one poll attempt — did not keep retrying
    assert mock_client.get.call_count == 1


def test_generate_5xx_retries_then_succeeds():
    """5xx is transient — poll retries and succeeds on next attempt."""
    fake_submit = {"status_url": "https://q.fal.run/status/123", "response_url": "https://q.fal.run/result/123"}
    fake_image_bytes = b"img"

    mock_client = AsyncMock()
    submit_resp = MagicMock(status_code=200)
    submit_resp.json.return_value = fake_submit

    poll_500 = MagicMock(status_code=500, text="Internal Server Error")
    poll_done = MagicMock(status_code=200)
    poll_done.json.return_value = {"status": "COMPLETED"}
    result_resp = MagicMock(status_code=200)
    result_resp.json.return_value = {"images": [{"url": "https://cdn.fal.run/img.png"}]}
    img_resp = MagicMock(status_code=200)
    img_resp.content = fake_image_bytes

    mock_client.post.return_value = submit_resp
    mock_client.get.side_effect = [poll_500, poll_done, result_resp, img_resp]

    with patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"), \
         patch("ride_dispatch.whiteboard.httpx.AsyncClient", return_value=_mock_client_ctx(mock_client)), \
         patch("ride_dispatch.whiteboard.POLL_INTERVAL", 0):
        result = asyncio.run(generate("TEST/USER", "CX100"))
        assert result == fake_image_bytes

    # 2 polls + result fetch + image download = 4 GETs
    assert mock_client.get.call_count == 4


# ---- auto-trigger integration ----


def _make_test_db_with_order(order_id, additional_services="", mark_whiteboard=False,
                             passenger_name="PIYA DEJKONG"):
    from ride_dispatch.db import init_db, save_order, mark_reminder_sent
    from ride_dispatch.parser import Order

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(db)
    order = Order(
        order_id=order_id, service_type="接机", vehicle_type="经济5座",
        passenger_name=passenger_name, scheduled_time="2026-07-13 14:00:00",
        passenger_phone="66 812345678", overseas_phone="",
        flight_number="TG607", pickup="香港国际机场 T1", dropoff="尖沙咀",
        distance_km=30, notes="", driver_notes="",
        additional_services=additional_services, passenger_exit_minutes=30,
        third_party_contact="", more_contacts="", raw_message="raw",
    )
    save_order(db, order, telegram_msg_id=1)
    if mark_whiteboard:
        mark_reminder_sent(db, order_id, "whiteboard")
    return db


def test_notify_landed_prompts_and_marks_without_generating():
    """Landing push is followed by a confirm prompt; no API call until it is tapped."""
    from ride_dispatch.db import get_order_by_id
    db = _make_test_db_with_order("WB001", additional_services="举牌接机")
    try:
        bot = AsyncMock()
        info = {"eta": "14:10", "gate": None, "status": "landed", "hall": "A"}

        with patch("ride_dispatch.bot.DB_PATH", db), \
             patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"), \
             patch("ride_dispatch.bot.generate_whiteboard", new=AsyncMock()) as gen:
            from ride_dispatch.bot import _notify_status_change
            asyncio.run(_notify_status_change(bot, 123, "WB001", info, None, "landed"))

        assert bot.send_message.call_count == 2
        assert "已降落" in bot.send_message.call_args_list[0][1]["text"]
        gen.assert_not_called()
        updated = get_order_by_id(db, "WB001")
        assert "whiteboard" in updated["reminders_sent"]
    finally:
        os.unlink(db)


def test_notify_landed_prompt_shows_board_text_and_button():
    """The prompt previews exactly what goes on the board, VIP marker stripped."""
    db = _make_test_db_with_order("WB006", additional_services="举牌接机",
                                  passenger_name="PIYA DEJKONG(重要贵宾)")
    try:
        bot = AsyncMock()
        info = {"eta": "14:10", "gate": None, "status": "landed", "hall": "A"}

        with patch("ride_dispatch.bot.DB_PATH", db), \
             patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
            from ride_dispatch.bot import _notify_status_change
            asyncio.run(_notify_status_change(bot, 123, "WB006", info, None, "landed"))

        prompt_call = bot.send_message.call_args_list[1]
        text = prompt_call[1]["text"]
        assert "舉牌相 #B006" in text
        assert "PIYA DEJKONG" in text
        assert "重要贵宾" not in text
        assert "TG607" in text

        button = prompt_call[1]["reply_markup"].inline_keyboard[0][0]
        assert button.text == "生成舉牌相"
        assert button.callback_data == "board:WB006"
    finally:
        os.unlink(db)


def test_notify_landed_no_prompt_without_banner():
    """Orders without 舉牌 service get no whiteboard prompt."""
    db = _make_test_db_with_order("WB002", additional_services="")
    try:
        bot = AsyncMock()
        info = {"eta": "14:10", "gate": None, "status": "landed", "hall": "A"}

        with patch("ride_dispatch.bot.DB_PATH", db), \
             patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
            from ride_dispatch.bot import _notify_status_change
            asyncio.run(_notify_status_change(bot, 123, "WB002", info, None, "landed"))

        bot.send_message.assert_called_once()
    finally:
        os.unlink(db)


def test_notify_landed_no_prompt_already_sent():
    """Already-marked orders must not be prompted twice on landed→gate."""
    db = _make_test_db_with_order("WB003", additional_services="举牌接机", mark_whiteboard=True)
    try:
        bot = AsyncMock()
        info = {"eta": "14:10", "gate": None, "status": "landed", "hall": "A"}

        with patch("ride_dispatch.bot.DB_PATH", db), \
             patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
            from ride_dispatch.bot import _notify_status_change
            asyncio.run(_notify_status_change(bot, 123, "WB003", info, None, "landed"))

        bot.send_message.assert_called_once()
    finally:
        os.unlink(db)


def test_notify_landed_no_prompt_without_fal_key():
    """No FAL_KEY means the feature is off, so no prompt."""
    db = _make_test_db_with_order("WB004", additional_services="举牌接机")
    try:
        bot = AsyncMock()
        info = {"eta": "14:10", "gate": None, "status": "landed", "hall": "A"}

        with patch("ride_dispatch.bot.DB_PATH", db), \
             patch("ride_dispatch.whiteboard.FAL_KEY", ""):
            from ride_dispatch.bot import _notify_status_change
            asyncio.run(_notify_status_change(bot, 123, "WB004", info, None, "landed"))

        bot.send_message.assert_called_once()
    finally:
        os.unlink(db)


# ---- _send_whiteboard failure path ----


def test_send_whiteboard_failure_sends_auto_fallback_message():
    """Auto path (default fail_text) sends the auto failure wording."""
    from ride_dispatch.bot import _send_whiteboard

    bot = AsyncMock()
    with patch("ride_dispatch.bot.generate_whiteboard", side_effect=WhiteboardError("boom")):
        asyncio.run(_send_whiteboard(bot, 123, "WB005", {"passenger_name": "PIYA DEJKONG", "flight_number": "TG607"}))

    bot.send_message.assert_called_once()
    text = bot.send_message.call_args[1]["text"]
    assert "自動生成失敗" in text
    assert "/board 重試" in text
    bot.send_photo.assert_not_called()


def test_send_whiteboard_failure_sends_manual_fallback_message():
    """Manual path (custom fail_text) sends the manual failure wording."""
    from ride_dispatch.bot import _send_whiteboard

    bot = AsyncMock()
    manual_fail = "舉牌相生成失敗 #B005，手動準備。"
    with patch("ride_dispatch.bot.generate_whiteboard", side_effect=WhiteboardError("boom")):
        asyncio.run(_send_whiteboard(bot, 123, "WB005",
                                     {"passenger_name": "PIYA DEJKONG", "flight_number": "TG607"},
                                     fail_text=manual_fail))

    bot.send_message.assert_called_once()
    text = bot.send_message.call_args[1]["text"]
    assert text == manual_fail
    bot.send_photo.assert_not_called()


def test_send_whiteboard_caption_uses_sanitized_name():
    """The caption must read like the board, not like the platform's raw field."""
    from ride_dispatch.bot import _send_whiteboard

    bot = AsyncMock()
    with patch("ride_dispatch.bot.generate_whiteboard", new=AsyncMock(return_value=b"img")):
        asyncio.run(_send_whiteboard(bot, 123, "WB007",
                                     {"passenger_name": "PIYA DEJKONG(重要贵宾)",
                                      "flight_number": "TG607"}))

    assert bot.send_photo.call_args[1]["caption"] == "舉牌 | PIYA DEJKONG TG607"


def test_board_callback_uses_create_task():
    """The board: callback dispatches generation via create_task, not inline await."""
    db = _make_test_db_with_order("WB010", additional_services="举牌接机")
    try:
        application = MagicMock()
        bot = AsyncMock()
        application.bot = bot

        query = MagicMock()
        query.data = "board:WB010"
        query.message.chat_id = 123
        query.message.message_id = 999
        query.answer = AsyncMock()
        query.message.edit_reply_markup = AsyncMock()
        query.message.reply_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        context.application = application
        context.bot = bot

        with patch("ride_dispatch.bot.DB_PATH", db), \
             patch("ride_dispatch.bot.ALLOWED_CHAT_IDS", set()), \
             patch("ride_dispatch.whiteboard.FAL_KEY", "test-key"):
            from ride_dispatch.bot import handle_callback
            asyncio.run(handle_callback(update, context))

        # Should fire create_task, not await generate_whiteboard inline
        application.create_task.assert_called_once()
        # The 生成中 message should have been sent
        query.message.reply_text.assert_called_once_with("生成中…")
    finally:
        os.unlink(db)


# ---- delivery failure vs generation failure ----


_ORDER = {"passenger_name": "PIYA DEJKONG", "flight_number": "TG607"}


def _run_send(bot, order_id, order=None, **kwargs):
    from ride_dispatch.bot import _send_whiteboard
    # Zero backoff keeps the retry timing out of the test's runtime.
    with patch("ride_dispatch.bot.DELIVERY_RETRY_DELAYS", (0, 0)):
        asyncio.run(_send_whiteboard(bot, 123, order_id, order or _ORDER, **kwargs))


def test_delivery_failure_message_differs_from_generation_failure():
    """A dropped connection must not be reported as a generation failure."""
    bot = AsyncMock()
    bot.send_photo.side_effect = NetworkError("httpx.ReadError")

    with patch("ride_dispatch.bot.generate_whiteboard", new=AsyncMock(return_value=b"img")):
        _run_send(bot, "WB020")

    text = bot.send_message.call_args[1]["text"]
    assert "已生成" in text
    assert "生成失敗" not in text
    assert "/board" in text


def test_delivery_retry_succeeds_on_second_attempt():
    """One transient blip costs a retry, not a generation and not a fail message."""
    bot = AsyncMock()
    bot.send_photo.side_effect = [NetworkError("httpx.ReadError"), None]
    gen = AsyncMock(return_value=b"img")

    with patch("ride_dispatch.bot.generate_whiteboard", new=gen):
        _run_send(bot, "WB021")

    assert bot.send_photo.call_count == 2
    gen.assert_awaited_once()
    bot.send_message.assert_not_called()


def test_delivery_retry_exhausted_reports_delivery_failure():
    bot = AsyncMock()
    bot.send_photo.side_effect = TimedOut()
    gen = AsyncMock(return_value=b"img")

    with patch("ride_dispatch.bot.generate_whiteboard", new=gen):
        _run_send(bot, "WB022")

    assert bot.send_photo.call_count == 3
    gen.assert_awaited_once()
    assert "已生成" in bot.send_message.call_args[1]["text"]


def test_delivery_does_not_retry_bad_request():
    """BadRequest subclasses NetworkError but is a refusal, not a dropped line."""
    bot = AsyncMock()
    bot.send_photo.side_effect = BadRequest("PHOTO_INVALID_DIMENSIONS")

    with patch("ride_dispatch.bot.generate_whiteboard", new=AsyncMock(return_value=b"img")):
        _run_send(bot, "WB023")

    bot.send_photo.assert_called_once()
    assert "已生成" in bot.send_message.call_args[1]["text"]


def test_generation_failure_still_reports_generation_failure():
    bot = AsyncMock()
    with patch("ride_dispatch.bot.generate_whiteboard", side_effect=WhiteboardError("boom")):
        _run_send(bot, "WB024")

    text = bot.send_message.call_args[1]["text"]
    assert "自動生成失敗" in text
    bot.send_photo.assert_not_called()


# ---- generated-image cache ----


def test_cache_written_after_generation():
    bot = AsyncMock()
    bot.send_photo.side_effect = NetworkError("httpx.ReadError")

    with patch("ride_dispatch.bot.generate_whiteboard", new=AsyncMock(return_value=b"img")):
        _run_send(bot, "WB030")

    assert cache_path("WB030", "PIYA DEJKONG", "TG607").read_bytes() == b"img"


def test_cache_hit_skips_generation_and_resends():
    cache_store("WB031", "PIYA DEJKONG", "TG607", b"cached")
    bot = AsyncMock()
    gen = AsyncMock(return_value=b"fresh")

    with patch("ride_dispatch.bot.generate_whiteboard", new=gen):
        _run_send(bot, "WB031")

    gen.assert_not_called()
    assert bot.send_photo.call_args[1]["photo"] == b"cached"


def test_cache_deleted_after_successful_delivery():
    bot = AsyncMock()
    with patch("ride_dispatch.bot.generate_whiteboard", new=AsyncMock(return_value=b"img")):
        _run_send(bot, "WB032")

    assert not cache_path("WB032", "PIYA DEJKONG", "TG607").exists()


def test_cache_survives_a_failed_delivery_for_the_next_attempt():
    """The whole point: a second /board resends without paying again."""
    bot = AsyncMock()
    bot.send_photo.side_effect = NetworkError("httpx.ReadError")
    gen = AsyncMock(return_value=b"img")

    with patch("ride_dispatch.bot.generate_whiteboard", new=gen):
        _run_send(bot, "WB033")

    bot2 = AsyncMock()
    with patch("ride_dispatch.bot.generate_whiteboard", new=gen):
        _run_send(bot2, "WB033")

    gen.assert_awaited_once()
    assert bot2.send_photo.call_args[1]["photo"] == b"img"
    assert not cache_path("WB033", "PIYA DEJKONG", "TG607").exists()


def test_cache_miss_when_name_changed():
    """An edited order must regenerate rather than resend the stale board."""
    cache_store("WB034", "OLD NAME", "TG607", b"stale")
    bot = AsyncMock()
    gen = AsyncMock(return_value=b"fresh")

    with patch("ride_dispatch.bot.generate_whiteboard", new=gen):
        _run_send(bot, "WB034")

    gen.assert_awaited_once()
    assert bot.send_photo.call_args[1]["photo"] == b"fresh"


def test_cache_key_uses_sanitized_name():
    """Cache lookup must agree with the name the generator was given."""
    cache_store("WB035", "PIYA DEJKONG", "TG607", b"cached")
    bot = AsyncMock()
    gen = AsyncMock(return_value=b"fresh")

    with patch("ride_dispatch.bot.generate_whiteboard", new=gen):
        _run_send(bot, "WB035",
                  {"passenger_name": "PIYA DEJKONG(重要贵宾)", "flight_number": "TG607"})

    gen.assert_not_called()
    assert bot.send_photo.call_args[1]["photo"] == b"cached"
