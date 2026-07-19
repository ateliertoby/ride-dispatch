import os
import tempfile
import pytest
from ride_dispatch.parser import Order
from ride_dispatch.db import init_db, save_order, update_price, cancel_order
from ride_dispatch.pricing import match_zone, suggest_price
import ride_dispatch.web as web


def _make_order(order_id, service_type, pickup, dropoff):
    return Order(
        order_id=order_id,
        service_type=service_type,
        vehicle_type="经济5座",
        passenger_name="TEST",
        scheduled_time="2026-07-01 10:00:00",
        passenger_phone="",
        overseas_phone="",
        flight_number="",
        pickup=pickup,
        dropoff=dropoff,
        distance_km=None,
        notes="",
        driver_notes="",
        additional_services="",
        passenger_exit_minutes=None,
        third_party_contact="",
        more_contacts="",
        raw_message="",
    )


def _seed(db_path, order_id, service_type, pickup, dropoff, price, cancelled=False):
    order = _make_order(order_id, service_type, pickup, dropoff)
    save_order(db_path, order, telegram_msg_id=None)
    update_price(db_path, order_id, price)
    if cancelled:
        cancel_order(db_path, order_id)


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.unlink(path)


# ---- match_zone ----

class TestMatchZone:
    def test_kowloon_simplified(self):
        assert match_zone("香港尖沙咀凯悦酒店(河内道18 号)") == "kowloon"

    def test_kowloon_traditional(self):
        assert match_zone("承啟道33號") == "kowloon"

    def test_hk_island_north(self):
        assert match_zone("香港君悦酒店(港湾道1号)") == "hk_island_north"

    def test_shatin_traditional(self):
        assert match_zone("香港帝都酒店(白鶴汀街8号)") == "shatin"

    def test_southern(self):
        assert match_zone("香港仔海洋徑") == "southern"

    def test_disney(self):
        assert match_zone("迪士尼乐园酒店") == "disney"

    def test_tsuen_wan(self):
        assert match_zone("荃湾西站") == "tsuen_wan_tsing_yi"

    def test_same_zone_multi_keyword(self):
        # Both keywords in hk_island_north -> single zone, valid
        assert match_zone("金钟太古广场") == "hk_island_north"

    def test_multi_zone_null(self):
        assert match_zone("尖沙咀到铜锣湾") is None

    def test_venue_alias_no_district_in_string(self):
        # Well-known venues whose platform address strings omit the district
        assert match_zone("香港喜来登酒店(Sheraton Hong Kong Hotel & Towers)") == "kowloon"
        assert match_zone("香港万丽海景酒店") == "hk_island_north"
        assert match_zone("香港帝都酒店(香港帝都酒店)") == "shatin"

    def test_english_district_token(self):
        assert match_zone("The Mira Hong Kong(118 Nathan Road, Tsim Sha Tsui)") == "kowloon"

    def test_bare_kowloon_hotel_name(self):
        # Some platform strings carry only the hotel name, no district token
        assert match_zone("香港九龙海逸君绰酒店(Harbour Grand Kowloon)") == "kowloon"

    def test_unknown_area_null(self):
        assert match_zone("东涌") is None

    def test_empty_null(self):
        assert match_zone("") is None

    def test_no_keywords_null(self):
        assert match_zone("某个酒店") is None


# ---- suggest_price ----

