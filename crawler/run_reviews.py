# -*- coding: utf-8 -*-
"""一键运行：点评口碑关键词分析（随每日快照执行，也可单独冒烟）。
用法: python run_reviews.py [--limit N] [--refs id1,id2,...]"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from muniao_crawler import bootstrap, reviews  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--refs", type=str, default=None,
                    help="逗号分隔的 roomId，只分析这些房源")
    args = ap.parse_args()
    cfg, client, store, alert_cb = bootstrap.setup(
        os.path.dirname(os.path.abspath(__file__)))
    try:
        only = args.refs.split(",") if args.refs else None
        result = reviews.run(client, store, cfg, alert_cb,
                             limit=args.limit, only_refs=only)
        print("RESULT:", result)
    finally:
        store.close()


if __name__ == "__main__":
    main()
