#!/usr/bin/env python3
"""单机保活 / 闲散机器超限停机的端到端测试（dry-run 内存桩）。

锁死 1.1.15 补上的规则：
  1. 保活机器流量到阈值 → 节省停机 + 推「超限停机报告（单机保活）」+ 打标记，不再拉起；
  2. 闲散机器（没进组、没挂保活）流量到阈值 → 同样节省停机，推「超限停机报告（闲散手动）」；
  3. 流量回落到阈值以下 → 清标记，保活恢复「停了就拉」。

跑法：python scripts/test_keepalive_limit.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-keepalive-limit-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, notify, repo, settings, shift  # noqa: E402
from app import messages  # noqa: E402

db.init_db()


def _make_server(label: str, instance_id: str, threshold: float = 188) -> int:
    return repo.upsert_server(
        {
            "label": label,
            "access_key_id": "LTAI5tFakeKeyForTest",
            "access_key_secret": "fake-secret-for-test",
            "region_id": "cn-hongkong",
            "site": "international",
            "instance_id": instance_id,
            "quota_gb": 200,
            "threshold_gb": threshold,
        }
    )


class KeepaliveLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo.set_settings({"notify_on_limit_stop": "1"})

    def setUp(self) -> None:
        self.sent: list[str] = []
        self._orig_send = notify.send_all
        notify.send_all = self._fake_send

    def tearDown(self) -> None:
        notify.send_all = self._orig_send

    def _fake_send(self, config, message):  # noqa: ANN001, ARG002
        self.sent.append(message.title)
        return {"webhook": "ok"}

    def _new_keepalive(self, label: str, instance_id: str) -> int:
        sid = _make_server(label, instance_id)
        repo.add_keepalive(sid, enabled=True)
        return sid

    def _over(self, server_id: int, used: float = 200.0) -> None:
        server = repo.get_server(server_id, decrypt_secret=True)
        aliyun._mock_set(server, traffic=f"{used:.2f}", status="Running", stopped_mode="")

    def test_over_limit_stops_and_notifies(self):
        sid = self._new_keepalive("保活机A", "i-keepalive-a")
        self._over(sid, used=200.0)

        results = shift.engine.reconcile_keepalives(
            repo.get_settings(), _now_dt(), set()
        )
        over = [r for r in results if r.get("over_limit")]
        self.assertTrue(over, results)

        # 只停一次：标记在，不再重复停机
        results2 = shift.engine.reconcile_keepalives(
            repo.get_settings(), _now_dt(), set()
        )
        self.assertFalse([r for r in results2 if r.get("over_limit")], results2)

        # 打标记 + 记事件 + 推「超限停机报告（单机保活）」
        self.assertTrue(repo.get_flag(f"limit_stop:{messages.SCOPE_KEEPALIVE}:{sid}"))
        self.assertEqual(self.sent, ["CDT 超限停机报告（单机保活）"], self.sent)

    def test_flag_blocks_restart_while_over(self):
        sid = self._new_keepalive("保活机B", "i-keepalive-b")
        self._over(sid, used=200.0)
        shift.engine.reconcile_keepalives(repo.get_settings(), _now_dt(), set())

        # 标记在：机器即使被手动停了，也不能被保活拉起来
        server = repo.get_server(sid, decrypt_secret=True)
        aliyun.stop_instance(server, "StopCharging")
        flag_key = f"limit_stop:{messages.SCOPE_KEEPALIVE}:{sid}"
        self.assertTrue(repo.get_flag(flag_key))
        # 超限仍在，本轮直接 continue，不会走到 start_instance
        results = shift.engine.reconcile_keepalives(repo.get_settings(), _now_dt(), set())
        self.assertFalse([r for r in results if r.get("over_limit")], results)

    def test_drop_below_threshold_clears_flag(self):
        sid = self._new_keepalive("保活机C", "i-keepalive-c")
        self._over(sid, used=200.0)
        shift.engine.reconcile_keepalives(repo.get_settings(), _now_dt(), set())
        flag_key = f"limit_stop:{messages.SCOPE_KEEPALIVE}:{sid}"
        self.assertTrue(repo.get_flag(flag_key))

        # 流量回落
        self._over(sid, used=50.0)
        shift.engine.reconcile_keepalives(repo.get_settings(), _now_dt(), set())
        self.assertFalse(repo.get_flag(flag_key))


class IdleLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sid = _make_server("闲散机", "i-idle-1")

    def setUp(self) -> None:
        self.sent: list[str] = []
        self._orig_send = notify.send_all
        notify.send_all = self._fake_send

    def tearDown(self) -> None:
        notify.send_all = self._orig_send

    def _fake_send(self, config, message):  # noqa: ANN001, ARG002
        self.sent.append(message.title)
        return {"webhook": "ok"}

    def test_idle_over_limit_stops(self):
        server = repo.get_server(self.sid, decrypt_secret=True)
        aliyun._mock_set(server, traffic="200.00", status="Running", stopped_mode="")

        results = shift.engine.reconcile_idle_limits(
            repo.get_settings(), _now_dt(), set(), set()
        )
        self.assertTrue([r for r in results if r.get("over_limit")], results)
        flag_key = f"limit_stop:{messages.SCOPE_IDLE}:{self.sid}"
        self.assertTrue(repo.get_flag(flag_key))

        # 停稳后状态应该是 Stopped
        state = aliyun._mock(server)
        self.assertEqual(state["status"], "Stopped")
        self.assertEqual(self.sent, ["CDT 超限停机报告（闲散手动）"], self.sent)


def _now_dt():
    import datetime
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
