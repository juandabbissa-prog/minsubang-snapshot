# -*- coding: utf-8 -*-
"""一键运行：房东体量采集（M2-变更-004 补充·体量维数据源）。

两阶段：
  阶段1 host_id 补采：对缺房东 ID 的房源抓详情页，解析 #chat@房东ID 回写底册
       （只写 host_id，不动快照表，与每日追踪层无冲突）；
  阶段2 套数采集：按房东 ID 去重抓房东主页（/fangdong/{id}/，robots 未禁止，
       2026-08-28 逐条比对），房源区 data-id 去重计数，回写该房东名下全部房源。

频率/间隔/UA 复用既有 HttpClient（REQUEST_INTERVAL 3.5s、实名 UA），
robots 检查复用 client.check_robots()；支持 --limit 分段补采（可断点续跑，
已采的自动跳过）。
用法: python run_hosts.py [--limit N] [--skip-details]
"""
import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from muniao_crawler import bootstrap, parse  # noqa: E402

log = logging.getLogger("muniao.hosts")


def run(client, store, cfg, alert_cb, limit=None, skip_details=False) -> dict:
    started = datetime.now().isoformat(timespec="seconds")
    if not client.check_robots():
        alert_cb("状态码异常", "robots.txt 获取失败或被新增禁止路径，房东体量采集中止")
        return {"aborted": True}

    detail_ok = detail_fail = host_ok = host_fail = 0

    # 阶段1：host_id 补采
    if not skip_details:
        refs = store.listings_missing_host(cfg["platform"])
        if limit:
            refs = refs[:limit]
        for i, rid in enumerate(refs, 1):
            code, html = client.get(cfg["detail_url"].replace("{room_id}", rid),
                                    referer=cfg["list_page1"])
            if code == 200:
                rec = parse.parse_detail_page(html, rid)
                if rec.get("host_id"):
                    store.update_listing_host(cfg["platform"], rid,
                                              host_id=rec["host_id"])
                    detail_ok += 1
                else:
                    detail_fail += 1
                    log.warning("详情页无房东ID: %s", rid)
                # 顺带回写设施/房量（详情页已在手，不浪费响应）
                if rec.get("facility_count") is not None or rec.get("capacity") is not None:
                    store.update_listing_metrics(cfg["platform"], rid,
                                                 facility_count=rec.get("facility_count"),
                                                 room_capacity=rec.get("capacity"))
            else:
                detail_fail += 1
                log.warning("详情页 HTTP %s: %s", code, rid)
            if i % 20 == 0:
                store.commit()
                log.info("host_id 补采进度 %d/%d", i, len(refs))
        store.commit()

    # 阶段2：房东主页套数
    hosts = store.hosts_missing_count(cfg["platform"])
    if limit and skip_details:
        hosts = hosts[:limit]
    host_url = cfg.get("host_url", "https://www.muniao.com/fangdong/{host_id}/")
    for i, hid in enumerate(hosts, 1):
        code, html = client.get(host_url.replace("{host_id}", hid),
                                referer=cfg["list_page1"])
        if code == 200:
            count = parse.parse_host_page(html).get("listing_count")
            if count:
                store.update_host_count(cfg["platform"], hid, count)
                host_ok += 1
            else:
                host_fail += 1
                log.warning("房东页未解析到房源: host=%s", hid)
        else:
            host_fail += 1
            log.warning("房东页 HTTP %s: host=%s", code, hid)
        if i % 20 == 0:
            store.commit()
            log.info("套数采集进度 %d/%d", i, len(hosts))
    store.commit()

    detail = {"detail_ok": detail_ok, "detail_fail": detail_fail,
              "host_ok": host_ok, "host_fail": host_fail,
              "hosts_total": len(hosts)}
    store.log_run("host_scale", started, datetime.now().isoformat(timespec="seconds"),
                  client.stats["total"], client.stats["ok"], client.stats["fail"], detail)
    log.info("房东体量采集完成: %s", detail)
    return detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip-details", action="store_true",
                    help="跳过 host_id 补采，只采房东主页套数")
    args = ap.parse_args()
    cfg, client, store, alert_cb = bootstrap.setup(
        os.path.dirname(os.path.abspath(__file__)))
    try:
        result = run(client, store, cfg, alert_cb,
                     limit=args.limit, skip_details=args.skip_details)
        print("RESULT:", result)
    finally:
        store.close()


if __name__ == "__main__":
    main()
