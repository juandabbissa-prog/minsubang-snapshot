# -*- coding: utf-8 -*-
"""高德 POI 底册扩容采集器（M4）。

按网格化周边搜索拉取大连全市住宿类 POI，落独立库 data/poi_base.db
（与木鸟库物理隔离：云端 data 分支同步会整库替换 muniao.db，POI 底册必须独立存放）。

机制要点（侦察报告实测口径）：
- 周边搜索 types=100000（住宿服务大类），单查询深翻页上限 8 页 x 25 条 = 200 条；
- 网格点命中 200 条截断时自动四细分（最深 2 层），保证密集区不漏量；
- 断点续跑：状态存 data/amap_poi_state.json，中断后从队列位置继续；
- 双周轮询：距上次完整轮询 <13 天自动跳过（配合每日调度的开机补跑机制）；
  首跑实测 1263 次/轮，周频会击穿 5000 次/月配额，故定双周；
- 空格低频巡检：连续 2 轮无 POI 的底层网格每 3 轮才实采一次，省配额；
- 消失店标记：超过 45 天未在任何轮次见到的 POI 置 active=0（防低频格误杀）；
- 归一去重：与木鸟底册「归一化名相同且距离 <100 米」视为同一店，不重复建；
- 配额：Key 只从 server/.env 读取，每次请求写 fetch_log，请求间隔 >=0.34s，
  QPS 限流码退避重试一次。

用法：
  python run_amap_poi.py            # 周轮询（<6 天自动跳过）
  python run_amap_poi.py --force    # 强制新一轮
  python run_amap_poi.py --max-requests 120   # 限制本轮请求数（分段跑）
"""
import json
import math
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POI_DB = os.path.join(BASE_DIR, "data", "poi_base.db")
MUNIAO_DB = os.path.join(BASE_DIR, "data", "muniao.db")
STATE_PATH = os.path.join(BASE_DIR, "data", "amap_poi_state.json")
ENV_PATH = os.path.join(BASE_DIR, "..", "..", "M0_工程基座", "server", ".env")

AMAP_BASE = "https://restapi.amap.com"
TIMEOUT = 10
MIN_CALL_INTERVAL = 0.34
QPS_RETRY_DELAY = 0.6
QPS_INFOCODES = {"10015", "10019", "10020", "10021", "10029"}
MAX_PAGE = 8          # 侦察实测：p9 起断供
PAGE_SIZE = 25
TRUNC_FULL = MAX_PAGE * PAGE_SIZE  # 单查询 200 条上限
MAX_DEPTH = 2         # 网格细分最深 2 层
FUSE_RADIUS_KM = 0.1  # 归一去重距离阈值（任务单：同名且 <100 米）
WEEK_SKIP_DAYS = 13       # 双周轮询（首跑实测 1263 次/轮，周频会击穿 5000 次/月配额）
EMPTY_SKIP_STREAK = 2     # 连续 2 轮无 POI 的底层网格进入低频巡检
EMPTY_SKIP_MOD = 3        # 低频网格每 3 轮才实采一次（其余轮次跳过，零请求）

# 网格框（GCJ-02）：(名称, 西lng, 南lat, 东lng, 北lat)
GRID_BOXES = [
    ("主城金州开发区", 121.20, 38.78, 121.80, 39.15),
    ("金石滩", 121.88, 39.02, 122.05, 39.14),
    ("普兰店城区", 121.90, 39.33, 122.05, 39.46),
    ("瓦房店城区", 121.92, 39.55, 122.08, 39.70),
    ("庄河城区", 122.88, 39.62, 123.05, 39.76),
    ("长海大长山岛", 122.48, 39.22, 122.68, 39.32),
]
GRID_SPACING_KM = 4.2
GRID_RADIUS_M = 3000

_last_call_at = 0.0


# ---------------------------------------------------------------- 基础工具


def _load_key() -> str:
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                if line.startswith("AMAP_KEY="):
                    return line.strip().split("=", 1)[1]
    except OSError:
        pass
    return ""


