# -*- coding: utf-8 -*-
"""M12 C 线 PaiQiApi 排期信号小批量试跑（58 家有评分房源，3 天观察期）。

纪律：
- 样本冻结在 data/paiqi/pilot_sample.json（可见集 ∩ 在册 ∩ 有评分有点评）；
- 每家 1 次 POST /Home/PaiQiApi?id=，间隔 4~5 秒，全程约 4~5 分钟；
- 原始响应落盘 data/paiqi/YYYYMMDD/{listing_id}.json；
- 当日汇总 data/paiqi/summary_YYYYMMDD.json；
- 试跑期不写数据库任何表；
- 熔断：任一请求 302/验证码/200 但非 JSON → 立即中止，退出码 2；
  连续 3 家超时或异常 → 熔断，退出码 2；
- 正常完成打印 DONE: 行、退出码 0；今日已跑打印 SKIP: 行、退出码 0；
- 试跑期至 PILOT_UNTIL（含）为止，过期自动 SKIP。

日志：logger muniao.paiqi，TimedRotatingFileHandler 进 logs/crawler.log，格式与既有采集一致。
"""
import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("muniao.paiqi")

API_URL = "https://www.muniao.com/Home/PaiQiApi?id={ref}"
REFERER = "https://www.muniao.com/room/{ref}.html"
SAMPLE_PATH = os.path.join("data", "paiqi", "pilot_sample.json")
OUT_DIR_TMPL = os.path.join("data", "paiqi", "{stamp}")
SUMMARY_TMPL = os.path.join("data", "paiqi", "summary_{stamp}.json")

PILOT_UNTIL = "2026-09-05"          # 试跑最后一天（含），过期自动 SKIP
INTERVAL_MIN = 4.0                  # 间隔下限（秒），纪律要求 4~5 秒不许加密
INTERVAL_MAX = 5.0
TIMEOUT = 25                        # 单次请求超时（秒）
MAX_CONSEC_TIMEOUT = 3              # 连续超时/异常熔断阈值

CAPTCHA_MARKERS = ("LimitingCaptcha", "验证码", "captcha")
REDIRECT_CODES = (301, 302, 303, 307, 308)


def setup_logging():
    """与 muniao_crawler.bootstrap 相同的格式与落点，但不构造数据库/HTTP 三大件。"""
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    root = logging.getLogger("muniao")
    root.setLevel(logging.INFO)
    if not root.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        fh = TimedRotatingFileHandler(
            os.path.join(HERE, "logs", "crawler.log"),
            when="midnight", backupCount=30, encoding="utf-8")
        fh.setFormatter(fmt)
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(ch)


# ---------- 纯函数（离线可测） ----------

def flatten_month_list(payload):
    """month_list → 逐日记录列表：[{date, sameroom, priceday, before}]，date 为 ISO 字符串。"""
    days = []
    for month in payload.get("month_list") or []:
        for d in month.get("list") or []:
            try:
                iso = "%04d-%02d-%02d" % (int(d["year"]), int(d["month"]), int(d["date"]))
            except (KeyError, TypeError, ValueError):
                continue
            days.append({
                "date": iso,
                "sameroom": d.get("sameroom"),
                "priceday": d.get("priceday"),
                "before": d.get("before"),
            })
    days.sort(key=lambda r: r["date"])
    return days


def summarize_days(days, today):
    """汇总：未来 7/30 天已租天数（sameroom==0）、未来 30 天日价区间。

    「未来」= 日期严格大于 today 且 before 标记不为 1。
    """
    future = [d for d in days
              if d["date"] > today and d.get("before") in (0, "0", None, False)]
    w7 = future[:7]
    w30 = future[:30]
    rented_7d = sum(1 for d in w7 if d.get("sameroom") == 0)
    rented_30d = sum(1 for d in w30 if d.get("sameroom") == 0)
    prices = [d["priceday"] for d in w30
              if isinstance(d.get("priceday"), (int, float)) and d["priceday"] > 0]
    return {
        "rented_7d": rented_7d,
        "rented_30d": rented_30d,
        "days_total": len(future),
        "price_min_30d": min(prices) if prices else None,
        "price_max_30d": max(prices) if prices else None,
    }


def classify_response(http_code, body, curl_failed=False):
    """响应分类（熔断判定核心）：
    返回 "blocked"（通道被拦，立即熔断）/ "ok" / "fail"（计入连续失败）。
    """
    if curl_failed or http_code in (-1, 0, None):
        return "fail"  # 超时/网络异常
    if http_code in REDIRECT_CODES:
        return "blocked"
    if http_code != 200:
        return "fail"
    text = body or ""
    if any(m in text for m in CAPTCHA_MARKERS):
        return "blocked"
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return "blocked"  # 200 但非 JSON：视为通道被拦
    if not isinstance(payload, dict) or "month_list" not in payload:
        return "blocked"
    return "ok"


