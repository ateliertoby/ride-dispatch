from ride_dispatch.parser import parse_order, parse_feizhu, parse_tongcheng

DROPOFF_MSG = """服务类型: 送机
接单车型: 特斯拉 Model S
乘客姓名: ZHANG/XUAN(重要贵宾)
用车时间: 2026-06-27 10:30:00
乘客境外电话:
航班号: QW916
上车点: 香港数码港艾美酒店(南区/数码港 数码港道100号)
下车点: 香港国际机场 T1(香港国际机场 T1)
订单备注:
附加服务:
订单号: 1128148899006317
司机可见备注:
乘客出场时长:
第三方联系方式:
订单里程: 40
更多联系方式:
乘客电话: 86 13809802194"""

PICKUP_MSG = """服务类型: 接机
接单车型: 经济5座
乘客姓名: HSU/ICHIEH
用车时间: 2026-06-27 12:35:00
乘客境外电话: 886 919456025
航班号: CX477
上车点: 香港国际机场1号航站楼(香港国际机场1号航站楼)
下车点: Dorsett Kai Tak, Hong Kong(香港九龍城承啟道43號)
订单备注:
附加服务:
订单号: 1128148173253603
司机可见备注: 請司機務必加客人WhatsApp,帳號如下: 886919456025
乘客出场时长: 30
第三方联系方式: 【WhatsApp】 886919456025
订单里程: 36
更多联系方式:
乘客电话:  """


def test_parse_dropoff():
    order = parse_order(DROPOFF_MSG)
    assert order.service_type == "送机"
    assert order.order_id == "1128148899006317"
    assert order.passenger_name == "ZHANG/XUAN(重要贵宾)"
    assert order.scheduled_time == "2026-06-27 10:30:00"
    assert order.flight_number == "QW916"
    assert order.pickup == "香港数码港艾美酒店(南区/数码港 数码港道100号)"
    assert order.dropoff == "香港国际机场 T1(香港国际机场 T1)"
    assert order.distance_km == 40
    assert order.passenger_phone == "86 13809802194"
    assert order.vehicle_type == "特斯拉 Model S"


def test_parse_pickup():
    order = parse_order(PICKUP_MSG)
    assert order.service_type == "接机"
    assert order.order_id == "1128148173253603"
    assert order.passenger_name == "HSU/ICHIEH"
    assert order.flight_number == "CX477"
    assert order.overseas_phone == "886 919456025"
    assert order.passenger_exit_minutes == 30
    assert order.distance_km == 36
    assert order.driver_notes == "請司機務必加客人WhatsApp,帳號如下: 886919456025"


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
            订单号：VBK6A3F92D051362A5675-同程

            车型：舒适5座

            用车时间：2026-06-28 10:30:00
            出发地：8度海逸酒店
            目的地：香港国际机场 T1
乘客姓名ZHANG,YAN
    乘客手机号86-13758170978
成人数2    儿童数0
"""


def test_parse_tongcheng_dropoff():
    order = parse_tongcheng(TONGCHENG_MSG)
    assert order.order_id == "VBK6A3F92D051362A5675"
    assert order.service_type == "送机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-06-28 10:30:00"
    assert order.pickup == "8度海逸酒店"
    assert order.dropoff == "香港国际机场 T1"
    assert order.passenger_name == "ZHANG,YAN"
    assert order.passenger_phone == "86 13758170978"
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


FEIZHU_MSG = """订单编号：5122215325850009239-飛豬
经济5座
【接机】
中国-中国香港
[出发]香港国际机场T1
[抵达]香港城市大学
约38公里
UO725
[预计抵达]
2026-06-29 16:00:00
杨文振
真实号：15010135716"""


def test_parse_feizhu_pickup():
    order = parse_feizhu(FEIZHU_MSG)
    assert order.order_id == "5122215325850009239"
    assert order.service_type == "接机"
    assert order.vehicle_type == "经济5座"
    assert order.pickup == "香港国际机场T1"
    assert order.dropoff == "香港城市大学"
    assert order.distance_km == 38
    assert order.flight_number == "UO725"
    assert order.scheduled_time == "2026-06-29 16:00:00"
    assert order.passenger_name == "杨文振"
    assert order.passenger_phone == "15010135716"


FEIZHU_NO_FLIGHT = """订单编号：9999999999-飛豬
经济5座
【送机】
中国-中国香港
[出发]尖沙咀酒店
[抵达]香港国际机场T1
约30公里
[预计抵达]
2026-06-29 10:00:00
李明
真实号：13800001111"""


def test_parse_feizhu_no_flight():
    order = parse_feizhu(FEIZHU_NO_FLIGHT)
    assert order.order_id == "9999999999"
    assert order.service_type == "送机"
    assert order.flight_number == ""
    assert order.passenger_name == "李明"
