# -*- coding: utf-8 -*-
"""M12 补订 A · 竞对可订日历原理前置验证（一次性验证工具，非生产模块）。

原理假设：木鸟列表检索带日期参数时只返回该日期窗口可订的房源；
同一房源在某窗口检索结果中消失 = 该窗口大概率不可订。

冻结口径（任务单补订 A，逐字执行）：
  样本    ≥20 家在架房源 × 未来 7 天 ≥140 个「房源×窗口」组合；
  方法    同一窗口两次独立检索（a/b 两轮，间隔须 ≥20 分钟，由执行人/调度保证）；
  判定    两轮都出现=判可订；仅一轮出现=待确认；两轮都缺席=判不可订；
          任一轮该窗口整体扫描失败=探测失败（不计入分母）；
  通过线  整体吻合率 ≥85% 且「实际可订却判不可订」假阳性率 ≤10%。

阶段用法：
  python m12_principle_check.py --phase select            # 只读行情库选样本
  python m12_principle_check.py --phase a [--max-pages N] [--windows 1,2]
  python m12_principle_check.py --phase b [--max-pages N] [--windows 1,2]
  python m12_principle_check.py --phase report            # 出 matrix.csv + 核对清单
  python m12_principle_check.py --phase verdict           # 出验证报告（需人工 ground_truth.csv）

云端运行（A/B 迁云端轨，2026-09-02 起）：GitHub Actions workflow 手动触发，
请求间隔由环境变量 REQUEST_INTERVAL 控制（云端 7 秒，宁慢勿封）；
单轮请求预算 85 硬闸、≥3 窗口整窗失败自动中止剩余窗口；
RESULT 汇总行走 stdout（Actions 日志可见），窗口 JSON 以 artifacts 回收。

合规说明：全程复用既有 HttpClient（实名 UA + 间隔自律 + 失败重试），
不修改任何既有采集文件；a/b 每轮启动时执行 robots 核查并留档；
单页请求失败（非 200 / 异常）记失败页、不算空页；已存在的轮次文件自动跳过（断点续跑）。

测算人：K3 · 2026-08-30
"""
import argparse
import csv
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

CHECK_DIR = os.path.join("data", "m12_check")
SAMPLE_PATH = os.path.join(CHECK_DIR, "sample.json")
MATRIX_PATH = os.path.join(CHECK_DIR, "matrix.csv")
CHECKLIST_PATH = os.path.join(CHECK_DIR, "核对清单.md")
GROUND_TRUTH_PATH = os.path.join(CHECK_DIR, "ground_truth.csv")
VERDICT_PATH = os.path.join(CHECK_DIR, "验证报告.md")

WINDOW_COUNT = 7            # 未来第 1~7 天单晚窗口
MIN_SAMPLE = 20             # 冻结口径下限
SAMPLE_SIZE = 24            # 多取 4 家防废号
MIN_INTERVAL_MIN = 20       # 两轮独立检索最小间隔（分钟）
PASS_MATCH_RATE = 0.85      # 整体吻合率通过线
PASS_FALSE_POSITIVE = 0.10  # 「实际可订却判不可订」假阳性率通过线
ROUND_REQUEST_BUDGET = 85   # 单轮请求预算硬闸（robots 1 + 7 窗口翻页），超限即中止本轮
ABORT_FAILED_WINDOWS = 3    # 单轮内整窗扫描失败达到此数即中止剩余窗口（检查单第 4 条自动化）

J_BOOKABLE = "判可订"
J_UNBOOKABLE = "判不可订"
J_PENDING = "待确认"
J_FAILED = "探测失败"

log = logging.getLogger("muniao.m12")


# ---------------------------------------------------------------- 工具

def load_cfg():
    """离线阶段（select/report/verdict）专用：只读 config.json，不触发网络。"""
    os.chdir(BASE_DIR)
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


def setup_console_log():
    if not logging.getLogger("muniao").handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger("muniao").addHandler(h)
        logging.getLogger("muniao").setLevel(logging.INFO)