# ---------- 在线执行 ----------

def _curl_post(url, referer, timeout):
    """curl 子进程发 POST（目标站 WAF 拦 python TLS 指纹，curl 稳定放行，沿用既有纪律）。
    必须显式空 body（Content-Length: 0），否则服务端 411。
    返回 (http_code:int, body:str, curl_failed:bool)。
    """
    jar = os.path.join("data", "cookies.txt")
    cmd = ["curl", "-s", "--compressed", "-X", "POST",
           "-H", "Content-Length: 0",
           "-A", "满房帮爬虫/1.0",
           "-H", "Referer: " + referer,
           "--max-time", str(timeout),
           "-c", jar, "-b", jar,
           "-w", "\n%{http_code}", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout + 10)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("curl 执行异常: %s", exc)
        return -1, "", True
    body, _, code = (r.stdout or "").rpartition("\n")
    if r.returncode != 0:  # 28=超时 7=连接失败等
        log.warning("curl 退出码 %s（28=超时）stderr=%s", r.returncode, (r.stderr or "")[:120])
        try:
            return int(code), body, True
        except ValueError:
            return -1, body, True
    try:
        return int(code), body, False
    except ValueError:
        return -1, body, True


def run_pilot(today=None):
    """主流程。返回退出码：0 正常（含 SKIP），2 熔断，1 自身异常。"""
    os.chdir(HERE)
    setup_logging()
    today = today or date.today().isoformat()
    stamp = today.replace("-", "")

    if today > PILOT_UNTIL:
        log.info("试跑期已于 %s 结束，自动跳过", PILOT_UNTIL)
        print("SKIP: pilot expired after %s" % PILOT_UNTIL)
        return 0

    summary_path = SUMMARY_TMPL.format(stamp=stamp)
    if os.path.exists(summary_path):
        log.info("今日汇总已存在 %s，跳过", summary_path)
        print("SKIP: already_done_today")
        return 0

    if not os.path.exists(SAMPLE_PATH):
        log.error("样本文件缺失: %s", SAMPLE_PATH)
        return 1
    sample = json.load(open(SAMPLE_PATH, encoding="utf-8"))
    items = sample["items"]
    log.info("PaiQi 试跑启动：样本 %d 家，间隔 %.1f~%.1f 秒", len(items), INTERVAL_MIN, INTERVAL_MAX)

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
        url = API_URL.format(ref=ref)
        code, body, curl_failed = _curl_post(url, REFERER.format(ref=ref), TIMEOUT)
        verdict = classify_response(code, body, curl_failed)

        if verdict == "blocked":
            log.error("通道被拦（http=%s ref=%s），立即熔断中止", code, ref)
            log.error("已完成 %d/%d，成功 %d，失败 %d，用时 %.0f 秒",
                      idx, len(items), len(results), len(failures), time.time() - started)
            return 2

        if verdict == "fail":
            consec_fail += 1
            failures.append({"listing_ref": ref, "http_code": code})
            log.warning("请求失败 ref=%s http=%s（连续失败 %d/%d）",
                        ref, code, consec_fail, MAX_CONSEC_TIMEOUT)
            if consec_fail >= MAX_CONSEC_TIMEOUT:
                log.error("连续 %d 家超时/异常，熔断中止", MAX_CONSEC_TIMEOUT)
                log.error("已完成 %d/%d，成功 %d，失败 %d，用时 %.0f 秒",
                          idx + 1, len(items), len(results), len(failures), time.time() - started)
                return 2
            continue

        consec_fail = 0
        raw_path = os.path.join(out_dir, "%s.json" % ref)
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(body)  # 原始响应原文落盘
        payload = json.loads(body)
        days = flatten_month_list(payload)
        agg = summarize_days(days, today)
        results.append({
            "listing_ref": ref,
            "name": it.get("name"),
            "sampled_at": datetime.now().isoformat(timespec="seconds"),
            **agg,
        })
        log.info("ref=%s ok：未来7天已租 %d 天，未来30天已租 %d 天，日价 %s~%s",
                 ref, agg["rented_7d"], agg["rented_30d"],
                 agg["price_min_30d"], agg["price_max_30d"])

    summary = {
        "date": today,
        "source": "PaiQiApi 试跑（M12 C 线）",
        "sample_count": len(items),
        "ok_count": len(results),
        "fail_count": len(failures),
        "elapsed_seconds": round(time.time() - started, 1),
        "failures": failures,
        "items": results,
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    log.info("试跑完成：%d/%d 成功，用时 %.0f 秒，汇总 %s",
             len(results), len(items), time.time() - started, summary_path)
    print("DONE: paiqi pilot %d/%d ok, %d failed" % (len(results), len(items), len(failures)))
    return 0


if __name__ == "__main__":
    sys.exit(run_pilot())
