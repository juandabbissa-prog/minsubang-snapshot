# -*- coding: utf-8 -*-
"""解析层：列表页 roomId 提取 + 详情页字段解析。
解析规则全部来自留档样本实证（首日复测/留档/木鸟_详情页样本.html）：
  价格  <div class="room_price"> ￥<span class="f30">109</span>
  评分  <span class="room_Escorerightnum"> 4.9
  点评  <span class="room_Enum f14">66条评价</span>
  坐标  页面内嵌 lng/lat（121.x/38.x，大连真实坐标）
"""
import re
from html import unescape

RE_LIST_IDS = re.compile(r'href="/room/(\d+)\.html"')
RE_PRICE = re.compile(r'class="room_price"[^>]*>\s*￥\s*<span[^>]*>(\d{2,6})</span>')
RE_RATING = re.compile(r'class="room_Escorerightnum">\s*([\d.]+)\s*<')
RE_REVIEWS = re.compile(r'class="room_Enum[^"]*">(\d+)条评价')
RE_TITLE = re.compile(r"<title>([^<]{2,80})</title>")
RE_LNG = re.compile(r'["\']?(?:lng|longitude)["\']?\s*[:=]\s*["\']?(1[0-3][0-9]\.\d{3,8})')
RE_LAT = re.compile(r'["\']?(?:lat|latitude)["\']?\s*[:=]\s*["\']?((?:[2-5])\d\.\d{3,8})')
RE_MAP = re.compile(r'id="roommap"[^>]*?name="([^"]*)"[^>]*?lng="([\d.]+)"[^>]*?lat="([\d.]+)"')
RE_ADDR = re.compile(r'class="room_address[^"]*"[^>]*>(.*?)<', re.S)


def parse_list_page(html: str) -> list[str]:
    """返回本页 roomId 列表（去重保序）。列表本体 30 套/页，页面另含推荐位。"""
    seen, out = set(), []
    for rid in RE_LIST_IDS.findall(html or ""):
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def parse_detail_page(html: str, room_id: str) -> dict:
    """解析详情页，字段可缺失（新店无评分/点评）。返回字段级结果。"""
    rec = {"listing_ref": room_id, "price": None, "rating": None,
           "review_count": None, "lng": None, "lat": None,
           "name": None, "address": None, "region_code": None}
    if not html:
        return rec
    m = RE_PRICE.search(html)
    if m:
        rec["price"] = int(m.group(1))
    m = RE_RATING.search(html)
    if m:
        rec["rating"] = float(m.group(1))
    m = RE_REVIEWS.search(html)
    if m:
        rec["review_count"] = int(m.group(1))
    m = RE_MAP.search(html)
    if m:
        # roommap 锚点：name="城市-区域-位置-小区"，坐标最可靠
        parts = (m.group(1) or "").split("-")
        rec["lng"], rec["lat"] = float(m.group(2)), float(m.group(3))
        if len(parts) >= 2:
            rec["region_code"] = parts[1].strip()
        if len(parts) >= 3 and not rec["address"]:
            rec["address"] = "-".join(parts[1:]).strip()[:80]
    else:
        m = RE_LNG.search(html)
        if m:
            rec["lng"] = float(m.group(1))
        m = RE_LAT.search(html)
        if m:
            try:
                rec["lat"] = float(m.group(1))
            except ValueError:
                pass
    m = RE_TITLE.search(html)
    if m:
        rec["name"] = unescape(m.group(1)).split("|")[0].strip()[:60]
    m = RE_ADDR.search(html)
    if m:
        rec["address"] = re.sub(r"\s+", " ", unescape(m.group(1))).strip()[:80]
    return rec
