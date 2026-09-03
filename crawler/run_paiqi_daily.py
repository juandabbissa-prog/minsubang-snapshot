# -*- coding: utf-8 -*-
"""M12 C 线正式接线：PaiQiApi 排期信号全量日采（415 家在册房源）。

启用闸（双重）：
- 本脚本闸：crawler/paiqi_enabled.flag 文件不存在 → 打印「未启用」退出码 0，
  零请求、零数据库触碰；
- 云端闸：workflow 先检查 data 分支 paiqi_enabled.flag，存在才下载到
  crawler/ 再调用本脚本（见 .github/workflows/daily-snapshot.yml）。

纪律（与试跑同口径）：
- 每家 1 次 POST /Home/PaiQiApi?id=，间隔 4~5 秒，415 家全程约 32~35 分钟；
- 熔断：302/验证码/200 非 JSON 立即中止退出码 2；连续 3 家超时/异常熔断退出码 2；
- 熔断当天零落库（整批事务只在全量成功后提交），原始 JSON 保留供排查；
- 原始响应落盘 data/paiqi/YYYYMMDD/{listing_ref}.json；
- 汇总写 paiqi_daily_summary 表（INSERT OR REPLACE，幂等可重跑）；
- 建表 DDL 内置幂等，云端库首次启用自动建表。

退出码：0 正常（含未启用 SKIP / 今日已采 SKIP），1 自身异常，2 熔断。
日志：logger muniao.paiqi.daily，进 logs/crawler.log 同格式。
"""
import json
import os
import random
import sqlite3
import sys
import time
from datetime import date, datetime

import run_paiqi_pilot as pilot  # 复用试跑已验证的纯函数与传输层

log = pilot.log.getChild("daily")

HERE = os.path.dirname(os.path.abspath(__file__))
FLAG_PATH = os.path.join(HERE, "paiqi_enabled.flag")   # 启用闸：父代理在监理批准后手工放置
DB_PATH = os.path.join(HERE, "data", "muniao.db")
OUT_DIR_TMPL = pilot.OUT_DIR_TMPL
SUMMARY_TMPL = pilot.SUMMARY_TMPL

INTERVAL_MIN = pilot.INTERVAL_MIN   # 4.0，纪律锁死
INTERVAL_MAX = pilot.INTERVAL_MAX   # 5.0
TIMEOUT = pilot.TIMEOUT
MAX_CONSEC_TIMEOUT = pilot.MAX_CONSEC_TIMEOUT

DDL = """
CREATE TABLE IF NOT EXISTS paiqi_daily_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_ref TEXT NOT NULL,
    probe_date TEXT NOT NULL,
    rented_7d INTEGER,
    rented_30d INTEGER,
    price_min_30d REAL,
    price_max_30d REAL,
    days_total INTEGER,
    raw_path TEXT,
    probed_at TEXT,
    UNIQUE (listing_ref, probe_date)
)
"""

INSERT_SQL = """
INSERT OR REPLACE INTO paiqi_daily_summary
(listing_ref, probe_date, rented_7d, rented_30d, price_min_30d, price_max_30d,
 days_total, raw_path, probed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def enabled(flag_path=FLAG_PATH):
    return os.path.exists(flag_path)


def load_refs(db_path=DB_PATH):
    """415 家在册房源（全量，不按评分过滤）。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT listing_ref, name FROM listing_base WHERE platform = 'muniao'"
        ).fetchall()
    finally:
        conn.close()
    return [{"listing_ref": str(r[0]), "name": r[1]} for r in rows]


def write_summary_table(db_path, records, today):
    """整批事务写入；records 为空不写。busy_timeout 沿用 M16 短连接纪律。"""
    if not records:
        return 0
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute(DDL)
        conn.executemany(INSERT_SQL, [
            (r["listing_ref"], today, r["rented_7d"], r["rented_30d"],
             r["price_min_30d"], r["price_max_30d"], r["days_total"],
             r["raw_path"], r["probed_at"]) for r in records
        ])
        conn.commit()
        return len(records)
    finally:
        conn.close()


