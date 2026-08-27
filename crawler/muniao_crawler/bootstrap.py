# -*- coding: utf-8 -*-
"""公共引导：加载 .env / config / 日志，构造三大件。"""
import json
import logging
import os
from logging.handlers import TimedRotatingFileHandler


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def setup(root):
    os.chdir(root)
    load_env()
    cfg = json.load(open("config.json", encoding="utf-8"))
    os.makedirs(cfg["log_dir"], exist_ok=True)
    os.makedirs(os.path.dirname(cfg["db_path"]), exist_ok=True)
    logger = logging.getLogger("muniao")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = TimedRotatingFileHandler(
        os.path.join(cfg["log_dir"], "crawler.log"),
        when="midnight", backupCount=30, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    from .http_client import HttpClient
    from .storage import Storage
    from . import alerts
    client = HttpClient(cfg)
    store = Storage(cfg["db_path"])
    alerts.heartbeat(cfg["state_path"])
    return cfg, client, store, alerts.alert
