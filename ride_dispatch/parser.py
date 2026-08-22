import re
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

# A terminal name alone identifies the airport ("香港 T2" never names anything
# else), but T1/T2 also occur inside unrelated ASCII runs (CAT2, T25, HYATT1).
# \b cannot draw the boundary: a CJK char and an ASCII letter are both \w, so
# 香港T2 has no word boundary before the T — hence explicit lookarounds.
_TERMINAL_RE = re.compile(r"(?<![a-z0-9])t[12](?![a-z0-9])")

# 送/接 direction markers, in either script, as they appear in free text.
_DEPARTURE_MARKERS = ("送机", "送機")
_ARRIVAL_MARKERS = ("接机", "接機")


def _mentions_airport(text: str) -> bool:
    lowered = text.lower()
    return (any(kw in lowered for kw in _AIRPORT_KEYWORDS)
            or _TERMINAL_RE.search(lowered) is not None)


def _infer_service_type(pickup: str, dropoff: str) -> str:
    """Derive 送机/接机 from the endpoints; "" when neither is the airport."""
    if _mentions_airport(dropoff):
        return "送机"
    if _mentions_airport(pickup):
        return "接机"
    return ""


def _direction_from_free_text(text: str) -> str:
    """Derive 送机/接机 from an explicit direction marker in free text.

    Returns "" unless exactly one direction is named: text carrying both, or
    neither, is ambiguous and must not be guessed at.
    """
    departure = any(m in text for m in _DEPARTURE_MARKERS)
    arrival = any(m in text for m in _ARRIVAL_MARKERS)
    if departure and not arrival:
        return "送机"
    if arrival and not departure:
        return "接机"
    return ""


_TC_FIELD_MAP = {
    "订单号": "order_id",
    "车型": "vehicle_type",
    "用车时间": "scheduled_time",
    "出发地": "pickup",
    "目的地": "dropoff",
    "航班号": "flight_number",
    "订单里程": "distance_km",
}

_TC_NO_COLON = {
    "乘客姓名": "passenger_name",
    "乘客英文名": "passenger_en_name",
    "乘客手机号": "passenger_phone",
    "同行人电话": "companion_phone",
    "成人数": "adults",
    "儿童数": "children",
}

# The sign-holding request arrives as a free-standing line rather than a
# labelled field, in either script depending on who relayed it.
_TC_BANNER_MARKERS = ("舉牌", "举牌")


def _tc_no_colon_fields(line: str):
    """Yield (field, value) for every unlabelled-field marker on one line.

    A line can pack several fields ("乘客姓名X  乘客英文名  乘客手机号Y"), so a
    marker's value ends where the next marker begins, not at end of line.
    Anything before the first marker, and any line without one, is ignored.
    """
    hits = []
    for marker, field in _TC_NO_COLON.items():
        pos = line.find(marker)
        if pos != -1:
            hits.append((pos, marker, field))
    hits.sort()

    for i, (pos, marker, field) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(line)
        yield field, line[pos + len(marker):end].strip()


def _tc_distance(value: str) -> Optional[float]:
    """Parse 订单里程, which arrives with its unit attached ("39.257 km")."""
    num = value.strip()
    if num.lower().endswith("km"):
        num = num[:-2].strip()
    try:
        return float(num)
    except ValueError:
        return None


