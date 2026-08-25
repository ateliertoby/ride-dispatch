from ride_dispatch.parser import (
    parse_order, parse_feizhu, parse_tongcheng, parse_space, parse_fenxiao
)

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
    assert order.order_id == "VBKTEST00000000000001"
    assert order.service_type == "送机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-06-28 10:30:00"
    assert order.pickup == "8度海逸酒店"
    assert order.dropoff == "香港国际机场 T1"
    assert order.passenger_name == "CHAN,MEI"
    assert order.passenger_phone == "86 13800000004"
    assert order.flight_number == ""
    assert order.additional_services == ""
    assert order.more_contacts == ""
    assert order.raw_message == TONGCHENG_MSG


def test_parse_tongcheng_pickup():
    raw = """订单号：TC12345-同程
车型：经济5座
用车时间：2026-06-28 14:00:00
出发地：香港国际机场 T1
目的地：尖沙咀
航班号：￥ GJ8053
乘客姓名LI,WEI
乘客手机号86-13900001111"""
    order = parse_tongcheng(raw)
    assert order.service_type == "接机"
    assert order.flight_number == "GJ8053"


def test_tongcheng_no_pickup_from_standard():
    order = parse_order(TONGCHENG_MSG)
    assert order.pickup == ""


TONGCHENG_BANNER_MSG = """
            订单号：VBKTEST00000000000002-同程

            车型：舒适5座

            用车时间：2026-08-09 21:45:00
            出发地：香港国际机场 T1
            目的地：尖沙咀彌敦道1號
            航班号：￥ HX9999
乘客姓名WONG,TAIMAN
    乘客手机号852-61234567
同行人电话852-69876543
成人数2    儿童数0
舉牌接機！舉牌接機！舉牌接機！
"""


def test_parse_tongcheng_banner():
    order = parse_tongcheng(TONGCHENG_BANNER_MSG)
    assert order.order_id == "VBKTEST00000000000002"
    assert order.service_type == "接机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-08-09 21:45:00"
    assert order.pickup == "香港国际机场 T1"
    assert order.dropoff == "尖沙咀彌敦道1號"
    assert order.passenger_name == "WONG,TAIMAN"
    assert order.passenger_phone == "852 61234567"
    assert order.flight_number == "HX9999"
    assert order.more_contacts == "【同行人】852-69876543"
    assert order.additional_services == "举牌"


def test_parse_tongcheng_banner_marker_normalized_to_simplified():
    raw = TONGCHENG_BANNER_MSG.replace("舉牌接機", "举牌接机")
    assert parse_tongcheng(raw).additional_services == "举牌"


def test_parse_tongcheng_companion_phone_only():
    raw = TONGCHENG_BANNER_MSG.replace("舉牌接機！舉牌接機！舉牌接機！", "")
    order = parse_tongcheng(raw)
    assert order.more_contacts == "【同行人】852-69876543"
    assert order.additional_services == ""


# Newer 同程 layout: several labelled fields share one line, and the message
# carries 名称/订单里程/行驶时长 that the earlier layout did not.
TONGCHENG_ONE_LINE_MSG = """
            订单号：VBKTEST00000000000003-同程
            名称： 香港机场-九龙启德（接机）
            车型：5座
            用车时间：2026-08-14 10:20:00
            出发地：香港国际机场 T1
            目的地：香港九龙启德酒店
            订单里程：39.257 km
            行驶时长：40 分钟

            航班号：￥ HX9001
          乘客姓名PANG,SIUYIN    乘客英文名    乘客手机号86-13800000008
"""


def test_parse_tongcheng_one_line_fields():
    order = parse_tongcheng(TONGCHENG_ONE_LINE_MSG)
    assert order.order_id == "VBKTEST00000000000003"
    assert order.service_type == "接机"
    assert order.vehicle_type == "5座"
    assert order.scheduled_time == "2026-08-14 10:20:00"
    assert order.pickup == "香港国际机场 T1"
    assert order.dropoff == "香港九龙启德酒店"
    assert order.passenger_name == "PANG,SIUYIN"
    assert order.passenger_phone == "86 13800000008"
    assert order.flight_number == "HX9001"
    assert order.distance_km == 39.257
    assert order.more_contacts == ""
    assert order.raw_message == TONGCHENG_ONE_LINE_MSG


def test_parse_tongcheng_english_name_appended():
    raw = TONGCHENG_ONE_LINE_MSG.replace("乘客英文名    ", "乘客英文名SIU YIN    ")
    order = parse_tongcheng(raw)
    assert order.passenger_name == "PANG,SIUYIN SIU YIN"