def _pace() -> None:
    global _last_call_at
    wait = MIN_CALL_INTERVAL - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _haversine_km(lng1, lat1, lng2, lat2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _normalize(name: str) -> str:
    """归一化店名：NFKC + 小写 + 去非字母数字汉字。与服务端口径一致。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", name or "").casefold()
    return "".join(ch for ch in s if ch.isalnum() or "一" <= ch <= "鿿")


def _around(lng: float, lat: float, radius_m: int, page: int, key: str) -> dict:
    """周边搜索单页。QPS 限流退避重试一次；其他异常抛出。"""
    params = {
        "location": f"{lng},{lat}",
        "radius": str(radius_m),
        "types": "100000",
        "offset": str(PAGE_SIZE),
        "page": str(page),
        "extensions": "all",
        "output": "json",
        "key": key,
    }
    url = f"{AMAP_BASE}/v3/place/around?{urllib.parse.urlencode(params)}"
    for attempt in (0, 1):
        _pace()
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if str(payload.get("status")) == "1":
            return payload
        infocode = str(payload.get("infocode", ""))
        if infocode in QPS_INFOCODES and attempt == 0:
            time.sleep(QPS_RETRY_DELAY)
            continue
        raise RuntimeError(f"amap infocode={infocode} info={payload.get('info', '')}")
    return {}


# ---------------------------------------------------------------- 库与状态


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS poi_base (
            poi_id TEXT PRIMARY KEY,
            name TEXT, address TEXT, lng REAL, lat REAL,
            district TEXT, typecode TEXT,
            rating REAL,
            first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1
        )"""
    )
    # 老库迁移：补 rating 列（M4 biz_ext 抽样：rating 全量有值，cost/lowest_price 空）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(poi_base)")}
    if "rating" not in cols:
        conn.execute("ALTER TABLE poi_base ADD COLUMN rating REAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id TEXT, started_at TEXT, finished_at TEXT,
            requests INTEGER, poi_fetched INTEGER, poi_new INTEGER,
            poi_fused INTEGER, poi_gone INTEGER, note TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS grid_stats (
            lng REAL, lat REAL, radius INTEGER,
            last_count INTEGER, empty_streak INTEGER,
            PRIMARY KEY (lng, lat, radius)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_poi_geo ON poi_base(lng, lat)")
    conn.commit()


def _load_state() -> dict | None:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def _build_grid() -> list[list]:
    """生成网格点队列：[lng, lat, radius_m, depth]。"""
    queue = []
    for _name, w, s, e, n in GRID_BOXES:
        lat = s
        while lat <= n:
            lng = w
            step_lng = GRID_SPACING_KM / (111.0 * math.cos(math.radians(lat)))
            while lng <= e:
                queue.append([round(lng, 6), round(lat, 6), GRID_RADIUS_M, 0])
                lng += step_lng
            lat += GRID_SPACING_KM / 111.0
    return queue


def _subdivide(lng: float, lat: float, depth: int) -> list[list]:
    """网格四细分：半间距、半径收窄。"""
    half = GRID_SPACING_KM / (2 ** (depth + 1))
    radius = int(GRID_RADIUS_M / (2 ** (depth + 0.5)))
    cells = []
    for dlat in (-half / 2, half / 2):
        for dlng_km in (-half / 2, half / 2):
            dlng = dlng_km / (111.0 * math.cos(math.radians(lat)))
            cells.append([round(lng + dlng, 6), round(lat + dlat / 111.0, 6), radius, depth + 1])
    return cells


def _load_muniao_base() -> list[dict]:
    """木鸟底册（归一去重比对用）。库缺失时返回空表（不阻断 POI 采集）。"""
    if not os.path.exists(MUNIAO_DB):
        return []
    conn = sqlite3.connect(f"file:{MUNIAO_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT listing_ref, name, lng, lat FROM listing_base "
            "WHERE lng IS NOT NULL AND lat IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"ref": r[0], "norm": _normalize(r[1] or ""), "lng": r[2], "lat": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------- 主流程


def main() -> None:
    force = "--force" in sys.argv
    max_requests = None
    if "--max-requests" in sys.argv:
        idx = sys.argv.index("--max-requests")
        max_requests = int(sys.argv[idx + 1])

    key = _load_key()
    if not key:
        print("FATAL: 未找到 AMAP_KEY（server/.env）")
        sys.exit(1)

    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    conn = sqlite3.connect(POI_DB)
    _init_db(conn)

    # 周轮询节奏：距上次完整轮询 <6 天且无中断状态则跳过
    state = _load_state()
    last = conn.execute(
        "SELECT round_id, finished_at FROM fetch_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not force and state is None and last:
        try:
            last_date = datetime.fromisoformat(last[1]).date()
            if (date.today() - last_date).days < WEEK_SKIP_DAYS:
                print(f"SKIP: 上轮 {last[0]} 完成于 {last[1]}，未满 {WEEK_SKIP_DAYS} 天")
                conn.close()
                return
        except (TypeError, ValueError):
            pass

    # 新一轮或续跑
    if state is None:
        state = {
            "round_id": date.today().isoformat(),
            "queue": _build_grid(),
            "seen": [],
            "requests": 0,
            "poi_fetched": 0,
            "poi_new": 0,
            "poi_fused": 0,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        print(f"新一轮 {state['round_id']}：网格点 {len(state['queue'])} 个")
    else:
        print(f"续跑 {state['round_id']}：剩余网格点 {len(state['queue'])} 个")

    muniao_base = _load_muniao_base()
    seen = set(state["seen"])
    round_id = state["round_id"]
    # 轮次序号（用于低频空格巡检的取模）与本轮各格采集计数
    round_seq = conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0] + 1
    cell_counts: dict = state.setdefault("cell_counts", {})
    skipped_cells = 0

    try:
        while state["queue"]:
            if max_requests is not None and state["requests"] >= max_requests:
                print(f"达到请求上限 {max_requests}，本轮暂停（状态已存，可续跑）")
                break
            lng, lat, radius, depth = state["queue"][0]
            # 低频巡检：连续多轮无 POI 的底层网格（depth=0），每 3 轮才实采一次
            if depth == 0:
                gs = conn.execute(
                    "SELECT empty_streak FROM grid_stats WHERE lng=? AND lat=? AND radius=?",
                    (lng, lat, radius),
                ).fetchone()
                if gs and gs[0] >= EMPTY_SKIP_STREAK and round_seq % EMPTY_SKIP_MOD != 0:
                    cell_counts[f"{lng},{lat},{radius}"] = None  # 本轮未实采，streak 保持不变
                    state["queue"].pop(0)
                    skipped_cells += 1
                    continue
            truncated = False
            cell_poi = 0
            for page in range(1, MAX_PAGE + 1):
                payload = _around(lng, lat, radius, page, key)
                state["requests"] += 1
                pois = payload.get("pois", []) or []
                for poi in pois:
                    pid = str(poi.get("id", "") or "")
                    name = (poi.get("name") or "").strip()
                    loc = poi.get("location", "")
                    try:
                        plng, plat = (float(x) for x in loc.split(","))
                    except (ValueError, AttributeError):
                        continue
                    if not pid or not name:
                        continue
                    state["poi_fetched"] += 1
                    cell_poi += 1
                    if pid in seen:
                        continue
                    seen.add(pid)
                    # 归一去重：与木鸟底册同名且 <100 米
                    norm = _normalize(name)
                    fused = False
                    if norm:
                        for mb in muniao_base:
                            if not mb["norm"]:
                                continue
                            if (norm in mb["norm"] or mb["norm"] in norm) and _haversine_km(
                                plng, plat, mb["lng"], mb["lat"]
                            ) <= FUSE_RADIUS_KM:
                                fused = True
                                break
                    if fused:
                        state["poi_fused"] += 1
                        continue
                    address = poi.get("address", "")
                    if isinstance(address, list):
                        address = ""
                    # biz_ext.rating（高德用户评分，5 分制）；biz_ext 偶发为空数组
                    biz_ext = poi.get("biz_ext")
                    rating = None
                    if isinstance(biz_ext, dict):
                        try:
                            rv = biz_ext.get("rating")
                            rating = float(rv) if rv not in (None, "", []) else None
                        except (TypeError, ValueError):
                            rating = None
                    existed = conn.execute(
                        "SELECT poi_id FROM poi_base WHERE poi_id=?", (pid,)
                    ).fetchone()
                    conn.execute(
                        """INSERT INTO poi_base
                           (poi_id, name, address, lng, lat, district, typecode,
                            rating, first_seen, last_seen, active)
                           VALUES (?,?,?,?,?,?,?,?,?,?,1)
                           ON CONFLICT(poi_id) DO UPDATE SET
                             name=excluded.name, address=excluded.address,
                             lng=excluded.lng, lat=excluded.lat,
                             district=excluded.district, typecode=excluded.typecode,
                             rating=excluded.rating,
                             last_seen=excluded.last_seen, active=1""",
                        (pid, name, str(address).strip(), plng, plat,
                         str(poi.get("adname", "") or ""),
                         str(poi.get("typecode", "") or ""),
                         rating,
                         round_id, round_id),
                    )
                    if not existed:
                        state["poi_new"] += 1
                if len(pois) < PAGE_SIZE:
                    break
                if page == MAX_PAGE:
                    truncated = True
                if max_requests is not None and state["requests"] >= max_requests:
                    break
            state["queue"].pop(0)
            if depth == 0:
                cell_counts[f"{lng},{lat},{radius}"] = cell_poi
            if truncated and depth < MAX_DEPTH:
                subs = _subdivide(lng, lat, depth)
                state["queue"] = subs + state["queue"]
                print(f"网格 {lng},{lat} 满页截断，细分为 {len(subs)} 子格")
            if state["requests"] % 50 == 0:
                conn.commit()
                state["seen"] = sorted(seen)
                _save_state(state)
                print(
                    f"进度：请求 {state['requests']} 次，剩余 {len(state['queue'])} 格，"
                    f"采 {state['poi_fetched']} / 新 {state['poi_new']} / 融合 {state['poi_fused']}"
                )
        conn.commit()
    finally:
        state["seen"] = sorted(seen)
        _save_state(state)

    if not state["queue"]:
        # 完整一轮结束：消失店标记失效。
        # 口径：超过 45 天未在任何轮次见到的才判消失（低频巡检格每 3 轮≈6 周实采一次，
        # 不能因「本轮没采」误杀在册店）。
        gone = conn.execute(
            "UPDATE poi_base SET active=0 WHERE active=1 "
            "AND julianday(?) - julianday(last_seen) > 45",
            (round_id,),
        ).rowcount
        # 空格巡检档案：实采 0 条的底层网格 streak+1，有量的清零；未实采（低频跳过）不动
        for cell_key, count in cell_counts.items():
            if count is None:
                continue
            clng, clat, cradius = cell_key.split(",")
            row = conn.execute(
                "SELECT empty_streak FROM grid_stats WHERE lng=? AND lat=? AND radius=?",
                (float(clng), float(clat), int(cradius)),
            ).fetchone()
            streak = 0 if count > 0 else ((row[0] if row else 0) + 1)
            conn.execute(
                """INSERT INTO grid_stats (lng, lat, radius, last_count, empty_streak)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(lng, lat, radius) DO UPDATE SET
                     last_count=excluded.last_count, empty_streak=excluded.empty_streak""",
                (float(clng), float(clat), int(cradius), count, streak),
            )
        conn.execute(
            """INSERT INTO fetch_log
               (round_id, started_at, finished_at, requests, poi_fetched,
                poi_new, poi_fused, poi_gone, note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (round_id, state["started_at"], datetime.now().isoformat(timespec="seconds"),
             state["requests"], state["poi_fetched"], state["poi_new"],
             state["poi_fused"], gone, f"完整轮询；低频跳过 {skipped_cells} 格"),
        )
        conn.commit()
        os.remove(STATE_PATH)
        total = conn.execute("SELECT COUNT(*) FROM poi_base WHERE active=1").fetchone()[0]
        print(
            f"完成：请求 {state['requests']} 次，采集 {state['poi_fetched']}，"
            f"新增 {state['poi_new']}，融合去重 {state['poi_fused']}，"
            f"消失标记 {gone}，在册有效 {total}"
        )
    conn.close()


if __name__ == "__main__":
    main()
