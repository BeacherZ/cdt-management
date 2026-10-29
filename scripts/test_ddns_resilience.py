#!/usr/bin/env python3
"""DDNS / DNS 韧性测试（dry-run 桩，不碰 Cloudflare、不发真通知）。

锁死三条这轮补上的规则：
  1. 换班时 DDNS 一直同步不上，最多等 _DDNS_WAIT_SECONDS 就照常结算 ——
     不能再无限期卡住，让「机器早就切好了」被误报成换班超时；
  2. 一条 DNS 记录同步失败后进冷却，冷却期内不再反复打接口；
  3. 目标在跑但公网 IP 还没拿到 → 「待同步」，不是「目标未运行」。

外加停机措辞：不支持节省停机的规格会 fallback 成普通停机，
文案必须跟着真实模式走，不能一律写「已节省停机」。

跑法：.venv/Scripts/python.exe scripts/test_ddns_resilience.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cdt-ddns-resilience-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, ddns, dns, messages, notify, repo, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))


def _now_dt() -> dt.datetime:
    return dt.datetime.now(TZ)


def _make_server(label: str, status: str = "Running", traffic: float = 0.0) -> int:
    server_id = repo.upsert_server(
        {
            "label": label,
            "access_key_id": "LTAI5tFakeKeyForTest",
            "access_key_secret": "fake-secret-for-test",
            "region_id": "cn-hongkong",
            "site": "international",
            "instance_id": f"i-{label}",
            "quota_gb": 200,
            "threshold_gb": 188,
        }
    )
    repo.update_server_runtime(
        server_id,
        status=status,
        stopped_mode="StopCharging" if status == "Stopped" else "",
        public_ip="47.0.0.1" if status == "Running" else "",
    )
    db.execute("UPDATE servers SET traffic_used=? WHERE id=?", (traffic, server_id))
    return server_id


def _wipe() -> None:
    db.init_db()
    for table in (
        "events",
        "flags",
        "shift_runs",
        "shift_members",
        "shift_groups",
        "dns_records",
        "keepalives",
        "servers",
        "traffic_samples",
    ):
        db.execute(f"DELETE FROM {table}")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        _wipe()
        self.sent: list[str] = []
        self._orig_send = notify.send_all
        self._orig_refresh = shift._refresh  # noqa: SLF001
        self._orig_sync_ddns = shift.engine._sync_ddns  # noqa: SLF001

        def _fake_send(config, message):  # noqa: ANN001, ARG002
            self.sent.append(message.title)
            return {}

        notify.send_all = _fake_send

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        shift._refresh = self._orig_refresh  # noqa: SLF001
        shift.engine._sync_ddns = self._orig_sync_ddns  # noqa: SLF001


class DdnsWaitGivesUp(_Base):
    """DDNS 同步失败不能把换班卡到超时。"""

    def setUp(self) -> None:
        super().setUp()
        repo.set_settings({"ddns_enabled": "1", "notify_on_shift": "0"})
        self.duty = _make_server("当班A", "Running")
        self.standby = _make_server("待机B", "Stopped")
        self.group_id = repo.upsert_group(
            {
                "name": "DDNS 组",
                "enabled": True,
                "members": [
                    {"server_id": self.duty, "hours": 12},
                    {"server_id": self.standby, "hours": 12},
                ],
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), self.group_id),
        )

        def _fake_refresh(server):  # noqa: ANN001
            if int(server["id"]) == self.duty:
                server.update(
                    traffic_used=1.0,
                    instance_status="Running",
                    stopped_mode="",
                    public_ip="47.0.0.1",
                )
            else:
                server.update(
                    traffic_used=0.0,
                    instance_status="Stopped",
                    stopped_mode="StopCharging",
                    public_ip="",
                )

        shift._refresh = _fake_refresh  # noqa: SLF001
        # 域名同步永远失败
        shift.engine._sync_ddns = lambda *a, **kw: "❌ 更新失败"  # noqa: SLF001
        self._orig_records_of = repo.dns_records_of
        repo.dns_records_of = (
            lambda kind, target_id: ["demo.example.com"] if kind == dns.TARGET_GROUP else []
        )

    def tearDown(self) -> None:
        repo.dns_records_of = self._orig_records_of
        super().tearDown()

    def _run(self) -> dict:
        group = repo.get_group(self.group_id) or {}
        return shift.engine.reconcile_group(
            group, repo.get_settings(), _now_dt(), float(time.time())
        )

    def test_first_round_waits(self):
        self._run()
        row = db.query_one("SELECT key FROM flags WHERE key LIKE 'ddns_wait:%'")
        self.assertIsNotNone(row, "第一次失败应该开始计时等待")
        run = db.query_one(
            "SELECT phase FROM shift_runs WHERE group_id=?", (self.group_id,)
        )
        self.assertNotEqual(dict(run)["phase"], shift.PHASE_SETTLED)

    def test_gives_up_and_settles_after_wait(self):
        self._run()
        row = db.query_one("SELECT key FROM flags WHERE key LIKE 'ddns_wait:%'")
        self.assertIsNotNone(row)
        # 把等待起点往前拨到阈值之外，模拟「已经等了 3 分钟」
        db.execute(
            "UPDATE flags SET created_at=? WHERE key=?",
            (int(time.time()) - shift._DDNS_WAIT_SECONDS - 10, row["key"]),  # noqa: SLF001
        )

        self._run()
        run = db.query_one(
            "SELECT * FROM shift_runs WHERE group_id=?", (self.group_id,)
        )
        self.assertEqual(dict(run)["phase"], shift.PHASE_SETTLED, dict(run))
        titles = [e["title"] for e in repo.list_events(limit=50)]
        self.assertTrue(
            any("DDNS 未同步" in t for t in titles), titles
        )
        # 等待标记清掉，下一班重新计时
        self.assertIsNone(
            db.query_one("SELECT key FROM flags WHERE key LIKE 'ddns_wait:%'")
        )


class DnsRetryCooldown(_Base):
    """失败记录进冷却，别每分钟打一遍接口。"""

    def setUp(self) -> None:
        super().setUp()
        repo.set_settings(
            {"ddns_enabled": "1", "ddns_records": '["demo.example.com"]'}
        )
        self.server = _make_server("DNS机", "Running")
        repo.ensure_dns_record("demo.example.com")
        repo.bind_dns_record("demo.example.com", dns.TARGET_SERVER, self.server)
        self._orig_sync = dns.sync_record
        self.calls: list[str] = []

        def _fake_sync(record, config, ip=None, force=False):  # noqa: ANN001, ARG001
            self.calls.append(record)
            return ddns.DDNSResult(ddns.STATUS_UPDATE_FAILED, "HTTP 403", ip=ip or "")

        dns.sync_record = _fake_sync

    def tearDown(self) -> None:
        dns.sync_record = self._orig_sync
        super().tearDown()

    def test_second_round_is_cooled_down(self):
        config = repo.get_settings()
        dns.reconcile(config)
        self.assertEqual(len(self.calls), 1, self.calls)
        # 冷却期内再来一轮：不再打接口
        dns.reconcile(config)
        self.assertEqual(len(self.calls), 1, self.calls)
        # 手动「立即同步」不受冷却限制
        dns.reconcile(config, force=True)
        self.assertEqual(len(self.calls), 2, self.calls)

    def test_success_clears_cooldown(self):
        config = repo.get_settings()
        dns.reconcile(config)
        self.assertIsNotNone(
            db.query_one("SELECT key FROM flags WHERE key='dns_retry:demo.example.com'")
        )
        dns.sync_record = lambda *a, **kw: ddns.DDNSResult(  # noqa: E731
            ddns.STATUS_SYNCED, "ok", ip="47.0.0.1"
        )
        dns.reconcile(config, force=True)
        self.assertIsNone(
            db.query_one("SELECT key FROM flags WHERE key='dns_retry:demo.example.com'")
        )


class DnsStatePending(_Base):
    """在跑没 IP ≠ 没在跑。"""

    def setUp(self) -> None:
        super().setUp()
        repo.set_settings(
            {"ddns_enabled": "1", "ddns_records": '["demo.example.com"]'}
        )
        self.server = _make_server("刚起来", "Running")
        # 在跑但公网 IP 还没绑上
        repo.update_server_runtime(
            self.server, status="Running", stopped_mode="", public_ip=""
        )
        repo.ensure_dns_record("demo.example.com")
        repo.bind_dns_record("demo.example.com", dns.TARGET_SERVER, self.server)

    def test_running_without_ip_is_pending(self):
        row = repo.get_dns_record("demo.example.com") or {}
        target = dns.target_info(dns.TARGET_SERVER, self.server)
        self.assertEqual(dns.state_of(row, target), dns.STATE_PENDING)

    def test_stopped_target_is_idle(self):
        repo.update_server_runtime(
            self.server, status="Stopped", stopped_mode="StopCharging", public_ip=""
        )
        row = repo.get_dns_record("demo.example.com") or {}
        target = dns.target_info(dns.TARGET_SERVER, self.server)
        self.assertEqual(dns.state_of(row, target), dns.STATE_IDLE)


class StopPhraseFollowsMode(unittest.TestCase):
    """文案跟着实际生效的停机模式走。"""

    def setUp(self) -> None:
        _wipe()
        # 不支持节省停机时 stop_instance 走 fallback，返回 "Stop"
        self._orig = aliyun.stop_instance

    def tearDown(self) -> None:
        aliyun.stop_instance = self._orig

    def test_phrase(self):
        self.assertEqual(messages.stop_phrase("StopCharging"), "已节省停机")
        self.assertEqual(messages.stop_phrase("Stop"), "已普通停机")
        self.assertEqual(messages.stop_phrase("KeepCharging"), "已普通停机")
        self.assertEqual(messages.stop_phrase("Stop", "已强制"), "已强制普通停机")
        self.assertEqual(messages.stop_phrase(""), "已停机")

    def test_stop_instance_returns_effective_mode(self):
        server_id = _make_server("降级机", "Running")
        server = repo.get_server(server_id, decrypt_secret=True)
        mode = aliyun.stop_instance(server, "StopCharging", fallback=True)
        self.assertEqual(mode, "StopCharging")

    def test_over_limit_text_uses_real_mode(self):
        server = {"label": "闲散机", "instance_id": "i-x", "stopped_mode": ""}
        message = messages.over_limit(server, messages.SCOPE_IDLE, mode="Stop")
        self.assertIn("已普通停机", message.text)
        self.assertNotIn("已节省停机", message.text)

    def test_limit_stop_text_uses_real_mode(self):
        server = {"label": "当班机", "instance_id": "i-y", "stopped_mode": "Stop"}
        message = messages.limit_stop(server, group_name="组A")
        self.assertIn("已强制普通停机", message.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
