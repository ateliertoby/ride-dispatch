"""Shared order ingestion: parser cascade + fee rules.

Single source of truth for bot (Telegram) and web (paste) entry points.
"""
from .parser import Order, parse_order, parse_feizhu, parse_tongcheng


def parse_any(text: str) -> tuple[Order, str]:
    """Try 携程 → 飛豬 → 同程. Caller checks order.order_id for success."""
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
    return 32.0 if source == "携程" and order.service_type == "接机" else 0.0


def banner_fee(additional_services: str | None) -> float:
    return 40.0 if "举牌" in (additional_services or "") else 0.0
