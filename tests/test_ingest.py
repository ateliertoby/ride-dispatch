from ride_dispatch.ingest import parse_any, parking_fee, banner_fee

XIECHENG_MSG = """服务类型: 接机
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

FEIZHU_MSG = """订单编号：FZ12345678-飞猪
经济型5座
【接机】
总里程约36.5公里
CX888
[预计抵达] 18:30
[出发] 香港国际机场T1
[抵达] 尖沙咀九龙酒店
2026-07-20 18:00:00
CHAN/TAIMAN
真实号：852 61111111"""

TONGCHENG_MSG = """订单号：TC9876543-同程用车
车型：舒适5座
用车时间：2026-07-21 09:00:00
出发地：尖沙咀九龙酒店
目的地：香港国际机场T1
乘客姓名CHAN TAI MAN
乘客手机号852-62222222
航班号：UO123"""


def test_parse_any_xiecheng():
    order, source = parse_any(XIECHENG_MSG)
    assert source == "携程"
    assert order.order_id == "1128000000000099"
    assert order.service_type == "接机"
    assert order.scheduled_time == "2026-07-22 12:35:00"


def test_parse_any_feizhu_source_from_suffix():
    order, source = parse_any(FEIZHU_MSG)
    assert source == "飞猪"
    assert order.order_id == "FZ12345678"
    assert order.flight_number == "CX888"
    assert order.passenger_name == "CHAN/TAIMAN"


def test_parse_any_tongcheng():
    order, source = parse_any(TONGCHENG_MSG)
    assert source == "同程"
    assert order.order_id == "TC9876543"
    assert order.service_type == "送机"
    assert order.passenger_phone == "852 62222222"


def test_parse_any_garbage_returns_empty_id():
    order, source = parse_any("hello world\n唔係訂單")
    assert order.order_id == ""


def test_parking_fee_xiecheng_pickup_only():
    order, source = parse_any(XIECHENG_MSG)
    assert parking_fee(order, source) == 32.0
    assert parking_fee(order, "同程") == 0.0
    dropoff, src2 = parse_any(TONGCHENG_MSG)
    assert parking_fee(dropoff, src2) == 0.0


def test_banner_fee():
    assert banner_fee("举牌服务") == 40.0
    assert banner_fee("") == 0.0
    assert banner_fee(None) == 0.0
