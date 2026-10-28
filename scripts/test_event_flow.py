#!/usr/bin/env python3
"""事件进度顺序测试（dry-run 桩，不碰真实阿里云、不发真通知）。

锁死一条阅读规则：概览里点开事件详情，进度必须读成
    下发指令 → 已确认 <状态> → DDNS / 解析 → 已推送通知
即「状态确认」排在所有收尾动作之前，「通知」永远是最后一步。
早期版本先发通知再记「已确认」，详情里会看到「已推送抢占恢复通知」
夹在中间，看起来像没确认就报了警。

跑法：python scripts/test_event_flow.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前设好：数据目录指向临时目录，dry-run 不产生真实调用
_TMP = tempfile.mkdtemp(prefix="cdt-event-flow-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, dns, notify, repo, scheduler  # noqa: E402


def _make_server(label: str, instance_id: str) -> int:
    return repo.upsert_server(
        {
            "label": label,
            "access_key_id": "LTAI5tFakeKeyForTest",
            "access_key_secret": "fake-secret-for-test",
            "region_id": "cn-hongkong",
            "site": "international",
            "instance_id": instance_id,
            "quota_gb": 200,
            "threshold_gb": 188,
        }
    )


class _FakeDDNS:
    def __str__(self) -> str:
        return "✅ 已同步"


class _FakeSync:
    status = "已同步"


def _texts(event_id: int) -> list[str]:
    return [step["text"] for step in _event(event_id).get("progress") or []]


def _event(event_id: int) -> dict:
    row = db.query_one("SELECT * FROM events WHERE id=?", (event_id,))
    if row is None:
        return {}
    event = dict(row)
    event["progress"] = repo._parse_progress(event.get("progress"))
    return event


class EventFlow(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        # DDNS 打开：这两个用例测的正是「DDNS / 解析排在已确认之后」，
        # 关掉 DDNS 时那一步本来就不该出现（见 test_3）。
        repo.set_settings({"notify_on_keepalive": "1", "ddns_enabled": "1"})
        # 桩掉所有出口：状态查询、DDNS、通知
        self._orig = (
            scheduler.aliyun.get_instance,
            scheduler.engine._sync_ddns,
            scheduler.notify.send_all,
            scheduler.dns.sync_record,
        )
        self.sent: list[str] = []
        scheduler.aliyun.get_instance = lambda server: aliyun.InstanceInfo(
            "Running", "", "1.2.3.4", "cn-hongkong"
        )
        scheduler.engine._sync_ddns = (
            lambda server, config, kind, target_id, ip=None: _FakeDDNS()
        )
        scheduler.dns.sync_record = lambda *args, **kwargs: _FakeSync()

        def _send(config, message):
            self.sent.append(message.title)
            return {"webhook": "ok"}

        scheduler.notify.send_all = _send

    def tearDown(self) -> None:
        (
            scheduler.aliyun.get_instance,
            scheduler.engine._sync_ddns,
            scheduler.notify.send_all,
            scheduler.dns.sync_record,
        ) = self._orig

    def test_1_keepalive_notify_is_last(self):
        """保活拉起：已确认 → DDNS → 报告已发送，通知永远是最后一步。"""
        server_id = _make_server("测试机-保活", "i-test-event-01")
        event_id = repo.add_event(
            "start",
            "测试机-保活：保活拉起",
            "原状态 Stopped",
            level="warning",
            server_id=server_id,
            status="running",
            pending_status="Running",
            pending_kind="keepalive",
        )
        repo.add_event_progress(event_id, "已下发启动指令（原状态 Stopped）")

        scheduler._confirm_pending_events()

        self.assertEqual(
            _texts(event_id),
            [
                "已下发启动指令（原状态 Stopped）",
                "已确认 Running（运行中）",
                "DDNS：✅ 已同步",
                "报告已发送",
            ],
        )
        event = _event(event_id)
        self.assertEqual(event.get("status"), "done")
        self.assertEqual(len(self.sent), 1)

    def test_2_manual_start_dns_after_confirm(self):
        """手动开机：解析同步也排在「已确认」之后，且事件里没有更靠后的动作。"""
        server_id = _make_server("测试机-手动", "i-test-event-02")
        orig_record_of = scheduler.repo.dns_records_of
        scheduler.repo.dns_records_of = lambda kind, target_id: ["demo.example.com"]
        try:
            event_id = repo.add_event(
                "start",
                "测试机-手动：手动开机",
                "原状态 Stopped",
                level="info",
                server_id=server_id,
                status="running",
                pending_status="Running",
            )
            repo.add_event_progress(event_id, "已下发开机指令")

            scheduler._confirm_pending_events()
        finally:
            scheduler.repo.dns_records_of = orig_record_of

        self.assertEqual(
            _texts(event_id),
            [
                "已下发开机指令",
                "已确认 Running（运行中）",
                "解析 demo.example.com 已同步",
            ],
        )

    def test_3_ddns_off_writes_no_dns_step(self):
        """DDNS 关着：手动开机不许写「解析 … ⏸ DDNS 未启用」。

        这条曾经会污染记录状态：sync_record 把「⏸ DDNS 未启用」当成这条域名的
        同步结果落库，原本「✅ 已同步」的记录在 DNS 页变成「待同步」黄点。
        """
        repo.set_settings({"ddns_enabled": "0"})
        server_id = _make_server("测试机-DDNS关", "i-test-event-03")
        orig_record_of = scheduler.repo.dns_records_of
        scheduler.repo.dns_records_of = lambda kind, target_id: ["off.example.com"]
        try:
            event_id = repo.add_event(
                "start",
                "测试机-DDNS关：手动开机",
                "原状态 Stopped",
                level="info",
                server_id=server_id,
                status="running",
                pending_status="Running",
            )
            repo.add_event_progress(event_id, "已下发开机指令")

            scheduler._confirm_pending_events()
        finally:
            scheduler.repo.dns_records_of = orig_record_of

        texts = _texts(event_id)
        self.assertEqual(texts, ["已下发开机指令", "已确认 Running（运行中）"])
        self.assertFalse(
            any("DDNS 未启用" in text for text in texts),
            f"DDNS 关闭时不该出现同步步骤：{texts}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
