#!/usr/bin/env python3
"""单机保活的重试节奏与事件合并（dry-run 内存桩，不碰真实阿里云）。

锁死三条：

1. **每分钟都重试**。保活的价值就在「一分钟一轮」：这一轮看到还是 Stopped，
   就说明上一轮没成，立刻再下发一次开机，不做退避窗口。只有 Stopped 才下发，
   Starting 这类中间态不碰（正在启动的实例不该被重复开）。
2. **一直试到起来，事件只有一条**。机器躺一整天也只有一个事件：概要行
   （事件列表收起时看到的那行）随重试次数刷新成「已尝试拉起 N 次，仍未运行」，
   进度里只在第 3、10、30… 次补一行里程碑，不会一分钟一行。
3. **只有它离开保活才收尾**。确认器不按 10 分钟超时切它的记录；被移出保活
   或停用保活时才结束那条记录，否则会留一条永远转圈的事件。

跑法：python scripts/test_keepalive_retry.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-keepalive-retry-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, notify, repo, scheduler, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))


def _make_server(label: str) -> int:
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
    repo.update_server_runtime(server_id, status="Stopped", stopped_mode="StopCharging")
    return server_id


class KeepaliveRetry(unittest.TestCase):
    def setUp(self) -> None:
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
            "revoked_sessions",
            "traffic_samples",
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings({"ddns_enabled": "0", "notify_on_keepalive": "1"})
        self.sent: list[notify.Message] = []
        self._orig_send = notify.send_all
        notify.send_all = lambda config, message: (  # noqa: ANN001, ARG005
            self.sent.append(message) or {}
        )
        self._orig_refresh = shift._refresh  # noqa: SLF001
        self._orig_start = aliyun.start_instance
        self._orig_get = aliyun.get_instance
        self.started: list[int] = []
        self.state = "Stopped"
        self.start_error: Exception | None = None

        def _fake_refresh(server: dict) -> None:  # noqa: ANN001
            server["instance_status"] = self.state
            server["stopped_mode"] = "StopCharging" if self.state == "Stopped" else ""
            server["public_ip"] = "47.0.0.1" if self.state == "Running" else ""

        def _fake_start(server: dict) -> None:  # noqa: ANN001
            self.started.append(int(server["id"]))
            if self.start_error is not None:
                raise self.start_error

        shift._refresh = _fake_refresh  # noqa: SLF001
        aliyun.start_instance = _fake_start
        # 确认器查的是实时状态，这里跟着 self.state 走
        aliyun.get_instance = lambda server: aliyun.InstanceInfo(  # noqa: ARG005
            self.state,
            "StopCharging" if self.state == "Stopped" else "",
            "47.0.0.1" if self.state == "Running" else "",
            "cn-hongkong",
        )

        self.server_id = _make_server("保活机")
        self.keepalive_id = repo.add_keepalive(self.server_id, enabled=True)

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        shift._refresh = self._orig_refresh  # noqa: SLF001
        aliyun.start_instance = self._orig_start
        aliyun.get_instance = self._orig_get

    def _tick(self) -> None:
        shift.engine.reconcile_keepalives(repo.get_settings(), dt.datetime.now(TZ), set())

    def _events(self) -> list[dict]:
        return repo.list_events(limit=50)

    def _texts(self, event: dict) -> list[str]:
        return [str(step.get("text")) for step in event["progress"]]

    def test_every_round_resends_start(self):
        """每分钟一轮：连跑 5 轮就下发 5 次，不退避。"""
        for _ in range(5):
            self._tick()

        self.assertEqual(len(self.started), 5, "只要还是 Stopped，每轮都该再开一次")

    def test_one_event_for_the_whole_outage(self):
        """一直起不来也只有一条事件，概要在累加次数。"""
        for _ in range(6):
            self._tick()

        events = self._events()
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        self.assertEqual(events[0]["status"], "running")
        self.assertEqual(events[0]["detail"], "已尝试拉起 6 次，仍未运行（已停机）")

    def test_progress_only_on_milestones(self):
        """进度不能一分钟一行：只有在第 3、10 次这些里程碑上补。"""
        for _ in range(6):
            self._tick()

        texts = self._texts(self._events()[0])
        self.assertEqual(
            texts,
            [
                "已下发启动指令（原状态 Stopped）",
                "仍未运行（已停机），已尝试拉起 3 次",
            ],
            texts,
        )

    def test_middle_state_is_not_restarted(self):
        """Starting 这类中间态不重发指令：正在启动的实例不该被再开一次。"""
        self._tick()
        self.state = "Starting"
        for _ in range(3):
            self._tick()

        self.assertEqual(len(self.started), 1, "中间态不该再下发开机")
        self.assertEqual(len(self._events()), 1)

    def test_recovers_stops_retrying(self):
        """起来了就不再下发，计数归零。"""
        for _ in range(3):
            self._tick()
        self.state = "Running"
        for _ in range(3):
            self._tick()

        self.assertEqual(len(self.started), 3)
        self.assertFalse(repo.get_flag(f"keepalive_tries:{self.server_id}"))

    def test_start_error_reports_once_but_keeps_retrying(self):
        """开机接口报错（库存不足等）：每分钟照常重试，错误事件只报一次。"""
        self.start_error = RuntimeError("OperationDenied.NoStock")
        for _ in range(3):
            self._tick()

        self.assertEqual(len(self.started), 3, "报错更要重试，不能停手")
        errors = [
            e for e in self._events() if e["status"] == "done" and e["kind"] == "error"
        ]
        self.assertEqual(len(errors), 1, [e["title"] for e in self._events()])
        self.assertIn("保活启动失败", errors[0]["title"])


class KeepaliveEventLifecycle(unittest.TestCase):
    """事件什么时候结束：不按 10 分钟切，只在离开保活时收尾。"""

    def setUp(self) -> None:
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
            "revoked_sessions",
            "traffic_samples",
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings({"ddns_enabled": "0", "notify_on_keepalive": "1"})
        self.sent: list[notify.Message] = []
        self._orig_send = notify.send_all
        notify.send_all = lambda config, message: (  # noqa: ANN001, ARG005
            self.sent.append(message) or {}
        )
        self._orig_refresh = shift._refresh  # noqa: SLF001
        self._orig_start = aliyun.start_instance
        self._orig_get = aliyun.get_instance
        self.state = "Stopped"

        def _fake_refresh(server: dict) -> None:  # noqa: ANN001
            server["instance_status"] = self.state
            server["stopped_mode"] = "StopCharging" if self.state == "Stopped" else ""
            server["public_ip"] = "47.0.0.1" if self.state == "Running" else ""

        shift._refresh = _fake_refresh  # noqa: SLF001
        aliyun.start_instance = lambda server: None
        aliyun.get_instance = lambda server: aliyun.InstanceInfo(  # noqa: ARG005
            self.state,
            "StopCharging" if self.state == "Stopped" else "",
            "47.0.0.1" if self.state == "Running" else "",
            "cn-hongkong",
        )
        self.server_id = _make_server("躺一天的机器")
        self.keepalive_id = repo.add_keepalive(self.server_id, enabled=True)

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        shift._refresh = self._orig_refresh  # noqa: SLF001
        aliyun.start_instance = self._orig_start
        aliyun.get_instance = self._orig_get

    def _tick(self) -> None:
        shift.engine.reconcile_keepalives(repo.get_settings(), dt.datetime.now(TZ), set())

    def _age_event(self, seconds: int) -> None:
        db.execute("UPDATE events SET ts=ts-? WHERE status='running'", (seconds,))

    def test_confirm_does_not_timeout_keepalive_event(self):
        """超过 10 分钟的确认超时也不收尾：它要一直等到实例起来。"""
        self._tick()
        self._age_event(scheduler.EVENT_CONFIRM_TIMEOUT + 600)

        scheduler._confirm_pending_events()

        events = repo.list_events(limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "running", "保活事件不该被超时收掉")

    def test_confirm_finishes_event_when_removed_from_keepalive(self):
        """移出保活：那条记录立刻收尾，不留永远转圈。"""
        self._tick()
        repo.set_keepalive_enabled(self.keepalive_id, False)

        scheduler._confirm_pending_events()

        events = repo.list_events(limit=10)
        self.assertEqual(events[0]["status"], "error")
        self.assertEqual(events[0]["detail"], "已移出单机保活，停止拉起")

    def test_confirm_closes_event_on_recovery(self):
        """起来了：一条事件结束，标题改成完成态，计数清掉。"""
        self._tick()
        self.state = "Running"

        scheduler._confirm_pending_events()

        events = repo.list_events(limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "done")
        self.assertEqual(events[0]["title"], "躺一天的机器：保活拉起完成")
        self.assertFalse(repo.get_flag(f"keepalive_tries:{self.server_id}"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