def parse_tongcheng(raw: str) -> Order:
    parsed = {}
    # 名称 is a product name, not an order field — it is read for direction
    # inference only and never stored, so it stays out of `parsed`.
    route_name = ""
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
                elif key == "名称":
                    route_name = value
                break
        else:
            # Fields without colon
            for field, value in _tc_no_colon_fields(line):
                parsed[field] = value

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

    # The endpoints are the authoritative signal. 名称 only breaks the tie when
    # they say nothing: it is free text, absent from older messages, and its
    # airport keywords are non-directional (「香港机场-荃湾」 names the airport
    # whichever way the car goes) — only its 送/接 marker carries direction.
    service_type = _infer_service_type(
        parsed.get("pickup", ""), parsed.get("dropoff", "")
    )
    if not service_type:
        service_type = _direction_from_free_text(route_name)

    phone = parsed.get("passenger_phone", "").replace("-", " ")
    flight = parsed.get("flight_number", "").lstrip("￥").strip()

    # 乘客英文名 is a second name field, not a contact — the card has one name
    # slot. The marker ships with an empty value too, which must leave the name
    # untouched rather than trailing a separator.
    name = parsed.get("passenger_name", "")
    en_name = parsed.get("passenger_en_name", "")
    if en_name:
        name = f"{name} {en_name}".strip()

    # 【label】number is the contact-line convention every renderer understands;
    # without the label the companion's number is indistinguishable from the
    # passenger's own.
    companion = parsed.get("companion_phone", "")
    more_contacts = f"【同行人】{companion}" if companion else ""

    # Normalized to the simplified literal because that is what every consumer
    # (banner fee, whiteboard prompt, card line) matches on.
    banner = "举牌" if any(m in raw for m in _TC_BANNER_MARKERS) else ""

    return Order(
        order_id=parsed.get("order_id", ""),
        service_type=service_type,
        vehicle_type=parsed.get("vehicle_type", ""),
        passenger_name=name,
        scheduled_time=parsed.get("scheduled_time", ""),
        passenger_phone=phone,
        overseas_phone="",
        flight_number=flight,
        pickup=parsed.get("pickup", ""),
        dropoff=parsed.get("dropoff", ""),
        distance_km=_tc_distance(parsed.get("distance_km", "")),
        notes="",
        driver_notes="",
        additional_services=banner,
        passenger_exit_minutes=None,
        third_party_contact="",
        more_contacts=more_contacts,
        raw_message=raw,
    )


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
            # 真实号 can carry two numbers separated by "/". passenger_phone
            # must stay a single dialable number: the card e164-formats it for
            # tap-to-call and "/" is not a separator any formatter splits on.
            first, slash, rest = line.partition(sep)[2].strip().partition("/")
            parsed["passenger_phone"] = first.strip()
            if slash:
                parsed["more_contacts"] = f"【備用】{rest.strip()}"
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
        more_contacts=parsed.get("more_contacts", ""),
        raw_message=raw,
    )


_SPACE_FIELD_MAP = {
    "订单号": "order_id",
    "类型": "service_type",
    "车型": "vehicle_type",
    "联系人": "passenger_name",
    "联系电话": "passenger_phone",
    # Relayed copies of this format carry the contact under shorter keys.
    # Matching is exact-key, so these cannot swallow 乘客姓名/乘客电话.
    "姓名": "passenger_name",
    "电话": "passenger_phone",
    # Another variant labels the same contact pair 乘车人/联系方式. 联系方式 is
    # free text and often holds no number at all (e.g. 微信群联系); it is stored
    # verbatim, because dropping an unrecognised value loses the only way to
    # reach the passenger.
    "乘车人": "passenger_name",
    "联系方式": "passenger_phone",
    "航班号": "flight_number",
    "上车点": "pickup",
    "下车点": "dropoff",
    "用车日期": "_date",
    "用车时间": "_time",
    "预计降落时间": "_landing_time",
    # Relayed copies spell the pickup, dropoff and landing-time keys out in
    # full. They label the same fields as the short keys above.
    "上车地点": "pickup",
    "下车地点": "dropoff",
    "航班预计降落时间": "_landing_time",
    # Head count and luggage arrive as one free-text phrase (乘车人数：一个人两件
    # 行李). It goes to driver_notes, the only notes field the dispatch card and
    # the dashboard render. Exact-key matching is what stops it being read as
    # 乘车人; a prefix or substring match here would overwrite passenger_name.
    "乘车人数": "driver_notes",
}

# The example-model suffix after 车型 comes in any of these bracket styles.
_SPACE_BRACKET_RE = re.compile(r"[【\[（(].*")
_SPACE_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
# Prefixes that imply PM when hour < 12
_SPACE_PM_PREFIXES = ("下午", "晚上")
_SPACE_TIME_PREFIXES = ("中午", "上午", "下午", "晚上", "凌晨")


