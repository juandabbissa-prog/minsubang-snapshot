# -*- coding: utf-8 -*-
"""发现层：木鸟大连列表翻页，发现新房源、更新 last_seen。
翻页规则（实测留档）：第 1 页裸 URL；N≥2 用 null-0-...-N.html + 明日/后日日期。"""
import logging
from datetime import datetime, timedelta

from . import parse

log = logging.getLogger("muniao.discover")


def run(client, store, cfg, max_pages=None, alert_cb=None):
    started = datetime.now().isoformat(timespec="seconds")
    if not client.check_robots():
        if alert_cb:
            alert_cb("状态码异常", "robots.txt 获取失败或被新增禁止路径，发现层中止")
        return {"aborted": True}
    d1 = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    d2 = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    limit = max_pages or cfg["max_pages"]
    seen, empty_streak, pages_done = set(), 0, 0

    for n in range(1, limit + 1):
        if n == 1:
            url = cfg["list_page1"]
        else:
            url = (cfg["list_page_n"].replace("{page}", str(n))
                   .replace("{d1}", d1).replace("{d2}", d2))
        code, html = client.get(url, referer=cfg["list_page1"])
        if code != 200:
            empty_streak += 1
        else:
            ids = parse.parse_list_page(html)
            new = [i for i in ids if i not in seen]
            log.info("第%d页: ids=%d new=%d", n, len(ids), len(new))
            for rid in ids:
                seen.add(rid)
                store.upsert_listing(cfg["platform"], rid)
            store.commit()
            empty_streak = 0 if new else empty_streak + 1
        pages_done = n
        if empty_streak >= cfg["stop_after_empty_pages"]:
            log.info("连续 %d 页无新房源，翻页结束", empty_streak)
            break

    finished = datetime.now().isoformat(timespec="seconds")
    detail = {"pages": pages_done, "unique_rooms": len(seen)}
    store.log_run("discover", started, finished, client.stats["total"],
                  client.stats["ok"], client.stats["fail"], detail)
    log.info("发现层完成: %s", detail)
    return detail
