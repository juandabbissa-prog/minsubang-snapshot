# -*- coding: utf-8 -*-
"""追踪层：对已入池房源逐一抓详情页，落每日快照（幂等）。
告警场景：HTTP 非 200（首次即报）、单日失败率 >5%、耗时 >3h、数据量骤降 >30%（7 日均值）。"""
import logging
import time
from datetime import datetime

from . import parse

log = logging.getLogger("muniao.track")


def run(client, store, cfg, alert_cb, limit=None, only_refs=None):
    started_dt = datetime.now()
    started = started_dt.isoformat(timespec="seconds")
    if not client.check_robots():
        alert_cb("状态码异常", "robots.txt 获取失败或被新增禁止路径，追踪层中止")
        return {"aborted": True}

    refs = only_refs or store.active_listings(cfg["platform"])
    if limit:
        refs = refs[:limit]
    total = len(refs)
    ok = fail = 0
    alerted_non200 = False
    deadline = time.time() + cfg["track_timeout_hours"] * 3600

    for i, rid in enumerate(refs, 1):
        if time.time() > deadline:
            alert_cb("采集超时", f"追踪层耗时超过 {cfg['track_timeout_hours']}h，已完成 {i}/{total}")
            break
        code, html = client.get(cfg["detail_url"].replace("{room_id}", rid),
                                referer=cfg["list_page1"])
        if code == 200:
            rec = parse.parse_detail_page(html, rid)
            store.insert_snapshot(cfg["platform"], rid, rec["price"],
                                  rec["rating"], rec["review_count"])
            if rec["name"] or rec["lng"]:
                store.update_listing_profile(cfg["platform"], rid, rec["name"],
                                             rec["address"], rec["lng"], rec["lat"],
                                             rec.get("region_code"))
            if rec.get("facility_count") is not None or rec.get("capacity") is not None:
                store.update_listing_metrics(cfg["platform"], rid,
                                             facility_count=rec.get("facility_count"),
                                             room_capacity=rec.get("capacity"))
            if rec.get("host_id"):
                store.update_listing_host(cfg["platform"], rid,
                                          host_id=rec["host_id"])
            ok += 1
        else:
            fail += 1
            if not alerted_non200:
                alerted_non200 = True
                alert_cb("状态码异常", f"详情页 HTTP {code}: {rid}")
        if i % 20 == 0:
            store.commit()
            log.info("进度 %d/%d ok=%d fail=%d", i, total, ok, fail)
    store.commit()

    snap_count = store.count_snapshots(cfg["platform"])
    fail_rate = (fail / total * 100) if total else 0
    if total and fail_rate > cfg["alert_fail_rate_pct"]:
        alert_cb("采集失败率", f"单日失败率 {fail_rate:.1f}%（{fail}/{total}）超过 {cfg['alert_fail_rate_pct']}%")
    avg7 = store.seven_day_avg(cfg["platform"])
    if avg7 and snap_count < avg7 * (1 - cfg["alert_drop_pct"] / 100):
        alert_cb("数据量骤降", f"今日快照 {snap_count} 较 7 日均值 {avg7:.0f} 下降超 {cfg['alert_drop_pct']}%")

    finished = datetime.now().isoformat(timespec="seconds")
    detail = {"refs": total, "ok": ok, "fail": fail, "snapshot_count": snap_count}
    store.log_run("track", started, finished, client.stats["total"],
                  client.stats["ok"], client.stats["fail"], detail)
    log.info("追踪层完成: %s", detail)
    return detail
