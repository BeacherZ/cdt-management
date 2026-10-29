#!/usr/bin/env python3
"""单机保活的重试节流（dry-run 内存桩，不碰真实阿里云）。

锁死一条：开机指令下发后要等实例起来，别每分钟重发。
以前保活分支完全不看等待标记：一台始终起不来的机器（库存不足、镜像坏了、
被平台限制）会每分钟被开一次，还每分钟多一条新的「保活拉起」转圈事件 ——
既刷接口又把事件列表刷满。

等待窗口（KEEPALIVE_RETRY_SECONDS）内：只等不重发；
窗口到期还没起来：补一次指令，进度追加到同一条事件上；
那条事件已经因为超时被确认器收掉：新开一条，别把结论写进已结束的事件。

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

from app import aliyun, db, notify, repo, settings, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
RETRY = settings.KEEPALIVE_RETRY_SECONDS


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
        self.started: list[int] = []
        self.state = "Stopped"

        def _fake_refresh(server: dict) -> None:  # noqa: ANN001
            server["instance_status"] = self.state
            server["stopped_mode"] = "StopCharging" if self.state == "Stopped" else ""
            server["public_ip"] = "47.0.0.1" if self.state == "Running" else ""

        def _fake_start(server: dict) -> None:  # noqa: ANN001
            # 指令下发成功，但实例就是起不来（卡在 Stopped）
            self.started.append(int(server["id"]))

        shift._refresh = _fake_refresh  # noqa: SLF001
        aliyun.start_instance = _fake_start

        self.server_id = _make_server("保活机")
        repo.add_keepalive(self.server_id, enabled=True)

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        shift._refresh = self._orig_refresh  # noqa: SLF001
        aliyun.start_instance = self._orig_start

    def _tick(self) -> None:
        shift.engine.reconcile_keepalives(repo.get_settings(), dt.datetime.now(TZ), set())

    def _age_wait(self, seconds: float) -> None:
        db.execute(
            "UPDATE flags SET created_at=? WHERE key=?",
            (int(time.time() - seconds), f"keepalive_wait:{self.server_id}"),
        )

    def _events(self) -> list[dict]:
        return repo.list_events(limit=50)

    def test_waits_without_repeating_within_window(self):
        """窗口内只等：不重发指令，也不多开一条事件。"""
        for _ in range(5):
            self._tick()

        self.assertEqual(len(self.started), 1, "等待窗口内不该重复下发开机")
        events = self._events()
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        self.assertEqual(events[0]["status"], "running")
        self.assertTrue(repo.get_flag(f"keepalive_wait:{self.server_id}"))

    def test_retries_after_window_and_reuses_event(self):
        """窗口到期还没起来：补一次指令，进度写进同一条事件。"""
        self._tick()
        self._age_wait(RETRY + 5)

        self._tick()

        self.assertEqual(len(self.started), 2, "窗口到期应补一次开机")
        events = self._events()
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        texts = [step["text"] for step in events[0]["progress"]]
        self.assertIn("仍未运行（Stopped），已重新下发启动指令", texts)
        # 计时重置：紧接着几轮又回到「只看不发」
        self._tick()
        self.assertEqual(len(self.started), 2)

    def test_opens_new_event_when_previous_timed_out(self):
        """上一条已经因为超时被收掉：新开一条，别往已结束的事件里写进度。"""
        self._tick()
        first = self._events()[0]
        repo.finish_event(int(first["id"]), "error", "超时未确认")
        self._age_wait(RETRY + 5)

        self._tick()

        events = self._events()
        self.assertEqual(len(events), 2, [e["title"] for e in events])
        fresh = [e for e in events if e["status"] == "running"]
        self.assertEqual(len(fresh), 1, "应有一条新的进行中事件接手")
        self.assertEqual(len(self.started), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
