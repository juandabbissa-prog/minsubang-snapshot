# -*- coding: utf-8 -*-
"""存储层：SQLite。幂等设计——同日重跑覆盖不重复计数，缺失日期 crawl_log 可查。
表结构与任务单第五节对齐（snapshot_price 含 rating/review_count 两列，核对表结论=是）。"""
import json
import os
import sqlite3
from datetime import datetime, date

SCHEMA = """
CREATE TABLE IF NOT EXISTS listing_base (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    listing_ref TEXT NOT NULL,
    name TEXT, address TEXT, lng REAL, lat REAL,
    region_code TEXT,
    first_seen DATE, last_seen DATE,
    UNIQUE(platform, listing_ref)
);
CREATE TABLE IF NOT EXISTS snapshot_price (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date DATE NOT NULL,
    platform TEXT NOT NULL,
    listing_ref TEXT NOT NULL,
    price INTEGER,
    soldout_flag INTEGER,
    price_changed INTEGER DEFAULT 0,
    rating REAL,
    review_count INTEGER,
    collected_at TEXT,
    UNIQUE(snapshot_date, platform, listing_ref)
);
CREATE TABLE IF NOT EXISTS crawl_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT, started_at TEXT, finished_at TEXT,
    total INTEGER, success INTEGER, fail INTEGER, detail_json TEXT
);
"""


class Storage:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self):
        """M2-变更-004 增量列：listing_base 加 facility_count / room_capacity /
        open_year / renovate_year。
        幂等：已存在则 ALTER 报错即跳过，不破坏既有库与旧代码读取。
        open_year / renovate_year 暂无可采数据源（2026-08-28 实测：高德
        place/detail biz_ext.open_time 对住宿类 POI 10/10 返回空；木鸟页面
        无结构化开业/装修年份字段），先留列位，有源即写。"""
        for col in ("facility_count INTEGER", "room_capacity INTEGER",
                    "open_year INTEGER", "renovate_year INTEGER",
                    "host_id TEXT", "host_listing_count INTEGER"):
            try:
                self.conn.execute(f"ALTER TABLE listing_base ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # duplicate column name：已存在

    def upsert_listing(self, platform, ref, first_seen=None, last_seen=None,
                       room_capacity=None):
        today = date.today().isoformat()
        self.conn.execute(
            """INSERT INTO listing_base(platform, listing_ref, first_seen, last_seen,
                       room_capacity)
               VALUES(?,?,?,?,?)
               ON CONFLICT(platform, listing_ref)
               DO UPDATE SET last_seen=excluded.last_seen,
                   room_capacity=COALESCE(excluded.room_capacity,
                                          listing_base.room_capacity)""",
            (platform, ref, first_seen or today, last_seen or today, room_capacity))

    def update_listing_metrics(self, platform, ref, facility_count=None,
                               room_capacity=None):
        """M2-变更-004：详情页设施标签数 / 列表页宜住人数回写底册（只填非 None）。"""
        if facility_count is not None:
            self.conn.execute(
                """UPDATE listing_base SET facility_count=?
                   WHERE platform=? AND listing_ref=?""",
                (facility_count, platform, ref))
        if room_capacity is not None:
            self.conn.execute(
                """UPDATE listing_base SET room_capacity=?
                   WHERE platform=? AND listing_ref=?""",
                (room_capacity, platform, ref))

    def update_listing_host(self, platform, ref, host_id=None,
                            host_listing_count=None):
        """体量维（M2-变更-004 补充）：房东 ID / 房东挂牌套数回写底册。"""
        if host_id is not None:
            self.conn.execute(
                """UPDATE listing_base SET host_id=?
                   WHERE platform=? AND listing_ref=?""",
                (host_id, platform, ref))
        if host_listing_count is not None:
            self.conn.execute(
                """UPDATE listing_base SET host_listing_count=?
                   WHERE platform=? AND listing_ref=?""",
                (host_listing_count, platform, ref))

    def listings_missing_host(self, platform):
        """缺房东 ID 的活跃房源（host_id 补采清单）。"""
        return [r[0] for r in self.conn.execute(
            "SELECT listing_ref FROM listing_base WHERE platform=? AND host_id IS NULL",
            (platform,))]

    def hosts_missing_count(self, platform):
        """缺套数的房东 ID 清单（去重）。"""
        return [r[0] for r in self.conn.execute(
            """SELECT DISTINCT host_id FROM listing_base
               WHERE platform=? AND host_id IS NOT NULL
                 AND host_listing_count IS NULL""",
            (platform,))]

    def update_host_count(self, platform, host_id, count):
        """按房东 ID 批量回写套数（该房东名下全部房源一次到位）。"""
        self.conn.execute(
            """UPDATE listing_base SET host_listing_count=?
               WHERE platform=? AND host_id=?""",
            (count, platform, host_id))

    def update_listing_profile(self, platform, ref, name, address, lng, lat,
                               region_code=None):
        self.conn.execute(
            """UPDATE listing_base SET name=?, address=?, lng=?, lat=?,
                   region_code=COALESCE(?, region_code)
               WHERE platform=? AND listing_ref=?""",
            (name, address, lng, lat, region_code, platform, ref))

    def insert_snapshot(self, platform, ref, price, rating, review_count,
                        soldout=None, snap_date=None):
        d = snap_date or date.today().isoformat()
        prev = self.conn.execute(
            """SELECT price FROM snapshot_price
               WHERE platform=? AND listing_ref=? AND snapshot_date<?
               ORDER BY snapshot_date DESC LIMIT 1""",
            (platform, ref, d)).fetchone()
        changed = 1 if (prev and price is not None and prev[0] != price) else 0
        self.conn.execute(
            """INSERT INTO snapshot_price(snapshot_date, platform, listing_ref,
                       price, soldout_flag, price_changed, rating, review_count, collected_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(snapshot_date, platform, listing_ref) DO UPDATE SET
                       price=excluded.price, soldout_flag=excluded.soldout_flag,
                       price_changed=excluded.price_changed, rating=excluded.rating,
                       review_count=excluded.review_count,
                       collected_at=excluded.collected_at""",
            (d, platform, ref, price, soldout, changed, rating, review_count,
             datetime.now().isoformat(timespec="seconds")))

    def active_listings(self, platform):
        return [r[0] for r in self.conn.execute(
            "SELECT listing_ref FROM listing_base WHERE platform=?", (platform,))]

    def count_snapshots(self, platform, snap_date=None):
        d = snap_date or date.today().isoformat()
        return self.conn.execute(
            "SELECT COUNT(*) FROM snapshot_price WHERE platform=? AND snapshot_date=?",
            (platform, d)).fetchone()[0]

    def seven_day_avg(self, platform):
        rows = self.conn.execute(
            """SELECT detail_json FROM crawl_log
               WHERE layer='track' ORDER BY id DESC LIMIT 7""").fetchall()
        vals = []
        for (dj,) in rows:
            try:
                v = json.loads(dj).get("snapshot_count")
                if v:
                    vals.append(v)
            except Exception:
                pass
        return sum(vals) / len(vals) if vals else None

    def log_run(self, layer, started, finished, total, success, fail, detail=None):
        self.conn.execute(
            """INSERT INTO crawl_log(layer, started_at, finished_at, total, success, fail, detail_json)
               VALUES(?,?,?,?,?,?,?)""",
            (layer, started, finished, total, success, fail,
             json.dumps(detail or {}, ensure_ascii=False)))
        self.conn.commit()

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()
