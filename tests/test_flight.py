from ride_dispatch.flight import (
    normalize_flight_no,
    match_flights,
    parse_status,
    svc_time,
    exit_urgency,
    predicted_landing_hhmm,
    depart_hhmm,
    effective_service_time,
)


def test_normalize_strips_spaces():
    assert normalize_flight_no("CX 489") == "CX489"


def test_normalize_uppercase():
    assert normalize_flight_no("cx489") == "CX489"


def test_normalize_multiple_spaces():
    assert normalize_flight_no("CX  4 89") == "CX489"


def test_parse_status_est():
    assert parse_status("Est at 14:26") == {"eta": "14:26", "gate": None, "status": "est"}


def test_parse_status_landed():
    assert parse_status("Landed 14:30") == {"eta": "14:30", "gate": None, "status": "landed"}


def test_parse_status_gate():
    assert parse_status("At gate 14:35") == {"eta": None, "gate": "14:35", "status": "gate"}


def test_parse_status_gate_crossday():
    assert parse_status("At gate 23:52 (28/06/2026)") == {"eta": None, "gate": "23:52", "status": "gate"}


def test_parse_status_empty():
    assert parse_status("") == {"eta": None, "gate": None, "status": None}


def test_parse_status_diverted():
    assert parse_status("Diverted To Guangzhou") == {"eta": None, "gate": None, "status": None}


def test_parse_status_cancelled():
    # HKIA uses the bare string 'Cancelled' (verified against live feed 2026-07-02)
    assert parse_status("Cancelled") == {"eta": None, "gate": None, "status": "cancelled"}


SAMPLE_ARRIVALS = [
    {
        "date": "2026-07-02",
        "time": "13:00",
        "flight": [{"no": "CX 489", "airline": "CPA"}],
        "status": "Est at 14:26",
        "hall": "A",
        "baggage": "3",
    },
    {
        "date": "2026-07-02",
        "time": "14:30",
        "flight": [
            {"no": "QR 3457", "airline": "QTR"},
            {"no": "CX 505", "airline": "CPA"},
        ],
        "status": "Landed 14:35",
        "hall": "B",
        "baggage": "9",
    },
    {
        "date": "2026-07-02",
        "time": "16:00",
        "flight": [{"no": "UO 117", "airline": "HKE"}],
        "status": "",
        "hall": "",
        "baggage": "",
    },
]


def test_match_direct_flight():
    orders = [{"order_id": "O1", "flight_number": "CX489", "scheduled_time": "2026-07-02 15:00:00"}]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {"O1": {"date": "2026-07-02", "scheduled": "13:00", "hall": "A", "baggage": "3", "raw_status": "Est at 14:26", "eta": "14:26", "gate": None, "status": "est"}}


def test_match_codeshare():
    orders = [{"order_id": "O2", "flight_number": "CX505", "scheduled_time": "2026-07-02 15:30:00"}]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {"O2": {"date": "2026-07-02", "scheduled": "14:30", "hall": "B", "baggage": "9", "raw_status": "Landed 14:35", "eta": "14:35", "gate": None, "status": "landed"}}


def test_match_no_status_still_returns_scheduled():
    orders = [{"order_id": "O3", "flight_number": "UO117", "scheduled_time": "2026-07-02 17:00:00"}]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {"O3": {"date": "2026-07-02", "scheduled": "16:00", "hall": "", "baggage": "", "raw_status": "", "eta": None, "gate": None, "status": None}}


def test_match_not_found():
    orders = [{"order_id": "O4", "flight_number": "XX999", "scheduled_time": "2026-07-02 15:00:00"}]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {}


def test_match_multiple_orders():
    orders = [
        {"order_id": "O1", "flight_number": "CX489", "scheduled_time": "2026-07-02 15:00:00"},
        {"order_id": "O2", "flight_number": "CX505", "scheduled_time": "2026-07-02 15:30:00"},
        {"order_id": "O4", "flight_number": "XX999", "scheduled_time": "2026-07-02 15:00:00"},
    ]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {
        "O1": {"date": "2026-07-02", "scheduled": "13:00", "hall": "A", "baggage": "3", "raw_status": "Est at 14:26", "eta": "14:26", "gate": None, "status": "est"},
        "O2": {"date": "2026-07-02", "scheduled": "14:30", "hall": "B", "baggage": "9", "raw_status": "Landed 14:35", "eta": "14:35", "gate": None, "status": "landed"},
    }