# 店名清洗：等价简化版（参照 M0 服务端 namelib 口径，本脚本自实现、不跨目录引用）
NAME_SUFFIX_PATTERNS = [
    r"-[^-]*木鸟民宿\s*$",      # 「-大连金州区日租-木鸟民宿」整段
    r"木鸟民宿\s*$",            # 裸「木鸟民宿」结尾
    r"-木(鸟(民(宿)?)?)?\s*$",  # 截断残留「-木」「-木鸟」「-木鸟民」「-木鸟民宿」
    r"-大连[^-]*日租\s*$",      # 「-大连XX区日租」
    r"-[^-]*日租\s*$",          # 其它城市「-XX日租」同类后缀
]
_COMPILED_SUFFIX = [re.compile(p) for p in NAME_SUFFIX_PATTERNS]


def clean_name(name):
    """剥平台来源后缀；空值如实回空串。循环剥离直至无后缀可去。"""
    if not name:
        return ""
    out = name.strip()
    changed = True
    while changed and out:
        changed = False
        for pat in _COMPILED_SUFFIX:
            new = pat.sub("", out).rstrip("-—· ").strip()
            if new != out:
                out = new
                changed = True
    return out


def detail_url(cfg, ref):
    return cfg["detail_url"].replace("{room_id}", str(ref))


def run_path(round_name, window_index):
    return os.path.join(CHECK_DIR, f"run_{round_name}_w{window_index}.json")


def write_json(path, obj):
    """落盘先写临时文件再改名，避免半截文件被断点续跑误当成品。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def window_dates(window_index):
    """第 i 个窗口：checkin=明天起第 i 天（i=1 即明天），checkout=+1 天。"""
    checkin = date.today() + timedelta(days=window_index)
    checkout = checkin + timedelta(days=1)
    return checkin.isoformat(), checkout.isoformat()


def parse_windows_arg(text):
    """--windows 1,2,5 → [1,2,5]；缺省 None 表示全部 7 个窗口。"""
    if not text:
        return None
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        w = int(part)
        if not 1 <= w <= WINDOW_COUNT:
            raise SystemExit(f"--windows 取值须在 1~{WINDOW_COUNT} 之间: {part}")
        out.append(w)
    return out or None


# ---------------------------------------------------------------- select

def phase_select(cfg, sample_size):
    """从行情库选「在架房源」样本：最近一次 snapshot_date 有 price 非空，
    按 last_seen 新到旧取前 N 家（默认 24，多取 4 家防废号）。只读模式开库。"""
    db_abs = os.path.abspath(cfg["db_path"])
    con = sqlite3.connect(f"file:{db_abs}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT b.listing_ref, b.name, b.last_seen, s.snapshot_date
            FROM listing_base b
            JOIN snapshot_price s ON s.listing_ref = b.listing_ref
            JOIN (SELECT listing_ref, MAX(snapshot_date) AS md
                  FROM snapshot_price GROUP BY listing_ref) t
              ON t.listing_ref = s.listing_ref AND t.md = s.snapshot_date
            WHERE s.price IS NOT NULL
            GROUP BY b.listing_ref
            ORDER BY b.last_seen DESC, b.listing_ref
            LIMIT ?
            """, (sample_size,)).fetchall()
    finally:
        con.close()

    if len(rows) < MIN_SAMPLE:
        raise SystemExit(
            f"在架样本不足：仅选出 {len(rows)} 家，低于冻结口径 {MIN_SAMPLE} 家，"
            "请先确认行情库快照完整性，不得降样本量硬跑。")

    items = [{
        "ref": ref,
        "name": clean_name(name),
        "url": detail_url(cfg, ref),
    } for ref, name, _last_seen, _snap in rows]

    payload = {
        "selected_at": datetime.now().isoformat(timespec="seconds"),
        "source_db": cfg["db_path"],
        "criteria": "最近一次 snapshot_date 有 price 非空，按 last_seen 新到旧取前 N 家",
        "count": len(items),
        "items": items,
    }
    write_json(SAMPLE_PATH, payload)
    log.info("样本已选出 %d 家 → %s", len(items), SAMPLE_PATH)
    for it in items:
        print(f"  {it['ref']:>9}  {it['name'][:36]:<38} {it['url']}")
    return len(items)


# ---------------------------------------------------------------- a / b