def test_parse_tongcheng_unparsable_distance():
    raw = TONGCHENG_ONE_LINE_MSG.replace("39.257 km", "未知")
    assert parse_tongcheng(raw).distance_km is None


# 订单号 suffixes: the message is re-sent with a different one whenever the
# customer changes a detail, and every variant has to resolve to the one id.
def _tc_oid(value: str) -> str:
    raw = TONGCHENG_MSG.replace("VBKTEST00000000000001-同程", value)
    return parse_tongcheng(raw).order_id


def test_parse_tongcheng_order_id_strips_channel_suffix():
    assert _tc_oid("VBKTEST00000000000001-同程") == "VBKTEST00000000000001"


def test_parse_tongcheng_order_id_strips_service_type_suffix():
    assert _tc_oid("VBKTEST00000000000001（接机）") == "VBKTEST00000000000001"


def test_parse_tongcheng_order_id_bare_is_unchanged():
    assert _tc_oid("VBKTEST00000000000001") == "VBKTEST00000000000001"


def test_parse_tongcheng_order_id_empty_without_leading_alnum():
    assert _tc_oid("（接机）") == ""


TONGCHENG_RESENT_MSG = """订单号：VBKSYNTHETIC0000002（接机）
车型：经济5座
用车时间：2026-08-25 17:25:00
出发地：香港国际机场 T1
目的地：新界坑口裕明苑裕昌閣B座
订单里程：49.105 km
行驶时长：44 分钟
航班号：￥ 3U3959
乘客姓名CHAN,TAIMAN"""


def test_parse_tongcheng_resent_message():
    order = parse_tongcheng(TONGCHENG_RESENT_MSG)
    assert order.order_id == "VBKSYNTHETIC0000002"
    assert order.service_type == "接机"
    assert order.dropoff == "新界坑口裕明苑裕昌閣B座"
    assert order.distance_km == 49.105
    assert order.flight_number == "3U3959"


# Same one-line layout, but the airport end is named by terminal only and the
# direction is spelled out in 名称 — the shape that shipped an empty
# service_type to production.
TONGCHENG_TERMINAL_MSG = """订单号：VBKSYNTHETIC0000001

            名称： 香港机场-荃湾屯门（送机）
            车型：经济5座
            用车时间：2026-08-20 18:15:00
            出发地：荃湾帝盛酒店
            目的地：香港 T2
            订单里程：24.626 km
            行驶时长：21 分钟
乘客姓名CHAN,TAIMAN\t乘客英文名\t乘客手机号86-13800000000"""


def test_parse_tongcheng_terminal_only_dropoff():
    order = parse_tongcheng(TONGCHENG_TERMINAL_MSG)
    assert order.service_type == "送机"
    assert order.order_id == "VBKSYNTHETIC0000001"
    assert order.scheduled_time == "2026-08-20 18:15:00"
    assert order.pickup == "荃湾帝盛酒店"
    assert order.dropoff == "香港 T2"
    assert order.passenger_name == "CHAN,TAIMAN"
    assert order.passenger_phone == "86 13800000000"
    assert order.distance_km == 24.626


def test_parse_tongcheng_route_name_never_stored():
    order = parse_tongcheng(TONGCHENG_TERMINAL_MSG)
    stored = [v for k, v in vars(order).items()
              if k != "raw_message" and isinstance(v, str)]
    assert all("荃湾屯门" not in v for v in stored)


def _tc_route(pickup: str, dropoff: str, name: str = "") -> str:
    name_line = f"名称：{name}\n" if name else ""
    return f"""订单号：VBKSYNTHETIC0000002
{name_line}车型：经济5座
用车时间：2026-08-20 09:00:00
出发地：{pickup}
目的地：{dropoff}
乘客姓名CHAN,TAIMAN    乘客手机号86-13800000000"""


def test_parse_tongcheng_terminal_pickup_is_arrival():
    order = parse_tongcheng(_tc_route("T2客运大楼", "荃湾帝盛酒店"))
    assert order.service_type == "接机"


def test_parse_tongcheng_bare_terminal_token():
    assert parse_tongcheng(_tc_route("荃湾帝盛酒店", "T1")).service_type == "送机"
    assert parse_tongcheng(_tc_route("T1", "荃湾帝盛酒店")).service_type == "接机"


def test_parse_tongcheng_terminal_inside_word_is_not_airport():
    for dropoff in ("CAT2 商场", "T25", "T21 号铺", "Hyatt1"):
        assert parse_tongcheng(_tc_route("荃湾帝盛酒店", dropoff)).service_type == ""


