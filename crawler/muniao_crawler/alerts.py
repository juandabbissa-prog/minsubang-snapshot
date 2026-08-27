# -*- coding: utf-8 -*-
"""告警层：飞书 Webhook + 邮件双通道，每日心跳自检。
任务单第七节四场景：HTTP 非 200 / 数据量骤降 >30% / 追踪层超时 >3h / 单日失败率 >5%。
密钥铁规矩：Webhook URL 与 SMTP 口令只读环境变量，绝不写进代码。"""
import json
import logging
import os
import smtplib
import ssl
import time
from email.mime.text import MIMEText

import requests

log = logging.getLogger("muniao.alert")


def _feishu(text: str) -> bool:
    url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not url:
        log.warning("FEISHU_WEBHOOK_URL 未配置，跳过飞书通道")
        return False
    try:
        r = requests.post(url, json={"msg_type": "text",
                                     "content": {"text": text}}, timeout=10)
        return r.status_code == 200
    except requests.RequestException as e:
        log.warning("飞书发送失败: %s", e)
        return False


def _email(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    to = os.environ.get("ALERT_EMAIL_TO", "")
    port = int(os.environ.get("SMTP_PORT", "465"))
    if not (host and user and pwd and to):
        log.warning("SMTP 未配置，跳过邮件通道")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(),
                              timeout=15) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        return True
    except Exception as e:
        log.warning("邮件发送失败: %s", e)
        return False


def alert(scenario: str, detail: str):
    """四场景告警统一入口：飞书 + 邮件双通道。"""
    text = f"【满房帮M-1告警】场景={scenario}\n{detail}"
    log.error("ALERT %s %s", scenario, detail)
    _feishu(text)
    _email(f"【满房帮M-1告警】{scenario}", detail)


def heartbeat(state_path: str) -> bool:
    """每日 1 次 Webhook 心跳；心跳失败走邮件告警。状态持久化到 state.json。"""
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, encoding="utf-8"))
        except Exception:
            pass
    last = state.get("last_heartbeat", 0)
    if time.time() - last < 24 * 3600:
        return True
    ok = _feishu("【满房帮M-1心跳】Webhook 通道自检正常")
    if ok:
        state["last_heartbeat"] = time.time()
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        json.dump(state, open(state_path, "w", encoding="utf-8"))
    else:
        _email("【满房帮M-1告警】心跳失败", "飞书 Webhook 心跳发送失败，请检查 FEISHU_WEBHOOK_URL")
    return ok
