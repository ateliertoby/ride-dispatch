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
    assert order.additional_services == ""
    assert order.more_contacts == ""


# The same booking re-sent after the customer changed the destination: the
# 订单号 suffix names the service type instead of the channel.
TONGCHENG_RESENT_MSG = """订单号：TC9876543（接机）
车型：经济5座
用车时间：2026-08-25 17:25:00
出发地：香港国际机场 T1
目的地：新界坑口裕明苑裕昌閣B座
订单里程：49.105 km
行驶时长：44 分钟
航班号：￥ 3U3959
乘客姓名CHAN,TAIMAN"""


def test_parse_any_tongcheng_resent_message():
    order, source = parse_any(TONGCHENG_RESENT_MSG)
    assert source == "同程"
    assert order.order_id == "TC9876543"
    assert order.service_type == "接机"
    assert order.dropoff == "新界坑口裕明苑裕昌閣B座"
    assert order.distance_km == 49.105


TONGCHENG_BANNER_MSG = """订单号：TC1234567-同程用车
车型：舒适5座
用车时间：2026-08-09 21:45:00
出发地：香港国际机场T1
目的地：尖沙咀彌敦道1號
航班号：￥ HX9999
乘客姓名WONG,TAIMAN
乘客手机号852-61234567
同行人电话852-69876543
舉牌接機！舉牌接機！舉牌接機！"""


def test_parse_any_tongcheng_banner():
    order, source = parse_any(TONGCHENG_BANNER_MSG)
    assert source == "同程"
    assert order.order_id == "TC1234567"
    assert order.service_type == "接机"
    assert order.additional_services == "举牌"
    assert order.more_contacts == "【同行人】852-69876543"
    assert banner_fee(order.additional_services) == 40.0
    assert parking_fee(order, source) == 32.0


def test_parse_any_garbage_returns_empty_id():
    order, source = parse_any("hello world\n唔係訂單")
    assert order.order_id == ""


XIECHENG_DROPOFF_MSG = """服务类型: 送机
接单车型: 经济5座
乘客姓名: WONG/SIUMING
用车时间: 2026-07-22 08:10:00
航班号: CX477
上车点: 九龙塘又一城
下车点: 香港国际机场1号航站楼
订单号: 1128000000000100
附加服务:
乘客电话: 86 13800000003"""


def test_parking_fee_xiecheng_pickup_only():
    order, source = parse_any(XIECHENG_MSG)
    assert parking_fee(order, source) == 32.0
    dropoff, dsource = parse_any(XIECHENG_DROPOFF_MSG)
    assert dropoff.service_type == "送机"
    assert parking_fee(dropoff, dsource) == 0.0
    tc, tcsource = parse_any(TONGCHENG_MSG)
    assert parking_fee(tc, tcsource) == 0.0


def test_parking_fee_banner_enters_car_park():
    order, source = parse_any(TONGCHENG_BANNER_MSG)
    assert parking_fee(order, source) == 32.0
    # Same order minus the 舉牌 line: no terminal meet, so no car park.
    plain, plain_source = parse_any(TONGCHENG_BANNER_MSG.rsplit("\n", 1)[0])
    assert plain.service_type == "接机"
    assert plain.additional_services == ""
    assert parking_fee(plain, plain_source) == 0.0


def test_banner_fee():
    assert banner_fee("举牌服务") == 40.0
    assert banner_fee("举牌") == 40.0
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
    # No 订单号 suffix means no relaying distributor, so the order is the
    # platform's own — which its order number already says.
    assert source == "SPACE"
    assert order.order_id == "SPACE12345678"
    assert order.service_type == "送机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-07-25 12:30:00"
    assert order.passenger_name == "王小明"


SPACE_RELAY_MSG = """订单号：3300000000000000001-測試分銷
类型：香港-送机
车型：舒适5座(丰田雷凌等同级车)
用车日期：2026-07-28  
用车时间：17:10
上车点：尖沙咀瑰丽酒店
下车点：香港国际机场
 姓名:李小芳 
电话:13800001234"""


def test_parse_any_space_relay_source_from_suffix():
    order, source = parse_any(SPACE_RELAY_MSG)
    assert source == "測試分銷"
    assert order.order_id == "3300000000000000001"
    assert order.service_type == "送机"
    assert order.vehicle_type == "舒适5座"
    assert order.scheduled_time == "2026-07-28 17:10:00"
    assert order.passenger_name == "李小芳"
    assert order.passenger_phone == "13800001234"


SPACE_NO_SUFFIX_MSG = """订单号：SPACE202608231226
类型：香港-接机
车型：舒适5座【丰田雷凌等同级车】
用车日期：2026-08-23
航班预计降落时间：12:26
航班号：GJ8007
上车地点：香港国际机场
下车地点：尖沙咀
姓名:陈小明
电话:13800005678"""


def test_parse_any_space_no_suffix_source_is_space():
    """订单号 prefix is the channel id; only a distributor suffix overrides it."""
    _, source = parse_any(SPACE_NO_SUFFIX_MSG)
    assert source == "SPACE"


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


def test_parse_any_space_full_key_variant_has_pickup_time():
    order, source = parse_any(SPACE_FULL_KEY_MSG)
    assert source == "SPACE"
    assert order.order_id == "SPACE202608990002"
    # Web paste rejects a scheduled_time with no time part, and every consumer
    # reads it as '%Y-%m-%d %H:%M:%S'.
    assert " " in order.scheduled_time
    assert order.scheduled_time == "2026-08-22 11:50:00"
    assert order.pickup == "香港国际机场"
    assert order.dropoff == "黄埔必嘉坊"
    assert order.driver_notes == "一个人两件行李"


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