def scan_window(client, cfg, checkin, checkout, max_pages):
    """单窗口全量翻页扫描，收集该窗口全部 seen ref 集合。

    翻页规则：page1 也用 list_page_n 模板（{page}=1，带日期参数；
    裸 list_page1 不带日期，禁用）。空页口径与既有发现层一致
    （连续 stop_after_empty_pages 页无新 ref 即到底）；单页非 200/异常
    记失败页、不算空页，失败页出现即视为该窗口整体扫描失败
    （seen 集合不完整，缺席判定不可信）。
    """
    from muniao_crawler import parse  # 延迟导入，离线阶段不依赖包

    limit = max_pages or cfg["max_pages"]
    seen, order, failed_pages = set(), [], []
    empty_streak, pages_done = 0, 0

    for n in range(1, limit + 1):
        if client.stats["total"] >= ROUND_REQUEST_BUDGET:
            log.error("单轮请求预算 %d 已用尽，本窗口剩余页中止（宁缺毋滥）",
                      ROUND_REQUEST_BUDGET)
            failed_pages.append({"page": n, "http": "budget_abort"})
            break
        url = (cfg["list_page_n"].replace("{page}", str(n))
               .replace("{d1}", checkin).replace("{d2}", checkout))
        code, html = client.get(url, referer=cfg["list_page1"])
        pages_done = n
        if code != 200:
            failed_pages.append({"page": n, "http": code})
            log.warning("第%d页失败 http=%s（记失败页，不算空页）", n, code)
            continue
        ids = parse.parse_list_page(html)
        new = [i for i in ids if i not in seen]
        for rid in new:
            seen.add(rid)
            order.append(rid)
        log.info("第%d页: ids=%d new=%d seen=%d", n, len(ids), len(new), len(seen))
        empty_streak = 0 if new else empty_streak + 1
        if empty_streak >= cfg["stop_after_empty_pages"]:
            log.info("连续 %d 页无新房源，本窗口翻页结束", empty_streak)
            break

    return {
        "seen": order,                    # 保序去重
        "pages_done": pages_done,
        "failed_pages": failed_pages,
        "scan_failed": bool(failed_pages),
    }


def phase_round(cfg, client, round_name, max_pages, windows):
    """a/b 轮次通用：对 7 个窗口逐一扫描，已存在的轮次文件跳过（断点续跑）。"""
    os.makedirs(CHECK_DIR, exist_ok=True)
    if not client.check_robots():
        log.error("robots 核查未通过，本轮中止（谨慎原则）")
        return {"aborted": True}

    todo = windows or list(range(1, WINDOW_COUNT + 1))
    results = {}
    failed_windows = 0
    aborted = None
    for w in todo:
        if failed_windows >= ABORT_FAILED_WINDOWS:
            log.error("已有 %d 个窗口整窗扫描失败，本轮剩余窗口中止（顺延重跑）",
                      failed_windows)
            aborted = "failed_windows"
            break
        if client.stats["total"] >= ROUND_REQUEST_BUDGET:
            log.error("单轮请求预算 %d 已用尽，本轮剩余窗口中止", ROUND_REQUEST_BUDGET)
            aborted = "budget"
            break
        path = run_path(round_name, w)
        if os.path.exists(path):
            log.info("窗口%d 已存在 %s，跳过（断点续跑）", w, path)
            continue
        checkin, checkout = window_dates(w)
        started = datetime.now().isoformat(timespec="seconds")
        log.info("=== 轮次%s 窗口%d: %s ~ %s ===", round_name, w, checkin, checkout)
        res = scan_window(client, cfg, checkin, checkout, max_pages)
        payload = {
            "round": round_name,
            "window_index": w,
            "checkin": checkin,
            "checkout": checkout,
            "started_at": started,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            **res,
        }
        write_json(path, payload)
        log.info("窗口%d 落盘 %s（seen=%d 页数=%d 失败页=%d）",
                 w, path, len(res["seen"]), res["pages_done"], len(res["failed_pages"]))
        if res["scan_failed"]:
            failed_windows += 1
        results[w] = {"seen": len(res["seen"]), "pages": res["pages_done"],
                      "failed": len(res["failed_pages"]),
                      "scan_failed": res["scan_failed"]}
    return {"round": round_name, "aborted": aborted,
            "failed_windows": failed_windows,
            "requests_total": client.stats["total"],
            "windows": results}


# ---------------------------------------------------------------- report

def load_run(round_name, window_index):
    """返回轮次 dict；文件缺失返回 None。"""
    path = run_path(round_name, window_index)
    if not os.path.exists(path):
        return None
    return read_json(path)


