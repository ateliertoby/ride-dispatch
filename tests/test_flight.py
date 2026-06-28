from ride_dispatch.flight import normalize_flight_no, match_flights


def test_normalize_strips_spaces():
    assert normalize_flight_no("CX 489") == "CX489"


def test_normalize_uppercase():
    assert normalize_flight_no("cx489") == "CX489"


def test_normalize_multiple_spaces():
    assert normalize_flight_no("CX  4 89") == "CX489"


SAMPLE_ARRIVALS = [
    {
        "time": "13:00",
        "flight": [{"no": "CX 489", "airline": "CPA"}],
        "status": "Est at 14:26",
    },
    {
        "time": "14:30",
        "flight": [
            {"no": "QR 3457", "airline": "QTR"},
            {"no": "CX 505", "airline": "CPA"},
        ],
        "status": "Landed 14:35",
    },
    {
        "time": "16:00",
        "flight": [{"no": "UO 117", "airline": "HKE"}],
        "status": "",
    },
]


def test_match_direct_flight():
    orders = [{"order_id": "O1", "flight_number": "CX489"}]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {"O1": "Est at 14:26"}


def test_match_codeshare():
    orders = [{"order_id": "O2", "flight_number": "CX505"}]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {"O2": "Landed 14:35"}


def test_match_no_status_skipped():
    orders = [{"order_id": "O3", "flight_number": "UO117"}]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {}


def test_match_not_found():
    orders = [{"order_id": "O4", "flight_number": "XX999"}]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {}


def test_match_multiple_orders():
    orders = [
        {"order_id": "O1", "flight_number": "CX489"},
        {"order_id": "O2", "flight_number": "CX505"},
        {"order_id": "O4", "flight_number": "XX999"},
    ]
    result = match_flights(orders, SAMPLE_ARRIVALS)
    assert result == {"O1": "Est at 14:26", "O2": "Landed 14:35"}
