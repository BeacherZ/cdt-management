#!/usr/bin/env python3
"""回归测试：当班起不来（被抢占后拉不起来 / 启动卡住）按故障转移处理。

锁死这几条：
  1. 当班刚掉线时不立刻换人 —— 先记时、先尝试拉起（冷启动要几十秒，不能当死）。
  2. 超过 DUTY_STALL_SECONDS 仍未运行 → 换一台顶这一班，run 记 failover_reason='stall'、
     failover_from=原当班，后续按正常换班流程结算。
  3. 结算通知正文说「当班实例疑被抢占，保活超时」，不是「流量超限」；
     被接管那台的块标题是「离线账号」而不是「超限账号」。
  4. 组里没启用故障转移 → 只留一条 warning，不自作主张换人。
  5. 同一班次里顶过班的成员不再重复挑，全都试完就记一条 error 等人工，
     不会几台之间来回踢皮球。
  6. 手动换班的班次不自动转移（人正盯着界面）。
  7. 一天一轮同样适用：这一班由别人顶上，下一个轮到它的班次会重新尝试拉起，
     不需要「按天复位」那套（那只是月组为了不让一台顶满整月才加的）。

跑法：python scripts/test_stall_failover.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-stall-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, messages, notify, repo, settings, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
STALL = settings.DUTY_STALL_SECONDS


def _now_dt() -> dt.datetime:
    return dt.datetime.now(TZ)


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
        """接上桩：状态由 self.status 决定，开机后落到 start_to（默认卡在 Starting）。"""

        def _fake_refresh(server: dict) -> None:  # noqa: ANN001
            state = self.status[int(server["id"])]
            server["instance_status"] = state
            server["stopped_mode"] = "StopCharging" if state == "Stopped" else ""
            server["public_ip"] = "47.0.0.1" if state == "Running" else ""
            server["traffic_used"] = float(server.get("traffic_used") or 0.0)

        def _fake_start(server: dict) -> None:  # noqa: ANN001
            self.started.append(int(server["id"]))
            self.status[int(server["id"])] = start_to

        def _fake_stop(server: dict, mode: str = "StopCharging", fallback: bool = False) -> None:  # noqa: ANN001, ARG001
            self.stopped.append(int(server["id"]))
            self.status[int(server["id"])] = "Stopped"

        shift._refresh = _fake_refresh  # noqa: SLF001
        aliyun.start_instance = _fake_start
        aliyun.stop_instance = _fake_stop

    def _reconcile(self, group_id: int, notify_enabled: bool = True) -> dict:
        group = repo.get_group(group_id) or {}
        return shift.engine.reconcile_group(
            group, repo.get_settings(), _now_dt(), float(time.time()), notify_enabled
        )

    def _events(self) -> list[dict]:
        return repo.list_events(limit=100)

    def _titles(self) -> list[str]:
        return [e["title"] for e in self._events()]

    def _age_flag(self, key: str, seconds: float) -> None:
        """把标记的时间往前推，模拟「已经等了这么久」。"""
        db.execute(
            "UPDATE flags SET created_at=? WHERE key=?",
            (int(time.time() - seconds), key),
        )


class StallEscalates(_Base):
    """当班被抢后拉不起来 → 换一台顶班。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("当班A")
        self.b = _make_server("待机B")
        self.status = {self.a: "Running", self.b: "Stopped"}
        self._wire()
        self.group_id = repo.upsert_group(
            {
                "name": "换班组",
                "enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )
        # 先跑一轮落到「已结算」：A 当班在跑，B 停着
        self._reconcile(self.group_id)
        self.assertEqual(self._duty(), self.a, "首轮应把 A 认成当班")
        self.assertEqual(
            str((repo.get_run(self.group_id, self._slot()) or {}).get("phase")), "settled"
        )

    def _slot(self) -> str:
        run = repo.recent_runs(1)
        return str(run[0]["slot"]) if run else ""

    def _duty(self) -> int:
        run = repo.get_run(self.group_id, self._slot()) or {}
        return int(run.get("duty_server_id") or 0)

    def test_first_round_only_restarts_and_starts_clock(self):
        """刚掉线只拉起 + 计时，不换人。"""
        self.status[self.a] = "Stopped"
        self.started.clear()
        self.stopped.clear()

        self._reconcile(self.group_id)

        self.assertEqual(self.started, [self.a], "应先尝试拉起原当班")
        self.assertEqual(self.stopped, [], "这时不该停任何人")
        self.assertTrue(
            any("已自动拉起" in t for t in self._titles()), self._titles()
        )
        run = repo.get_run(self.group_id, self._slot()) or {}
        self.assertEqual(int(run.get("failover") or 0), 0, "计时刚开始，不该记故障转移")
        self.assertTrue(repo.get_flag(f"duty_stall:{self.group_id}:{self._slot()}"))

    def test_three_attempts_before_failover(self):
        """一分钟试一次，试满 3 次还起不来才换人（口径：试 3 次，不是试 3 秒）。"""
        # 每次拉起都原地回到 Stopped：模拟「指令发了但机器根本起不来」，
        # 这样每分钟那一轮都会再试一次，才数得出真实的尝试次数。
        self._wire(start_to="Stopped")
        self.status[self.a] = "Stopped"
        self.started.clear()
        stall = f"duty_stall:{self.group_id}:{self._slot()}"

        self._reconcile(self.group_id)  # 第 1 分钟：记时 + 第 1 次拉起
        self.assertEqual(
            [s for s in self.started if s == self.a], [self.a], self.started
        )

        for minutes, expected in ((60, 2), (120, 3)):
            self._age_flag(stall, minutes)
            self._reconcile(self.group_id)
            self.assertEqual(
                len([s for s in self.started if s == self.a]),
                expected,
                f"第 {minutes // 60 + 1} 分钟该再试一次：{self.started}",
            )
            self.assertEqual(self._duty(), self.a, "还没试满 3 次，不许换人")

        # 第 4 分钟：3 次都试过了，当班还是起不来 → 换人
        self._age_flag(stall, STALL + 5)
        self._reconcile(self.group_id)
        self.assertEqual(
            int((repo.get_run(self.group_id, self._slot()) or {}).get("duty_server_id") or 0),
            self.b,
            self._titles(),
        )
        self.assertIn(self.b, self.started, "试满 3 次仍起不来，该换 B 顶这一班")

    def test_escalates_after_stall_timeout(self):
        """超过容忍时间仍未运行 → 转给另一台，原因记 stall。"""
        self.status[self.a] = "Stopped"
        self._reconcile(self.group_id)  # 开始计时 + 拉起（卡在 Starting）
        self._age_flag(f"duty_stall:{self.group_id}:{self._slot()}", STALL + 5)

        self.started.clear()
        result = self._reconcile(self.group_id)

        run = repo.get_run(self.group_id, self._slot()) or {}
        self.assertEqual(int(run.get("duty_server_id") or 0), self.b, result)
        self.assertEqual(int(run.get("failover") or 0), 1)
        self.assertEqual(str(run.get("failover_reason")), "stall")
        self.assertEqual(int(run.get("failover_from") or 0), self.a)
        self.assertIn(self.b, self.started, "应去拉起接替的那台")
        self.assertTrue(
            any("当班疑被抢占，保活超时，故障转移" in t for t in self._titles()), self._titles()
        )
        # 原当班也进了待停名单：接替者就绪后的下一轮才真正下发停机
        self.status[self.b] = "Running"
        self._reconcile(self.group_id)
        self.assertIn(self.a, self.stopped, "起不来的原当班要让位")

    def test_settle_reports_keepalive_timeout_not_traffic(self):
        """结算报告说「保活超时」，被接管那台叫「离线账号」。"""
        self.status[self.a] = "Stopped"
        self._reconcile(self.group_id)
        self._age_flag(f"duty_stall:{self.group_id}:{self._slot()}", STALL + 5)
        self._reconcile(self.group_id)  # 转到 B，B 开始启动

        self.status[self.b] = "Running"
        self.sent.clear()
        self._reconcile(self.group_id)  # B 就绪 → 停原当班，这一轮还不算全部到位
        self._reconcile(self.group_id)  # 原当班已停 → 结算

        failover_msgs = [m for m in self.sent if m.kind == "shift_failover"]
        self.assertTrue(failover_msgs, [m.kind for m in self.sent])
        msg = failover_msgs[-1]
        self.assertIn("当班实例疑被抢占，保活超时", msg.text)
        self.assertNotIn("流量超限", msg.text)
        self.assertIn("离线账号", msg.text)
        self.assertNotIn("超限账号", msg.text)
        # 事件标题能区分是哪一种转移
        self.assertTrue(
            any("故障转移（保活超时）" in t for t in self._titles()), self._titles()
        )

    def test_no_duplicate_recovery_event_after_switch(self):
        """转移后不该再留一条「已自动拉起」在转圈。"""
        self.status[self.a] = "Stopped"
        self._reconcile(self.group_id)
        self._age_flag(f"duty_stall:{self.group_id}:{self._slot()}", STALL + 5)
        self._reconcile(self.group_id)

        recovery = [
            e
            for e in self._events()
            if e.get("kind") == "recovery" and e.get("status") == "running"
        ]
        self.assertEqual(recovery, [], [e["title"] for e in self._events()])


class DayCycleStall(_Base):
    """一天一轮（含 0–24 点时刻表）：当班被抢占同样由另一台顶这一班。

    和一月一轮的区别只在「要不要按天复位」：一天一轮一段只有几小时，下一个
    换班点排班表自然换人，落后的那台会在自己的班次里被重新尝试拉起，
    所以这里不写 duty_day、也不做跨日复位。
    """

    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("一天A")
        self.b = _make_server("一天B")
        self._wire(start_to="Stopped")  # 下发开机也不管事：真被抢占就是这样，一直 Stopped
        self.group_id = repo.upsert_group(
            {
                "name": "一天一轮",
                "enabled": True,
                "cycle": "day",
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )
        # 用真实排班函数定位「此刻谁当班」，不假设 anchor 落在哪个成员上
        self.t0 = float(time.time())
        picked = shift.duty_at(repo.get_group(self.group_id) or {}, self.t0)
        self.duty = int(picked[0]["server_id"])
        self.other = self.b if self.duty == self.a else self.a
        self.slot = str(picked[1])
        self.status = {self.duty: "Running", self.other: "Stopped"}
        self._reconcile_at(self.t0)
        self.assertEqual(self._run(self.slot).get("phase"), "settled")

    def _run(self, slot: str) -> dict:
        return repo.get_run(self.group_id, slot) or {}

    def _slot_of(self, now_ts: float) -> str:
        picked = shift.duty_at(repo.get_group(self.group_id) or {}, now_ts)
        return str(picked[1])

    def _reconcile_at(self, now_ts: float) -> dict:
        return shift.engine.reconcile_group(
            repo.get_group(self.group_id) or {},
            repo.get_settings(),
            dt.datetime.fromtimestamp(now_ts, TZ),
            float(now_ts),
        )

    def test_preempted_duty_fails_over_without_day_fields(self):
        """当班被抢占拉不起来 → 另一台顶这一班，不写 duty_day。"""
        self.status[self.duty] = "Stopped"
        self.started.clear()
        self._reconcile_at(self.t0)
        self.assertEqual(self.started, [self.duty], "第一轮先试着拉起原当班")

        self._age_flag(f"duty_stall:{self.group_id}:{self.slot}", STALL + 5)
        self._reconcile_at(self.t0)

        run = self._run(self.slot)
        self.assertEqual(int(run.get("duty_server_id") or 0), self.other, run)
        self.assertEqual(str(run.get("failover_reason")), "stall")
        self.assertEqual(str(run.get("duty_day") or ""), "", "一天一轮不需要按天复位字段")

    def test_next_slot_retries_the_scheduled_server(self):
        """下一个轮到它的班次，排班表那台会被重新尝试拉起。

        这一段是用户问的核心：一天一轮不用管「整月都被代班」，因为几小时后
        排班表又会点到这台，系统会再拉一次；它要是还起不来，才由另一台顶这一班。
        """
        # 本节：原当班起不来 → 另一台顶班
        self.status[self.duty] = "Stopped"
        self._reconcile_at(self.t0)
        self._age_flag(f"duty_stall:{self.group_id}:{self.slot}", STALL + 5)
        self._reconcile_at(self.t0)
        self.assertEqual(int(self._run(self.slot).get("duty_server_id") or 0), self.other)

        # 两轮 24 小时后，排班表又轮到同一台
        later = self.t0 + 24 * 3600 + 60
        slot = self._slot_of(later)
        self.assertNotEqual(slot, self.slot, "应已进入新的班次")
        self.started.clear()
        self.stopped.clear()
        self._reconcile_at(later)

        run = self._run(slot)
        self.assertEqual(int(run.get("duty_server_id") or 0), self.duty, "新班次应回到排班表")
        self.assertEqual(self.started, [self.duty], "排班表那台必须被重新尝试拉起")
        self.assertNotIn(self.other, self.stopped, "新当班还没起来前不能把当前在跑的停掉")

        # 还是起不来：这一班同样交给另一台，不会空着
        self._age_flag(f"duty_stall:{self.group_id}:{slot}", STALL + 5)
        self._reconcile_at(later)
        self.assertEqual(
            int(self._run(slot).get("duty_server_id") or 0), self.other, "这一班也要有人顶"
        )


class StallRespectsSettings(_Base):
    """关掉故障转移 / 手动换班时不自动转移。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("当班A")
        self.b = _make_server("待机B")
        self.status = {self.a: "Running", self.b: "Stopped"}
        self._wire()

    def _group(self, failover_enabled: bool = True) -> int:
        group_id = repo.upsert_group(
            {
                "name": "换班组",
                "enabled": True,
                "failover_enabled": failover_enabled,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )
        self._reconcile(group_id)
        return group_id

    def test_disabled_failover_keeps_waiting(self):
        group_id = self._group(failover_enabled=False)
        slot = str(repo.recent_runs(1)[0]["slot"])

        self.status[self.a] = "Stopped"
        self._reconcile(group_id)
        self._age_flag(f"duty_stall:{group_id}:{slot}", STALL + 5)
        self.started.clear()
        self._reconcile(group_id)

        run = repo.get_run(group_id, slot) or {}
        self.assertEqual(int(run.get("failover") or 0), 0, "关掉故障转移就不该换人")
        self.assertNotIn(self.b, self.started)
        self.assertTrue(
            any("未启用故障转移" in (e.get("detail") or "") for e in self._events()),
            [e.get("detail") for e in self._events()],
        )

    def test_manual_switch_is_not_overridden(self):
        """手动切到 A 之后 A 起不来，系统不能自己换回去。"""
        group_id = self._group()
        slot = str(repo.recent_runs(1)[0]["slot"])
        repo.upsert_run(group_id, slot, manual=1)

        self.status[self.a] = "Stopped"
        self._reconcile(group_id)
        self._age_flag(f"duty_stall:{group_id}:{slot}", STALL + 5)
        self.started.clear()
        self._reconcile(group_id)

        run = repo.get_run(group_id, slot) or {}
        self.assertEqual(int(run.get("failover") or 0), 0)
        self.assertNotIn(self.b, self.started, "人手动指定的当班，系统不该改")

    def test_second_bad_member_is_not_ping_ponged(self):
        """顶班的那台也起不来时，不会在两台之间来回踢。"""
        group_id = self._group()
        slot = str(repo.recent_runs(1)[0]["slot"])

        # 第一次：A 掉线超时 → 转给 B（B 也卡在 Starting）
        self.status[self.a] = "Stopped"
        self._reconcile(group_id)
        self._age_flag(f"duty_stall:{group_id}:{slot}", STALL + 5)
        self._reconcile(group_id)
        run = repo.get_run(group_id, slot) or {}
        self.assertEqual(int(run.get("duty_server_id") or 0), self.b)

        # 第二次：B 也起不来。转移时计时被清空，要重新等满一轮才判超时 ——
        # 这也是对的：新当班同样享有完整的冷启动时间。
        self._reconcile(group_id)
        self._age_flag(f"duty_stall:{group_id}:{slot}", STALL + 5)
        self._reconcile(group_id)

        run = repo.get_run(group_id, slot) or {}
        self.assertEqual(int(run.get("duty_server_id") or 0), self.b, "不该又切回 A")
        self.assertTrue(
            any("没有可接替的成员" in t for t in self._titles()), self._titles()
        )


class MessageMapping(unittest.TestCase):
    """文案层：两种原因各说各的，别混。"""

    def test_traffic_reason_keeps_original_wording(self):
        server = {"label": "A", "instance_id": "i-a", "instance_status": "Stopped"}
        msg = messages.shift_failover(server, [server], [], "✅ 已同步", "组", reason="traffic")
        self.assertIn("当班实例流量超限", msg.text)
        self.assertIn("超限账号", msg.text)
        self.assertNotIn("保活超时", msg.text)

    def test_stall_reason_switches_wording(self):
        server = {"label": "A", "instance_id": "i-a", "instance_status": "Stopped"}
        msg = messages.shift_failover(server, [server], [], "✅ 已同步", "组", reason="stall")
        self.assertIn("当班实例疑被抢占，保活超时", msg.text)
        self.assertIn("离线账号", msg.text)
        self.assertNotIn("流量超限", msg.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