def judge(ref, run_a, run_b):
    """四态判定：两轮都出现=判可订；仅一轮=待确认；两轮都缺席=判不可订；
    任一轮该窗口缺失或整体扫描失败=探测失败。"""
    if run_a is None or run_b is None:
        return J_FAILED
    if run_a.get("scan_failed") or run_b.get("scan_failed"):
        return J_FAILED
    in_a = ref in set(run_a.get("seen", []))
    in_b = ref in set(run_b.get("seen", []))
    if in_a and in_b:
        return J_BOOKABLE
    if in_a or in_b:
        return J_PENDING
    return J_UNBOOKABLE


def build_matrix(sample):
    """合并 a/b 两轮 → 每房源 7 窗口判定矩阵 + 轮次元信息。"""
    matrix = []   # [{ref,name,url,judgments:{1..7}}]
    runs_meta = {}  # (round,w) -> {checkin,checkout,pages,scan_failed,started_at,finished_at}
    for r in ("a", "b"):
        for w in range(1, WINDOW_COUNT + 1):
            run = load_run(r, w)
            if run:
                runs_meta[(r, w)] = run
    for item in sample["items"]:
        judgments = {}
        for w in range(1, WINDOW_COUNT + 1):
            judgments[w] = judge(item["ref"], load_run("a", w), load_run("b", w))
        matrix.append({"ref": item["ref"], "name": item["name"],
                       "url": item["url"], "judgments": judgments})
    return matrix, runs_meta