def test_parse_tongcheng_route_name_breaks_the_tie():
    raw = _tc_route("荃湾帝盛酒店", "九龙塘又一城", "荃湾屯门-香港机场（送机）")
    assert parse_tongcheng(raw).service_type == "送机"


def test_parse_tongcheng_route_name_traditional_marker():
    raw = _tc_route("荃湾帝盛酒店", "九龙塘又一城", "香港機場-荃灣屯門（接機）")
    assert parse_tongcheng(raw).service_type == "接机"


def test_parse_tongcheng_route_name_both_directions_stays_empty():
    raw = _tc_route("荃湾帝盛酒店", "九龙塘又一城", "接机送机套餐")
    assert parse_tongcheng(raw).service_type == ""


def test_parse_tongcheng_route_name_without_marker_stays_empty():
    raw = _tc_route("荃湾帝盛酒店", "九龙塘又一城", "香港机场-荃湾屯门")
    assert parse_tongcheng(raw).service_type == ""


def test_parse_tongcheng_endpoints_outrank_contradictory_route_name():
    raw = _tc_route("荃湾帝盛酒店", "香港国际机场 T1", "荃湾屯门-香港机场（接机）")
    assert parse_tongcheng(raw).service_type == "送机"


FEIZHU_MSG = """订单编号：5122000000000000001-飛豬
经济5座
【接机】
中国-中国香港
[出发]香港国际机场T1
[抵达]香港城市大学
约38公里
UO725
[预计抵达]
2026-06-29 16:00:00
陈大文
真实号：15000000005"""


def test_parse_feizhu_pickup():
    order = parse_feizhu(FEIZHU_MSG)
    assert order.order_id == "5122000000000000001"
    assert order.service_type == "接机"
    assert order.vehicle_type == "经济5座"
    assert order.pickup == "香港国际机场T1"
    assert order.dropoff == "香港城市大学"
    assert order.distance_km == 38
    assert order.flight_number == "UO725"
    assert order.scheduled_time == "2026-06-29 16:00:00"
    assert order.passenger_name == "陈大文"
    assert order.passenger_phone == "15000000005"


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


# 真实号 with two numbers, plus the passenger-count trailer this variant adds.
FEIZHU_DUAL_PHONE = """订单编号：5122000000000000002-飛豬
经济5座
【接机】
中国-中国香港
[出发]香港国际机场T1
[抵达]尖沙咀酒店
约35公里
MU9001
[预计抵达]
2026-08-14 20:30:00
李小明
真实号：13400001111/19900002222
---1成人1儿童"""


def test_parse_feizhu_dual_phone():
    order = parse_feizhu(FEIZHU_DUAL_PHONE)
    assert order.passenger_phone == "13400001111"
    assert order.more_contacts == "【備用】19900002222"
    assert order.passenger_name == "李小明"
    assert order.flight_number == "MU9001"
    assert order.scheduled_time == "2026-08-14 20:30:00"


def test_parse_feizhu_single_phone_leaves_more_contacts_empty():
    assert parse_feizhu(FEIZHU_MSG).more_contacts == ""


SPACE_MSG = """订单号：SPACE12345678
类型：香港-送机
车型：舒适5座【丰田雷凌等同级车]】
用车日期：2026-07-25
用车时间：中午12:30
上车点：紫珀酒店
下车点：香港国际机场
联系人：王小明
联系电话：13800000007"""


def test_parse_space_dropoff():
    order = parse_space(SPACE_MSG)
    assert order.order_id == "SPACE12345678"
    assert order.service_type == "送机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-07-25 12:30:00"
    assert order.pickup == "紫珀酒店"
    assert order.dropoff == "香港国际机场"
    assert order.passenger_name == "王小明"
    assert order.passenger_phone == "13800000007"
    assert order.raw_message == SPACE_MSG


def test_parse_space_pm_time():
    raw = """订单号：SPACE99999
类型：深圳-接机
车型：经济5座
用车日期：2026-08-01
用车时间：下午3:30
上车点：宝安机场
下车点：南山科技园
联系人：张三
联系电话：13500001111"""
    order = parse_space(raw)
    assert order.scheduled_time == "2026-08-01 15:30:00"


