#!/usr/bin/env python3
"""结算落事件的回归测试（dry-run 内存桩，不碰真实阿里云、不发真通知）。

锁死两条：
  1. 关掉「换班通知」时，事件照样要写结算摘要。以前通知开关直接 return，
     那条 running 事件永远停在「正在启动当班 X」—— 界面看着像卡死，班其实早换完了。
  2. 一次故障转移只留一条事件。以前决策（「当班流量超限，故障转移」）和结果
     （「故障转移」）各开一条，事件列表里两条标题几乎一样的记录，事后分不清
     哪条是结论、哪条是过程。

跑法：python scripts/test_settle_events.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-settle-events-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, notify, repo, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))


def _make_server(label: str, status: str = "Stopped", traffic: float = 0.0) -> int:
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


class _Base(unittest.TestCase):
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
        repo.set_settings(
            {
                "ddns_enabled": "0",
                "notify_on_shift": "1",
                "notify_on_failover": "1",
                "notify_on_error": "0",
            }
        )
        self.sent: list[notify.Message] = []
        self._orig_send = notify.send_all
        notify.send_all = lambda config, message: (  # noqa: ANN001, ARG005
            self.sent.append(message) or {}
        )
        self._orig_refresh = shift._refresh  # noqa: SLF001
        self._orig_start = aliyun.start_instance
        self._orig_stop = aliyun.stop_instance
        self.status: dict[int, str] = {}
        self.started: list[int] = []
        self.stopped: list[int] = []

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        shift._refresh = self._orig_refresh  # noqa: SLF001
        aliyun.start_instance = self._orig_start
        aliyun.stop_instance = self._orig_stop

    def _wire(self, start_to: str = "Running") -> None:
        def _fake_refresh(server: dict) -> None:  # noqa: ANN001
            state = self.status[int(server["id"])]
            server["instance_status"] = state
            server["stopped_mode"] = "StopCharging" if state == "Stopped" else ""
            server["public_ip"] = "47.0.0.1" if state == "Running" else ""

        def _fake_start(server: dict) -> None:  # noqa: ANN001
            self.started.append(int(server["id"]))
            self.status[int(server["id"])] = start_to

        def _fake_stop(server: dict, mode: str = "StopCharging", fallback: bool = False) -> str:  # noqa: ANN001, ARG001
            self.stopped.append(int(server["id"]))
            self.status[int(server["id"])] = "Stopped"
            return "StopCharging"

        shift._refresh = _fake_refresh  # noqa: SLF001
        aliyun.start_instance = _fake_start
        aliyun.stop_instance = _fake_stop

    def _group(self, members: list[dict], failover_enabled: bool = True) -> int:
        group_id = repo.upsert_group(
            {
                "name": "换班组",
                "enabled": True,
                "failover_enabled": failover_enabled,
                "members": members,
            }
        )
        # 把轮换起点挪到一小时前，保证第一位成员当前当班
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), group_id),
        )
        return group_id

    def _reconcile(self, group_id: int) -> dict:
        return shift.engine.reconcile_group(
            repo.get_group(group_id) or {},
            repo.get_settings(),
            dt.datetime.now(TZ),
            float(time.time()),
        )

    def _events(self, group_id: int) -> list[dict]:
        return repo.list_events(limit=100, group_id=group_id)


class SettleWhenNotifyOff(_Base):
    """通知关着：事件仍然是完整的流水。"""

    def test_event_detail_is_written_even_if_notify_off(self):
        repo.set_settings({"notify_on_shift": "0"})
        a = _make_server("当班A", "Running")
        b = _make_server("待机B", "Running")  # 待机还在跑，换班要把它停掉
        self.status = {a: "Running", b: "Running"}
        self._wire()
        group_id = self._group(
            [
                {"server_id": a, "hours": 12},
                {"server_id": b, "hours": 12},
            ]
        )

        for _ in range(4):
            self._reconcile(group_id)

        self.assertEqual(self.sent, [], "通知开关关着，不该发报告")
        events = self._events(group_id)
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        event = events[0]
        self.assertEqual(event["status"], "done")
        self.assertTrue(event["title"].endswith("：换班完成"), event["title"])
        # 关键：概览必须是结算结论，不能还停在「正在启动当班 A」
        self.assertEqual(event["detail"], "当班 当班A / 待机 1 台 / ⏸ 未绑定")


class FailoverIsOneEvent(_Base):
    """一次转移 = 一条事件。"""

    def test_traffic_failover_keeps_single_event(self):
        a = _make_server("当班A", "Running", traffic=195)  # 已超阈值 188
        b = _make_server("待机B", "Stopped", traffic=10)
        self.status = {a: "Running", b: "Stopped"}
        self._wire(start_to="Running")
        group_id = self._group(
            [
                {"server_id": a, "hours": 12},
                {"server_id": b, "hours": 12},
            ]
        )

        for _ in range(4):
            self._reconcile(group_id)

        events = self._events(group_id)
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        event = events[0]
        self.assertEqual(event["kind"], "shift_failover")
        self.assertTrue(event["title"].endswith("：故障转移"), event["title"])
        self.assertEqual(event["status"], "done")
        # 决策、启动、停机、结算都在同一条事件里
        texts = [step["text"] for step in event["progress"]]
        self.assertIn(
            "当班 当班A 流量超限（195.00 GB / 阈值 188 GB），改由 待机B 接班", texts
        )
        self.assertIn("已下发启动当班 待机B", texts)
        self.assertIn("全部状态确认完毕", texts)
        self.assertEqual(event["detail"].split(" / ")[0], "当班 待机B")
        # 报告只发一条
        self.assertEqual([m.kind for m in self.sent], ["shift_failover"], self.sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
