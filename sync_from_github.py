# -*- coding: utf-8 -*-
"""从 GitHub data 分支同步云端采集的快照库到本机（方案 A：云端每日定时采集）。

逻辑：
1. 下载 data 分支最新 muniao.db 到临时文件（public 仓库 raw 直连，无需 token）。
2. 云端库比本机新 → 备份本机库后替换。
3. 替换后回捞本机独有表（合并式同步，2026-09-03 丢表事故修复）：
   manual_* 三张 / availability_probe / paiqi_daily_summary 为本机（或本机先行）
   拥有的表，云端库没有；整库替换会把它们擦掉，必须从替换前备份回捞。
   回捞只补「新库里缺失」的表，绝不覆盖新库已有同名表。
4. 云端已含今天全量快照（>=300 行）→ 写本机当天完成标记，本机调度自动跳过，不重复采集。
5. 任何失败均不中断本机采集链路（本机照常自己采）。

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
# 本脚本位于 minsubang/repo-tmp/，资料库与 repo-tmp 同级，需向上一级
LOCAL_DB = os.path.join(
    BASE_DIR, "..", "资料库", "04_开发交付", "M1_数据采集器", "crawler", "data", "muniao.db"
)
MARKER_DIR = os.path.join(
    BASE_DIR, "..", "资料库", "04_开发交付", "M1_数据采集器", "crawler", "logs"
)
TMP_DB = os.path.join(BASE_DIR, "muniao_cloud_tmp.db")
MIN_ROWS_TODAY = 300

# 本机独有表（云端 data 分支库不包含，整库替换后必须回捞）
LOCAL_OWNED_TABLES = [
    "manual_snapshot", "manual_listing", "manual_upload_batch",  # M16 人工补录
    "availability_probe",   # M12 旧探测表（演示数据，上屏前保留）
    "paiqi_daily_summary",  # M12 C 线排期信号日采（本地补轨先行建表）
]


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


def restore_local_owned_tables(db_path, bak_path):
    """整库替换后，从替换前备份回捞本机独有表（结构 + 数据 + 自增序号）。

    只补新库中缺失的表；新库已有同名表（如云端也开始写该表）一律跳过，
    绝不覆盖。返回实际回捞的表名列表。任何异常向上抛由调用方降级处理。
    """
    if not os.path.exists(bak_path):
        return []
    restored = []
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("PRAGMA busy_timeout = 10000")
        con.execute("ATTACH DATABASE ? AS bak", (bak_path,))
        try:
            for t in LOCAL_OWNED_TABLES:
                exists = con.execute(
                    "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?",
                    (t,)).fetchone()
                if exists:
                    continue
                row = con.execute(
                    "SELECT sql FROM bak.sqlite_master WHERE type='table' AND name=?",
                    (t,)).fetchone()
                if not row or not row[0]:
                    continue  # 备份里也没有（该表尚未诞生），正常跳过
                con.execute(row[0])
                con.execute('INSERT INTO main."%s" SELECT * FROM bak."%s"' % (t, t))
                seq = con.execute(
                    "SELECT seq FROM bak.sqlite_sequence WHERE name=?", (t,)).fetchone()
                if seq:
                    con.execute(
                        "INSERT OR REPLACE INTO main.sqlite_sequence (name, seq)"
                        " VALUES (?, ?)", (t, seq[0]))
                restored.append(t)
            con.commit()
        finally:
            con.execute("DETACH DATABASE bak")
    finally:
        con.close()
    return restored


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
            # 合并式同步：回捞本机独有表（丢表事故修复，2026-09-03）
            try:
                restored = restore_local_owned_tables(LOCAL_DB, LOCAL_DB + ".bak")
            except (sqlite3.Error, OSError) as exc:
                restored = []
                print("WARN: 本机独有表回捞失败（旧备份 %s 仍在，可人工回捞）: %s"
                      % (LOCAL_DB + ".bak", exc))
            result["synced"] = True
            result["restored_tables"] = restored
            print(f"SYNC: 云端库 {cloud_latest} 较本机 {local_latest} 新，已同步。"
                  f"回捞本机独有表 {len(restored)} 张: {restored}")
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
