# -*- coding: utf-8 -*-
"""离线单测：用首日复测留档的真实样本验证解析规则（不触网）。
样本：木鸟_大连_p1.html（列表）、木鸟_详情页样本.html（room 75978：价109/分4.9/66条/坐标121.522,38.883）"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from muniao_crawler import parse  # noqa: E402

EVID = os.path.join(os.path.dirname(__file__), "..", "..", "首日复测", "留档")


def test_list_page():
    html = open(os.path.join(EVID, "木鸟_大连_p1.html"), encoding="utf-8").read()
    ids = parse.parse_list_page(html)
    assert len(ids) >= 30, f"列表页解析过少: {len(ids)}"
    assert len(set(ids)) == len(ids), "存在重复 id"
    print(f"PASS list_page: {len(ids)} ids")


def test_detail_page():
    html = open(os.path.join(EVID, "木鸟_详情页样本.html"), encoding="utf-8").read()
    rec = parse.parse_detail_page(html, "75978")
    assert rec["price"] == 109, f"price 错误: {rec['price']}"
    assert rec["rating"] == 4.9, f"rating 错误: {rec['rating']}"
    assert rec["review_count"] == 66, f"review_count 错误: {rec['review_count']}"
    assert rec["lng"] and 121.0 < rec["lng"] < 122.5, f"lng 异常: {rec['lng']}"
    assert rec["lat"] and 38.0 < rec["lat"] < 40.0, f"lat 异常: {rec['lat']}"
    assert rec["name"], "name 为空"
    print(f"PASS detail_page: {rec}")


def test_detail_missing_fields():
    rec = parse.parse_detail_page("", "99999")
    assert rec["price"] is None and rec["rating"] is None
    print("PASS detail_missing_fields: 空页面字段全 None，不报错")


def test_list_cards_capacity():
    """M2-变更-004：列表页卡片宜住人数解析。"""
    html = open(os.path.join(EVID, "木鸟_大连_p1.html"), encoding="utf-8").read()
    cards = parse.parse_list_cards(html)
    assert len(cards) >= 30, f"卡片解析过少: {len(cards)}"
    caps = [c["capacity"] for c in cards if c["capacity"] is not None]
    assert len(caps) >= 30, f"宜住人数缺失过多: {len(caps)}"
    assert all(1 <= c <= 30 for c in caps), f"宜住人数越界: {caps[:5]}"
    print(f"PASS list_cards: {len(cards)} 卡片，宜住人数样例 {caps[:5]}")


def test_detail_facility_count():
    """M2-变更-004：详情页配套设施标签计数（样本 75978 = 16）、可住人数。"""
    html = open(os.path.join(EVID, "木鸟_详情页样本.html"), encoding="utf-8").read()
    rec = parse.parse_detail_page(html, "75978")
    assert rec["facility_count"] == 16, f"facility_count 错误: {rec['facility_count']}"
    assert rec["capacity"] == 2, f"capacity 错误: {rec['capacity']}"
    rec2 = parse.parse_detail_page("", "99999")
    assert rec2["facility_count"] is None and rec2["capacity"] is None
    print(f"PASS facility_count: {rec['facility_count']}，capacity: {rec['capacity']}，空页面 None")


if __name__ == "__main__":
    test_list_page()
    test_detail_page()
    test_detail_missing_fields()
    test_list_cards_capacity()
    test_detail_facility_count()
    print("ALL TESTS PASSED")
