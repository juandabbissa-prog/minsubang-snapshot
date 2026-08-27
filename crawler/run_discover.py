# -*- coding: utf-8 -*-
"""一键运行：发现层（每周三/周日凌晨，见 README 调度方案）。
用法: python run_discover.py [--max-pages N]"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from muniao_crawler import bootstrap, discover  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    cfg, client, store, alert_cb = bootstrap.setup(
        os.path.dirname(os.path.abspath(__file__)))
    try:
        result = discover.run(client, store, cfg,
                              max_pages=args.max_pages, alert_cb=alert_cb)
        print("RESULT:", result)
    finally:
        store.close()


if __name__ == "__main__":
    main()
