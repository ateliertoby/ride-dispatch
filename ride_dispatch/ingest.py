"""Shared order ingestion: parser cascade + fee rules.

Single source of truth for bot (Telegram) and web (paste) entry points.
"""
from .parser import Order, parse_order, parse_feizhu, parse_tongcheng, parse_space, parse_fenxiao


def parse_any(text: str) -> tuple[Order, str]:
    """Try SPACE → 分銷 → 携程 → 飛豬 → 同程. Caller checks order.order_id for success."""
    # SPACE format — detect by split date field (携程 would false-positive on shared keys)
    if "用车日期" in text:
        order = parse_space(text)
        if order.order_id:
            # Several channels relay this format, so it no longer identifies
            # one. Only the 订单号 suffix names the relaying distributor;
            # without it the source stays empty rather than misattributing.
            source = ""
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


def parking_fee(order: Order, source: str) -> float:
    # 举牌 means meeting the passenger inside the terminal, so the driver enters
    # the car park whatever the channel. Pickups from 携程 always park too.
    return 32.0 if (
        (source == "携程" and order.service_type == "接机")
        or "举牌" in (order.additional_services or "")
    ) else 0.0


def banner_fee(additional_services: str | None) -> float:
    return 40.0 if "举牌" in (additional_services or "") else 0.0
