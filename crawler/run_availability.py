# -*- coding: utf-8 -*-
"""M12 · 竞对可订日历探测入口。

**放量闸门未过，本脚本暂不接入任何调度，手动执行需主控许可。**

用法:
  python run_availability.py --print-windows [--today YYYY-MM-DD]
      离线干跑：只按 m12_windows.json 计算并打印窗口日期，零网络请求。
  python run_availability.py [--max-pages N] [--windows name1,name2]
      实跑探测（需主控许可）：bootstrap → 读窗口配置 → ensure_table →
      逐 enabled 窗口 provider.probe_window → record_probe → 打印 RESULT。

开发：K3 · 2026-08-31
"""
import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from muniao_crawler import availability  # noqa: E402  （纯离线模块，导入不触网）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WINDOWS_CFG_PATH = os.path.join(BASE_DIR, "m12_windows.json")

WEEKDAY_CN = "一二三四五六日"


def load_windows_cfg(path=WINDOWS_CFG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def enabled_windows(wcfg, only=None):
    """按配置顺序产出 (窗口名, 窗口配置)；only 为窗口名列表时只取指定窗口。"""
    out = []
    for name, w in wcfg["windows"].items():
        if not w.get("enabled", False):
            continue
        if only and name not in only:
            continue
        out.append((name, w))
    return out


def parse_only(text):
    """--windows a,b → [a, b]；缺省 None 表示全部 enabled 窗口。"""
    if not text:
        return None
    return [p.strip() for p in text.split(",") if p.strip()] or None


def print_windows(wcfg, only, today):
    for name, w in enabled_windows(wcfg, only):
        d1, d2 = availability.window_dates(
            today, w["start_offset_days"], w["end_offset_days"])
        wd = WEEKDAY_CN[date.fromisoformat(d1).weekday()]
        print(f"{name}: 入住 {d1}（周{wd}） 退房 {d2}  {w.get('description', '')}")


def run_probes(wcfg, only, max_pages):
    """实跑路径：会发起网络请求，须主控许可。今天不接线任何调度。"""
    from muniao_crawler import bootstrap
    cfg, client, store, alert_cb = bootstrap.setup(BASE_DIR)
    try:
        if not client.check_robots():
            if alert_cb:
                alert_cb("状态码异常", "robots.txt 获取失败或被新增禁止路径，M12 探测中止")
            return {"aborted": True}
        availability.ensure_table(store)
        refs = store.active_listings(cfg["platform"])  # 只读底册，零写入
        provider = availability.ListPresenceProvider(client, cfg, max_pages=max_pages)
        today = date.today()
        summary = {"today": today.isoformat(), "refs": len(refs), "windows": {}}
        for name, w in enabled_windows(wcfg, only):
            d1, d2 = availability.window_dates(
                today, w["start_offset_days"], w["end_offset_days"])
            results = provider.probe_window(refs, d1, d2)
            tally = availability.record_probe(
                store, results, {"window_start": d1, "window_end": d2})
            summary["windows"][name] = {"window": [d1, d2], **tally}
        # 纪律（补订 I）：既有表零写入——不写 crawl_log，探测痕迹只落 availability_probe
        return summary
    finally:
        store.close()


def main():
    ap = argparse.ArgumentParser(
        description="M12 竞对可订日历探测（放量闸门未过，不接入调度，手动执行需主控许可）")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="覆盖 config.max_pages（冒烟用）")
    ap.add_argument("--windows", type=str, default=None,
                    help="只跑指定窗口名，逗号分隔；缺省全部 enabled 窗口")
    ap.add_argument("--print-windows", action="store_true",
                    help="离线干跑：只计算并打印窗口日期，零网络请求")
    ap.add_argument("--today", type=str, default=None,
                    help="模拟「今天」（YYYY-MM-DD），仅配合 --print-windows")
    args = ap.parse_args()

    wcfg = load_windows_cfg()
    only = parse_only(args.windows)
    unknown = [n for n in (only or []) if n not in wcfg["windows"]]
    if unknown:
        raise SystemExit(f"未知窗口名: {unknown}（可选: {list(wcfg['windows'])}）")

    if args.print_windows:
        today = date.fromisoformat(args.today) if args.today else date.today()
        print_windows(wcfg, only, today)
        return

    summary = run_probes(wcfg, only, args.max_pages)
    print("RESULT:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
