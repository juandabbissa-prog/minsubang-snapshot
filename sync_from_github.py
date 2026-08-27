# -*- coding: utf-8 -*-
"""从 GitHub data 分支同步云端采集的快照库到本机（方案 A：云端每日定时采集）。

逻辑：
1. 下载 data 分支最新 muniao.db 到临时文件（public 仓库 raw 直连，无需 token）。
2. 云端库比本机新 → 备份本机库后替换。
3. 云端已含今天全量快照（>=300 行）→ 写本机当天完成标记，本机调度自动跳过，不重复采集。
4. 任何失败均不中断本机采集链路（本机照常自己采）。

用法：python sync_from_github.py
末行输出 JSON：{"artifact": {"synced": true/false, "cloud_date": "...", "today_done": true/false}}
"""
import json
import os
import shutil
import sqlite3
import sys
import urllib.request
from datetime import date

RAW_URL = "https://raw.githubusercontent.com/juandabbissa-prog/minsubang-snapshot/data/muniao.db"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_DB = os.path.join(
    BASE_DIR, "资料库", "04_开发交付", "M1_数据采集器", "crawler", "data", "muniao.db"
)
MARKER_DIR = os.path.join(
    BASE_DIR, "资料库", "04_开发交付", "M1_数据采集器", "crawler", "logs"
)
TMP_DB = os.path.join(BASE_DIR, "muniao_cloud_tmp.db")
MIN_ROWS_TODAY = 300


def db_stats(path):
    con = sqlite3.connect(path)
    try:
        latest = con.execute("SELECT MAX(snapshot_date) FROM snapshot_price").fetchone()[0]
        today = date.today().isoformat()
        today_rows = con.execute(
            "SELECT COUNT(*) FROM snapshot_price WHERE snapshot_date=?", (today,)
        ).fetchone()[0]
        return latest, today_rows
    finally:
        con.close()


def main():
    result = {"synced": False, "cloud_date": None, "today_done": False}
    try:
        req = urllib.request.Request(RAW_URL, headers={"User-Agent": "minsubang-sync/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(TMP_DB, "wb") as f:
            shutil.copyfileobj(resp, f)

        cloud_latest, cloud_today = db_stats(TMP_DB)
        result["cloud_date"] = cloud_latest

        local_latest = None
        if os.path.exists(LOCAL_DB):
            local_latest, _ = db_stats(LOCAL_DB)

        if cloud_latest and (local_latest is None or cloud_latest > local_latest):
            shutil.copy2(LOCAL_DB, LOCAL_DB + ".bak")
            shutil.move(TMP_DB, LOCAL_DB)
            result["synced"] = True
            print(f"SYNC: 云端库 {cloud_latest} 较本机 {local_latest} 新，已同步。")
        else:
            print(f"SKIP: 云端 {cloud_latest} 不新于本机 {local_latest}，无需同步。")

        if cloud_today >= MIN_ROWS_TODAY:
            os.makedirs(MARKER_DIR, exist_ok=True)
            marker = os.path.join(MARKER_DIR, f"snapshot_done_{date.today().isoformat()}")
            with open(marker, "w", encoding="utf-8") as f:
                f.write(date.today().isoformat())
            result["today_done"] = True
            print(f"DONE: 云端今日快照 {cloud_today} 行，本机标记已写，本机调度将跳过。")
    except Exception as exc:
        print(f"WARN: 云端同步失败（本机照常采集）: {type(exc).__name__}: {exc}")
    finally:
        if os.path.exists(TMP_DB):
            try:
                os.remove(TMP_DB)
            except OSError:
                pass
    print(json.dumps({"artifact": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