def run_daily(today=None, flag_path=FLAG_PATH, db_path=DB_PATH):
    """主流程。返回退出码：0 正常（含闸关 SKIP），1 自身异常，2 熔断。"""
    os.chdir(HERE)
    pilot.setup_logging()

    if not enabled(flag_path):
        log.info("paiqi_enabled.flag 不存在，排期信号日采未启用，跳过")
        print("SKIP: 未启用（paiqi_enabled.flag 不存在）")
        return 0

    today = today or date.today().isoformat()
    stamp = today.replace("-", "")

    # 幂等：今日表内已有全量行则跳过（重跑安全）
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(DDL)
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM paiqi_daily_summary WHERE probe_date = ?",
            (today,)).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM listing_base WHERE platform = 'muniao'"
        ).fetchone()[0]
    finally:
        conn.close()
    if total and n >= total:
        log.info("今日已采 %d/%d，跳过", n, total)
        print("SKIP: already_done_today")
        return 0

    items = load_refs(db_path)
    log.info("PaiQi 日采启动：%d 家，间隔 %.1f~%.1f 秒", len(items), INTERVAL_MIN, INTERVAL_MAX)

    out_dir = OUT_DIR_TMPL.format(stamp=stamp)
    os.makedirs(out_dir, exist_ok=True)

    results = []
    failures = []
    consec_fail = 0
    started = time.time()
    for idx, it in enumerate(items):
        ref = str(it["listing_ref"])
        if idx > 0:
            time.sleep(random.uniform(INTERVAL_MIN, INTERVAL_MAX))
        code, body, curl_failed = pilot._curl_post(
            pilot.API_URL.format(ref=ref), pilot.REFERER.format(ref=ref), TIMEOUT)
        verdict = pilot.classify_response(code, body, curl_failed)

        if verdict == "blocked":
            log.error("通道被拦（http=%s ref=%s），立即熔断中止，本次零落库", code, ref)
            log.error("已完成 %d/%d，成功 %d，失败 %d，用时 %.0f 秒",
                      idx, len(items), len(results), len(failures), time.time() - started)
            return 2

        if verdict == "fail":
            consec_fail += 1
            failures.append({"listing_ref": ref, "http_code": code})
            log.warning("请求失败 ref=%s http=%s（连续失败 %d/%d）",
                        ref, code, consec_fail, MAX_CONSEC_TIMEOUT)
            if consec_fail >= MAX_CONSEC_TIMEOUT:
                log.error("连续 %d 家超时/异常，熔断中止，本次零落库", MAX_CONSEC_TIMEOUT)
                return 2
            continue

        consec_fail = 0
        raw_path = os.path.join("data", "paiqi", stamp, "%s.json" % ref)  # 落库用规范相对路径
        with open(os.path.join(out_dir, "%s.json" % ref), "w", encoding="utf-8") as f:
            f.write(body)
        payload = json.loads(body)
        days = pilot.flatten_month_list(payload)
        agg = pilot.summarize_days(days, today)
        results.append({
            "listing_ref": ref,
            "probe_date": today,
            "rented_7d": agg["rented_7d"],
            "rented_30d": agg["rented_30d"],
            "price_min_30d": agg["price_min_30d"],
            "price_max_30d": agg["price_max_30d"],
            "days_total": agg["days_total"],
            "raw_path": raw_path,
            "probed_at": datetime.now().isoformat(timespec="seconds"),
        })
        if (idx + 1) % 50 == 0:
            log.info("进度 %d/%d，用时 %.0f 秒", idx + 1, len(items), time.time() - started)

    n_written = write_summary_table(db_path, results, today)
    summary = {
        "date": today,
        "source": "PaiQiApi 日采（M12 C 线正式轨）",
        "sample_count": len(items),
        "ok_count": len(results),
        "fail_count": len(failures),
        "db_written": n_written,
        "elapsed_seconds": round(time.time() - started, 1),
        "failures": failures,
    }
    with open(SUMMARY_TMPL.format(stamp=stamp), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    log.info("日采完成：%d/%d 成功，落库 %d 行，用时 %.0f 秒",
             len(results), len(items), n_written, time.time() - started)
    print("DONE: paiqi daily %d/%d ok, %d failed, %d rows written"
          % (len(results), len(items), len(failures), n_written))
    return 0


if __name__ == "__main__":
    sys.exit(run_daily())
