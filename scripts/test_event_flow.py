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
import time
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
    """保活路径的同步桩：详情/概览/通知三种说法各一份。"""

    detail = "✅ 已同步 1.2.3.4（首次指向）"
    brief = "✅ DDNS已同步（已更新）"
    status = "✅ 已同步"
    results: list = []


class _FakeSync:
    """手动开机路径的 sync_record 桩：够 dns.report() 拼出三种说法即可。"""

    status = "✅ 已同步"
    changed = True
    ip = "1.2.3.4"
    previous = ""


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
        # 三个「强制校验」时间点：确认器必须把 verify 传下去
        self.verify_calls: list[bool] = []
        scheduler.aliyun.get_instance = lambda server: aliyun.InstanceInfo(
            "Running", "", "1.2.3.4", "cn-hongkong"
        )

        def _fake_sync_ddns(
            server, config, kind, target_id, ip=None, verify=False
        ):  # noqa: ANN001
            self.verify_calls.append(verify)
            return _FakeDDNS()

        def _fake_sync_record(*args, **kwargs):  # noqa: ANN002
            self.verify_calls.append(bool(kwargs.get("verify")))
            return _FakeSync()

        scheduler.engine._sync_ddns = _fake_sync_ddns
        scheduler.dns.sync_record = _fake_sync_record

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
        """保活拉起：已确认 → DDNS → 通知已发送，通知永远是最后一步。"""
        server_id = _make_server("测试机-保活", "i-test-event-01")
        # 保活事件只可能出现在「这台还在保活名单里」的时候；确认器发现它被移出
        # 保活会直接把事件收尾，所以这里要把它登记进保活，模拟真实状态。
        repo.add_keepalive(server_id, enabled=True)
        # 这台机器挂了域名：没绑域名的机器不该有 DDNS 这一步（见 test_4）
        dns.add_record("keep.example.com", repo.get_settings())
        dns.bind("keep.example.com", dns.TARGET_SERVER, server_id)
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
        repo.add_event_progress(event_id, "已下发开机指令（原状态 Stopped）")

        scheduler._confirm_pending_events()

        self.assertEqual(
            _texts(event_id),
            [
                "已下发开机指令（原状态 Stopped）",
                "已确认 Running（运行中）",
                "DDNS：✅ 已同步 1.2.3.4（首次指向）",
                "通知已发送",
            ],
        )
        event = _event(event_id)
        self.assertEqual(event.get("status"), "done")
        # 收起状态只显示 detail：概览只讲结果，「原状态 Stopped」「已下发指令」
        # 这些过程留给展开的步骤列表；DDNS 汇总带「已更新 / IP 未变化」。
        self.assertEqual(
            event.get("detail"),
            "运行中 / ✅ DDNS已同步（已更新）",
        )
        self.assertEqual(len(self.sent), 1)
        # 保活拉起是「强制向 CF 校验一次」的时间点
        self.assertEqual(self.verify_calls, [True])

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
                "DDNS：✅ 已同步 1.2.3.4（首次指向）",
            ],
        )
        # 手动开机同样只写结果：状态 + 同步结果，与保活、换班同一种写法
        self.assertEqual(
            _event(event_id).get("detail"),
            "运行中 / ✅ DDNS已同步（已更新）",
        )
        # 手动开机也是「强制校验」时间点
        self.assertEqual(self.verify_calls, [True])

    def test_3_ddns_off_writes_disabled_step(self):
        """DDNS 关着：写一句「未启用」，但一次同步都不许走。

        这条曾经会污染记录状态：sync_record 把「⏸ DDNS 未启用」当成这条域名的
        同步结果落库，原本「✅ 已同步」的记录在 DNS 页变成「待同步」黄点。
        现在只拼文案（dns.no_sync_report），记录状态一个字节都不动。
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
        # 概览和详情都留 DDNS 这一段，值统一写「未启用」；不打接口、不碰记录
        self.assertEqual(
            texts,
            ["已下发开机指令", "已确认 Running（运行中）", "DDNS：⏸ 未启用"],
        )
        self.assertEqual(_event(event_id).get("detail"), "运行中 / ⏸ DDNS未启用")

    def test_4_keepalive_without_binding_still_shows_ddns(self):
        """DDNS 开着但这台没绑域名：保活事件也要写「DDNS：⏸ DDNS未绑定」。

        以前未绑就整段不写，列表里「运行中」和「运行中 / ✅ DDNS已同步（…）」
        两种形状混着，分不清是没绑域名还是漏写了。
        """
        scheduler.engine._sync_ddns = self._orig[1]  # 用真实实现，走「没有记录」分支
        server_id = _make_server("测试机-未绑定", "i-test-event-04")
        # 同上：保活事件出现的先决条件是这台还在保活名单里
        repo.add_keepalive(server_id, enabled=True)
        event_id = repo.add_event(
            "start",
            "测试机-未绑定：保活拉起",
            "原状态 Stopped",
            level="warning",
            server_id=server_id,
            status="running",
            pending_status="Running",
            pending_kind="keepalive",
        )
        repo.add_event_progress(event_id, "已下发开机指令（原状态 Stopped）")

        scheduler._confirm_pending_events()

        texts = _texts(event_id)
        self.assertIn("DDNS：⏸ 未绑定", texts)
        self.assertEqual(_event(event_id).get("detail"), "运行中 / ⏸ DDNS未绑定")
        # 通知里照样告诉用户域名没跟着走（报告是另一回事，不因事件省略而省略）
        self.assertEqual(len(self.sent), 1)
        # 完成态标题：不能还留着进行时的「保活拉起」
        self.assertEqual(_event(event_id).get("title"), "单机保活抢占恢复（测试机-未绑定）")


class PruneKeepsRunningEvents(unittest.TestCase):
    """清理历史事件时，仍在 running 的事件必须保留。

    保活 / 换班事件本来就允许跨天（一台机器持续起不来时，事件可以合法地挂上
    十几天），按 ts 一刀切会把它在界面上凭空删掉，「已尝试 N 次」也从 1 重数。
    """

    def setUp(self) -> None:
        db.init_db()
        for table in ("events", "flags", "shift_runs", "traffic_samples"):
            db.execute(f"DELETE FROM {table}")

    def test_running_event_survives_prune(self):
        running_id = repo.add_event("start", "长期在转的事件", "等待开机到位", status="running")
        done_id = repo.add_event("start", "早就结束的事件", "运行中", status="done")
        old = int(time.time()) - 20 * 86400
        db.execute("UPDATE events SET ts=? WHERE id=?", (old, running_id))
        db.execute("UPDATE events SET ts=? WHERE id=?", (old, done_id))

        repo.prune_events(14)

        self.assertIsNotNone(repo.get_event(running_id), "running 事件不能被清理")
        self.assertIsNone(repo.get_event(done_id), "早就结束的事件该被清理")


if __name__ == "__main__":
    unittest.main(verbosity=2)
