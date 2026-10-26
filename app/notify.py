"""通知渠道：Telegram / 邮件 / Webhook。

文案由 shift 模块统一构造（HTML + 纯文本双版本），这里只负责把消息送出去。
所有渠道失败只记日志，绝不阻塞换班主流程。
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Any

import requests

from . import settings

log = logging.getLogger("cdt-management.notify")


@dataclass
class Message:
    kind: str
    title: str
    html: str
    text: str
    fields: dict[str, Any] = field(default_factory=dict)


def _cfg(config: dict[str, str]) -> dict[str, str]:
    return config


def enabled_channels(config: dict[str, str]) -> list[str]:
    channels: list[str] = []
    if config.get("notify_tg_enabled") == "1" and config.get("notify_tg_token") and config.get(
        "notify_tg_chat_id"
    ):
        channels.append("telegram")
    if (
        config.get("notify_mail_enabled") == "1"
        and config.get("notify_mail_host")
        and config.get("notify_mail_to")
    ):
        channels.append("email")
    if config.get("notify_wh_enabled") == "1" and config.get("notify_wh_url"):
        channels.append("webhook")
    return channels


def send_all(config: dict[str, str], message: Message) -> dict[str, str]:
    """逐个渠道发送，返回 {渠道: 结果}。换班报告里要回显这个结果。"""
    results: dict[str, str] = {}
    for channel in enabled_channels(config):
        try:
            send(config, channel, message)
            results[channel] = "ok"
        except Exception as exc:  # noqa: BLE001 - 通知失败不能影响主流程
            results[channel] = f"failed: {exc}"
            log.warning("通知发送失败 channel=%s error=%s", channel, exc)
    return results


def send(config: dict[str, str], channel: str, message: Message) -> None:
    if channel == "telegram":
        _send_telegram(config, message)
    elif channel == "email":
        _send_email(config, message)
    elif channel == "webhook":
        _send_webhook(config, message)
    else:
        raise ValueError(f"未知通知渠道 {channel}")


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def _proxies(proxy: str) -> dict[str, str] | None:
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _send_telegram(config: dict[str, str], message: Message) -> None:
    token = config.get("notify_tg_token", "")
    chat_id = config.get("notify_tg_chat_id", "")
    if not token or not chat_id:
        raise ValueError("Telegram token 或 chat_id 未配置")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message.html[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=settings.HTTP_TIMEOUT,
                proxies=_proxies(config.get("notify_tg_proxy", "")),
            )
            if response.status_code == 200:
                return
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            # 400 一般是格式错误，重试无意义
            if 400 <= response.status_code < 500:
                break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if attempt < 2:
            import time as _time

            _time.sleep(2)
    raise RuntimeError(f"Telegram 推送失败：{last_error}")


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------

def _send_email(config: dict[str, str], message: Message) -> None:
    host = config.get("notify_mail_host", "")
    port = int(config.get("notify_mail_port") or 465)
    username = config.get("notify_mail_username", "")
    password = config.get("notify_mail_password", "")
    recipient = config.get("notify_mail_to", "")
    security = (config.get("notify_mail_security") or "ssl").lower()

    mime = MIMEText(message.html, "html", "utf-8")
    mime["Subject"] = Header(f"{settings.BRAND} · {message.title}", "utf-8")
    mime["From"] = formataddr((str(Header(settings.BRAND, "utf-8")), username))
    mime["To"] = recipient
    mime["Date"] = formatdate(localtime=True)

    context = ssl.create_default_context()
    if security == "ssl" or port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=settings.HTTP_TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=settings.HTTP_TIMEOUT)
        if security in ("tls", "starttls"):
            server.starttls(context=context)
    try:
        if password:
            server.login(username, password)
        server.sendmail(username, [recipient], mime.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------

def _send_webhook(config: dict[str, str], message: Message) -> None:
    url = config.get("notify_wh_url", "")
    method = (config.get("notify_wh_method") or "POST").upper()
    headers = {"Content-Type": "application/json"}
    raw_headers = config.get("notify_wh_headers", "")
    if raw_headers:
        try:
            headers.update(json.loads(raw_headers))
        except Exception:
            log.warning("Webhook 自定义 headers 不是合法 JSON，已忽略")

    body = config.get("notify_wh_body", "")
    if not body:
        body = json.dumps(
            {"title": message.title, "text": message.text, "kind": message.kind},
            ensure_ascii=False,
        )
    else:
        body = (
            body.replace("{title}", message.title)
            .replace("{text}", message.text.replace("\n", "\\n"))
            .replace("{kind}", message.kind)
        )

    if method == "GET":
        response = requests.get(url, headers=headers, timeout=settings.HTTP_TIMEOUT)
    else:
        response = requests.request(
            method, url, headers=headers, data=body.encode(), timeout=settings.HTTP_TIMEOUT
        )
    if response.status_code >= 300:
        raise RuntimeError(f"Webhook 返回 HTTP {response.status_code}")
