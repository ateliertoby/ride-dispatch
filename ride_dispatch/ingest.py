"""Shared order ingestion: parser cascade + fee rules.

Single source of truth for bot (Telegram) and web (paste) entry points.
"""
from .parser import Order, parse_order, parse_feizhu, parse_tongcheng, parse_space, parse_fenxiao
from .service import is_flight_pickup


def parse_any(text: str) -> tuple[Order, str]:
    """Try SPACE → 分銷 → 携程 → 飛豬 → 同程. Caller checks order.order_id for success."""
    # SPACE format — detect by split date field (携程 would false-positive on shared keys)
    if "用车日期" in text:
        order = parse_space(text)
        if order.order_id:
            # Several channels relay this format, so the format itself no
            # longer identifies one. The 订单号 tells them apart: its SPACE
            # prefix marks the platform's own orders, and a "-" suffix names
            # the distributor relaying one through this channel.
            source = "SPACE"
            for line in text.strip().splitlines():
                line_s = line.strip()
                if line_s.startswith("订单号") and ("：" in line_s or ":" in line_s):
                    sep = "：" if "：" in line_s else ":"
                    oid_full = line_s.partition(sep)[2].strip()
                    if "-" in oid_full:
                        source = oid_full.split("-", 1)[1]
                    break
            return order, source

    # 分銷 format — 平台订单号 is unique to it; the distributor name rides
    # along as the order id suffix, so each distributor gets its own source.
    if "平台订单号" in text:
        order = parse_fenxiao(text)
        if order.order_id:
            source = "分銷"
            for line in text.strip().splitlines():
                line_s = line.strip()
                if line_s.startswith("平台订单号") and ("：" in line_s or ":" in line_s):
                    sep = "：" if "：" in line_s else ":"
                    oid_full = line_s.partition(sep)[2].strip()
                    if "-" in oid_full:
                        source = oid_full.split("-", 1)[1]
                    break
            return order, source

    order = parse_order(text)
    source = "携程"
    if not (order.order_id and order.pickup):
        order = parse_feizhu(text)
        source = "飛豬"
        for line in text.strip().splitlines():
            line_s = line.strip()
            if line_s.startswith("订单编号") and ("：" in line_s or ":" in line_s):
                sep = "：" if "：" in line_s else ":"
                oid_full = line_s.partition(sep)[2].strip()
                if "-" in oid_full:
                    source = oid_full.split("-", 1)[1]
                break
    if not order.order_id:
        order = parse_tongcheng(text)
        source = "同程"
    return order, source


# Where a flight pickup meets the passenger, and what waiting there costs for
# the first hour. 富豪 is the Regal Airport Hotel: outside HKIA's car parks, so
# it is free and never appears as a car park visit. A stay past the first hour
# is priced by HKIA and written back when the visit closes.
PICKUP_POINTS = {"P1": 35.0, "P4": 32.0, "富豪": 0.0}
DEFAULT_CAR_PARK = "P4"


def _must_park(order: Order, source: str) -> bool:
    # 举牌 means meeting the passenger inside the terminal, so the driver enters
    # the car park whatever the channel. Pickups from 携程 always park too.
    return (
        (source == "携程" and order.service_type == "接机")
        or "举牌" in (order.additional_services or "")
    )


def pickup_point(order: Order, source: str) -> str | None:
    """The planned meeting point of a flight pickup; None for any other order."""
    if not is_flight_pickup(order.service_type):
        return None
    return DEFAULT_CAR_PARK if _must_park(order, source) else "富豪"


def parking_fee(order: Order, source: str) -> float:
    return PICKUP_POINTS[DEFAULT_CAR_PARK] if _must_park(order, source) else 0.0


def banner_fee(additional_services: str | None) -> float:
    return 40.0 if "举牌" in (additional_services or "") else 0.0
