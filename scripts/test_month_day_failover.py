#!/usr/bin/env python3
"""回归测试：一月一轮的「按天代班」。

背景：一月一轮一段是几天甚至整月。当班被抢占、180 秒拉不起来就转给别的成员，
如果不管，顶班那台会一直顶到这一段结束 —— 组员 1 的 8 天全算到组员 2 头上，
用户「每台都摊一点、谁都不超月费上限」的排班目的就废了。

所以要隔天 0 点把代班清掉，重新请排班表上的当班上线：
  - 它恢复了 → 自然复位，回到原排班；
  - 还是起不来 → 180 秒后按今天的流量重新挑一台顶（三台以上会自然轮换）。

锁死这几条：
  1. 跨日复位的时机是自然日 0 点，第二天第一轮就把班交回排班表；
  2. 同一天内不重复复位（否则每分钟都把代班者踢下来）；
  3. 流量超限的故障转移不按天复位（原当班已经烧穿阈值，整段都不能再跑）；
  4. 一天一轮完全不受影响（段本来就短，下个换班点自然复位）；
  5. 升级前留下的、没有 duty_day 的旧 run 不被误复位；
  6. 复位会把 failover_tried 黑名单一起清掉，否则每天都认死同一台。

跑法：python scripts/test_month_day_failover.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-monthday-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, notify, repo, settings, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
STALL = settings.DUTY_STALL_SECONDS


def _this_month(day: int, hour: int = 0, minute: int = 30) -> float:
    now = dt.datetime.now(TZ)
    return now.replace(
        day=day, hour=hour, minute=minute, second=0, microsecond=0
    ).timestamp()


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

    def _wire(self, start_to: str = "Starting") -> None:
        def _fake_refresh(server: dict) -> None:  # noqa: ANN001
            state = self.status[int(server["id"])]
            server["instance_status"] = state
            server["stopped_mode"] = "StopCharging" if state == "Stopped" else ""
            server["public_ip"] = "47.0.0.1" if state == "Running" else ""
            server["traffic_used"] = float(server.get("traffic_used") or 0.0)
            # 真实 _refresh 会把状态写回库，桩也要写：不写的话下一轮 repo.get_server
            # 又读到旧状态（比如昨天代班那台仍显示 Stopped），停机判定就漏了。
            repo.update_server_runtime(
                int(server["id"]),
                status=state,
                stopped_mode=server["stopped_mode"],
                public_ip=server["public_ip"],
                traffic=server["traffic_used"],
            )

        def _fake_start(server: dict) -> None:  # noqa: ANN001
            self.started.append(int(server["id"]))
            self.status[int(server["id"])] = start_to

        def _fake_stop(  # noqa: ANN001, ARG001
            server: dict, mode: str = "StopCharging", fallback: bool = False
        ) -> None:
            self.stopped.append(int(server["id"]))
            self.status[int(server["id"])] = "Stopped"

        shift._refresh = _fake_refresh  # noqa: SLF001
        aliyun.start_instance = _fake_start
        aliyun.stop_instance = _fake_stop

    def _reconcile_at(self, group_id: int, ts: float) -> dict:
        group = repo.get_group(group_id) or {}
        return shift.engine.reconcile_group(
            group,
            repo.get_settings(),
            dt.datetime.fromtimestamp(ts, TZ),
            ts,
            True,
        )

    def _slot(self, group_id: int, ts: float) -> str:
        picked = shift.duty_at(repo.get_group(group_id) or {}, ts)
        return str(picked[1]) if picked else ""

    def _run(self, group_id: int, ts: float) -> dict:
        return repo.get_run(group_id, self._slot(group_id, ts)) or {}

    def _age_flag(self, key: str, seconds: float) -> None:
        db.execute(
            "UPDATE flags SET created_at=? WHERE key=?",
            (int(time.time() - seconds), key),
        )

    def _titles(self) -> list[str]:
        return [e["title"] for e in repo.list_events(limit=100)]


class MonthDayFailover(_Base):
    """一月一轮：隔天 0 点把代班交回排班表。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("组员1", "Running", traffic=1.0)
        self.b = _make_server("组员2", "Stopped", traffic=0.0)
        self.status = {self.a: "Running", self.b: "Stopped"}
        self._wire()
        self.group_id = repo.upsert_group(
            {
                "name": "月组",
                "enabled": True,
                "cycle": "month",
                "members": [
                    {"server_id": self.a, "days": 8},
                    {"server_id": self.b, "days": 8},
                ],
            }
        )
        self.d1 = _this_month(1)
        self.d2 = _this_month(2)
        # 1 号 00:30：A 当班，正常结算
        self._reconcile_at(self.group_id, self.d1)
        self.assertEqual(int(self._run(self.group_id, self.d1).get("duty_server_id")), self.a)

    def _fail_over_to_b(self) -> None:
        """让 A 掉线并推过容忍时间，转移给 B。"""
        self.status[self.a] = "Stopped"
        self._reconcile_at(self.group_id, self.d1)  # 计时 + 拉起（卡在 Starting）
        self._age_flag(f"duty_stall:{self.group_id}:{self._slot(self.group_id, self.d1)}", STALL + 5)
        self._reconcile_at(self.group_id, self.d1)
        run = self._run(self.group_id, self.d1)
        self.assertEqual(int(run.get("duty_server_id") or 0), self.b, run)
        self.assertEqual(str(run.get("failover_reason") or ""), "stall")
        self.assertTrue(
            str(run.get("duty_day") or "").endswith("-01"),
            f"代班日期应落在 1 号: {run.get('duty_day')}",
        )

    def test_next_day_hands_back_to_schedule(self):
        """第二天第一轮：代班清掉，重新按排班表拉原当班。"""
        self._fail_over_to_b()
        self.status[self.a] = "Stopped"
        self.started.clear()

        self._reconcile_at(self.group_id, self.d2)

        run = self._run(self.group_id, self.d2)
        self.assertIn(self.a, self.started, "第二天应先尝试拉起排班表上的当班")
        self.assertEqual(int(run.get("failover") or 0), 0, "代班状态应被清掉")
        self.assertEqual(int(run.get("duty_server_id") or 0), self.a, run)

    def test_same_day_does_not_reset(self):
        """同一天内不再复位，否则每分钟都把代班者踢下来。"""
        self._fail_over_to_b()
        self.started.clear()

        self._reconcile_at(self.group_id, _this_month(1, 6, 0))

        run = self._run(self.group_id, self.d1)
        self.assertNotIn(self.a, self.started, "同一天不该重新拉原当班")
        self.assertEqual(int(run.get("duty_server_id") or 0), self.b, run)
        self.assertEqual(int(run.get("failover") or 0), 1)

    def test_original_recovers_and_takes_over(self):
        """原当班第二天能起来了：复位后它接手，代班那台被停掉。"""
        self._fail_over_to_b()
        self.status[self.a] = "Running"  # 抢占后重新拿到机器
        self.status[self.b] = "Running"  # 昨天代班那台一直在跑
        self.started.clear()
        self.stopped.clear()

        self._reconcile_at(self.group_id, self.d2)  # A 已在跑 → 查非当班
        self._reconcile_at(self.group_id, self.d2)  # 停 B → 结算

        run = self._run(self.group_id, self.d2)
        self.assertEqual(int(run.get("duty_server_id") or 0), self.a, run)
        self.assertIn(self.b, self.stopped, "复位后待机的 B 应该被停掉")
        self.assertEqual(int(run.get("failover") or 0), 0)

    def test_traffic_failover_is_not_reset(self):
        """流量超限的转移不按天复位：原当班整段都不能再跑。"""
        self._fail_over_to_b()
        slot = self._slot(self.group_id, self.d1)
        repo.upsert_run(
            self.group_id, slot, failover_reason="traffic", duty_day=""
        )
        self.started.clear()

        self._reconcile_at(self.group_id, self.d2)

        run = self._run(self.group_id, self.d2)
        self.assertNotIn(self.a, self.started, "流量超限的不该把原当班弄回来")
        self.assertEqual(int(run.get("duty_server_id") or 0), self.b, run)
        self.assertEqual(int(run.get("failover") or 0), 1)

    def test_legacy_run_without_day_is_kept(self):
        """升级前的旧 run 没有 duty_day：补成今天，不能当场把人踢下来。"""
        self._fail_over_to_b()
        slot = self._slot(self.group_id, self.d1)
        repo.upsert_run(self.group_id, slot, duty_day="")
        self.started.clear()

        self._reconcile_at(self.group_id, self.d1)

        run = self._run(self.group_id, self.d1)
        self.assertNotIn(self.a, self.started, "旧数据不该被当成跨日")
        self.assertEqual(int(run.get("duty_server_id") or 0), self.b, run)
        self.assertTrue(str(repo.get_run(self.group_id, slot).get("duty_day") or ""))

    def test_next_day_failover_notifies_again(self):
        """每天 0 点起不来都要有事件 + 故障转移通知。

        通知去重键原本是「组 + 班次 + failover」，一月一轮一段只有一个班次，
        1 号发过之后后面几天全被当成重复静默吞掉 —— 用户明确要每天都有。
        """
        # 第一天：转移 → B 就绪 → 结算，拿到第一条通知
        self._fail_over_to_b()
        self.status[self.b] = "Running"
        self.sent.clear()
        for _ in range(4):
            self._reconcile_at(self.group_id, self.d1)
        first = [m for m in self.sent if m.kind == "shift_failover"]
        self.assertTrue(first, [m.kind for m in self.sent])

        # 第二天：A 还是起不来，照常复位、转移、结算
        self.sent.clear()
        self.started.clear()
        self.stopped.clear()
        self.status[self.a] = "Stopped"
        self.status[self.b] = "Running"
        self._reconcile_at(self.group_id, self.d2)  # 复位 + 拉起 A（卡在 Starting）
        self._age_flag(
            f"duty_stall:{self.group_id}:{self._slot(self.group_id, self.d2)}", STALL + 5
        )
        self._reconcile_at(self.group_id, self.d2)  # 又转回 B
        # 转移后要等原当班停机落地才结算，真实轮询就是每分钟一轮，多跑几轮
        for _ in range(6):
            self._reconcile_at(self.group_id, self.d2)

        again = [m for m in self.sent if m.kind == "shift_failover"]
        self.assertTrue(again, "第二天应再发一条故障转移通知，不能被去重吞掉")
        self.assertEqual(again[-1].title, first[-1].title, "标题不变")
        self.assertIn("当班实例疑被抢占，保活超时", again[-1].text)
        self.assertIn("已启用备用实例", again[-1].text)

    def test_next_day_events_are_closed(self):
        """跨日复位新建的那条换班事件要被收尾，不能在列表里永远转圈。"""
        self._fail_over_to_b()
        self.status[self.b] = "Running"
        for _ in range(3):
            self._reconcile_at(self.group_id, self.d1)  # 转移后要等停机落地才结算

        self.status[self.a] = "Stopped"
        self._reconcile_at(self.group_id, self.d2)
        self._age_flag(
            f"duty_stall:{self.group_id}:{self._slot(self.group_id, self.d2)}", STALL + 5
        )
        self._reconcile_at(self.group_id, self.d2)
        for _ in range(6):
            self._reconcile_at(self.group_id, self.d2)

        running = [
            e for e in repo.list_events(limit=100) if str(e.get("status")) == "running"
        ]
        self.assertEqual(running, [], "不该留下悬而未决的事件")
        # 转移的过程和结果在同一条事件里：结算后标题是「故障转移（保活超时）」，
        # 不会像以前那样先留一条「当班疑被抢占，保活超时，故障转移」再另起一条。
        settled = [
            e
            for e in repo.list_events(limit=100)
            if e.get("kind") == "shift_failover" and "故障转移（保活超时）" in e["title"]
        ]
        self.assertTrue(settled, self._titles())

    def test_reset_clears_failover_blacklist(self):
        """复位要清掉 failover_tried，否则每天都认死同一台。"""
        self._fail_over_to_b()
        slot = self._slot(self.group_id, self.d1)
        repo.set_flag(f"failover_tried:{self.group_id}:{slot}:{self.a}")

        self._reconcile_at(self.group_id, self.d2)

        self.assertFalse(
            repo.get_flag(f"failover_tried:{self.group_id}:{slot}:{self.a}"),
            "跨日复位应清空黑名单",
        )