def parse_space(raw: str) -> Order:
    parsed = {}
    for line in raw.strip().splitlines():
        sep = "：" if "：" in line else ":"
        if sep not in line:
            continue
        key, _, value = line.partition(sep)
        key = key.strip()
        value = value.strip()
        if key in _SPACE_FIELD_MAP:
            parsed[_SPACE_FIELD_MAP[key]] = value

    # A relaying distributor appends its name to 订单号. order_id is the DB
    # UNIQUE dedupe key, so the suffix must go or the same order relayed by two
    # channels lands twice.
    oid = parsed.get("order_id", "")
    if "-" in oid:
        oid = oid.split("-")[0]
    parsed["order_id"] = oid

    # service_type: extract part after last '-'
    stype = parsed.get("service_type", "")
    if "-" in stype:
        stype = stype.rsplit("-", 1)[1]
    parsed["service_type"] = stype

    # vehicle_type: strip bracket suffix
    vtype = parsed.get("vehicle_type", "")
    vtype = _SPACE_BRACKET_RE.sub("", vtype).strip()
    parsed["vehicle_type"] = vtype

    # Merge date + time into scheduled_time
    date_str = parsed.pop("_date", "")
    time_str = parsed.pop("_time", "")
    landing_str = parsed.pop("_landing_time", "")
    # 用车时间 always wins. Variants that omit it fall back to 预计降落时间: for
    # 接机 the scheduled time is only an estimate that live flight tracking
    # supersedes anyway, and a date without a time breaks every consumer that
    # reads scheduled_time as '%Y-%m-%d %H:%M:%S'.
    time_str = time_str or landing_str
    scheduled = ""
    if time_str:
        is_pm = any(time_str.startswith(p) for p in _SPACE_PM_PREFIXES)
        for p in _SPACE_TIME_PREFIXES:
            if time_str.startswith(p):
                time_str = time_str[len(p):]
                break
        m = _SPACE_TIME_RE.search(time_str)
        if m:
            hour, minute = int(m.group(1)), m.group(2)
            if is_pm and hour < 12:
                hour += 12
            time_part = f"{hour:02d}:{minute}:00"
            scheduled = f"{date_str} {time_part}" if date_str else time_part
    elif date_str:
        scheduled = date_str
    parsed["scheduled_time"] = scheduled

    if not parsed.get("order_id"):
        return Order(
            order_id="", service_type="", vehicle_type="", passenger_name="",
            scheduled_time="", passenger_phone="", overseas_phone="",
            flight_number="", pickup="", dropoff="", distance_km=None,
            notes="", driver_notes="", additional_services="",
            passenger_exit_minutes=None, third_party_contact="",
            more_contacts="", raw_message=raw,
        )

    return Order(
        order_id=parsed.get("order_id", ""),
        service_type=parsed.get("service_type", ""),
        vehicle_type=parsed.get("vehicle_type", ""),
        passenger_name=parsed.get("passenger_name", ""),
        scheduled_time=parsed.get("scheduled_time", ""),
        passenger_phone=parsed.get("passenger_phone", ""),
        overseas_phone="",
        flight_number=parsed.get("flight_number", ""),
        pickup=parsed.get("pickup", ""),
        dropoff=parsed.get("dropoff", ""),
        distance_km=None,
        notes="",
        driver_notes=parsed.get("driver_notes", ""),
        additional_services="",
        passenger_exit_minutes=None,
        third_party_contact="",
        more_contacts="",
        raw_message=raw,
    )


_FX_FIELD_MAP = {
    "平台订单号": "order_id",
    "出行时间": "scheduled_time",
    "出发地": "pickup",
    "目的地": "dropoff",
    "客人姓名": "passenger_name",
    "客人联系方式": "passenger_phone",
    "平台备注": "notes",
}

# 出行时间 arrives without seconds, hour not always zero-padded.
_FX_TIME_NO_SEC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2})$")


def parse_fenxiao(raw: str) -> Order:
    parsed = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        sep = "：" if "：" in line else ":"
        if sep not in line:
            continue
        key, _, value = line.partition(sep)
        key = key.strip()
        value = value.strip()
        if key in _FX_FIELD_MAP:
            parsed[_FX_FIELD_MAP[key]] = value

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

    # Every scheduled_time consumer (flight.py, reminder pushes) strptime's
    # "%Y-%m-%d %H:%M:%S"; a seconds-less value silently breaks all of them.
    scheduled = parsed.get("scheduled_time", "")
    m = _FX_TIME_NO_SEC_RE.match(scheduled)
    if m:
        scheduled = f"{m.group(1)} {int(m.group(2)):02d}:{m.group(3)}:00"

    # No explicit service field in this format — infer from pickup/dropoff.
    service_type = _infer_service_type(
        parsed.get("pickup", ""), parsed.get("dropoff", "")
    )

    return Order(
        order_id=parsed.get("order_id", ""),
        service_type=service_type,
        vehicle_type="",
        passenger_name=parsed.get("passenger_name", ""),
        scheduled_time=scheduled,
        passenger_phone=parsed.get("passenger_phone", ""),
        overseas_phone="",
        flight_number="",
        pickup=parsed.get("pickup", ""),
        dropoff=parsed.get("dropoff", ""),
        distance_km=None,
        notes=parsed.get("notes", ""),
        driver_notes="",
        additional_services="",
        passenger_exit_minutes=None,
        third_party_contact="",
        more_contacts="",
        raw_message=raw,
    )
