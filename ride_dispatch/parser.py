from dataclasses import dataclass
from typing import Optional


@dataclass
class Order:
    order_id: str
    service_type: str
    vehicle_type: str
    passenger_name: str
    scheduled_time: str
    passenger_phone: str
    overseas_phone: str
    flight_number: str
    pickup: str
    dropoff: str
    distance_km: Optional[float]
    notes: str
    driver_notes: str
    additional_services: str
    passenger_exit_minutes: Optional[int]
    third_party_contact: str
    more_contacts: str
    raw_message: str


FIELD_MAP = {
    "订单号": "order_id",
    "服务类型": "service_type",
    "接单车型": "vehicle_type",
    "乘客姓名": "passenger_name",
    "用车时间": "scheduled_time",
    "乘客电话": "passenger_phone",
    "乘客境外电话": "overseas_phone",
    "航班号": "flight_number",
    "上车点": "pickup",
    "下车点": "dropoff",
    "订单里程": "distance_km",
    "订单备注": "notes",
    "司机可见备注": "driver_notes",
    "附加服务": "additional_services",
    "乘客出场时长": "passenger_exit_minutes",
    "第三方联系方式": "third_party_contact",
    "更多联系方式": "more_contacts",
}

INT_FIELDS = {"passenger_exit_minutes"}
FLOAT_FIELDS = {"distance_km"}


def _parse_int(val: str) -> Optional[int]:
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def parse_order(raw: str) -> Order:
    parsed = {}
    for line in raw.strip().splitlines():
        sep = "：" if "：" in line else ":"
        if sep not in line:
            continue
        key, _, value = line.partition(sep)
        key = key.strip()
        value = value.strip()
        if key in FIELD_MAP:
            field = FIELD_MAP[key]
            if field in INT_FIELDS:
                parsed[field] = _parse_int(value) if value else None
            elif field in FLOAT_FIELDS:
                try:
                    parsed[field] = float(value) if value else None
                except ValueError:
                    parsed[field] = None
            else:
                parsed[field] = value

    return Order(
        order_id=parsed.get("order_id", ""),
        service_type=parsed.get("service_type", ""),
        vehicle_type=parsed.get("vehicle_type", ""),
        passenger_name=parsed.get("passenger_name", ""),
        scheduled_time=parsed.get("scheduled_time", ""),
        passenger_phone=parsed.get("passenger_phone", ""),
        overseas_phone=parsed.get("overseas_phone", ""),
        flight_number=parsed.get("flight_number", ""),
        pickup=parsed.get("pickup", ""),
        dropoff=parsed.get("dropoff", ""),
        distance_km=parsed.get("distance_km"),
        notes=parsed.get("notes", ""),
        driver_notes=parsed.get("driver_notes", ""),
        additional_services=parsed.get("additional_services", ""),
        passenger_exit_minutes=parsed.get("passenger_exit_minutes"),
        third_party_contact=parsed.get("third_party_contact", ""),
        more_contacts=parsed.get("more_contacts", ""),
        raw_message=raw,
    )


_AIRPORT_KEYWORDS = ("机场", "機場", "airport")

_TC_FIELD_MAP = {
    "订单号": "order_id",
    "车型": "vehicle_type",
    "用车时间": "scheduled_time",
    "出发地": "pickup",
    "目的地": "dropoff",
    "航班号": "flight_number",
}

_TC_NO_COLON = {
    "乘客姓名": "passenger_name",
    "乘客手机号": "passenger_phone",
    "成人数": "adults",
    "儿童数": "children",
}