class DayCycleUntouched(_Base):
    """一天一轮：行为完全不变（不写 duty_day、不跨日复位）。"""

    def test_day_cycle_has_no_duty_day(self):
        a = _make_server("A", "Running", traffic=1.0)
        b = _make_server("B", "Stopped", traffic=0.0)
        self.status = {a: "Running", b: "Stopped"}
        self._wire()
        group_id = repo.upsert_group(
            {
                "name": "日组",
                "enabled": True,
                "members": [
                    {"server_id": a, "hours": 12},
                    {"server_id": b, "hours": 12},
                ],
            }
        )
        ts = float(time.time())
        self._reconcile_at(group_id, ts)

        self.status[a] = "Stopped"
        self._reconcile_at(group_id, ts)
        picked = shift.duty_at(repo.get_group(group_id) or {}, ts)
        slot = str(picked[1])
        self._age_flag(f"duty_stall:{group_id}:{slot}", STALL + 5)
        self._reconcile_at(group_id, ts)

        run = repo.get_run(group_id, slot) or {}
        self.assertEqual(int(run.get("duty_server_id") or 0), b, run)
        self.assertEqual(str(run.get("failover_reason") or ""), "stall")
        self.assertEqual(
            str(run.get("duty_day") or ""),
            "",
            "一天一轮不记代班日期，跨日复位对它无效",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
