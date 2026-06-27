from ride_dispatch.parser import parse_order, parse_tongcheng

DROPOFF_MSG = """服务类型: 送机
接单车型: 特斯拉 Model S
乘客姓名: CHAN/TAIMAN(重要贵宾)
用车时间: 2026-06-27 10:30:00
乘客境外电话:
航班号: QW916
上车点: 香港数码港艾美酒店(南区/数码港 数码港道100号)
下车点: 香港国际机场 T1(香港国际机场 T1)
订单备注:
附加服务:
订单号: 1128000000000001
司机可见备注:
乘客出场时长:
第三方联系方式:
订单里程: 40
更多联系方式:
乘客电话: 86 13800000003"""

PICKUP_MSG = """服务类型: 接机
接单车型: 经济5座
乘客姓名: WONG/SIUMING
用车时间: 2026-06-27 12:35:00
乘客境外电话: 886 912345678
航班号: CX477
上车点: 香港国际机场1号航站楼(香港国际机场1号航站楼)
下车点: Dorsett Kai Tak, Hong Kong(香港九龍城承啟道43號)
订单备注:
附加服务:
订单号: 1128000000000002
司机可见备注: 請司機務必加客人WhatsApp,帳號如下: 886912345678
乘客出场时长: 30
第三方联系方式: 【WhatsApp】 886912345678
订单里程: 36
更多联系方式:
乘客电话:  """


def test_parse_dropoff():
    order = parse_order(DROPOFF_MSG)
    assert order.service_type == "送机"
    assert order.order_id == "1128000000000001"
    assert order.passenger_name == "CHAN/TAIMAN(重要贵宾)"
    assert order.scheduled_time == "2026-06-27 10:30:00"
    assert order.flight_number == "QW916"
    assert order.pickup == "香港数码港艾美酒店(南区/数码港 数码港道100号)"
    assert order.dropoff == "香港国际机场 T1(香港国际机场 T1)"
    assert order.distance_km == 40
    assert order.passenger_phone == "86 13800000003"
    assert order.vehicle_type == "特斯拉 Model S"


def test_parse_pickup():
    order = parse_order(PICKUP_MSG)
    assert order.service_type == "接机"
    assert order.order_id == "1128000000000002"
    assert order.passenger_name == "WONG/SIUMING"
    assert order.flight_number == "CX477"
    assert order.overseas_phone == "886 912345678"
    assert order.passenger_exit_minutes == 30
    assert order.distance_km == 36
    assert order.driver_notes == "請司機務必加客人WhatsApp,帳號如下: 886912345678"


def test_parse_empty_fields():
    order = parse_order(DROPOFF_MSG)
    assert order.overseas_phone == ""
    assert order.notes == ""
    assert order.driver_notes == ""
    assert order.passenger_exit_minutes is None
    assert order.third_party_contact == ""


def test_raw_message_preserved():
    order = parse_order(DROPOFF_MSG)
    assert order.raw_message == DROPOFF_MSG


TONGCHENG_MSG = """
            订单号：VBKTEST00000000000001-同程

            车型：舒适5座

            用车时间：2026-06-28 10:30:00
            出发地：8度海逸酒店
            目的地：香港国际机场 T1
乘客姓名CHAN,MEI
    乘客手机号86-13800000004
成人数2    儿童数0
"""


def test_parse_tongcheng_dropoff():
    order = parse_tongcheng(TONGCHENG_MSG)
    assert order.order_id == "VBKTEST00000000000001-同程"
    assert order.service_type == "送机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-06-28 10:30:00"
    assert order.pickup == "8度海逸酒店"
    assert order.dropoff == "香港国际机场 T1"
    assert order.passenger_name == "CHAN,MEI"
    assert order.passenger_phone == "86 13800000004"
    assert order.flight_number == ""
    assert order.raw_message == TONGCHENG_MSG


def test_parse_tongcheng_pickup():
    raw = """订单号：TC12345-同程
车型：经济5座
用车时间：2026-06-28 14:00:00
出发地：香港国际机场 T1
目的地：尖沙咀
乘客姓名LI,WEI
乘客手机号86-13900001111"""
    order = parse_tongcheng(raw)
    assert order.service_type == "接机"


def test_tongcheng_no_pickup_from_standard():
    order = parse_order(TONGCHENG_MSG)
    assert order.pickup == ""
