# -*- coding: utf-8 -*-
"""旅顺口区定向补采（变更007·数据层）。

列表页区域入口：https://www.muniao.com/dalian/lushunkou-0-0-0-0-0-0-0-1.html
翻页规律与发现层一致：第 N 页把路径末尾的 -1.html 换成 -N.html，
查询串带明日/后日日期与 tn 参数（同 config.json list_page_n 的日期规则）。

流程：robots 核查 → 翻页发现（upsert 入 listing_base，与发现层同函数）
→ 连续 2 页无新房源或到 30 页上限停 → 对新发现房源立即跑一轮详情采集
（复用追踪层 track.run 的 only_refs 通道，当日快照落 snapshot_price）。
频控/UA/重试全部沿用 http_client 内置纪律，日志进 logs/crawler.log。

用法: python run_lvshun.py [--max-pages N] [--resume-track]
  --resume-track：跳过翻页，读 data/lvshun_new_refs.txt 中尚无当日快照的
  房源续跑详情采集（前台终端超时被打断后的恢复通道）。
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from muniao_crawler import bootstrap, parse, track  # noqa: E402

log = logging.getLogger("muniao.lvshun")

# 旅顺口区列表页（注意拼音为 lushunkou）；{d1}/{d2} 为明日/后日
PAGE1 = ("https://www.muniao.com/dalian/lushunkou-0-0-0-0-0-0-0-1.html"
         "?start_date={d1}&end_date={d2}&tn=mn19091015")
MAX_PAGES = 30
STOP_EMPTY = 2
NEW_REFS_FILE = os.path.join("data", "lvshun_new_refs.txt")


def page_url(n: int, d1: str, d2: str) -> str:
    url = PAGE1.replace("{d1}", d1).replace("{d2}", d2)
    if n > 1:
        url = url.replace("-1.html", f"-{n}.html")
    return url


def _pending_refs(store, cfg) -> list:
    """读新房源清单，剔除已有当日快照的，返回待补详情列表。"""
    if not os.path.exists(NEW_REFS_FILE):
        return []
    with open(NEW_REFS_FILE, encoding="utf-8") as f:
        refs = [line.strip() for line in f if line.strip()]
    today = date.today().isoformat()
    done = {r[0] for r in store.conn.execute(
        "SELECT listing_ref FROM snapshot_price WHERE platform=? AND snapshot_date=?",
        (cfg["platform"], today))}
    return [r for r in refs if r not in done]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    ap.add_argument("--resume-track", action="store_true",
                    help="跳过翻页，续跑新房源详情采集")
    args = ap.parse_args()
    cfg, client, store, alert_cb = bootstrap.setup(
        os.path.dirname(os.path.abspath(__file__)))
    try:
        if args.resume_track:
            refs = _pending_refs(store, cfg)
            log.info("断点续采：待补详情 %d 套", len(refs))
            result = track.run(client, store, cfg, alert_cb, only_refs=refs) \
                if refs else {"refs": 0, "ok": 0, "fail": 0}
            print(f"RESULT: 续采详情 成功 {result.get('ok', 0)} / "
                  f"失败 {result.get('fail', 0)}（剩余待补见 {NEW_REFS_FILE}）")
            return

        if not client.check_robots():
            alert_cb("状态码异常", "robots.txt 获取失败或被新增禁止路径，旅顺补采中止")
            print("RESULT: aborted (robots)")
            return

        d1 = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        limit = min(args.max_pages, MAX_PAGES)

        # ---------------- 翻页发现 ----------------
        started = datetime.now().isoformat(timespec="seconds")
        stat0 = dict(client.stats)
        known = set(store.active_listings(cfg["platform"]))
        seen, new_refs, empty_streak, pages_done = set(), [], 0, 0

        for n in range(1, limit + 1):
            url = page_url(n, d1, d2)
            code, html = client.get(url, referer=cfg["list_page1"])
            if code != 200:
                empty_streak += 1
                log.warning("第%d页 HTTP %s", n, code)
            else:
                cards = parse.parse_list_cards(html)
                page_new = 0
                for c in cards:
                    ref = c["listing_ref"]
                    if ref in seen:
                        continue
                    seen.add(ref)
                    if ref not in known:
                        known.add(ref)
                        new_refs.append(ref)
                        page_new += 1
                    store.upsert_listing(cfg["platform"], ref,
                                         room_capacity=c.get("capacity"))
                store.commit()
                log.info("第%d页: 卡片=%d 新房源=%d", n, len(cards), page_new)
                empty_streak = 0 if page_new else empty_streak + 1
            pages_done = n
            if empty_streak >= STOP_EMPTY:
                log.info("连续 %d 页无新房源，翻页结束", empty_streak)
                break

        finished = datetime.now().isoformat(timespec="seconds")
        detail = {"pages": pages_done, "unique_rooms": len(seen),
                  "new_rooms": len(new_refs)}
        store.log_run("lvshun_discover", started, finished,
                      client.stats["total"] - stat0["total"],
                      client.stats["ok"] - stat0["ok"],
                      client.stats["fail"] - stat0["fail"], detail)
        log.info("旅顺发现完成: %s", detail)
        # 新房源清单落盘：中断后可 --resume-track 续采，也作补采留痕
        with open(NEW_REFS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(new_refs))

        # ---------------- 新房源即时详情采集 ----------------
        track_result = {"refs": 0, "ok": 0, "fail": 0}
        if new_refs:
            log.info("对 %d 套新房源启动详情采集", len(new_refs))
            track_result = track.run(client, store, cfg, alert_cb,
                                     only_refs=new_refs)

        print(f"RESULT: 旅顺新增房源 {len(new_refs)} 套；"
              f"详情采集 成功 {track_result.get('ok', 0)} / "
              f"失败 {track_result.get('fail', 0)}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
