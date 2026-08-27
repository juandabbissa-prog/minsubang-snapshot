# -*- coding: utf-8 -*-
"""HTTP 客户端：实名 UA（UTF-8 字节）、间隔自律、超时重试、robots 核查留档。
合规红线：不绕过验证码/登录墙；任何 4xx/5xx/302 到登录页立即计数并告警。"""
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime

import requests

log = logging.getLogger("muniao.http")


class HttpClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.interval = float(os.environ.get("REQUEST_INTERVAL", 3.5))
        self.timeout = int(os.environ.get("REQUEST_TIMEOUT", 25))
        # 传输选择：目标站 WAF 拦截 python TLS 指纹（实测），curl 稳定放行中文 UA
        self.use_curl = bool(shutil.which("curl"))
        self.session = None
        if not self.use_curl:
            self.session = requests.Session()
            self.session.headers.update({
                "User-Agent": cfg["ua"].encode("utf-8"),
                "Accept": b"text/html,application/xhtml+xml,application/json",
                "Accept-Language": b"zh-CN,zh;q=0.9",
            })
        self._last_ts = 0.0
        self.stats = {"total": 0, "ok": 0, "fail": 0}
        self.non200_events = []

    def _pace(self):
        wait = self.interval - (time.time() - self._last_ts)
        if wait > 0:
            time.sleep(wait)

    def _curl_get(self, url, referer):
        jar = os.path.join(os.path.dirname(self.cfg["db_path"]), "cookies.txt")
        cmd = ["curl", "-s", "--compressed", "-A", self.cfg["ua"],
               "--max-time", str(self.timeout),
               "-c", jar, "-b", jar]  # 标准会话 Cookie 持久化（非绕验证码）
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd += ["-w", "\n%{http_code}", url]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        body, _, code = (r.stdout or "").rpartition("\n")
        try:
            return int(code), body
        except ValueError:
            return -1, body

    def _requests_get(self, url, referer):
        headers = {"Referer": referer.encode("utf-8")} if referer else {}
        r = self.session.get(url, headers=headers, timeout=self.timeout,
                             allow_redirects=False)
        return r.status_code, r.text

    def get(self, url: str, referer: str = None) -> tuple[int, str]:
        """返回 (status, text)。重定向（到底/登录墙）不重试；其余失败重试 1 次。"""
        last_code = -1
        for attempt in (1, 2):
            self._pace()
            self.stats["total"] += 1
            self._last_ts = time.time()
            try:
                if self.use_curl:
                    code, body = self._curl_get(url, referer)
                else:
                    code, body = self._requests_get(url, referer)
                last_code = code
                if code == 200:
                    self.stats["ok"] += 1
                    return 200, body
                self._record_non200(url, code)
                log.warning("HTTP %s %s（第%d次）", code, url, attempt)
                if code in (301, 302, 303, 307, 308):
                    log.warning("重定向(可能到底/登录墙)，不重试: %s", url)
                    self.stats["fail"] += 1
                    return code, ""
            except (requests.RequestException, OSError) as e:
                log.warning("请求异常 %s 第%d次: %s", url, attempt, e)
            if attempt == 1:
                time.sleep(10)
        self.stats["fail"] += 1
        return last_code, ""

    def _record_non200(self, url, code):
        self.non200_events.append({"url": url, "code": code,
                                   "ts": datetime.now().isoformat()})

    def check_robots(self) -> bool:
        """每次运行前核查 robots：监控路径若被新增 Disallow 则中止运行。
        robots 原文留档到 logs/robots_YYYYMMDD.txt。"""
        code, body = self.get(self.cfg["robots_url"])
        os.makedirs(self.cfg["log_dir"], exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        with open(os.path.join(self.cfg["log_dir"], f"robots_{stamp}.txt"),
                  "w", encoding="utf-8") as f:
            f.write(body or f"FETCH_FAILED http={code}")
        if code != 200:
            log.error("robots.txt 获取失败 http=%s，按谨慎原则中止本次运行", code)
            return False
        disallows = re.findall(r"Disallow:\s*(\S+)", body or "")
        for watch in self.cfg["robots_watch_paths"]:
            for d in disallows:
                if d != "/" and watch.startswith(d.rstrip("*")):
                    log.error("robots 新增禁止路径 %s（命中 %s），中止运行", watch, d)
                    return False
        log.info("robots 核查通过，已留档 logs/robots_%s.txt", stamp)
        return True
