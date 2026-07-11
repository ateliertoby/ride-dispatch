from ride_dispatch.bot import _latest_pending_for_chat


def test_single_card():
    pending = {100: ("o1", "src1", 1)}
    assert _latest_pending_for_chat(pending, 1) == (100, ("o1", "src1", 1))


def test_multi_cards_same_chat_returns_latest():
    pending = {100: ("o1", "src1", 1), 200: ("o2", "src2", 1)}
    assert _latest_pending_for_chat(pending, 1) == (200, ("o2", "src2", 1))


def test_multi_chats():
    pending = {100: ("o1", "src1", 1), 200: ("o2", "src2", 2), 300: ("o3", "src3", 1)}
    assert _latest_pending_for_chat(pending, 1) == (300, ("o3", "src3", 1))
    assert _latest_pending_for_chat(pending, 2) == (200, ("o2", "src2", 2))


def test_no_match():
    pending = {100: ("o1", "src1", 1)}
    assert _latest_pending_for_chat(pending, 999) is None


def test_empty_dict():
    assert _latest_pending_for_chat({}, 1) is None


def test_pop_semantics():
    """Caller pops the returned key; earlier cards for the same chat remain."""
    pending = {100: ("o1", "src1", 1), 200: ("o2", "src2", 1)}
    hit = _latest_pending_for_chat(pending, 1)
    assert hit is not None
    msg_id, _ = hit
    pending.pop(msg_id)
    assert 100 in pending
    assert 200 not in pending
