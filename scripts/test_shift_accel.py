#!/usr/bin/env python3
"""换班加速通道的回归测试（dry-run 内存桩，不碰真实阿里云）。

锁死四条：
  1. 加速轮只读状态、**不查流量**：CDT 是账号级接口，跟着 10 秒一轮纯属白烧。
  2. 加速窗口只在「下发指令之后」打开，到位（ready）就关掉：
     没有指令在飞时不提速，等 DDNS 之类也不提速；且不无限续期。
  3. 加速轮里 60 秒内不重复下发开机指令，避免每 10 秒砸一次 StartInstance；
     日常巡检不受这条限制（它读的是真实状态，读到 Stopped 就该重试）。
  4. 「当班起不来」判死要次数 + 墙钟双条件：加速后次数涨得快，
     只看次数会在 30 秒内误判、连环换人。

跑法：python scripts/test_shift_accel.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-shift-accel-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, repo, settings, shift  # noqa: E402


def _make_server(label: str, instance_id: str, status: str = "Stopped") -> int:
    server_id = repo.upsert_server(
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
    repo.update_server_runtime(
        server_id,
        status=status,
        stopped_mode="StopCharging" if status == "Stopped" else "",
        public_ip="47.0.0.1" if status == "Running" else "",
    )
    return server_id


def _now_dt() -> dt.datetime:
    return dt.datetime.now(shift._timezone("Asia/Shanghai"))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        for table in ("events", "flags", "shift_runs", "shift_members", "shift_groups", "servers"):
            db.execute(f"DELETE FROM {table}")
        # engine 是全局单例，加速窗口 / 预算 / 组锁归属都是内存状态，测试间必须清干净
        shift.engine._awaiting.clear()  # noqa: SLF001
        shift.engine._await_used.clear()  # noqa: SLF001
        shift.engine._traffic_checked.clear()  # noqa: SLF001
        shift.engine._group_owner.clear()  # noqa: SLF001
        self.a_id = _make_server("当班A", "i-accel-aaa")
        self.b_id = _make_server("待机B", "i-accel-bbb", status="Running")
        self.group_id = repo.upsert_group(
            {
                "name": "加速组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": self.a_id, "hours": 12},
                    {"server_id": self.b_id, "hours": 12},
                ],
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), self.group_id),
        )

        self.states: dict[int, str] = {self.a_id: "Stopped", self.b_id: "Running"}
        self.started: list[int] = []
        self.stopped: list[int] = []
        self._orig_refresh = shift._refresh  # noqa: SLF001
        self._orig_start = aliyun.start_instance
        self._orig_stop = aliyun.stop_instance
        shift._refresh = self._fake_refresh  # noqa: SLF001
        aliyun.start_instance = self._fake_start
        aliyun.stop_instance = self._fake_stop

    def tearDown(self) -> None:
        shift._refresh = self._orig_refresh  # noqa: SLF001
        aliyun.start_instance = self._orig_start
        aliyun.stop_instance = self._orig_stop

    # -- 桩：状态由 states 决定，开关机只改 states，不碰阿里云 ---------------
    def _fake_refresh(self, server: dict, with_traffic: bool = True) -> None:  # noqa: ANN001, ARG002
        state = self.states.get(int(server["id"]), "Stopped")
        server["instance_status"] = state
        server["stopped_mode"] = "StopCharging" if state == "Stopped" else ""
        server["public_ip"] = "47.0.0.1" if state == "Running" else ""

    def _fake_start(self, server: dict) -> None:  # noqa: ANN001
        self.started.append(int(server["id"]))
        self.states[int(server["id"])] = "Starting"

    def _fake_stop(self, server: dict, mode: str = "StopCharging", fallback: bool = False) -> str:  # noqa: ANN001, ARG002
        self.stopped.append(int(server["id"]))
        self.states[int(server["id"])] = "Stopped"
        return "StopCharging"

    def _reconcile(self, fast: bool = False) -> dict:
        group = repo.get_group(self.group_id) or {}
        return shift.engine.reconcile_group(
            group,
            repo.get_settings(),
            _now_dt(),
            time.time(),
            notify_enabled=False,
            fast=fast,
        )

    def _slot(self) -> str:
        row = db.query_one(
            "SELECT slot FROM shift_runs WHERE group_id=? ORDER BY updated_at DESC LIMIT 1",
            (self.group_id,),
        )
        self.assertIsNotNone(row, "这一轮应该已经建过 run")
        return str(row["slot"])


class FastSkipsTraffic(_Base):
    def test_fast_round_does_not_query_traffic(self) -> None:
        shift._refresh = self._orig_refresh  # noqa: SLF001  用真实读取，才测得到流量查询
        calls: list[int] = []
        orig = aliyun.get_traffic_gb

        def _fake(account: dict) -> float:  # noqa: ANN001
            calls.append(int(account["id"]))
            return 0.0

        aliyun.get_traffic_gb = _fake
        try:
            self._reconcile(fast=True)
            self.assertEqual(calls, [], "加速轮不该查 CDT 流量")
            self._reconcile(fast=False)
            self.assertEqual(
                sorted(calls), sorted([self.a_id, self.b_id]), "日常轮照旧每台查一次"
            )
        finally:
            aliyun.get_traffic_gb = orig

    def test_fast_round_keeps_previous_traffic_value(self) -> None:
        shift._refresh = self._orig_refresh  # noqa: SLF001
        db.execute("UPDATE servers SET traffic_used=? WHERE id=?", (120.0, self.a_id))
        self._reconcile(fast=True)
        row = db.query_one("SELECT traffic_used FROM servers WHERE id=?", (self.a_id,))
        self.assertEqual(float(row["traffic_used"]), 120.0)


class AwaitWindow(_Base):
    def test_window_opens_on_command_and_closes_when_ready(self) -> None:
        self.assertEqual(shift.engine.awaiting_groups(), [], "没下发指令时不该加速")

        self._reconcile(fast=False)
        self.assertIn(self.group_id, shift.engine.awaiting_groups())

        self.states[self.a_id] = "Running"
        self.states[self.b_id] = "Stopped"
        self._reconcile(fast=True)
        self.assertNotIn(
            self.group_id, shift.engine.awaiting_groups(), "全部到位后必须退出加速"
        )

    def test_window_does_not_extend_forever(self) -> None:
        shift.engine._mark_awaiting(self.group_id, "slot-1")  # noqa: SLF001
        first = shift.engine._awaiting[self.group_id]  # noqa: SLF001
        shift.engine._mark_awaiting(self.group_id, "slot-1")  # noqa: SLF001
        self.assertEqual(
            shift.engine._awaiting[self.group_id],  # noqa: SLF001
            first,
            "同一班次重复登记不该续期，否则卡死时加速永不退出",
        )

    def test_expired_window_is_dropped(self) -> None:
        now = time.time()
        shift.engine._awaiting[self.group_id] = ("slot-1", now - 10, now - 1)  # noqa: SLF001
        self.assertEqual(shift.engine.awaiting_groups(), [])

    def test_budget_exhausted_stops_reopening(self) -> None:
        """卡死时每轮巡检都会重发指令，光靠 TTL 挡不住窗口被反复打开。

        预算用尽后不再打开窗口 —— 否则 10 秒一轮就成了常态（3 台组每天
        约 2.6 万次 DescribeInstances，正好撞在风控上）。
        """
        now = time.time()
        shift.engine._await_used[self.group_id] = (  # noqa: SLF001
            "slot-1",
            float(settings.SHIFT_AWAIT_BUDGET),
        )
        shift.engine._mark_awaiting(self.group_id, "slot-1")  # noqa: SLF001
        self.assertEqual(shift.engine.awaiting_groups(), [], "预算用尽不该再开窗口")

    def test_new_shift_resets_budget(self) -> None:
        shift.engine._await_used[self.group_id] = (  # noqa: SLF001
            "slot-1",
            float(settings.SHIFT_AWAIT_BUDGET),
        )
        shift.engine._mark_awaiting(self.group_id, "slot-2")  # noqa: SLF001
        self.assertIn(self.group_id, shift.engine.awaiting_groups(), "换班次该给新额度")

    def test_read_failure_closes_window(self) -> None:
        """状态读不到，「等状态到位」这个前提就不成立了：关窗口回落巡检节奏。

        不关的话故障期间每 10 秒重来一次，既刷接口，事件里写的
        「每 N 秒重试」也和实际频率对不上。
        """

        def _boom(server: dict, with_traffic: bool = True) -> None:  # noqa: ANN001, ARG001
            raise RuntimeError("network unreachable")

        self._reconcile(fast=False)  # 正常一轮：下发开机，窗口打开
        self.assertIn(self.group_id, shift.engine.awaiting_groups())

        shift._refresh = _boom  # noqa: SLF001
        try:
            result = self._reconcile(fast=True)
        finally:
            shift._refresh = self._fake_refresh  # noqa: SLF001
        self.assertIn("state_unreadable", result)
        self.assertNotIn(
            self.group_id, shift.engine.awaiting_groups(), "读不到状态必须退出加速"
        )

    def test_accel_round_does_not_write_read_errors(self) -> None:
        """加速轮不写健康事件：失败计数和「每 N 秒重试」都按巡检间隔算的。"""

        def _boom(server: dict, with_traffic: bool = True) -> None:  # noqa: ANN001, ARG001
            raise RuntimeError("network unreachable")

        shift._refresh = _boom  # noqa: SLF001
        self._reconcile(fast=True)
        shift._refresh = self._fake_refresh  # noqa: SLF001
        self.assertIsNone(
            repo.pending_event(self.a_id, "read_error"),
            "加速轮不该把一次抖动写成巡检异常事件",
        )

    def test_accel_round_backfills_traffic_when_stale(self) -> None:
        """日常巡检被跳过的那几轮没人读流量，加速通道要隔两轮补读一次。"""
        shift._refresh = self._orig_refresh  # noqa: SLF001
        calls: list[int] = []
        orig = aliyun.get_traffic_gb

        def _fake(account: dict) -> float:  # noqa: ANN001
            calls.append(int(account["id"]))
            return 0.0

        aliyun.get_traffic_gb = _fake
        try:
            shift.engine._mark_awaiting(self.group_id, "slot-1")  # noqa: SLF001
            shift.engine._traffic_checked.pop(self.group_id, None)  # noqa: SLF001
            shift.engine.reconcile_awaiting()  # noqa: SLF001
            self.assertTrue(calls, "读数太久没更新时，加速轮应补读一次流量")
        finally:
            aliyun.get_traffic_gb = orig


class StopThrottle(_Base):
    def test_fast_round_does_not_resend_stop(self) -> None:
        """停机接口成功但实例没停 → 10 秒一轮会变成 10 秒砸一次 StopInstance。"""
        # 让当班已经 Running、待机一直停不下来
        self.states[self.a_id] = "Running"

        def _stuck(server: dict, mode: str = "StopCharging", fallback: bool = False) -> str:  # noqa: ANN001, ARG002
            self.stopped.append(int(server["id"]))
            return "StopCharging"

        aliyun.stop_instance = _stuck
        for _ in range(5):
            self._reconcile(fast=True)
        self.assertEqual(len(self.stopped), 1, "60 秒内不该重复下发停机指令")

    def test_daily_round_still_resends_stop(self) -> None:
        self.states[self.a_id] = "Running"

        def _stuck(server: dict, mode: str = "StopCharging", fallback: bool = False) -> str:  # noqa: ANN001, ARG002
            self.stopped.append(int(server["id"]))
            return "StopCharging"

        aliyun.stop_instance = _stuck
        self._reconcile(fast=False)
        self._reconcile(fast=False)
        self.assertEqual(len(self.stopped), 2, "日常轮按真实状态重试，不受加速节流")


class UserEntryTakesGroupLock(_Base):
    """手动换班 / 解散 / 新建组必须和加速通道互斥，否则同一组被两条线推进。

    这三条入口以前只拿全局对账锁，而加速通道**不拿**全局锁，两边完全并行：
    实测会出现同一台机器连收两次 StopInstance。这里用「锁被占住」验证它们
    会挡住，而不是硬闯。
    """

    def setUp(self) -> None:
        super().setUp()
        self._orig_timeout = shift._GROUP_LOCK_TIMEOUT  # noqa: SLF001
        shift._GROUP_LOCK_TIMEOUT = 0.2  # noqa: SLF001

    def tearDown(self) -> None:
        shift._GROUP_LOCK_TIMEOUT = self._orig_timeout  # noqa: SLF001
        super().tearDown()

    def test_manual_switch_refuses_when_group_busy(self) -> None:
        self.assertTrue(shift.engine._group_lock(self.group_id))  # noqa: SLF001
        try:
            with self.assertRaises(ValueError):
                shift.engine.manual_switch(self.group_id)
        finally:
            shift.engine._group_unlock(self.group_id)  # noqa: SLF001

    def test_dissolve_refuses_when_group_busy(self) -> None:
        self.assertTrue(shift.engine._group_lock(self.group_id))  # noqa: SLF001
        try:
            with self.assertRaises(ValueError):
                shift.engine.dissolve_group(self.group_id)
        finally:
            shift.engine._group_unlock(self.group_id)  # noqa: SLF001
        self.assertIsNotNone(
            repo.get_group(self.group_id), "拿不到锁就不许删，否则「保持原状」不成立"
        )

    def test_bootstrap_defers_when_group_busy(self) -> None:
        self.assertTrue(shift.engine._group_lock(self.group_id))  # noqa: SLF001
        try:
            result = shift.engine.bootstrap_group(self.group_id)
        finally:
            shift.engine._group_unlock(self.group_id)  # noqa: SLF001
        self.assertTrue(result.get("deferred"), "拿不到锁应该交给正在跑的那一轮")
        self.assertEqual(self.started, [], "拿不到锁就不许下发开机指令")

    def test_manual_action_refuses_when_group_busy(self) -> None:
        self.assertTrue(shift.engine._group_lock(self.group_id))  # noqa: SLF001
        try:
            with self.assertRaises(ValueError):
                shift.engine.manual_action(self.a_id, "stop")
        finally:
            shift.engine._group_unlock(self.group_id)  # noqa: SLF001
        self.assertEqual(self.stopped, [], "拿不到锁就不许下发停机指令")


class StartThrottle(_Base):
    def test_fast_round_does_not_resend_start_within_minutes(self) -> None:
        self._reconcile(fast=True)
        self.assertEqual(len(self.started), 1, "第一轮应该下发开机")
        self._reconcile(fast=True)
        self.assertEqual(len(self.started), 1, "60 秒内不该重发开机指令")

    def test_daily_round_still_retries_immediately(self) -> None:
        self._reconcile(fast=True)
        self.assertEqual(len(self.started), 1)
        # 日常巡检读的是真实状态：仍是 Stopped 就该按「尝试次数」继续重试，
        # 不能被节流按时间猜成「正在启动」。
        self.states[self.a_id] = "Stopped"
        self._reconcile(fast=False)
        self.assertEqual(len(self.started), 2, "日常轮不受加速节流影响")

    def test_throttled_round_does_not_bump_tries(self) -> None:
        self._reconcile(fast=True)
        slot = self._slot()
        tries_key = f"duty_tries:{self.group_id}:{slot}"
        before = repo.flag_value(tries_key)
        self.states[self.a_id] = "Stopped"
        self._reconcile(fast=True)
        self.assertEqual(
            repo.flag_value(tries_key), before, "被节流的那一轮没有下发指令，不该计一次尝试"
        )


class StallNeedsWallClock(_Base):
    def _seed_tries(self, count: int, stall_age: float) -> None:
        picked = shift.actual_duty(repo.get_group(self.group_id), time.time())
        self.assertIsNotNone(picked, "组里应该排得出当班")
        self.slot = str(picked[1])
        repo.upsert_run(self.group_id, self.slot, phase=shift.PHASE_SWITCHING)
        tries_key = f"duty_tries:{self.group_id}:{self.slot}"
        stall_key = f"duty_stall:{self.group_id}:{self.slot}"
        repo.clear_flag(tries_key)
        repo.set_flag(tries_key)
        db.execute("UPDATE flags SET value=? WHERE key=?", (count, tries_key))
        repo.clear_flag(stall_key)
        repo.set_flag(stall_key)
        db.execute(
            "UPDATE flags SET created_at=? WHERE key=?",
            (int(time.time() - stall_age), stall_key),
        )

    def test_tries_full_but_too_soon_does_not_failover(self) -> None:
        self._seed_tries(settings.DUTY_START_TRIES, stall_age=5)
        self._reconcile(fast=True)
        run = repo.get_run(self.group_id, self.slot) or {}
        self.assertFalse(bool(run.get("failover")), "次数够了但墙钟没到，不该判死换人")

    def test_tries_full_and_wall_clock_passed_does_failover(self) -> None:
        self._seed_tries(settings.DUTY_START_TRIES, stall_age=settings.DUTY_STALL_SECONDS + 20)
        self._reconcile(fast=True)
        run = repo.get_run(self.group_id, self.slot) or {}
        self.assertTrue(bool(run.get("failover")), "次数与墙钟都满足才换人")


class GroupLock(_Base):
    def test_reconcile_all_skips_group_held_by_accel(self) -> None:
        self._reconcile(fast=False)
        slot = self._slot()
        before = (repo.get_run(self.group_id, slot) or {}).get("attempts") or 0

        self.assertTrue(shift.engine._group_lock(self.group_id))  # noqa: SLF001
        try:
            shift.engine.reconcile_all()
        finally:
            shift.engine._group_unlock(self.group_id)  # noqa: SLF001

        after = (repo.get_run(self.group_id, slot) or {}).get("attempts") or 0
        self.assertEqual(after, before, "加速通道持有该组时，日常对账应跳过它")

    def test_other_groups_still_run(self) -> None:
        c_id = _make_server("第三台", "i-accel-ccc")
        d_id = _make_server("第四台", "i-accel-ddd", status="Running")
        self.states[c_id] = "Stopped"
        self.states[d_id] = "Running"
        other_gid = repo.upsert_group(
            {
                "name": "另一组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": c_id, "hours": 12},
                    {"server_id": d_id, "hours": 12},
                ],
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), other_gid),
        )
        self.assertTrue(shift.engine._group_lock(self.group_id))  # noqa: SLF001
        try:
            shift.engine.reconcile_all()
        finally:
            shift.engine._group_unlock(self.group_id)  # noqa: SLF001
        self.assertIsNotNone(
            db.query_one("SELECT 1 AS ok FROM shift_runs WHERE group_id=?", (other_gid,)),
            "锁只挡那一组，别的组照常对账",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
