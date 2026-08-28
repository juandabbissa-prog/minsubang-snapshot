# -*- coding: utf-8 -*-
"""每日快照调度包装脚本（M2 开工首日 · 计划任务 + 补跑双保险）。

- 由 Windows 计划任务每日 21:30 唤起（任务已配置"错过计划后尽快启动"）；
- 脚本内当天完成标记：logs/snapshot_done_YYYY-MM-DD 存在且内容为当天日期 → 跳过；
  不存在 → 立即执行追踪层快照（补跑逻辑），成功后写标记。
- 用法：python daily_snapshot.py [--force] [--limit N]
"""
import argparse
import os
import sys
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
MARKER = os.path.join(LOGS_DIR, f"snapshot_done_{date.today().isoformat()}")

sys.path.insert(0, BASE_DIR)
from muniao_crawler import bootstrap, reviews, track  # noqa: E402
import run_hosts  # noqa: E402  采集器根目录脚本（房东体量补齐）


def already_done() -> bool:
    if not os.path.exists(MARKER):
        return False
    try:
        with open(MARKER, "r", encoding="utf-8") as f:
            return f.read().strip() == date.today().isoformat()
    except OSError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略当天标记强制执行")
    ap.add_argument("--limit", type=int, default=None, help="只采集前 N 间（补跑实测用）")
    args = ap.parse_args()

    if not args.force and already_done():
        print(f"SKIP: 当天（{date.today().isoformat()}）快照已完成，无需补跑。")
        return

    print(f"RUN: 开始当天（{date.today().isoformat()}）快照采集……")
    cfg, client, store, alert_cb = bootstrap.setup(BASE_DIR)
    try:
        result = track.run(client, store, cfg, alert_cb, limit=args.limit)
        print("RESULT:", result)
    finally:
        store.close()

    # 只有真实采到数据才写当天完成标记；中止/全失败时不写，
    # 下次唤起（计划任务补跑机制或手动）会再次尝试——补跑双保险的关键
    price_ok = isinstance(result, dict) and not result.get("aborted") and result.get("ok", 0) > 0
    if price_ok:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(MARKER, "w", encoding="utf-8") as f:
            f.write(date.today().isoformat())
        print(f"DONE: 当天完成标记已写入 {MARKER}")
    else:
        print("WARN: 本轮未采到有效数据，不写完成标记，等待下次唤起重试。")

    # 点评口碑关键词分析（M2 新增）：每日随快照执行，内存即时分析、原文绝不落盘；
    # 独立成败，不影响价格快照标记
    print("RUN: 开始点评口碑关键词分析……")
    cfg2, client2, store2, alert_cb2 = bootstrap.setup(BASE_DIR)
    try:
        rresult = reviews.run(client2, store2, cfg2, alert_cb2, limit=args.limit)
        print("REVIEWS:", rresult)
    except Exception as exc:
        print(f"WARN: 点评分析失败（不影响价格快照）: {type(exc).__name__}")
        rresult = None
    finally:
        store2.close()

    # 房东体量补齐（M2-变更-004 补充）：只采缺口——host_id 缺失的房源 +
    # 套数缺失的房东主页；稳态下每日 0 额外请求，新店入池自动补齐。
    # 独立成败，不影响价格快照标记
    print("RUN: 开始房东体量补齐……")
    cfg3, client3, store3, alert_cb3 = bootstrap.setup(BASE_DIR)
    try:
        hresult = run_hosts.run(client3, store3, cfg3, alert_cb3)
        print("HOSTS:", hresult)
    except Exception as exc:
        print(f"WARN: 房东体量补齐失败（不影响价格快照）: {type(exc).__name__}")
        hresult = None
    finally:
        store3.close()

    if price_ok:
        return
    sys.exit(1)


if __name__ == "__main__":
    main()
