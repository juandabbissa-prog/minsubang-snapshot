# -*- coding: utf-8 -*-
"""M12 · 竞对可订日历探测层：可订日历提供者接口 + 出现率实现 + 双确认状态机落库。

冻结口径（任务单补订 B/C/I，逐字执行）：
  三态    出现 / 缺席 / 探测失败（失败含网络错误、反爬拦截、检索异常）；
          失败与窗口配置缺失一律不计入分母；
  双确认  同一房源同一窗口单次缺席先记「待确认」(absent_pending)，
          连续两次独立检索均缺席才转终值「缺席」(absent)；
  表结构  availability_probe 含唯一约束 (listing_ref, window_start, window_end)，
          待确认转终值是更新同一行（INSERT / UPDATE 按状态机分支，不增新行）。

纪律（任务单补订 I）：行情库既有表零写入、零改结构；本模块只新建并读写
availability_probe 一张表。翻页扫描只收集 seen 集合，不触碰既有表。

放量闸门未过：本模块今天不接线任何每日调度入口，但可被 import 直接调用。

开发：K3 · 2026-08-31
"""
import logging
from datetime import datetime, timedelta

from . import parse

log = logging.getLogger("muniao.availability")

# seen 字段取值（三态 + 待确认中间态；入库 CHECK 约束同款）
SEEN = "seen"                    # 出现（终值：该窗口可订）
ABSENT = "absent"                # 缺席（终值：连续两次独立检索均缺席）
ABSENT_PENDING = "absent_pending"  # 待确认（单次缺席，未达双确认）
FAILED = "failed"                # 探测失败（不进分母）
VALID_STATES = (SEEN, ABSENT, ABSENT_PENDING, FAILED)

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS availability_probe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_ref TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    seen TEXT NOT NULL CHECK(seen IN ('seen','absent_pending','absent','failed')),
    probed_at TEXT,
    UNIQUE(listing_ref, window_start, window_end)
);
"""


def _conn_of(store_or_conn):
    """同时接受 Storage（.conn）与裸 sqlite3.Connection，便于离线自测。"""
    return getattr(store_or_conn, "conn", store_or_conn)


def ensure_table(store_or_conn):
    """建 availability_probe 表（幂等）。不动行情库任何既有表。"""
    conn = _conn_of(store_or_conn)
    conn.execute(TABLE_DDL)
    conn.commit()


def window_dates(today, start_offset_days, end_offset_days):
    """按配置偏移算窗口日期：入住=今天+start_offset，退房=今天+end_offset。
    窗口随「今天」每日滚动，不绑自然周。"""
    d1 = today + timedelta(days=start_offset_days)
    d2 = today + timedelta(days=end_offset_days)
    return d1.isoformat(), d2.isoformat()


class AvailabilityProvider:
    """可订日历提供者接口层（抽象基类）。
    上层只面向本接口编程，未来接入新实现类不改上层。"""

    def probe_window(self, refs, window_start, window_end):
        """探测一批房源在一个窗口的可订状态。
        入参: refs 房源 ref 列表；window_start/window_end 为 ISO 日期串。
        返回: dict[ref, 三态]，取值 seen / absent / failed
              （absent_pending 是库内状态机中间态，不是提供者输出）。"""
        raise NotImplementedError


class ListPresenceProvider(AvailabilityProvider):
    """实现类一 · 列表出现率：带日期翻页收集 seen 集合，出现=在集合内。

    翻页逻辑仿发现层/m12 原理验证脚本：page1 也用 list_page_n 模板
    （裸 list_page1 不带日期参数，禁用）；空页口径与发现层一致
    （连续 stop_after_empty_pages 页无新 ref 即到底）。
    任一翻页请求失败（非 200 / 异常）= 该窗口 seen 集合不完整，
    缺席判定不可信，整窗全部 ref 记「探测失败」，绝不用残缺集合判缺席。
    本实现只收集、不写入行情库任何既有表。
    """

    def __init__(self, client, cfg, max_pages=None):
        self.client = client
        self.cfg = cfg
        self.max_pages = max_pages

    def _scan_seen(self, window_start, window_end):
        """单窗口全量翻页，返回 (seen 集合, 扫描是否失败)。"""
        cfg = self.cfg
        limit = self.max_pages or cfg["max_pages"]
        seen, empty_streak = set(), 0
        scan_failed = False
        for n in range(1, limit + 1):
            url = (cfg["list_page_n"].replace("{page}", str(n))
                   .replace("{d1}", window_start).replace("{d2}", window_end))
            code, html = self.client.get(url, referer=cfg["list_page1"])
            if code != 200:
                scan_failed = True
                log.warning("第%d页失败 http=%s（记失败页，不算空页）", n, code)
                continue
            ids = parse.parse_list_page(html)
            new = [i for i in ids if i not in seen]
            for rid in new:
                seen.add(rid)
            log.info("第%d页: ids=%d new=%d seen=%d", n, len(ids), len(new), len(seen))
            empty_streak = 0 if new else empty_streak + 1
            if empty_streak >= cfg["stop_after_empty_pages"]:
                log.info("连续 %d 页无新房源，本窗口翻页结束", empty_streak)
                break
        return seen, scan_failed

    def probe_window(self, refs, window_start, window_end):
        seen, scan_failed = self._scan_seen(window_start, window_end)
        if scan_failed:
            log.error("窗口 %s~%s 扫描不完整，整窗记探测失败（不进分母）",
                      window_start, window_end)
            return {ref: FAILED for ref in refs}
        return {ref: (SEEN if ref in seen else ABSENT) for ref in refs}


class DetailCalendarProvider(AvailabilityProvider):
    """实现类二 · 详情页日历（留口）。

    详情页日历接口为 JS 异步加载、尚未逆向，本期空实现留口；
    未来接入时补齐本类即可，上层（入口/状态机/聚合）一律不改。
    """

    def probe_window(self, refs, window_start, window_end):
        raise NotImplementedError(
            "详情页日历接口 JS 异步未逆向，第二实现类留口，未来接入不改上层")


def record_probe(conn, results, window):
    """双确认状态机落库（只写 availability_probe）。

    入参: conn    sqlite3.Connection（或带 .conn 的 Storage）；
          results dict[ref, 三态]（提供者原始输出 seen/absent/failed）；
          window  (window_start, window_end) 元组或含同名键的 dict。
    规则:
      本轮「出现」 → seen（覆盖任何 pending/旧值）；
      本轮「缺席」且库内该 (ref,window) 已有 absent_pending → 转 absent 终值；
      本轮「缺席」其余情形 → 记 absent_pending（终值 absent 保持 absent）；
      本轮「失败」 → 不覆盖库内已有任何行（尤其不覆盖终值）；仅新行写 failed。
    幂等: 同一 (ref,window) 永远只有一行（唯一约束），重跑同轮结果不增行。
    返回: 各分支计数 dict。
    """
    conn = _conn_of(conn)
    if isinstance(window, dict):
        w_start, w_end = window["window_start"], window["window_end"]
    else:
        w_start, w_end = window
    now = datetime.now().isoformat(timespec="seconds")
    tally = {"seen": 0, "absent_pending": 0, "absent_confirmed": 0,
             "failed_new": 0, "failed_skipped": 0}

    for ref, state in results.items():
        if state not in (SEEN, ABSENT, FAILED):
            raise ValueError(f"提供者输出非法状态: {state!r}（ref={ref}）")
        row = conn.execute(
            """SELECT seen FROM availability_probe
               WHERE listing_ref=? AND window_start=? AND window_end=?""",
            (ref, w_start, w_end)).fetchone()
        old = row[0] if row else None

        if state == SEEN:
            # 出现覆盖一切：新行插入 / 旧行（含 pending、failed）更新为 seen
            conn.execute(
                """INSERT INTO availability_probe
                       (listing_ref, window_start, window_end, seen, probed_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(listing_ref, window_start, window_end)
                   DO UPDATE SET seen=excluded.seen, probed_at=excluded.probed_at""",
                (ref, w_start, w_end, SEEN, now))
            tally["seen"] += 1
        elif state == ABSENT:
            if old == ABSENT_PENDING:
                # 连续两次独立检索均缺席 → 转终值（更新同一行）
                conn.execute(
                    """UPDATE availability_probe SET seen=?, probed_at=?
                       WHERE listing_ref=? AND window_start=? AND window_end=?""",
                    (ABSENT, now, ref, w_start, w_end))
                tally["absent_confirmed"] += 1
            elif old == ABSENT:
                # 已是终值，仅刷新探测时点
                conn.execute(
                    """UPDATE availability_probe SET probed_at=?
                       WHERE listing_ref=? AND window_start=? AND window_end=?""",
                    (now, ref, w_start, w_end))
                tally["absent_confirmed"] += 1
            else:
                # 单次缺席 → 待确认（新行插入；旧 seen/failed 行转入待确认）
                conn.execute(
                    """INSERT INTO availability_probe
                           (listing_ref, window_start, window_end, seen, probed_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(listing_ref, window_start, window_end)
                       DO UPDATE SET seen=excluded.seen, probed_at=excluded.probed_at""",
                    (ref, w_start, w_end, ABSENT_PENDING, now))
                tally["absent_pending"] += 1
        else:  # FAILED：不覆盖已有任何行，新行才写 failed
            if old is None:
                conn.execute(
                    """INSERT INTO availability_probe
                           (listing_ref, window_start, window_end, seen, probed_at)
                       VALUES(?,?,?,?,?)""",
                    (ref, w_start, w_end, FAILED, now))
                tally["failed_new"] += 1
            else:
                tally["failed_skipped"] += 1

    conn.commit()
    log.info("record_probe 窗口 %s~%s: %s", w_start, w_end, tally)
    return tally
