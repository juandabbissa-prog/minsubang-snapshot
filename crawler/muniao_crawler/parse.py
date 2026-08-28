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
RE_CARD_SPLIT = re.compile(r'<div class="s_mn_house_tit">')
RE_CARD_ROOM = re.compile(r'href="/room/(\d+)\.html"')
RE_CAPACITY = re.compile(r'宜住(\d+)人')
RE_FACILITY_BLOCK = re.compile(r'<ul id="ptss".*?</ul>', re.S)
RE_LI = re.compile(r'<li>')
RE_DETAIL_CAPACITY = re.compile(r'可住人数：.*?title="(\d+)人"', re.S)
RE_PRICE = re.compile(r'class="room_price"[^>]*>\s*￥\s*<span[^>]*>(\d{2,6})</span>')
RE_RATING = re.compile(r'class="room_Escorerightnum">\s*([\d.]+)\s*<')
RE_REVIEWS = re.compile(r'class="room_Enum[^"]*">(\d+)条评价')
RE_TITLE = re.compile(r"<title>([^<]{2,80})</title>")
RE_LNG = re.compile(r'["\']?(?:lng|longitude)["\']?\s*[:=]\s*["\']?(1[0-3][0-9]\.\d{3,8})')
RE_LAT = re.compile(r'["\']?(?:lat|latitude)["\']?\s*[:=]\s*["\']?((?:[2-5])\d\.\d{3,8})')
RE_MAP = re.compile(r'id="roommap"[^>]*?name="([^"]*)"[^>]*?lng="([\d.]+)"[^>]*?lat="([\d.]+)"')
RE_ADDR = re.compile(r'class="room_address[^"]*"[^>]*>(.*?)<', re.S)
RE_HOST_ID = re.compile(r'#chat@(\d+)')
RE_HOST_PANE = re.compile(r'<div id="div_1".*?(?=<div id="div_2"|$)', re.S)
RE_HOST_ROOM = re.compile(r'data-id="(\d+)"')


def parse_list_page(html: str) -> list[str]:
    """返回本页 roomId 列表（去重保序）。列表本体 30 套/页，页面另含推荐位。"""
    seen, out = set(), []
    for rid in RE_LIST_IDS.findall(html or ""):
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def parse_list_cards(html: str) -> list[dict]:
    """解析列表页房源卡片（M2-变更-004 增量）：roomId + 宜住人数 capacity。

    卡片锚点 <div class="s_mn_house_tit">（首日复测留档 muniao_dalian2.html 实证，
    30/30 卡片均含「宜住N人」）；卡片解析失败时退化为纯 roomId，绝不影响发现层主流程。
    """
    cards = []
    seen = set()
    chunks = RE_CARD_SPLIT.split(html or "")
    for chunk in chunks[1:]:
        m = RE_CARD_ROOM.search(chunk)
        if not m:
            continue
        rid = m.group(1)
        if rid in seen:
            continue
        seen.add(rid)
        cap = RE_CAPACITY.search(chunk)
        cards.append({"listing_ref": rid,
                      "capacity": int(cap.group(1)) if cap else None})
    if not cards:  # 卡片结构变更时兜底：纯 id 列表，capacity 全 None
        cards = [{"listing_ref": rid, "capacity": None}
                 for rid in parse_list_page(html)]
    return cards


def parse_detail_page(html: str, room_id: str) -> dict:
    """解析详情页，字段可缺失（新店无评分/点评）。返回字段级结果。

    M2-变更-004 增量：facility_count = 配套设施区（ul#ptss）标签计数，
    样本实证（木鸟_详情页样本.html）一店 16 个；页面无该区时如实 None。
    """
    rec = {"listing_ref": room_id, "price": None, "rating": None,
           "review_count": None, "lng": None, "lat": None,
           "name": None, "address": None, "region_code": None,
           "facility_count": None, "capacity": None, "host_id": None}
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
    m = RE_FACILITY_BLOCK.search(html)
    if m:
        rec["facility_count"] = len(RE_LI.findall(m.group(0)))
    m = RE_DETAIL_CAPACITY.search(html)
    if m:
        rec["capacity"] = int(m.group(1))
    m = RE_HOST_ID.search(html)
    if m:
        rec["host_id"] = m.group(1)
    return rec


def parse_host_page(html: str) -> dict:
    """房东主页（/fangdong/{id}/）：房东房源区（div_1）在营挂牌套数。

    体量维数据源（2026-08-28 探源实证）：以带 /room/ 链接的卡片去重计数
    （data-id 含疑似下架房源，如样本 50299 无链接不可订，不计入在营规模）；
    页面无分页控件（样本实证），大房东若分页则按当前页如实计、口径留档。
    robots.txt 未禁止 /fangdong/ 路径（2026-08-28 逐条比对）。
    """
    if not html:
        return {"listing_count": None}
    m = RE_HOST_PANE.search(html)
    seg = m.group(0) if m else html
    ids = set(RE_LIST_IDS.findall(seg))
    return {"listing_count": len(ids) if ids else None}