# --- date-aware matching: HKIA span=1 returns the previous day too, and
# --- flight numbers repeat daily (MU5017 2026-07-02) ---


def test_match_prefers_same_day_over_adjacent_day_final():
    # Yesterday's same-number flight sits earlier in the feed at final 'gate'
    # status; today's leg must win regardless of feed order.
    arrivals = [
        {"date": "2026-07-01", "time": "08:15", "flight": [{"no": "MU 5017"}],
         "status": "At gate 08:20", "hall": "A", "baggage": "1"},
        {"date": "2026-07-02", "time": "08:15", "flight": [{"no": "MU 5017"}],
         "status": "Est at 08:30", "hall": "B", "baggage": "2"},
    ]
    orders = [{"order_id": "A", "flight_number": "MU5017", "scheduled_time": "2026-07-02 09:00:00"}]
    result = match_flights(orders, arrivals)
    assert result["A"]["status"] == "est"
    assert result["A"]["date"] == "2026-07-02"


def test_match_rejects_candidate_over_12h_away():
    # Tomorrow's leg not yet published; only yesterday's final entry present.
    # No match beats writing yesterday's 'gate' into tomorrow's order.
    arrivals = [
        {"date": "2026-07-02", "time": "08:15", "flight": [{"no": "MU 5017"}],
         "status": "At gate 08:20", "hall": "A", "baggage": "1"},
    ]
    orders = [{"order_id": "A", "flight_number": "MU5017", "scheduled_time": "2026-07-03 09:00:00"}]
    assert match_flights(orders, arrivals) == {}


def test_match_red_eye_previous_day_within_window():
    # 00:30 pickup, flight lands 23:50 the previous calendar day — must match.
    arrivals = [
        {"date": "2026-07-02", "time": "23:50", "flight": [{"no": "UO 623"}],
         "status": "Landed 23:55", "hall": "A", "baggage": "4"},
    ]
    orders = [{"order_id": "A", "flight_number": "UO623", "scheduled_time": "2026-07-03 00:30:00"}]
    result = match_flights(orders, arrivals)
    assert result["A"]["status"] == "landed"


def test_match_same_day_duplicate_number_picks_closest():
    # Feed order deliberately puts the wrong leg last so last-wins would fail.
    arrivals = [
        {"date": "2026-07-02", "time": "20:00", "flight": [{"no": "XX 100"}],
         "status": "Est at 20:10", "hall": "B", "baggage": "2"},
        {"date": "2026-07-02", "time": "08:00", "flight": [{"no": "XX 100"}],
         "status": "At gate 08:05", "hall": "A", "baggage": "1"},
    ]
    orders = [{"order_id": "A", "flight_number": "XX100", "scheduled_time": "2026-07-02 21:00:00"}]
    result = match_flights(orders, arrivals)
    assert result["A"]["status"] == "est"


# --- svc_time: 用車時間 = arrival + passenger exit minutes; must never crash
# --- on unknown arrival time ("?" reached production via est→gate jump) ---


def test_svc_time_adds_exit_minutes():
    assert svc_time("14:23", 40) == "15:03"


def test_svc_time_wraps_midnight():
    assert svc_time("23:50", 30) == "00:20"


def test_svc_time_unknown_eta_returns_none():
    assert svc_time("?", 40) is None
    assert svc_time(None, 40) is None
    assert svc_time("", 40) is None


def test_match_preserves_raw_status_for_unknown():
    # Unparsed statuses must stay visible so the poller can log them
    arrivals = [
        {"date": "2026-07-02", "time": "13:00", "flight": [{"no": "CX 489"}],
         "status": "Diverted To Guangzhou", "hall": "A", "baggage": "3"},
    ]
    orders = [{"order_id": "A", "flight_number": "CX489", "scheduled_time": "2026-07-02 15:00:00"}]
    result = match_flights(orders, arrivals)
    assert result["A"]["status"] is None
    assert result["A"]["raw_status"] == "Diverted To Guangzhou"


# ---- exit urgency + depart time ----


