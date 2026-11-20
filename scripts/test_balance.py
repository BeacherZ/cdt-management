#!/usr/bin/env python3
"""流量均衡的回归测试（dry-run 内存桩，不碰真实阿里云与 Cloudflare）。

锁死六条：
  1. 选人规则：始终是「累计用得最少」的那台，不是排班顺序。
  2. 切换要有理由：差值不到不动，最短当班没到不动 —— 否则域名每小时抖一次。
  3. 交接顺序：接替的那台 Running 之后才改域名、才停上一台；
     没就绪时不许提前停人，也不许提前写 DNS。
  4. 下班动作：只有标了「停机」的成员才在交班后被停掉。
  5. 兜底：当班到阈值立刻换人（不等评估点）；全员到阈值整组停机。
  6. 互斥：已在单机保活或联合换班里的实例进不了均衡组。

跑法：python scripts/test_balance.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-balance-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, balance, db, dns, notify, owners, repo, shift  # noqa: E402

CONFIG = {
    "timezone": "Asia/Shanghai",
    "ddns_enabled": "0",
    "notify_on_balance": "1",
    "notify_on_failover": "1",
}


def _make_server(label: str, instance_id: str, used: float, status: str = "Running") -> int:
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
        public_ip=f"203.0.113.{server_id}" if status == "Running" else "",
        traffic=used,
    )
    return server_id


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        for table in (
            "events",
            "flags",
            "balance_state",
            "balance_members",
            "balance_groups",
            "shift_runs",
            "shift_members",
            "shift_groups",
            "keepalives",
            "dns_records",
            "servers",
        ):
            db.execute(f"DELETE FROM {table}")

        self.a = _make_server("均衡A", "i-bal-aaa", 50.0)
        self.b = _make_server("均衡B", "i-bal-bbb", 20.0)
        self.c = _make_server("均衡C", "i-bal-ccc", 80.0)
        self.group_id = repo.upsert_balance_group(
            {
                "name": "均衡组",
                "enabled": True,
                "failover_enabled": True,
                "interval_minutes": 60,
                "diff_gb": 10,
                "min_duty_minutes": 30,
                "members": [
                    {"server_id": self.a, "off_action": "keep"},
                    {"server_id": self.b, "off_action": "keep"},
                    {"server_id": self.c, "off_action": "keep"},
                ],
            }
        )

        self.states = {self.a: "Running", self.b: "Running", self.c: "Running"}
        self.started: list[int] = []
        self.stopped: list[int] = []
        self._orig = (shift._refresh, aliyun.start_instance, aliyun.stop_instance)  # noqa: SLF001
        shift._refresh = self._fake_refresh  # noqa: SLF001
        aliyun.start_instance = self._fake_start
        aliyun.stop_instance = self._fake_stop

    def tearDown(self) -> None:
        shift._refresh, aliyun.start_instance, aliyun.stop_instance = self._orig  # noqa: SLF001

    def _fake_refresh(self, server: dict, with_traffic: bool = True) -> None:  # noqa: ANN001, ARG002
        state = self.states.get(int(server["id"]), "Stopped")
        server["instance_status"] = state
        server["stopped_mode"] = "StopCharging" if state == "Stopped" else ""
        if state == "Running":
            server["public_ip"] = f"203.0.113.{server['id']}"

    def _fake_start(self, server: dict) -> None:  # noqa: ANN001
        self.started.append(int(server["id"]))
        self.states[int(server["id"])] = "Running"

    def _fake_stop(self, server: dict, mode: str = "StopCharging", fallback: bool = False) -> str:  # noqa: ANN001, ARG002
        self.stopped.append(int(server["id"]))
        self.states[int(server["id"])] = "Stopped"
        return mode

    # -- 工具 ---------------------------------------------------------
    def _set_status(self, server_id: int, status: str) -> None:
        self.states[server_id] = status
        db.execute(
            "UPDATE servers SET instance_status=?, public_ip=? WHERE id=?",
            (status, f"203.0.113.{server_id}" if status == "Running" else "", server_id),
        )

    def _traffic(self, server_id: int, used: float) -> None:
        db.execute("UPDATE servers SET traffic_used=? WHERE id=?", (used, server_id))

    def _state(self) -> dict:
        return repo.get_balance_state(self.group_id) or {}

    def _duty(self) -> int:
        return int(self._state().get("duty_server_id") or 0)

    def _tick(self, offset: float = 0.0) -> None:
        balance.reconcile_all(CONFIG, time.time() + offset)

    def _settle_on(self, server_id: int, now: float | None = None) -> None:
        """把当班直接摆成 server_id，供后续用例从确定状态起步。"""
        repo.upsert_balance_state(
            self.group_id,
            duty_server_id=server_id,
            phase="idle",
            last_switch_at=int(now if now is not None else time.time()) - 7200,
            next_eval_at=0,
            cycle_key=dt.datetime.now().strftime("%Y-%m"),
        )


class FirstAssign(_Base):
    def test_picks_least_used(self) -> None:
        self._tick()
        self.assertEqual(self._duty(), self.b, "首次应指派累计最少的 B")

    def test_waits_for_running_before_settling(self) -> None:
        self._set_status(self.b, "Stopped")
        self._tick()
        self.assertEqual(self._duty(), self.b)
        self.assertEqual(self._state().get("phase"), "starting")
        # 还没起来：不许切 DNS，也不许动别的成员
        self.assertEqual(self.stopped, [])
        self._set_status(self.b, "Running")
        self._tick(offset=5)
        self.assertEqual(self._state().get("phase"), "idle")

    def test_disabled_member_never_duty(self) -> None:
        group = repo.get_balance_group(self.group_id)
        members = group["members"]
        # 把最少的 B 停用，应选 A（50）而不是 C（80）
        repo.upsert_balance_group(
            {
                "id": self.group_id,
                "name": "均衡组",
                "enabled": True,
                "failover_enabled": True,
                "interval_minutes": 60,
                "diff_gb": 10,
                "min_duty_minutes": 30,
                "members": [
                    {"server_id": self.a, "off_action": "keep"},
                    {"server_id": self.b, "off_action": "keep", "enabled": False},
                    {"server_id": self.c, "off_action": "keep"},
                ],
            }
        )
        self.assertTrue(members)
        self._tick()
        self.assertEqual(self._duty(), self.a)


class Evaluation(_Base):
    def test_gap_below_threshold_keeps_duty(self) -> None:
        self._settle_on(self.a)
        self._traffic(self.a, 50.0)
        self._traffic(self.b, 45.0)  # 差 5 < 10
        self._tick()
        self.assertEqual(self._duty(), self.a)
        self.assertGreater(self._state().get("next_eval_at") or 0, time.time())

    def test_gap_above_threshold_switches(self) -> None:
        self._settle_on(self.a)
        self._traffic(self.a, 80.0)
        self._traffic(self.b, 30.0)
        self._tick()
        self.assertEqual(self._duty(), self.b)

    def test_min_duty_cooldown_blocks_switch(self) -> None:
        now = time.time()
        repo.upsert_balance_state(
            self.group_id,
            duty_server_id=self.a,
            phase="idle",
            last_switch_at=int(now - 60),  # 只当班 1 分钟，最短要 30 分钟
            next_eval_at=0,
            cycle_key=dt.datetime.now().strftime("%Y-%m"),
        )
        self._traffic(self.a, 90.0)
        self._traffic(self.b, 10.0)
        self._tick()
        self.assertEqual(self._duty(), self.a)

    def test_not_evaluated_before_window(self) -> None:
        self._settle_on(self.a)
        repo.upsert_balance_state(self.group_id, next_eval_at=int(time.time() + 1800))
        self._traffic(self.a, 150.0)
        self._traffic(self.b, 10.0)
        self._tick()
        self.assertEqual(self._duty(), self.a, "没到评估点不该切")


class Failover(_Base):
    def test_over_threshold_switches_immediately(self) -> None:
        self._settle_on(self.a)
        repo.upsert_balance_state(self.group_id, next_eval_at=int(time.time() + 3600))
        self._traffic(self.a, 190.0)  # 阈值 188
        self._traffic(self.b, 60.0)
        self._tick()
        self.assertEqual(self._duty(), self.b, "超阈值应立即换人，不等评估点")

    def test_all_over_limit_stops_everything(self) -> None:
        self._settle_on(self.a)
        for server_id in (self.a, self.b, self.c):
            self._traffic(server_id, 195.0)
        self._tick()
        self.assertEqual(self._state().get("phase"), "exhausted")
        self.assertEqual(sorted(self.stopped), sorted([self.a, self.b, self.c]))

    def test_stopped_member_still_candidate(self) -> None:
        self._set_status(self.b, "Stopped")
        self._traffic(self.b, 5.0)  # 冻结的累计值，最小
        self._settle_on(self.a)
        self._traffic(self.a, 60.0)
        self._tick()
        self.assertIn(self.b, self.started, "停机成员被选中时应先开机")

    def test_stall_needs_wall_clock_not_just_tries(self) -> None:
        """加速轮 10 秒一次，「试满 3 次」只有 30 秒 —— 判死必须同时看墙钟。"""
        self._settle_on(self.a)
        for sid in (self.a, self.b, self.c):
            self._set_status(sid, "Stopped")
        # 让 A 起不来：每次开机只记一笔，状态不动
        aliyun.start_instance = lambda server: self.started.append(int(server["id"]))  # noqa: ARG005
        self._tick()
        self.assertEqual(self._state().get("phase"), "starting")
        # 次数够了，但「没在跑」才过了 10 秒：不许换人
        repo.upsert_balance_state(self.group_id, attempts=3)
        db.execute(
            "UPDATE flags SET created_at=? WHERE key=?",
            (int(time.time()) - 10, f"balance_stall:{self.group_id}:{self.a}"),
        )
        self._tick(offset=10)
        self.assertEqual(self._duty(), self.a, "刚 10 秒就换人，会把慢启动判成起不来")
        # 墙钟也过了 180 秒：换人
        db.execute(
            "UPDATE flags SET created_at=? WHERE key=?",
            (int(time.time()) - 200, f"balance_stall:{self.group_id}:{self.a}"),
        )
        self._tick(offset=200)
        self.assertNotEqual(self._duty(), self.a, "三次拉不起来 + 超过墙钟下限，应换人")


class OffAction(_Base):
    def test_keep_does_not_stop_previous(self) -> None:
        self._settle_on(self.a)
        self._traffic(self.a, 90.0)
        self._traffic(self.b, 10.0)
        self._tick()
        self.assertEqual(self._duty(), self.b)
        self.assertNotIn(self.a, self.stopped, "下班动作是保持运行，不该停机")

    def test_stop_stops_previous(self) -> None:
        repo.upsert_balance_group(
            {
                "id": self.group_id,
                "name": "均衡组",
                "enabled": True,
                "failover_enabled": True,
                "interval_minutes": 60,
                "diff_gb": 10,
                "min_duty_minutes": 30,
                "members": [
                    {"server_id": self.a, "off_action": "stop"},
                    {"server_id": self.b, "off_action": "keep"},
                    {"server_id": self.c, "off_action": "keep"},
                ],
            }
        )
        self._settle_on(self.a)
        self._traffic(self.a, 90.0)
        self._traffic(self.b, 10.0)
        self._tick()
        self.assertEqual(self._duty(), self.b)
        self.assertIn(self.a, self.stopped, "标了停机就该停掉上一台")


class Cycle(_Base):
    def test_month_rollover_resets_slot_but_keeps_duty(self) -> None:
        now = time.time()
        repo.upsert_balance_state(
            self.group_id,
            duty_server_id=self.a,
            phase="idle",
            cycle_key="2000-01",
            slot_base_gb=88.0,
            next_eval_at=0,
        )
        self._traffic(self.a, 0.0)
        self._traffic(self.b, 0.0)
        self._traffic(self.c, 0.0)
        balance.reconcile_all(CONFIG, now)
        state = self._state()
        self.assertEqual(state.get("cycle_key"), dt.datetime.now().strftime("%Y-%m"))
        self.assertEqual(float(state.get("slot_base_gb") or 0), 0.0)
        self.assertEqual(self._duty(), self.a, "跨月第一轮不该乱切")


class TrafficProtection(_Base):
    """流量保护优先：任何情况下都不许「因为读不到流量」或「组里关了开关」就放任烧穿。"""

    def setUp(self) -> None:
        super().setUp()
        self.sent: list = []
        self._orig_send = notify.send_all
        notify.send_all = lambda _config, message: (  # noqa: ARG005
            self.sent.append(message),
            {"webhook": "ok"},
        )[1]

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        super().tearDown()

    def test_unknown_traffic_never_stops_group(self) -> None:
        # 三台机器的流量都没读到：既不能当「都用完了」熔断，也不能停任何一台
        db.execute("UPDATE servers SET traffic_used=NULL")
        self._tick()
        self.assertEqual(self.stopped, [], "读不到流量时不许停机")
        self.assertNotEqual(self._state().get("phase"), "exhausted")
        self.assertFalse(repo.list_events(limit=10, kind="balance_breaker"))

    def test_over_limit_without_failover_stops_only_duty(self) -> None:
        repo.upsert_balance_group(
            {
                "id": self.group_id,
                "name": "均衡组",
                "enabled": True,
                "failover_enabled": False,
                "interval_minutes": 60,
                "diff_gb": 10,
                "min_duty_minutes": 30,
                "members": [
                    {"server_id": self.a, "off_action": "keep"},
                    {"server_id": self.b, "off_action": "keep"},
                    {"server_id": self.c, "off_action": "keep"},
                ],
            }
        )
        self._settle_on(self.a)
        self._traffic(self.a, 190.0)  # 阈值 188
        self._traffic(self.b, 10.0)
        self._traffic(self.c, 20.0)
        balance.reconcile_all(dict(CONFIG, notify_on_limit_stop="1"), time.time())
        self.assertEqual(self.stopped, [self.a], "只停超限的当班，不许连坐")
        self.assertEqual(self._state().get("phase"), "exhausted")
        self.assertTrue(self.sent, "超限停机必须通知")
        self.assertIn("超限停机报告（均衡组）", self.sent[0].title)

    def test_stopped_duty_does_not_drift_to_another_member(self) -> None:
        """关着故障转移：停了当班就等着，不能因为「别人没超」绕一圈又换一台。

        旧实现把 phase 置 exhausted 后，解除条件是「任一成员流量未超」，
        而 b、c 本来就没超 —— 下一轮立刻重新指派，开关等于没生效。
        """
        repo.upsert_balance_group(
            {
                "id": self.group_id,
                "name": "均衡组",
                "enabled": True,
                "failover_enabled": False,
                "interval_minutes": 60,
                "diff_gb": 10,
                "min_duty_minutes": 30,
                "members": [
                    {"server_id": self.a, "off_action": "keep"},
                    {"server_id": self.b, "off_action": "keep"},
                    {"server_id": self.c, "off_action": "keep"},
                ],
            }
        )
        self._settle_on(self.a)
        self._traffic(self.a, 190.0)
        self._traffic(self.b, 10.0)
        self._traffic(self.c, 20.0)
        self._tick()
        self.assertEqual(self._state().get("phase"), "exhausted")

        # 再跑几轮：b、c 一直没超，也不许换人
        for offset in (60, 120, 180):
            self._tick(offset=offset)
        self.assertEqual(self.stopped, [self.a], "不许换人，再停别的机器")
        self.assertEqual(self._state().get("duty_server_id"), self.a, "当班位要留给被停的那台")

        # 这台自己的流量回落（月初重置 / 调高阈值）后才恢复
        self._traffic(self.a, 5.0)
        self._tick(offset=240)
        self.assertNotEqual(self._state().get("phase"), "exhausted", "当班不再超限就该解除")
        # 解除后按「用得最少」重新指派，这台是 5 GB，还是它
        self.assertEqual(self._duty(), self.a)

    def test_stop_duty_releases_when_member_removed(self) -> None:
        # 被停的那台被用户移出组：没有「等它流量回落」可言，不能永久卡在熔断上
        repo.upsert_balance_state(
            self.group_id,
            phase="exhausted",
            note=balance.STOP_DUTY_NOTE,
            duty_server_id=self.a,
            next_eval_at=int(time.time() + 3600),
        )
        repo.upsert_balance_group(
            {
                "id": self.group_id,
                "name": "均衡组",
                "enabled": True,
                "failover_enabled": False,
                "interval_minutes": 60,
                "diff_gb": 10,
                "min_duty_minutes": 30,
                "members": [
                    {"server_id": self.b, "off_action": "keep"},
                    {"server_id": self.c, "off_action": "keep"},
                ],
            }
        )
        self._tick()
        self.assertEqual(self._state().get("phase"), "idle")
        self.assertEqual(self._duty(), self.b, "按新成员重新指派用得最少的那台")

    def test_idle_member_over_limit_stopped_at_eval(self) -> None:
        # b 不接域名但一直开着（保持运行），到阈值同样要停 —— 它也在烧流量
        self._settle_on(self.a)
        self._traffic(self.a, 10.0)
        self._traffic(self.b, 190.0)
        self._traffic(self.c, 30.0)
        self._tick()
        self.assertIn(self.b, self.stopped, "休息成员超限也要停")

    def test_pick_reads_fresh_traffic_before_switching(self) -> None:
        """选人靠库里的缓存读数，真读回来发现接替那台已经超限，就不许切过去。

        否则域名会指向一台正在烧流量的机器，下一轮又得往回切一趟。
        """
        self._settle_on(self.a)
        repo.upsert_balance_state(self.group_id, next_eval_at=int(time.time() + 3600))
        self._traffic(self.a, 190.0)  # 当班超限，触发换人
        self._traffic(self.b, 10.0)  # 缓存里 b 很干净，会被选中
        self._traffic(self.c, 20.0)
        original = shift._refresh  # noqa: SLF001

        def refresh(server: dict, with_traffic: bool = True) -> None:  # noqa: ANN001
            if int(server["id"]) == self.b:
                server["traffic_used"] = 195.0  # 真读回来已经到阈值
            original(server, with_traffic)

        shift._refresh = refresh  # noqa: SLF001
        try:
            self._tick()
        finally:
            shift._refresh = original  # noqa: SLF001
        self.assertEqual(self._duty(), self.a, "接替者已超限，不许切过去")
        self.assertFalse(repo.list_events(limit=10, kind="balance_failover"))
        self.assertEqual(repo.list_events(limit=10, kind="balance_done"), [])

    def test_abort_does_not_touch_settled_event(self) -> None:
        """`_stop_duty` 会 abort，但 state 里可能还留着上一轮已结算事件的 id。

        拿它再写一笔「切换中止」，就会把一条已经完成的「均衡完成」改成失败。
        """
        self._tick()  # 先结算一次，产生一条 done 的 balance_done
        settled = repo.list_events(limit=5, kind="balance_done")[0]
        repo.upsert_balance_group(
            {
                "id": self.group_id,
                "name": "均衡组",
                "enabled": True,
                "failover_enabled": False,
                "interval_minutes": 60,
                "diff_gb": 10,
                "min_duty_minutes": 30,
                "members": [
                    {"server_id": self.a, "off_action": "keep"},
                    {"server_id": self.b, "off_action": "keep"},
                    {"server_id": self.c, "off_action": "keep"},
                ],
            }
        )
        self._traffic(self._duty(), 195.0)
        self._tick(offset=5)
        after = repo.get_event(int(settled["id"])) or {}
        self.assertEqual(after.get("status"), "done", "已结算的事件不许被改成失败")
        texts = [str(step.get("text") or "") for step in after.get("progress") or []]
        self.assertFalse(
            [text for text in texts if "切换中止" in text], "不许往已完成事件里追加中止"
        )

    def test_breaker_body_matches_shift(self) -> None:
        # 全员超限：熔断报告的机器块要和换班那份同字段（地域 / 已用带阈值 / 状态）
        self._settle_on(self.a)
        for sid in (self.a, self.b, self.c):
            self._traffic(sid, 195.0)
        balance.reconcile_all(dict(CONFIG, notify_on_breaker="1"), time.time())
        self.assertTrue(self.sent)
        text = self.sent[0].html
        self.assertIn("📍 地域：", text)
        self.assertIn("📈 已用：", text)
        self.assertIn("状态：", text)


class DutyRecovery(_Base):
    """当班掉线：先原位拉起，别等到下一个评估点（24 小时周期的组会空一整天）。"""

    def setUp(self) -> None:
        super().setUp()
        self.sent: list = []
        self._orig_send = notify.send_all
        notify.send_all = lambda _config, message: (  # noqa: ARG005
            self.sent.append(message),
            {"webhook": "ok"},
        )[1]

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        super().tearDown()

    def test_duty_down_restarts_in_place(self) -> None:
        self._settle_on(self.a)
        repo.upsert_balance_state(self.group_id, next_eval_at=int(time.time() + 86400))
        self._set_status(self.a, "Stopped")
        self._tick()
        self.assertEqual(self._duty(), self.a, "掉线的是当班，应该拉起它自己，不是换人")
        self.assertIn(self.a, self.started)
        self.assertEqual(self._state().get("phase"), "starting")
        event = repo.list_events(limit=5, kind="balance_switch")[0]
        self.assertIn("均衡恢复中", str(event["title"]))
        self.assertIn("等待当班 均衡A 重新到位", str(event["detail"]))

    def test_recover_settles_quietly(self) -> None:
        self._settle_on(self.a)
        self._set_status(self.a, "Stopped")
        self._tick()
        self._set_status(self.a, "Running")
        self._tick(offset=5)
        self.assertEqual(self._state().get("phase"), "idle")
        event = repo.list_events(limit=5)[0]
        self.assertIn("均衡恢复（", str(event["title"]))
        self.assertNotIn("均衡完成", str(event["title"]))
        self.assertEqual(self.sent, [], "掉线恢复只记事件，不推报告")

    def test_accelerate_channel_settles_between_reconciles(self) -> None:
        # 加速通道只碰「进行中」的组：日常巡检还没到点，它先把结果落下来
        self._settle_on(self.a)
        self._set_status(self.a, "Stopped")
        self._tick()
        self.assertEqual(self._state().get("phase"), "starting")
        self._set_status(self.a, "Running")
        balance.reconcile_awaiting(CONFIG)
        self.assertEqual(self._state().get("phase"), "idle")

        # 已经结算的组不该再被加速通道读一次（没有指令在飞）
        self._set_status(self.a, "Stopped")
        calls = []
        self._orig_refresh = shift._refresh  # noqa: SLF001
        shift._refresh = lambda server, with_traffic=True: calls.append(server["id"])  # noqa: SLF001, ARG005
        try:
            balance.reconcile_awaiting(CONFIG)
        finally:
            shift._refresh = self._orig_refresh  # noqa: SLF001
        self.assertEqual(calls, [], "空闲组不该被加速通道读状态")


class SettleNotice(_Base):
    """结算必须发通知，自动 / 手动 / 故障转移各用各的标题与事件类型。

    以前三种都写成「均衡完成」+ kind=balance_done，故障转移混在正常轮换里分不出来；
    而且只有 notify_on_balance 一个开关，关掉「流量均衡完成」会连故障转移一起静音。
    """

    def setUp(self) -> None:
        super().setUp()
        self.sent: list = []
        self._orig_send = notify.send_all
        notify.send_all = lambda _config, message: (  # noqa: ARG005
            self.sent.append(message),
            {"webhook": "ok"},
        )[1]

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        super().tearDown()

    def _event(self, kind: str) -> dict:
        events = repo.list_events(limit=10, kind=kind)
        self.assertTrue(events, f"没有 {kind} 事件")
        return events[0]

    def test_first_assign_notifies(self) -> None:
        self._tick()
        event = self._event("balance_done")
        self.assertIn("均衡完成", str(event["title"]))
        # 概览和换班结算同格式：当班 + 待机台数
        self.assertIn("当班 均衡B，待机 2 台", str(event["detail"]))
        self.assertEqual(len(self.sent), 1, "自动均衡必须发通知")
        self.assertIn("均衡完成报告", self.sent[0].title)
        # 末条进度要写清「发没发出去」，不是内部渠道名
        self.assertEqual(event["progress"][-1]["text"], "通知已发送")

    def test_first_assign_copy_is_not_about_leading(self) -> None:
        # 首次指派不是「用量领先」，概览不能套差值切换那句话
        self._tick()
        text = self.sent[0].text
        self.assertIn("首次指派", text)
        self.assertNotIn("用量领先", text)

    def test_manual_uses_its_own_title(self) -> None:
        self._settle_on(self.a)
        balance.manual_switch(self.group_id, CONFIG)
        event = self._event("balance_manual")
        self.assertIn("手动均衡完成", str(event["title"]))
        self.assertEqual(len(self.sent), 1, "手动均衡必须发通知")
        self.assertIn("手动均衡完成报告", self.sent[0].title)

    def test_traffic_failover_uses_transfer(self) -> None:
        self._settle_on(self.a)
        repo.upsert_balance_state(self.group_id, next_eval_at=int(time.time() + 3600))
        self._traffic(self.a, 190.0)
        self._traffic(self.b, 60.0)
        self._tick()
        event = self._event("balance_failover")
        self.assertIn("均衡转移", str(event["title"]))
        self.assertIn("当班 均衡B", str(event["detail"]))
        self.assertEqual(len(self.sent), 1, "故障转移必须发通知")
        self.assertIn("均衡转移报告", self.sent[0].title)

    def test_stall_failover_names_the_dead_one(self) -> None:
        self._settle_on(self.a)
        for sid in (self.b, self.c):
            self._set_status(sid, "Running")
        self._set_status(self.a, "Stopped")
        aliyun.start_instance = lambda server: self.started.append(int(server["id"]))  # noqa: ARG005
        # 先让它进入「拉起中」，再把计时推到墙钟下限之外
        self._tick()
        repo.upsert_balance_state(self.group_id, attempts=3)
        # 计时标记是「第一次发现没在跑」时才落库的，这里直接落一条并回填时刻
        repo.set_flag(f"balance_stall:{self.group_id}:{self.a}")
        db.execute(
            "UPDATE flags SET created_at=? WHERE key=?",
            (int(time.time()) - 200, f"balance_stall:{self.group_id}:{self.a}"),
        )
        self._tick(offset=200)
        self._event("balance_failover")
        self.assertEqual(len(self.sent), 1)
        # 被换下的那台要出现在通知里，否则用户只看到「转移了」，不知道谁出的问题
        self.assertIn("均衡A", self.sent[0].text)

    def test_failover_switch_is_independent(self) -> None:
        # 关掉「流量均衡完成」不该把故障转移一起静音
        config = dict(CONFIG, notify_on_balance="0")
        self._settle_on(self.a)
        repo.upsert_balance_state(self.group_id, next_eval_at=int(time.time() + 3600))
        self._traffic(self.a, 190.0)
        self._traffic(self.b, 60.0)
        balance.reconcile_all(config, time.time())
        self.assertEqual(len(self.sent), 1, "notify_on_failover 独立生效")


class NewGroup(_Base):
    """新建组后的首次指派：和「新建换班组」同一套口径 —— 只记事件、不推报告。"""

    def setUp(self) -> None:
        super().setUp()
        self.sent: list = []
        self._orig_send = notify.send_all
        notify.send_all = lambda _config, message: (  # noqa: ARG005
            self.sent.append(message),
            {"webhook": "ok"},
        )[1]

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        super().tearDown()

    def test_bootstrap_is_quiet(self) -> None:
        balance.bootstrap_group(self.group_id, CONFIG)
        event = repo.list_events(limit=5, kind="balance_new")[0]
        self.assertIn("新建均衡组", str(event["title"]))
        # 全程一个说法：建组那条事件不该在结算时改口成「均衡完成」
        self.assertNotIn("均衡完成", str(event["title"]))
        self.assertIn("当班 均衡B", str(event["detail"]))
        self.assertEqual(self.sent, [], "刚保存完组不该再推一份报告")
        self.assertEqual(self._duty(), self.b)

    def test_bootstrap_records_why_it_did_nothing(self) -> None:
        db.execute("UPDATE balance_groups SET enabled=0 WHERE id=?", (self.group_id,))
        balance.bootstrap_group(self.group_id, CONFIG)
        event = repo.list_events(limit=5, kind="balance_new")[0]
        self.assertIn("未启用", str(event["detail"]))
        self.assertEqual(self.sent, [])
        self.assertEqual(self._duty(), 0)

    def test_new_group_reason_never_fires_on_plain_reconcile(self) -> None:
        # 巡检里的首次指派（比如熔断解除后重新指派）仍然要发报告，
        # 否则「组恢复了」这件事没有任何推送
        self._tick()
        event = repo.list_events(limit=5, kind="balance_done")[0]
        self.assertIn("均衡完成", str(event["title"]))
        self.assertEqual(len(self.sent), 1)


class StuckSwitch(_Base):
    """切换卡住又没有候选时，必须把 phase 拨回来，不能永远挂着「切换中」。"""

    def test_gives_up_and_reverts_duty(self) -> None:
        self._settle_on(self.a)
        # 想切到 b，b 起不来；其余能读到的成员都没了数据（CDT 抖动）
        self.states[self.b] = "Stopped"
        self._set_status(self.b, "Stopped")
        self._traffic(self.a, 190.0)
        db.execute("UPDATE servers SET traffic_used=NULL WHERE id IN (?, ?)", (self.b, self.c))
        repo.upsert_balance_state(
            self.group_id,
            phase="starting",
            duty_server_id=self.b,
            prev_duty_id=self.a,
            attempts=3,
            last_switch_at=int(time.time()) - 900,
        )
        self._tick()
        state = self._state()
        self.assertEqual(state["phase"], "idle", "不能停在 starting 无限加速轮询")
        self.assertEqual(
            int(state["duty_server_id"]), self.a, "域名还在 a 上，当班必须退回 a"
        )


class Mutex(_Base):
    def test_shift_member_cannot_join_balance(self) -> None:
        shift_id = repo.upsert_group(
            {
                "name": "换班组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )
        self.assertTrue(shift_id)
        with self.assertRaises(ValueError) as ctx:
            owners.ensure_free([self.a], owners.BALANCE, self.group_id, {})
        self.assertIn("联合换班", str(ctx.exception))

    def test_keepalive_member_cannot_join_balance(self) -> None:
        # 用组外的第四台：组内成员对本组不算冲突，测不出这条规则
        outsider = _make_server("保活D", "i-bal-ddd", 10.0)
        repo.add_keepalive(outsider, True)
        with self.assertRaises(ValueError) as ctx:
            owners.ensure_free([outsider], owners.BALANCE, self.group_id, {})
        self.assertIn("单机保活", str(ctx.exception))


class SlotAlignment(_Base):
    """评估点只落在整点：建组 / 切完都不许留零碎分钟。"""

    TZ = dt.timezone(dt.timedelta(hours=8))

    def _at(self, text: str) -> float:
        return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=self.TZ).timestamp()

    def test_hourly_snaps_to_next_hour(self) -> None:
        tz = shift._timezone("Asia/Shanghai")  # noqa: SLF001
        self.assertEqual(
            balance._next_slot(self._at("2026-11-19 14:23"), 3600, tz),  # noqa: SLF001
            int(self._at("2026-11-19 15:00")),
        )
        # 正好踩在整点上时，下一次要落到下一格，不然同一分钟内会连评两次
        self.assertEqual(
            balance._next_slot(self._at("2026-11-19 15:00"), 3600, tz),  # noqa: SLF001
            int(self._at("2026-11-19 16:00")),
        )

    def test_three_six_twelve_hours(self) -> None:
        tz = shift._timezone("Asia/Shanghai")  # noqa: SLF001
        cases = {
            10800: ("2026-11-19 14:23", "2026-11-19 15:00"),
            21600: ("2026-11-19 14:23", "2026-11-19 18:00"),
            43200: ("2026-11-19 14:23", "2026-11-20 00:00"),
            86400: ("2026-11-19 14:23", "2026-11-20 00:00"),
        }
        for interval, (start, expect) in cases.items():
            self.assertEqual(
                balance._next_slot(self._at(start), interval, tz),  # noqa: SLF001
                int(self._at(expect)),
                f"{interval // 3600} 小时周期",
            )

    def test_first_assign_lands_on_whole_hour(self) -> None:
        # 14:23 建组并首次指派，下一次评估是 15:00，不是 15:23
        now = self._at("2026-11-19 14:23")
        balance.reconcile_all(CONFIG, now)
        state = repo.get_balance_state(self.group_id) or {}
        self.assertEqual(int(state.get("next_eval_at") or 0), int(self._at("2026-11-19 15:00")))

    def test_idle_evaluation_reschedules_to_whole_hour(self) -> None:
        now = self._at("2026-11-19 14:23")
        balance.reconcile_all(CONFIG, now)
        # 把评估点拨到过期，差值调到不够切（b 当班 20，a 50 → 负数）
        repo.upsert_balance_state(
            self.group_id,
            duty_server_id=self.b,
            next_eval_at=int(now) - 1,
            last_switch_at=0,
        )
        balance.reconcile_all(CONFIG, now)
        state = repo.get_balance_state(self.group_id) or {}
        self.assertEqual(int(state.get("next_eval_at") or 0), int(self._at("2026-11-19 15:00")))

    def test_legacy_odd_slot_is_corrected(self) -> None:
        # 老版本留下的 14:47 这种零碎评估点，还没到点就就地挪到 15:00
        now = self._at("2026-11-19 14:20")
        balance.reconcile_all(CONFIG, now)
        repo.upsert_balance_state(
            self.group_id,
            duty_server_id=self.b,
            next_eval_at=int(self._at("2026-11-19 14:47")),
            last_switch_at=0,
        )
        balance.reconcile_all(CONFIG, now)
        state = repo.get_balance_state(self.group_id) or {}
        self.assertEqual(int(state.get("next_eval_at") or 0), int(self._at("2026-11-19 15:00")))


class Manual(_Base):
    def test_manual_switch_ignores_gap(self) -> None:
        self._settle_on(self.a)
        self._traffic(self.a, 51.0)
        self._traffic(self.b, 50.0)  # 差 1，正常不会切
        result = balance.manual_switch(self.group_id, CONFIG)
        self.assertEqual(result.get("duty"), self.b)

    def test_manual_switch_keeps_duty_when_it_is_least(self) -> None:
        """当班自己就是全组最少：手动均衡不该为了「有反应」切给更满的那台。"""
        self._settle_on(self.a)
        self._traffic(self.a, 40.0)
        self._traffic(self.b, 80.0)
        result = balance.manual_switch(self.group_id, CONFIG)
        self.assertEqual(result.get("state"), "best")
        self.assertEqual(self._duty(), self.a)


if __name__ == "__main__":
    unittest.main(verbosity=2)