def parse_tongcheng(raw: str) -> Order:
    parsed = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        # Fields with colon
        for sep in ("：", ":"):
            if sep in line:
                key, _, value = line.partition(sep)
                key = key.strip()
                value = value.strip()
                if key in _TC_FIELD_MAP:
                    parsed[_TC_FIELD_MAP[key]] = value
                break
        else:
            # Fields without colon
            for prefix, field in _TC_NO_COLON.items():
                if line.startswith(prefix):
                    parsed[field] = line[len(prefix):].strip()
                    break

    oid = parsed.get("order_id", "")
    if "-" in oid:
        oid = oid.split("-")[0]
    parsed["order_id"] = oid

    if not oid:
        return Order(
            order_id="", service_type="", vehicle_type="", passenger_name="",
            scheduled_time="", passenger_phone="", overseas_phone="",
            flight_number="", pickup="", dropoff="", distance_km=None,
            notes="", driver_notes="", additional_services="",
            passenger_exit_minutes=None, third_party_contact="",
            more_contacts="", raw_message=raw,
        )

    # Infer service type from pickup/dropoff
    dropoff = parsed.get("dropoff", "").lower()
    pickup = parsed.get("pickup", "").lower()
    if any(kw in dropoff for kw in _AIRPORT_KEYWORDS):
        service_type = "送机"
    elif any(kw in pickup for kw in _AIRPORT_KEYWORDS):
        service_type = "接机"
    else:
        service_type = ""

    phone = parsed.get("passenger_phone", "").replace("-", " ")
    flight = parsed.get("flight_number", "").lstrip("￥").strip()

    return Order(
        order_id=parsed.get("order_id", ""),
        service_type=service_type,
        vehicle_type=parsed.get("vehicle_type", ""),
        passenger_name=parsed.get("passenger_name", ""),
        scheduled_time=parsed.get("scheduled_time", ""),
        passenger_phone=phone,
        overseas_phone="",
        flight_number=flight,
        pickup=parsed.get("pickup", ""),
        dropoff=parsed.get("dropoff", ""),
        distance_km=None,
        notes="",
        driver_notes="",
        additional_services="",
        passenger_exit_minutes=None,
        third_party_contact="",
        more_contacts="",
        raw_message=raw,
    )


import re

_FZ_DISTANCE_RE = re.compile(r"约([\d.]+)公里")
_FZ_SERVICE_RE = re.compile(r"【(接机|送机)】")


def parse_feizhu(raw: str) -> Order:
    lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
    parsed = {}
    time_idx = None

    for i, line in enumerate(lines):
        if line.startswith("订单编号") and ("：" in line or ":" in line):
            sep = "：" if "：" in line else ":"
            oid = line.partition(sep)[2].strip()
            if "-" in oid:
                oid = oid.split("-")[0]
            parsed["order_id"] = oid

        elif _FZ_SERVICE_RE.search(line):
            parsed["service_type"] = _FZ_SERVICE_RE.search(line).group(1)

        elif line.startswith("[出发]") or line.startswith("【出发】"):
            parsed["pickup"] = re.sub(r"[\[【]出发[\]】]", "", line).strip()

        elif line.startswith("[抵达]") or line.startswith("【抵达】"):
            parsed["dropoff"] = re.sub(r"[\[【]抵达[\]】]", "", line).strip()

        elif _FZ_DISTANCE_RE.search(line):
            parsed["distance_km"] = float(_FZ_DISTANCE_RE.search(line).group(1))
            parsed["_dist_idx"] = i

        elif line.startswith("[预计抵达]") or line.startswith("【预计抵达】"):
            parsed["_eta_idx"] = i

        elif re.match(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", line):
            parsed["scheduled_time"] = line
            time_idx = i

        elif line.startswith("真实号") and ("：" in line or ":" in line):
            sep = "：" if "：" in line else ":"
            parsed["passenger_phone"] = line.partition(sep)[2].strip()
            parsed["_phone_idx"] = i

    if not parsed.get("order_id"):
        return Order(
            order_id="", service_type="", vehicle_type="", passenger_name="",
            scheduled_time="", passenger_phone="", overseas_phone="",
            flight_number="", pickup="", dropoff="", distance_km=None,
            notes="", driver_notes="", additional_services="",
            passenger_exit_minutes=None, third_party_contact="",
            more_contacts="", raw_message=raw,
        )

    # Flight: between distance line and [预计抵达] line
    dist_idx = parsed.get("_dist_idx")
    eta_idx = parsed.get("_eta_idx")
    flight = ""
    if dist_idx is not None and eta_idx is not None and eta_idx - dist_idx == 2:
        flight = lines[dist_idx + 1]

    # Passenger name: between time line and phone line
    phone_idx = parsed.get("_phone_idx")
    name = ""
    if time_idx is not None and phone_idx is not None and phone_idx - time_idx == 2:
        name = lines[time_idx + 1]

    # Vehicle type: second line (index 1)
    vehicle = lines[1] if len(lines) > 1 else ""

    return Order(
        order_id=parsed.get("order_id", ""),
        service_type=parsed.get("service_type", ""),
        vehicle_type=vehicle,
        passenger_name=name,
        scheduled_time=parsed.get("scheduled_time", ""),
        passenger_phone=parsed.get("passenger_phone", ""),
        overseas_phone="",
        flight_number=flight,
        pickup=parsed.get("pickup", ""),
        dropoff=parsed.get("dropoff", ""),
        distance_km=parsed.get("distance_km"),
        notes="",
        driver_notes="",
        additional_services="",
        passenger_exit_minutes=None,
        third_party_contact="",
        more_contacts="",
        raw_message=raw,
    )
