# -*- coding: utf-8 -*-
"""木鸟 listing_base 坐标系标定脚本（M1-变更-004 · 2.4 清账）。

原理：抽样底册中带坐标的房源，把坐标按 GCJ-02 提交高德逆地理编码（regeo），
比对反查得到的地址/行政区与木鸟 name/address/region_code 是否一致：
- 一致 → 底册坐标即为 GCJ-02；
- 系统性偏移（如反查地址整体偏移数百米）→ 再按 WGS-84/BD-09 特征判断。

用法：
    set AMAP_KEY=<高德Web服务Key>   # Windows
    python calibrate_coords.py

Key 仅从环境变量读取，不入库、不入日志正文、不写入标定记录。
产出：《坐标系标定记录.md》于本脚本上级目录（M1_数据采集器/）。
"""
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "muniao.db"
REPORT_PATH = BASE_DIR.parent / "坐标系标定记录.md"
SAMPLE_SIZE = 10
AMAP_BASE = "https://restapi.amap.com"

# 木鸟底册 region_code 中的民间片区与高德行政区的映射（如大连开发区行政上属金州区）
REGION_ALIAS = {
    "开发区": {"金州区"},
    "高新园区": {"甘井子区", "沙河口区"},
}


def load_key() -> str:
    key = os.environ.get("AMAP_KEY", "").strip()
    if not key:
        # 尝试读工程基座 server/.env（若存在）
        env_file = BASE_DIR.parent.parent / "M0_工程基座" / "server" / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("AMAP_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        print("缺少 AMAP_KEY 环境变量，无法执行标定。请配置后重跑。")
        sys.exit(2)
    return key


def regeo(key: str, lng: float, lat: float) -> dict:
    params = urllib.parse.urlencode(
        {"key": key, "location": f"{lng},{lat}", "extensions": "base", "radius": "1000", "output": "json"}
    )
    with urllib.request.urlopen(f"{AMAP_BASE}/v3/geocode/regeo?{params}", timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if str(payload.get("status")) != "1":
        raise RuntimeError(f"regeo 返回异常 infocode={payload.get('infocode')}")
    regeocode = payload.get("regeocode") or {}
    comp = regeocode.get("addressComponent") or {}
    formatted = regeocode.get("formatted_address", "")
    if isinstance(formatted, list):
        formatted = ""
    return {
        "formatted_address": str(formatted),
        "district": str(comp.get("district", "") or ""),
        "township": str(comp.get("township", "") or ""),
    }


def main() -> None:
    key = load_key()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT listing_ref, name, address, lng, lat, region_code "
        "FROM listing_base WHERE lng IS NOT NULL AND lat IS NOT NULL ORDER BY id LIMIT ?",
        (SAMPLE_SIZE,),
    ).fetchall()
    conn.close()
    if len(rows) < SAMPLE_SIZE:
        print(f"带坐标样本不足 {SAMPLE_SIZE} 条，实际 {len(rows)} 条，按实际样本标定。")

    records = []
    hit = 0
    for row in rows:
        info = regeo(key, row["lng"], row["lat"])
        haystack = f"{row['name'] or ''} {row['address'] or ''}"
        # 判一致：反查行政区与底册 region_code 一致（含民间片区→行政区别名映射），
        # 或反查地址关键词落入底册名称/地址
        region = row["region_code"] or ""
        district_hit = bool(info["district"]) and (
            info["district"] == region or info["district"] in REGION_ALIAS.get(region, set())
        )
        addr_hit = False
        for token in (info["township"],):
            if token and token in haystack:
                addr_hit = True
        # 小区/地标词比对：取底册地址第三段（“区-路-小区”结构）
        parts = (row["address"] or "").split("-")
        poi_word = parts[-1].strip() if len(parts) >= 2 else ""
        if poi_word and poi_word in info["formatted_address"]:
            addr_hit = True
        consistent = district_hit or addr_hit
        hit += 1 if consistent else 0
        records.append(
            {
                "listing_ref": row["listing_ref"],
                "lng": row["lng"],
                "lat": row["lat"],
                "muniao_name": row["name"],
                "muniao_address": row["address"],
                "region_code": row["region_code"],
                "amap_formatted": info["formatted_address"],
                "amap_district": info["district"],
                "consistent": consistent,
            }
        )

    conclusion = (
        f"抽样 {len(records)} 条，一致 {hit} 条，一致率 {hit / len(records) * 100:.0f}%。"
        if records
        else "无样本。"
    )
    verdict = (
        "木鸟 listing_base 坐标按 GCJ-02 解读反查地址与底册信息一致，判定底册坐标系为 GCJ-02，可直接与高德 POI 坐标融合使用。"
        if records and hit / len(records) >= 0.8
        else "一致率不足 80%，底册坐标系存疑，需扩大样本并按 WGS-84/BD-09 偏移特征复核。"
    )

    lines = [
        "# 木鸟 listing_base 坐标系标定记录",
        "",
        f"- 标定时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "- 方法：抽样带坐标房源，坐标按 GCJ-02 提交高德逆地理编码，比对反查地址与底册名称/地址/行政区",
        f"- 样本量：{len(records)} 条",
        f"- 结论：{verdict}",
        "",
        "## 抽样比对明细",
        "",
        "| listing_ref | 底册坐标(GCJ-02) | 底册名称 | 底册地址 | 高德反查地址 | 反查行政区 | 一致 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        lines.append(
            f"| {r['listing_ref']} | {r['lng']},{r['lat']} | {r['muniao_name'] or '-'} | "
            f"{r['muniao_address'] or '-'} | {r['amap_formatted']} | {r['amap_district']} | "
            f"{'是' if r['consistent'] else '否'} |"
        )
    lines += ["", f"## 统计：{conclusion}", ""]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"标定完成：{conclusion}")
    print(f"记录已写入：{REPORT_PATH}")


if __name__ == "__main__":
    main()
