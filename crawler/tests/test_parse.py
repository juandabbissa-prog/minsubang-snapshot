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


if __name__ == "__main__":
    test_list_page()
    test_detail_page()
    test_detail_missing_fields()
    print("ALL TESTS PASSED")