def test_exit_urgency_bands():
    assert exit_urgency(None) is None
    assert exit_urgency(0) is None
    assert exit_urgency(15) == "urgent"
    assert exit_urgency(20) == "urgent"
    assert exit_urgency(21) == "tight"
    assert exit_urgency(30) == "tight"
    assert exit_urgency(31) is None
    assert exit_urgency(60) is None


def test_predicted_landing_prefers_eta():
    assert predicted_landing_hhmm({"flight_eta": "14:26", "flight_scheduled": "14:00"}) == "14:26"


def test_predicted_landing_falls_back_to_scheduled():
    assert predicted_landing_hhmm({"flight_eta": None, "flight_scheduled": "14:00"}) == "14:00"


def test_predicted_landing_skips_invalid_eta():
    assert predicted_landing_hhmm({"flight_eta": "?", "flight_scheduled": "14:00"}) == "14:00"


def test_predicted_landing_derives_from_booking_time():
    order = {"scheduled_time": "2026-07-21 12:00:00", "passenger_exit_minutes": 30}
    assert predicted_landing_hhmm(order) == "11:30"


def test_predicted_landing_none_without_any_anchor():
    assert predicted_landing_hhmm({"scheduled_time": "2026-07-21 12:00:00"}) is None
    assert predicted_landing_hhmm({"passenger_exit_minutes": 30, "scheduled_time": "bad"}) is None


def test_depart_20min_exit_leads_landing():
    assert depart_hhmm({"flight_eta": "14:00", "passenger_exit_minutes": 20}) == "13:40"


def test_depart_30min_exit():
    assert depart_hhmm({"flight_eta": "14:00", "passenger_exit_minutes": 30}) == "13:50"


def test_depart_60min_exit_after_landing():
    assert depart_hhmm({"flight_eta": "14:00", "passenger_exit_minutes": 60}) == "14:20"


def test_depart_no_flight_data_is_booking_minus_drive():
    order = {"scheduled_time": "2026-07-21 12:35:00", "passenger_exit_minutes": 30}
    assert depart_hhmm(order) == "11:55"


def test_depart_wraps_midnight():
    assert depart_hhmm({"flight_eta": "00:10", "passenger_exit_minutes": 20}) == "23:50"


def test_depart_none_without_exit_minutes():
    assert depart_hhmm({"flight_eta": "14:00"}) is None
    assert depart_hhmm({"flight_eta": "14:00", "passenger_exit_minutes": 0}) is None


# ---- effective_service_time sort key ----


def test_effective_svc_time_delayed_pickup():
    order = {
        "service_type": "接机",
        "scheduled_time": "2026-07-23 18:15:00",
        "flight_status": "est",
        "flight_eta": "20:30",
        "passenger_exit_minutes": None,
    }
    assert effective_service_time(order) == "2026-07-23 20:30:00"


def test_effective_svc_time_early_with_exit():
    order = {
        "service_type": "接机",
        "scheduled_time": "2026-07-23 19:18:00",
        "flight_eta": "18:58",
        "passenger_exit_minutes": 40,
    }
    assert effective_service_time(order) == "2026-07-23 19:38:00"


def test_effective_svc_time_no_flight_data():
    order = {
        "service_type": "接机",
        "scheduled_time": "2026-07-23 18:15:00",
    }
    assert effective_service_time(order) == "2026-07-23 18:15:00"


def test_effective_svc_time_送机_ignores_flight():
    order = {
        "service_type": "送机",
        "scheduled_time": "2026-07-23 10:00:00",
        "flight_eta": "09:30",
        "passenger_exit_minutes": 30,
    }
    assert effective_service_time(order) == "2026-07-23 10:00:00"


def test_effective_svc_time_quick_order():
    order = {
        "service_type": "滴滴",
        "scheduled_time": "2026-07-23 14:00:00",
        "flight_eta": "13:00",
    }
    assert effective_service_time(order) == "2026-07-23 14:00:00"


def test_effective_svc_time_midnight_crossing():
    order = {
        "service_type": "接机",
        "scheduled_time": "2026-07-23 23:50:00",
        "flight_eta": "00:10",
        "passenger_exit_minutes": None,
    }
    assert effective_service_time(order) == "2026-07-24 00:10:00"


def test_effective_svc_time_malformed_scheduled():
    order = {
        "service_type": "接机",
        "scheduled_time": "bad-data",
        "flight_eta": "18:00",
    }
    assert effective_service_time(order) == "bad-data"
