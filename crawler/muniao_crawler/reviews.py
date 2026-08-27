# -*- coding: utf-8 -*-
"""点评口碑关键词分析（M2 反馈 2 新增模块，不改动任何既有采集逻辑）。

合规与隐私红线：
- 详情页点评内容只在内存中即时分析，原文绝不落盘、不入日志；
- 仅关键词命中统计入库（listing_review_stats，新表独立，既有表结构不动）；
- 频率/间隔/UA 复用既有 HttpClient（REQUEST_INTERVAL 3.5s、实名 UA），本模块不另设参数；
- 词典独立文件 review_keywords.json，mtime 热加载，追加词条不改代码即生效。
"""
import json
import logging
import os
import re
from datetime import date, datetime

log = logging.getLogger("muniao.reviews")

_DICT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_keywords.json")
_dict_cache = {"mtime": None, "positive": [], "negative": []}

REVIEW_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS listing_review_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date DATE NOT NULL,
    platform TEXT NOT NULL,
    listing_ref TEXT NOT NULL,
    pos_json TEXT,
    neg_json TEXT,
    sample_count INTEGER DEFAULT 0,
    collected_at TEXT,
    UNIQUE(snapshot_date, platform, listing_ref)
)
"""

_CONTENT_RE = re.compile(r'class="room_Enr f14">(.*?)</div>', re.S)
_SCORE_RE = re.compile(r"(?:设施装潢|服务态度|图片吻合|卫生状况)：<span class=\"red\">(\d+(?:\.\d+)?)</span>")
_TAG_RE = re.compile(r"<[^>]+>")


def load_keywords() -> dict:
    """加载关键词词典（mtime 热加载）。文件损坏时返回空组，不阻断采集。"""
    try:
        mtime = os.path.getmtime(_DICT_FILE)
    except OSError:
        mtime = None
    if _dict_cache["mtime"] == mtime and _dict_cache["positive"]:
        return _dict_cache
    try:
        with open(_DICT_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _dict_cache.update({
            "mtime": mtime,
            "positive": [w for w in raw.get("positive", []) if w.strip()],
            "negative": [w for w in raw.get("negative", []) if w.strip()],
        })
    except Exception as exc:
        log.warning("关键词词典加载失败，按空词典分析: %s", type(exc).__name__)
        _dict_cache.update({"mtime": mtime, "positive": [], "negative": []})
    return _dict_cache


def parse_reviews(html: str, limit: int = 10) -> list[dict]:
    """从详情页 HTML 解析点评条目（内存对象，调用方分析后即弃）。

    返回 [{content, score}]；score 为四个单项分（设施/服务/图片/卫生）均值，无分项则为 None。
    """
    reviews = []
    blocks = _CONTENT_RE.findall(html or "")[:limit]
    scores = [float(s) for s in _SCORE_RE.findall(html or "")]
    for i, raw in enumerate(blocks):
        content = _TAG_RE.sub("", raw).strip()
        if not content:
            continue
        per = scores[i * 4:(i + 1) * 4]
        score = round(sum(per) / len(per), 2) if len(per) == 4 else None
        reviews.append({"content": content, "score": score})
    return reviews


def analyze(reviews: list[dict]) -> dict:
    """内存即时关键词分析：返回 {pos: {词: 命中条数}, neg: {...}, sample: 样本数}。"""
    kw = load_keywords()
    pos: dict[str, int] = {}
    neg: dict[str, int] = {}
    for r in reviews:
        text = r["content"]
        for w in kw["positive"]:
            if w in text:
                pos[w] = pos.get(w, 0) + 1
        for w in kw["negative"]:
            if w in text:
                neg[w] = neg.get(w, 0) + 1
    return {"pos": pos, "neg": neg, "sample": len(reviews)}


def ensure_table(store) -> None:
    """创建点评统计新表（IF NOT EXISTS，不动既有表结构）。"""
    store.conn.executescript(REVIEW_STATS_SCHEMA)
    store.conn.commit()


def insert_stats(store, platform: str, ref: str, result: dict) -> None:
    """写关键词命中统计（只存 JSON 统计与样本数，绝无原文）。"""
    store.conn.execute(
        "INSERT OR REPLACE INTO listing_review_stats "
        "(snapshot_date, platform, listing_ref, pos_json, neg_json, sample_count, collected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            date.today().isoformat(), platform, ref,
            json.dumps(result["pos"], ensure_ascii=False),
            json.dumps(result["neg"], ensure_ascii=False),
            result["sample"],
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def run(client, store, cfg, alert_cb, limit: int | None = None, only_refs: list | None = None) -> dict:
    """点评关键词分析采集：对每间活跃房源抓详情页 → 内存分析 → 统计入库。"""
    started = datetime.now().isoformat(timespec="seconds")
    if not client.check_robots():
        alert_cb("状态码异常", "robots.txt 获取失败或被新增禁止路径，点评分析中止")
        return {"aborted": True}

    ensure_table(store)
    refs = only_refs or store.active_listings(cfg["platform"])
    if limit:
        refs = refs[:limit]
    total = len(refs)
    ok = fail = sampled = 0

    for i, rid in enumerate(refs, 1):
        code, html = client.get(cfg["detail_url"].replace("{room_id}", rid),
                                referer=cfg["list_page1"])
        if code == 200:
            reviews = parse_reviews(html)
            result = analyze(reviews)
            insert_stats(store, cfg["platform"], rid, result)
            sampled += result["sample"]
            ok += 1
            reviews.clear()  # 原文内存对象立即释放
        else:
            fail += 1
            log.warning("点评分析详情页 HTTP %s: %s", code, rid)
        if i % 20 == 0:
            store.commit()
            log.info("点评分析进度 %d/%d ok=%d fail=%d", i, total, ok, fail)
    store.commit()

    detail = {"refs": total, "ok": ok, "fail": fail, "sampled_reviews": sampled}
    store.log_run("review_stats", started, datetime.now().isoformat(timespec="seconds"),
                  client.stats["total"], client.stats["ok"], client.stats["fail"], detail)
    log.info("点评关键词分析完成: %s", detail)
    return detail