def phase_report():
    if not os.path.exists(SAMPLE_PATH):
        raise SystemExit(f"缺少样本文件 {SAMPLE_PATH}，请先跑 --phase select")
    sample = read_json(SAMPLE_PATH)
    matrix, runs_meta = build_matrix(sample)

    # matrix.csv（utf-8-sig 便于表格软件直接打开）
    os.makedirs(CHECK_DIR, exist_ok=True)
    with open(MATRIX_PATH, "w", encoding="utf-8-sig", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(["ref", "展示名"]
                     + [f"w{i}" for i in range(1, WINDOW_COUNT + 1)]
                     + ["详情页URL"])
        for row in matrix:
            wtr.writerow([row["ref"], row["name"]]
                         + [row["judgments"][w] for w in range(1, WINDOW_COUNT + 1)]
                         + [row["url"]])
    log.info("判定矩阵 → %s（%d 家 × %d 窗口）", MATRIX_PATH, len(matrix), WINDOW_COUNT)

    # 窗口日期对照（取 a 轮记录；b 轮同日窗口日期一致）
    win_dates = {}
    for w in range(1, WINDOW_COUNT + 1):
        run = runs_meta.get(("a", w)) or runs_meta.get(("b", w))
        if run:
            win_dates[w] = (run.get("checkin", ""), run.get("checkout", ""))

    lines = [
        "# M12 原理验证 · 人工核对清单",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 样本：{len(matrix)} 家在架房源 × {WINDOW_COUNT} 个单晚窗口",
        "- 系统判定四态：判可订 / 判不可订 / 待确认（两轮只有一轮出现）/ 探测失败（窗口扫描不完整）",
        "",
        "## 人工核对方法",
        "",
        "1. 打开木鸟 App，按下方详情页链接逐套进入房源页；",
        "2. 打开房源日历，一眼看未来 7 天（对照下表窗口日期），哪天可订、哪天不可订；",
        "3. 在「实际可订状态」列填 `bookable`（可订）或 `unbookable`（不可订）；",
        f"4. 填完后整理为 `{GROUND_TRUTH_PATH}`，列：`ref,window_index,actual`，"
        "actual 取值 bookable/unbookable，再跑 `--phase verdict` 出验证报告。",
        "",
        "## 窗口日期对照",
        "",
        "| 窗口 | 入住日 | 退房日 |",
        "|---|---|---|",
    ]
    for w in range(1, WINDOW_COUNT + 1):
        d1, d2 = win_dates.get(w, ("（未扫描）", ""))
        lines.append(f"| w{w} | {d1} | {d2} |")
    lines.append("")

    for idx, row in enumerate(matrix, 1):
        lines += [
            f"## {idx}. {row['name']}（{row['ref']}）",
            "",
            f"详情页：{row['url']}",
            "",
            "| 窗口 | 入住日 | 系统判定 | 实际可订状态 |",
            "|---|---|---|---|",
        ]
        for w in range(1, WINDOW_COUNT + 1):
            d1, _ = win_dates.get(w, ("（未扫描）", ""))
            lines.append(f"| w{w} | {d1} | {row['judgments'][w]} |  |")
        lines.append("")

    with open(CHECKLIST_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("人工核对清单 → %s", CHECKLIST_PATH)

    # 控制台汇总各态计数
    tally = {J_BOOKABLE: 0, J_UNBOOKABLE: 0, J_PENDING: 0, J_FAILED: 0}
    for row in matrix:
        for w in range(1, WINDOW_COUNT + 1):
            tally[row["judgments"][w]] += 1
    print("判定汇总:", json.dumps(tally, ensure_ascii=False))
    return matrix


# ---------------------------------------------------------------- verdict

def load_matrix_judgments():
    """matrix.csv → {ref: {window_index: judgment}}"""
    out = {}
    with open(MATRIX_PATH, encoding="utf-8-sig", newline="") as f:
        rdr = csv.reader(f)
        header = next(rdr)
        w_cols = {i: header.index(f"w{i}") for i in range(1, WINDOW_COUNT + 1)}
        for row in rdr:
            if not row:
                continue
            out[row[0]] = {i: row[c] for i, c in w_cols.items()}
    return out


def round_interval_minutes():
    """两轮独立检索的实际间隔：逐窗口取 b 轮开始 - a 轮结束，返回 (最小, 中位) 分钟。"""
    gaps = []
    for w in range(1, WINDOW_COUNT + 1):
        ra, rb = load_run("a", w), load_run("b", w)
        if not ra or not rb:
            continue
        try:
            t_a = datetime.fromisoformat(ra["finished_at"])
            t_b = datetime.fromisoformat(rb["started_at"])
        except (KeyError, ValueError):
            continue
        gaps.append((t_b - t_a).total_seconds() / 60.0)
    if not gaps:
        return None, None
    gaps.sort()
    return gaps[0], gaps[len(gaps) // 2]


def phase_verdict():
    if not os.path.exists(MATRIX_PATH):
        raise SystemExit(f"缺少 {MATRIX_PATH}，请先跑 --phase report")
    if not os.path.exists(GROUND_TRUTH_PATH):
        raise SystemExit(
            f"缺少人工核对结果 {GROUND_TRUTH_PATH}\n"
            "请按 核对清单.md 对照木鸟 App 填写，列：ref,window_index,actual"
            "（actual ∈ bookable/unbookable）")

    judgments = load_matrix_judgments()
    q = {"book_book": 0,   # 判可订 × 实际可订
         "book_unbook": 0,  # 判可订 × 实际不可订
         "unbook_book": 0,  # 判不可订 × 实际可订（假阳性：实际可订却判不可订）
         "unbook_unbook": 0}  # 判不可订 × 实际不可订
    n_pending = n_failed = n_no_judgment = n_bad_actual = 0
    total_gt = 0

    with open(GROUND_TRUTH_PATH, encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            ref = (row.get("ref") or "").strip()
            wtxt = (row.get("window_index") or "").strip()
            actual = (row.get("actual") or "").strip().lower()
            if not ref or not wtxt:
                continue
            total_gt += 1
            try:
                w = int(wtxt)
            except ValueError:
                n_bad_actual += 1
                continue
            if actual not in ("bookable", "unbookable"):
                n_bad_actual += 1
                continue
            j = judgments.get(ref, {}).get(w)
            if j is None:
                n_no_judgment += 1
                continue
            if j == J_PENDING:
                n_pending += 1
                continue
            if j == J_FAILED:
                n_failed += 1
                continue
            key = ("book" if j == J_BOOKABLE else "unbook") + "_" + \
                  ("book" if actual == "bookable" else "unbook")
            q[key] += 1

    n_judged = sum(q.values())
    agree = q["book_book"] + q["unbook_unbook"]
    match_rate = (agree / n_judged) if n_judged else None
    actual_bookable = q["book_book"] + q["unbook_book"]
    fp_rate = (q["unbook_book"] / actual_bookable) if actual_bookable else None
    passed = (match_rate is not None and fp_rate is not None
              and match_rate >= PASS_MATCH_RATE and fp_rate <= PASS_FALSE_POSITIVE)
    gap_min, gap_med = round_interval_minutes()
    now = datetime.now().isoformat(timespec="seconds")

    def pct(x):
        return "—" if x is None else f"{x * 100:.1f}%"

    lines = [
        "# M12 补订 A · 原理前置验证报告",
        "",
        f"- 报告时间：{now}",
        "- 验证原理：木鸟列表带日期检索只返回该窗口可订房源；窗口内消失 ≈ 该窗口不可订",
        f"- 通过线（冻结口径）：整体吻合率 ≥ {PASS_MATCH_RATE:.0%} 且"
        f"「实际可订却判不可订」假阳性率 ≤ {PASS_FALSE_POSITIVE:.0%}",
        "",
        "## 样本量",
        "",
        f"- 人工核对条数（ground_truth）：{total_gt}",
        f"- 进入四象限分母（判定明确）：{n_judged}",
        f"- 待确认（两轮仅一轮出现，不计分母）：{n_pending}",
        f"- 探测失败（窗口扫描不完整，不计分母）：{n_failed}",
        f"- 无对应判定（ref/窗口不在矩阵）：{n_no_judgment}",
        f"- actual 取值非法被跳过：{n_bad_actual}",
        "",
        "## 四象限",
        "",
        "|  | 实际可订 | 实际不可订 |",
        "|---|---|---|",
        f"| **判可订** | {q['book_book']} | {q['book_unbook']} |",
        f"| **判不可订** | {q['unbook_book']}（假阳性） | {q['unbook_unbook']} |",
        "",
        "## 指标与结论",
        "",
        f"- 整体吻合率 = （判可订×实际可订 + 判不可订×实际不可订）/ 分母 = **{pct(match_rate)}**",
        f"- 假阳性率 = 判不可订×实际可订 / 实际可订总数 = **{pct(fp_rate)}**",
        f"- 两次独立检索实际间隔：最小 {('—' if gap_min is None else f'{gap_min:.0f}')} 分钟，"
        f"中位 {('—' if gap_med is None else f'{gap_med:.0f}')} 分钟"
        f"（要求 ≥ {MIN_INTERVAL_MIN} 分钟）",
        "",
        f"## 结论：{'✅ 通过' if passed else '❌ 不通过'}",
        "",
        ("原理假设成立，M12 可进入开发。" if passed else
         "原理假设未达通过线，M12 不得开发；须复查失败窗口与假阳性样本后再议。"),
        "",
        "---",
        "测算人：K3",
    ]
    with open(VERDICT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"四象限: {json.dumps(q, ensure_ascii=False)}")
    print(f"吻合率={pct(match_rate)} 假阳性率={pct(fp_rate)} → "
          f"{'通过' if passed else '不通过'}")
    log.info("验证报告 → %s", VERDICT_PATH)
    return passed


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="M12 补订 A 原理前置验证（一次性工具）")
    ap.add_argument("--phase", required=True,
                    choices=["select", "a", "b", "report", "verdict"])
    ap.add_argument("--max-pages", type=int, default=None,
                    help="覆盖 config.max_pages（冒烟用）")
    ap.add_argument("--windows", type=str, default=None,
                    help="只跑指定窗口，如 1,2（冒烟用；缺省全部 7 个）")
    ap.add_argument("--sample-size", type=int, default=SAMPLE_SIZE,
                    help=f"select 取样数（默认 {SAMPLE_SIZE}，下限 {MIN_SAMPLE}）")
    args = ap.parse_args()

    setup_console_log()

    if args.phase in ("a", "b"):
        from muniao_crawler import bootstrap
        cfg, client, store, _alert_cb = bootstrap.setup(BASE_DIR)
        try:
            result = phase_round(cfg, client, args.phase,
                                 args.max_pages, parse_windows_arg(args.windows))
            print("RESULT:", json.dumps(result, ensure_ascii=False))
        finally:
            store.close()
        return

    cfg = load_cfg()
    if args.phase == "select":
        n = phase_select(cfg, args.sample_size)
        print(f"RESULT: selected={n}")
    elif args.phase == "report":
        phase_report()
    elif args.phase == "verdict":
        ok = phase_verdict()
        sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