class TestSuggestPrice:
    def test_kowloon_pickup_mode(self, db_path):
        # 3x 210, 1x 220 -> mode is 210
        _seed(db_path, "K1", "接机", "香港国际机场", "香港尖沙咀凯悦酒店(河内道18 号)", 210)
        _seed(db_path, "K2", "接机", "香港国际机场", "九龙旺角朗豪坊", 210)
        _seed(db_path, "K3", "接机", "香港国际机场", "红磡黄埔花园", 210)
        _seed(db_path, "K4", "接机", "香港国际机场", "太子道西200号", 220)
        query = _make_order("NEW", "接机", "香港国际机场", "九龙塘又一城")
        assert suggest_price(db_path, query) == 210.0

    def test_direction_asymmetry(self, db_path):
        _seed(db_path, "HI1", "接机", "香港国际机场", "香港君悦酒店(港湾道1号)", 300)
        _seed(db_path, "HI2", "接机", "香港国际机场", "铜锣湾时代广场", 300)
        _seed(db_path, "HI3", "送机", "中环四季酒店", "香港国际机场", 280)
        _seed(db_path, "HI4", "送机", "湾仔会展中心", "香港国际机场", 280)

        pickup_q = _make_order("QP", "接机", "香港国际机场", "北角城市花园")
        assert suggest_price(db_path, pickup_q) == 300.0

        dropoff_q = _make_order("QD", "送机", "金钟太古广场", "香港国际机场")
        assert suggest_price(db_path, dropoff_q) == 280.0

    def test_tie_lower_wins(self, db_path):
        _seed(db_path, "T1", "送机", "荃灣西站", "香港国际机场", 200)
        _seed(db_path, "T2", "送机", "青衣城", "香港国际机场", 200)
        _seed(db_path, "T3", "送机", "荃湾广场", "香港国际机场", 210)
        _seed(db_path, "T4", "送机", "青衣站", "香港国际机场", 210)
        query = _make_order("TQ", "送机", "荃灣某酒店", "香港国际机场")
        assert suggest_price(db_path, query) == 200.0

    def test_ambiguous_multi_zone_null(self, db_path):
        _seed(db_path, "A1", "接机", "香港国际机场", "尖沙咀凯悦", 210)
        query = _make_order("AQ", "接机", "香港国际机场", "尖沙咀到铜锣湾")
        assert suggest_price(db_path, query) is None

    def test_unknown_area_null(self, db_path):
        query = _make_order("UQ", "接机", "香港国际机场", "东涌某酒店")
        assert suggest_price(db_path, query) is None

    def test_danching_jiecheng_null(self, db_path):
        _seed(db_path, "D1", "接机", "香港国际机场", "尖沙咀凯悦", 210)
        query = _make_order("DQ", "单程接送", "尖沙咀凯悦", "大屿山")
        assert suggest_price(db_path, query) is None

    def test_thin_direction_falls_back_to_zone_pool(self, db_path):
        # One condition-adjusted pickup must not become the pickup base;
        # with <2 same-direction samples the zone-wide pool decides.
        _seed(db_path, "S1", "送机", "香港帝都酒店(白鹤汀街8号)", "香港国际机场", 240)
        _seed(db_path, "S2", "送机", "香港帝都酒店(香港帝都酒店)", "香港国际机场", 240)
        _seed(db_path, "S3", "接机", "香港国际机场", "香港沙田凯悦酒店(沙田 泽祥街18号)", 290)
        query = _make_order("SQ", "接机", "香港国际机场", "沙田新城市广场")
        assert suggest_price(db_path, query) == 240.0

    def test_zone_pool_serves_empty_direction(self, db_path):
        _seed(db_path, "D1", "接机", "香港国际机场", "迪士尼乐园酒店", 170)
        _seed(db_path, "D2", "接机", "香港国际机场", "迪士尼探索家度假酒店", 170)
        query = _make_order("DQ2", "送机", "香港迪士尼乐园度假区", "香港国际机场")
        assert suggest_price(db_path, query) == 170.0

    def test_cancelled_prices_count(self, db_path):
        # 2 cancelled at 290, 1 active at 240
        # With cancelled: mode = 290 (2 vs 1); without: 240 only
        _seed(db_path, "C1", "接机", "香港国际机场", "香港帝都酒店(白鹤汀街8号)", 290, cancelled=True)
        _seed(db_path, "C2", "接机", "香港国际机场", "大围名城", 290, cancelled=True)
        _seed(db_path, "C3", "接机", "香港国际机场", "火炭某酒店", 240)
        query = _make_order("CQ", "接机", "香港国际机场", "沙田新城市广场")
        assert suggest_price(db_path, query) == 290.0

    def test_traditional_keyword_hit(self, db_path):
        # Seed with traditional, query with simplified
        _seed(db_path, "TR1", "接机", "香港国际机场", "承啟道33號", 210)
        _seed(db_path, "TR2", "接机", "香港国际机场", "紅磡黃埔花園", 210)
        query = _make_order("TRQ", "接机", "香港国际机场", "九龙城启德邮轮码头")
        assert suggest_price(db_path, query) == 210.0

    def test_empty_endpoint_null(self, db_path):
        query = _make_order("EQ", "接机", "香港国际机场", "")
        assert suggest_price(db_path, query) is None

    def test_no_history_null(self, db_path):
        query = _make_order("NQ", "接机", "香港国际机场", "尖沙咀凯悦")
        assert suggest_price(db_path, query) is None


# ---- web integration ----

PASTE_MSG = """服务类型: 接机
接单车型: 经济5座
乘客姓名: WONG/SIUMING
用车时间: 2026-07-22 12:35:00
航班号: CX477
上车点: 香港国际机场1号航站楼
下车点: 九龙塘又一城
订单号: 1128000000000099
附加服务: 举牌服务
乘客出场时长: 30
乘客电话: 86 13800000003"""


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    monkeypatch.setattr(web, "DB_PATH", path)
    web.app.config["TESTING"] = True
    with web.app.test_client() as c:
        yield c
    os.unlink(path)


def test_parse_returns_suggested_price(client):
    _seed(web.DB_PATH, "H1", "接机", "香港国际机场", "尖沙咀凯悦酒店", 210)
    _seed(web.DB_PATH, "H2", "接机", "香港国际机场", "旺角朗豪坊", 210)
    _seed(web.DB_PATH, "H3", "接机", "香港国际机场", "红磡海逸酒店", 220)
    # Dropoff 九龙塘又一城 -> kowloon, mode 210
    res = client.post("/api/orders/parse", json={"text": PASTE_MSG})
    assert res.status_code == 200
    assert res.get_json()["suggested_price"] == 210.0


def test_parse_suggested_price_null_no_history(client):
    res = client.post("/api/orders/parse", json={"text": PASTE_MSG})
    assert res.status_code == 200
    assert res.get_json()["suggested_price"] is None
