"""Zone-based price suggestion from order history.

Zone assignment is pure substring matching against district keywords --
hotel names and exact addresses are irrelevant; only the district
encoded in the address matters.
"""

import sqlite3
from collections import Counter

from .parser import Order


# Each zone maps to substring keywords (both Simplified and Traditional).
# Place names are kept verbatim.  Besides district and street tokens, each
# zone may list venue names (hotels, campuses): platform address strings for
# well-known venues often omit the district entirely, and a venue name is
# just another way an address encodes its district.  Venue aliases only ever
# map to a zone -- prices still come exclusively from zone history, so a
# venue never carries a price of its own.
ZONE_KEYWORDS: dict[str, list[str]] = {
    "kowloon": [
        "尖沙咀", "尖沙嘴", "尖东", "尖東",
        "油麻地", "佐敦", "旺角", "太子", "何文田",
        "红磡", "紅磡", "九龙城", "九龍城",
        "启德", "啟德", "承启道", "承啟道",
        "深水埗", "长沙湾", "長沙灣",
        "弥敦道", "彌敦道", "柯士甸",
        "黄埔", "黃埔", "土瓜湾", "土瓜灣",
        "油尖旺", "九龙塘", "九龍塘",
        "观塘", "觀塘", "九龙站", "九龍站", "西九",
        "巧明街", "城市大学", "城市大學", "HKMU",
        "Tsim Sha Tsui", "Nathan Road", "Mong Kok",
        # "朗廷酒店" would be wrong here: hotels across zones carry the
        # group suffix 朗廷酒店集团旗下, so the token cannot identify a
        # district.  Group-branded strings fall back to manual entry.
        "喜来登", "喜來登", "嘉里酒店", "The Mira",
        "皇家太平洋", "梦卓恩", "夢卓恩",
        "8度海逸",
    ],
    "hk_island_north": [
        "中环", "中環", "上环", "上環",
        "西环", "西環", "西营盘", "西營盤",
        "石塘咀", "坚尼地城", "堅尼地城",
        "金钟", "金鐘", "湾仔", "灣仔",
        "铜锣湾", "銅鑼灣", "天后", "炮台山", "北角",
        "鲗鱼涌", "鰂魚涌", "太古",
        "跑马地", "跑馬地", "中西区", "中西區",
        "东区", "東區", "轩尼诗道", "軒尼詩道",
        "告士打道", "庄士敦道", "莊士敦道",
        "港湾道", "港灣道", "德辅道", "德輔道",
        "干诺道", "干諾道", "皇后大道",
        "摩理臣山道", "红棉路", "紅棉路",
        "Wan Chai", "Causeway Bay",
        "万丽海景", "萬麗海景", "维港凯悦", "維港凱悅",
        "尚翘峰", "尚翹峰", "美利酒店", "南洋酒店",
    ],
    "southern": [
        "数码港", "數碼港", "香港仔",
        "鸭脷洲", "鴨脷洲", "黄竹坑", "黃竹坑",
        "海洋公园", "海洋公園", "海怡",
        "浅水湾", "淺水灣", "赤柱", "薄扶林",
        "南区", "南區", "寿山村", "壽山村",
    ],
    "shatin": [
        "沙田", "大围", "大圍", "火炭",
        "白鹤汀", "白鶴汀", "帝都酒店",
    ],
    "tsuen_wan_tsing_yi": [
        "荃湾", "荃灣", "青衣",
    ],
    "disney": [
        "迪士尼",
    ],
}


def match_zone(text: str) -> str | None:
    """Return the zone name if exactly one zone's keywords appear in *text*.

    Returns None when zero or multiple zones match (ambiguous).
    """
    if not text:
        return None
    zones: set[str] = set()
    for zone, keywords in ZONE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            zones.add(zone)
    if len(zones) == 1:
        return next(iter(zones))
    return None


def suggest_price(db_path: str, order: Order) -> float | None:
    """Suggest a zone-based price from historical order data.

    Returns the statistical mode of prices for the same (zone, service_type)
    pair, with the lower price winning on ties.  Returns None when the zone
    is ambiguous, unknown, or there is no price history.
    """
    if order.service_type not in ("接机", "送机"):
        return None

    endpoint = order.dropoff if order.service_type == "接机" else order.pickup
    if not endpoint:
        return None

    zone = match_zone(endpoint)
    if zone is None:
        return None

    # Cancelled orders included -- an agreed price is signal.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT service_type, pickup, dropoff, price FROM orders "
            "WHERE service_type IN ('接机', '送机') AND price IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    prices: list[float] = []
    for stype, pickup, dropoff, price in rows:
        if stype != order.service_type:
            continue
        hist_endpoint = dropoff if stype == "接机" else pickup
        if hist_endpoint and match_zone(hist_endpoint) == zone:
            prices.append(price)

    if not prices:
        return None

    counts = Counter(prices)
    max_count = max(counts.values())
    candidates = [p for p, c in counts.items() if c == max_count]
    return float(min(candidates))
