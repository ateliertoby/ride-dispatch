"""Service-type classification layer.

Single source of truth for branching on service_type across the codebase.
All predicates accept the raw service_type string stored in the DB.

Every service type here has been received from a platform at least once.
A type that has not been seen is deliberately absent rather than guessed:
it falls through to the 單程 label with no reminders, which is visible on
the dashboard, whereas a guessed string that turns out wrong would route
an order silently and wrongly.
"""

FLIGHT_PICKUP = ('接机',)
FLIGHT_TYPES = ('接机', '送机')
STATION_TYPES = ('接站',)
QUICK_TYPES = ('滴滴', 'Uber', 'foodpanda')

# Settlement counterparties.  Every 接送 source (携程 / 飛豬 / 同程 / SPACE /
# 分銷) is billed to the same platform, so only the self-dispatched app trips
# get a counterparty of their own.
PLATFORMS = ('ride', 'didi', 'uber', 'foodpanda')

_PLATFORM_MAP = {
    '滴滴': 'didi',
    'Uber': 'uber',
    'foodpanda': 'foodpanda',
}

# Fixed booked time, no flight to track: these get the dep30/dep10 pushes.
_DEPARTURE_TYPES = ('送机', '单程接送') + STATION_TYPES

# Which trip endpoint varies for pricing.  Read from the service type, never
# from the address: a station order can have both endpoints in the same
# pricing zone, so address-based inference is provably wrong.
_ANCHOR_END = {
    '接机': 'dropoff',
    '送机': 'pickup',
    '接站': 'dropoff',
}

# Traditional Chinese labels for display.
_LABEL_MAP = {
    '接机': '接機',
    '送机': '送機',
    '接站': '接站',
}


def _norm(service_type: str | None) -> str:
    """Coerce a nullable orders.service_type column value to a string."""
    return service_type or ""


def is_flight_pickup(service_type: str | None) -> bool:
    """Gate for flight tracking, 出場時長, whiteboard, svc/depart reminders."""
    return _norm(service_type) in FLIGHT_PICKUP


def is_station(service_type: str | None) -> bool:
    """True for rail-station orders, whose fares are a separate history pool."""
    return _norm(service_type) in STATION_TYPES


def needs_departure_reminder(service_type: str | None) -> bool:
    """True for service types that get the dep30/dep10 milestones."""
    return _norm(service_type) in _DEPARTURE_TYPES


def anchor_end(service_type: str | None) -> str:
    """Which trip endpoint varies for pricing: 'dropoff' | 'pickup' | ''."""
    return _ANCHOR_END.get(_norm(service_type), '')


def platform_of(service_type: str | None) -> str:
    """Which platform an order settles with: ride | didi | uber | foodpanda.

    JS twin: platform() in the dashboard and the settle page — keep in sync.
    """
    return _PLATFORM_MAP.get(_norm(service_type), 'ride')


def expected_of(order: dict) -> float:
    """What the platform owes for one order.

    Each platform reimburses a different fee on top of the fare; the fee
    columns are nullable, so a missing fee counts as 0.
    JS twin: expectedOf() in the settle page — keep in sync.
    """
    platform = platform_of(order.get('service_type'))
    price = order.get('price') or 0
    if platform == 'ride':
        extra = order.get('banner_fee') or 0
    elif platform == 'foodpanda':
        extra = 0
    else:
        extra = order.get('tunnel_fee') or 0
    return float(price + extra)


def label(service_type: str | None) -> str:
    """Traditional Chinese display label for a service type."""
    service_type = _norm(service_type)
    if service_type in _LABEL_MAP:
        return _LABEL_MAP[service_type]
    if service_type in QUICK_TYPES:
        return service_type
    return '單程'