# Distributor-relayed copy of the SPACE format: 姓名/电话 keys, round-paren
# 车型 suffix, distributor name on 订单号. Stray spaces and half-width colons
# are reproduced from a real message — key.strip() has to absorb them.
SPACE_RELAY_MSG = """订单号：3300000000000000001-測試分銷
类型：香港-送机
车型：舒适5座(丰田雷凌等同级车)
用车日期：2026-07-28  
用车时间：17:10
上车点：尖沙咀瑰丽酒店
下车点：香港国际机场
 姓名:李小芳 
电话:13800001234"""


def test_parse_space_relay_variant():
    order = parse_space(SPACE_RELAY_MSG)
    assert order.order_id == "3300000000000000001"
    assert order.service_type == "送机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-07-28 17:10:00"
    assert order.pickup == "尖沙咀瑰丽酒店"
    assert order.dropoff == "香港国际机场"
    assert order.passenger_name == "李小芳"
    assert order.passenger_phone == "13800001234"
    assert order.raw_message == SPACE_RELAY_MSG


# 接机 variant of the SPACE format: 乘车人/联系方式 contact keys, a 航班号, and
# no 用车时间 — only 预计降落时间.
SPACE_LANDING_MSG = """订单号：SPACE202608990001
类型：香港-接机
车型：舒适5座
用车日期：2026-08-21
预计降落时间：11:25
航班号：CX880
上车点：香港国际机场
下车点：香港北角海逸酒店
乘车人：陈大文
联系方式：微信群联系"""


def test_parse_space_landing_variant():
    order = parse_space(SPACE_LANDING_MSG)
    assert order.order_id == "SPACE202608990001"
    assert order.service_type == "接机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-08-21 11:25:00"
    assert order.flight_number == "CX880"
    assert order.pickup == "香港国际机场"
    assert order.dropoff == "香港北角海逸酒店"
    assert order.passenger_name == "陈大文"
    # Non-numeric contact values are kept verbatim rather than dropped.
    assert order.passenger_phone == "微信群联系"
    assert order.raw_message == SPACE_LANDING_MSG


def test_parse_space_scheduled_time_prefers_pickup_time():
    raw = """订单号：SPACE77777
类型：香港-接机
车型：舒适5座
用车日期：2026-08-21
用车时间：13:00
预计降落时间：11:25
航班号：CX880
上车点：香港国际机场
下车点：香港北角海逸酒店
乘车人：陈大文
联系方式：微信群联系"""
    order = parse_space(raw)
    assert order.scheduled_time == "2026-08-21 13:00:00"


def test_parse_space_landing_time_prefix():
    raw = """订单号：SPACE66666
类型：香港-接机
车型：舒适5座
用车日期：2026-08-21
预计降落时间：上午11:25
航班号：CX880
上车点：香港国际机场
下车点：香港北角海逸酒店
乘车人：陈大文
联系方式：微信群联系"""
    order = parse_space(raw)
    assert order.scheduled_time == "2026-08-21 11:25:00"


# Relayed 接机 variant: the pickup, dropoff and landing-time keys are spelled
# out in full (上车地点/下车地点/航班预计降落时间), the contact pair uses 姓名/电话,
# and 乘车人数 carries head count plus luggage as one phrase.
SPACE_FULL_KEY_MSG = """订单号：SPACE202608990002
类型：香港-接机
车型 ：特斯拉5座【特斯拉Model Y/S等同级车】
用车日期：2026-08-22
航班预计降落时间：11:50
航班号： CA103
上车地点：香港国际机场
下车地点：黄埔必嘉坊
乘车人数：一个人两件行李
姓名:陈大文
电话:13800001234"""


def test_parse_space_full_key_variant():
    order = parse_space(SPACE_FULL_KEY_MSG)
    assert order.order_id == "SPACE202608990002"
    assert order.service_type == "接机"
    assert order.vehicle_type == "特斯拉5座"
    assert order.scheduled_time == "2026-08-22 11:50:00"
    assert order.flight_number == "CA103"
    assert order.pickup == "香港国际机场"
    assert order.dropoff == "黄埔必嘉坊"
    # Head count and luggage go to the field the dispatch card renders as 備註.
    assert order.driver_notes == "一个人两件行李"
    # 乘车人数 must not be read as the 乘车人 contact key.
    assert order.passenger_name == "陈大文"
    assert order.passenger_phone == "13800001234"
    assert order.raw_message == SPACE_FULL_KEY_MSG


def test_parse_space_head_count_does_not_become_passenger_name():
    raw = """订单号：SPACE202608990003
类型：香港-接机
车型：特斯拉5座
用车日期：2026-08-22
航班预计降落时间：11:50
上车地点：香港国际机场
下车地点：黄埔必嘉坊
乘车人数：两个人三件行李"""
    order = parse_space(raw)
    assert order.passenger_name == ""
    assert order.driver_notes == "两个人三件行李"


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


