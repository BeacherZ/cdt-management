#!/usr/bin/env python3
"""通知渠道的健壮性测试：Webhook 模板转义、邮件发信人兜底。

不真发任何请求 —— requests / smtplib 全部换成记录参数的桩。
跑法：python scripts/test_notify.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cdt-notify-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"

from app import notify  # noqa: E402


class _FakeResponse:
    status_code = 200
    content = b"{}"
    text = ""

    def json(self) -> dict:
        return {}


class WebhookTpl(unittest.TestCase):
    """模板是用户自己填的 JSON，占位符替换进去的值必须仍然是合法 JSON。"""

    def setUp(self) -> None:
        self._orig_request = notify.requests.request
        self._orig_get = notify.requests.get
        self.sent: dict = {}

        def _request(method, url, **kwargs):
            self.sent = {"method": method, "url": url, **kwargs}
            return _FakeResponse()

        notify.requests.request = _request
        notify.requests.get = lambda url, **kwargs: _FakeResponse()

    def tearDown(self) -> None:
        notify.requests.request = self._orig_request
        notify.requests.get = self._orig_get

    def _config(self, body: str) -> dict[str, str]:
        return {
            "notify_wh_enabled": "1",
            "notify_wh_url": "https://example.com/hook",
            "notify_wh_method": "POST",
            "notify_wh_body": body,
        }

    def test_quote_in_title_stays_valid_json(self):
        """标题里的双引号不能把 JSON 拆坏 —— 换班组名/服务器备注都是自由输入。"""
        message = notify.Message(
            kind="test",
            title='香港"主力"：换班完成',
            html="",
            text="第一行\n第二行",
        )
        notify.send(
            self._config('{"title":"{title}","content":"{text}"}'), "webhook", message
        )

        # 解析得动 = 转义做对了；以前这里会因为 `"主力"` 把字符串提前闭合而抛 JSONDecodeError
        payload = json.loads(self.sent["data"].decode("utf-8"))
        self.assertEqual(payload["title"], '香港"主力"：换班完成')
        self.assertEqual(payload["content"], "第一行\n第二行")

    def test_backslash_and_newline_escaped(self):
        """反斜杠和换行都不能破坏结构：换行在 JSON 字符串里必须是 \\n。"""
        message = notify.Message(kind="test", title="路径 C:\\temp", html="", text="a\nb")
        notify.send(
            self._config('{"title":"{title}","content":"{text}"}'), "webhook", message
        )

        raw = self.sent["data"].decode("utf-8")
        self.assertIn("\\\\temp", raw)  # 反斜杠被转义成 \\
        payload = json.loads(raw)
        self.assertEqual(payload["title"], "路径 C:\\temp")
        self.assertEqual(payload["content"], "a\nb")

    def test_plain_text_template_not_double_escaped(self):
        """非 JSON 模板（表单格式）也要能用，且换行按 JSON 规则转义。"""
        message = notify.Message(kind="test", title="标题", html="", text="甲\n乙")
        notify.send(self._config("title={title}&text={text}"), "webhook", message)
        self.assertEqual(self.sent["data"].decode("utf-8"), "title=标题&text=甲\\n乙")


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, *args, **kwargs):
        self.host = host
        self.port = port
        self.login_args = None
        self.mail = None
        _FakeSMTP.instances.append(self)

    def login(self, username, password):
        self.login_args = (username, password)

    def sendmail(self, sender, recipients, body):
        self.mail = (sender, recipients, body)

    def quit(self):
        return None

    def starttls(self, *args, **kwargs):
        return None


class EmailFrom(unittest.TestCase):
    """只填主机 + 收件人（不填 SMTP 账号）是合法配置，MAIL FROM 不能是空的。"""

    def setUp(self) -> None:
        _FakeSMTP.instances.clear()
        self._orig_ssl = notify.smtplib.SMTP_SSL
        self._orig_smtp = notify.smtplib.SMTP
        notify.smtplib.SMTP_SSL = _FakeSMTP
        notify.smtplib.SMTP = _FakeSMTP

    def tearDown(self) -> None:
        notify.smtplib.SMTP_SSL = self._orig_ssl
        notify.smtplib.SMTP = self._orig_smtp

    def _config(self, **overrides: str) -> dict[str, str]:
        config = {
            "notify_mail_enabled": "1",
            "notify_mail_host": "smtp.example.com",
            "notify_mail_port": "465",
            "notify_mail_security": "ssl",
            "notify_mail_username": "",
            "notify_mail_password": "",
            "notify_mail_to": "admin@example.com",
        }
        config.update(overrides)
        return config

    def test_sender_falls_back_to_recipient(self):
        """没填 SMTP 账号时用收件人兜底，否则空 from 会被 SMTP 直接拒信。"""
        notify.send(self._config(), "email", notify.Message("test", "标题", "<p>x</p>", "x"))

        server = _FakeSMTP.instances[-1]
        self.assertIsNotNone(server.mail)
        sender, recipients, _body = server.mail
        self.assertEqual(sender, "admin@example.com")
        self.assertEqual(recipients, ["admin@example.com"])
        self.assertIsNone(server.login_args)  # 没密码就别去登录

    def test_username_used_when_present(self):
        """填了账号就用账号，别被兜底逻辑盖掉。"""
        notify.send(
            self._config(notify_mail_username="bot@example.com", notify_mail_password="pass1234"),
            "email",
            notify.Message("test", "标题", "<p>x</p>", "x"),
        )

        server = _FakeSMTP.instances[-1]
        assert server.mail is not None
        self.assertEqual(server.mail[0], "bot@example.com")
        self.assertEqual(server.login_args, ("bot@example.com", "pass1234"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
