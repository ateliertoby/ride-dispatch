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


SPACE_MSG = """订单号：SPACE12345678
类型：香港-送机
车型：舒适5座【丰田雷凌等同级车]】
用车日期：2026-07-25
用车时间：中午12:30
上车点：紫珀酒店
下车点：香港国际机场
联系人：王小明
联系电话：13800000007"""


def test_parse_any_space():
    order, source = parse_any(SPACE_MSG)
    assert source == "SPACE"
    assert order.order_id == "SPACE12345678"
    assert order.service_type == "送机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-07-25 12:30:00"
    assert order.passenger_name == "王小明"


def test_space_does_not_match_xiecheng():
    order, source = parse_any(XIECHENG_MSG)
    assert source == "携程"
    assert order.order_id == "1128000000000099"


JIEZHAN_MSG = """服务类型: 接站
接单车型: 特斯拉 Model S
乘客姓名: 陈小明
用车时间: 2026-07-25 11:20:00
乘客境外电话:
航班号:
上车点: 香港西九龙站(香港西九龙站)
下车点: 香港紫珀酒店(尖沙咀诺士佛台6号)
订单备注:
附加服务:
订单号: 1128000000000003
司机可见备注:
乘客出场时长:
第三方联系方式:
订单里程: 3
更多联系方式:
乘客电话: 86 13800000006"""


def test_parse_any_jiezhan():
    order, source = parse_any(JIEZHAN_MSG)
    assert source == "携程"
    assert order.service_type == "接站"
    assert order.order_id == "1128000000000003"
    assert order.distance_km == 3


def test_jiezhan_parking_fee_zero():
    order, source = parse_any(JIEZHAN_MSG)
    assert parking_fee(order, source) == 0.0


FENXIAO_MSG = """平台订单号：DD26080100TEST0-銀河分銷

出行时间：2026-08-01 11:30
出发地：Disney Explorers Lodge
目的地：Hong Kong International Airport (HKG), Sky Plaza Road 1, Hong Kong, Chek Lap Kok, Hong Kong SAR China
乘客数：2
行李数：3
客人姓名：TANAKA/HANAKO
客人联系方式：+8108012345678
平台备注：Please provide the service according to the scheduled time."""


def test_parse_any_fenxiao_source_from_suffix():
    order, source = parse_any(FENXIAO_MSG)
    assert source == "銀河分銷"
    assert order.order_id == "DD26080100TEST0"
    assert order.service_type == "送机"
    assert order.scheduled_time == "2026-08-01 11:30:00"
    assert order.passenger_name == "TANAKA/HANAKO"


def test_parse_any_fenxiao_source_fallback():
    raw = """平台订单号：DD26080500TEST0
出行时间：2026-08-05 11:30
出发地：Disney Explorers Lodge
目的地：Hong Kong International Airport (HKG)
客人姓名：TANAKA/HANAKO
客人联系方式：+8108012345678"""
    order, source = parse_any(raw)
    assert source == "分銷"
    assert order.order_id == "DD26080500TEST0"


def test_fenxiao_no_parking_fee():
    order, source = parse_any(FENXIAO_MSG)
    assert parking_fee(order, source) == 0.0