def test_parse_jiezhan():
    order = parse_order(JIEZHAN_MSG)
    assert order.service_type == "接站"
    assert order.order_id == "1128000000000003"
    assert order.passenger_name == "陈小明"
    assert order.scheduled_time == "2026-07-25 11:20:00"
    assert order.flight_number == ""
    assert order.pickup == "香港西九龙站(香港西九龙站)"
    assert order.dropoff == "香港紫珀酒店(尖沙咀诺士佛台6号)"
    assert order.distance_km == 3
    assert order.vehicle_type == "特斯拉 Model S"
    assert order.passenger_phone == "86 13800000006"
    assert order.passenger_exit_minutes is None


def test_parse_space_no_prefix_time():
    raw = """订单号：SPACE88888
类型：香港-送机
车型：舒适5座
用车日期：2026-08-02
用车时间：14:00
上车点：铜锣湾酒店
下车点：香港国际机场
联系人：李四
联系电话：13600002222"""
    order = parse_space(raw)
    assert order.scheduled_time == "2026-08-02 14:00:00"


FENXIAO_MSG = """平台订单号：DD26080100TEST0-銀河分銷

出行时间：2026-08-01 11:30
出发地：Disney Explorers Lodge
目的地：Hong Kong International Airport (HKG), Sky Plaza Road 1, Hong Kong, Chek Lap Kok, Hong Kong SAR China
乘客数：2
行李数：3
客人姓名：TANAKA/HANAKO
客人联系方式：+8108012345678
平台备注：Please provide the service according to the scheduled time."""


def test_parse_fenxiao_dropoff():
    order = parse_fenxiao(FENXIAO_MSG)
    assert order.order_id == "DD26080100TEST0"
    assert order.service_type == "送机"
    assert order.scheduled_time == "2026-08-01 11:30:00"
    assert order.pickup == "Disney Explorers Lodge"
    assert order.dropoff.startswith("Hong Kong International Airport (HKG)")
    assert order.passenger_name == "TANAKA/HANAKO"
    assert order.passenger_phone == "+8108012345678"
    assert order.notes == "Please provide the service according to the scheduled time."
    assert order.driver_notes == ""
    assert order.vehicle_type == ""
    assert order.flight_number == ""
    assert order.raw_message == FENXIAO_MSG


def test_parse_fenxiao_pickup():
    raw = """平台订单号：DD26080200TEST0-銀河分銷
出行时间：2026-08-02 09:05
出发地：Hong Kong International Airport (HKG), Chek Lap Kok
目的地：Disney Explorers Lodge
客人姓名：TANAKA/TARO
客人联系方式：+8108012345679"""
    order = parse_fenxiao(raw)
    assert order.service_type == "接机"
    assert order.scheduled_time == "2026-08-02 09:05:00"


def test_parse_fenxiao_pads_single_digit_hour():
    raw = """平台订单号：DD26080300TEST0-銀河分銷
出行时间：2026-08-03 7:05
出发地：Disney Explorers Lodge
目的地：Hong Kong International Airport (HKG)"""
    order = parse_fenxiao(raw)
    assert order.scheduled_time == "2026-08-03 07:05:00"


def test_parse_fenxiao_keeps_existing_seconds():
    raw = """平台订单号：DD26080400TEST0-銀河分銷
出行时间：2026-08-04 18:45:30
出发地：Disney Explorers Lodge
目的地：Hong Kong International Airport (HKG)"""
    order = parse_fenxiao(raw)
    assert order.scheduled_time == "2026-08-04 18:45:30"


def test_parse_fenxiao_terminal_only_dropoff():
    raw = """平台订单号：DD26082000TEST0-銀河分銷
出行时间：2026-08-20 18:15
出发地：Disney Explorers Lodge
目的地：Hong Kong T2
客人姓名：TANAKA/HANAKO
客人联系方式：+8108012345678"""
    assert parse_fenxiao(raw).service_type == "送机"


def test_parse_fenxiao_no_order_id():
    raw = """出行时间：2026-08-01 11:30
出发地：Disney Explorers Lodge
目的地：Hong Kong International Airport (HKG)
客人姓名：TANAKA/HANAKO"""
    order = parse_fenxiao(raw)
    assert order.order_id == ""
    assert order.pickup == ""
    assert order.scheduled_time == ""
    assert order.raw_message == raw
