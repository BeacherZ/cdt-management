#!/usr/bin/env python3
"""一月一轮（按天排班）的判定测试。

锁死几条规则：
  1. 每月 1 号 0 点从第 1 位开始，按填的天数依次铺开；
  2. 最后一位一直排到月末，28/29/30/31 天的月份自动伸缩；
  3. 跨月重新从第 1 位开始，slot 键随之变化（run 表不用改）；
  4. 下一班开始 = 当前这段最后一天的次日 0 点；
  5. 合计天数超过 31 天、或有人没填天数，保存时就拒绝。

跑法：python scripts/test_month_cycle.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前设好
_TMP = tempfile.mkdtemp(prefix="cdt-month-cycle-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import api, db, repo, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return dt.datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp()


def _make_server(label: str) -> int:
    return repo.upsert_server(
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


class MonthCycle(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        repo.set_settings({"timezone": "Asia/Shanghai"})
        # 每个用例从空表开始：启用组之间不允许共用服务器
        db.execute("DELETE FROM shift_members")
        db.execute("DELETE FROM shift_groups")
        db.execute("DELETE FROM shift_runs")
        self.a = _make_server("香港-A")
        self.b = _make_server("香港-B")
        self.c = _make_server("香港-C")

    def _group(self, days: list[int]) -> dict:
        members = [
            {"server_id": sid, "days": day}
            for sid, day in zip((self.a, self.b, self.c), days, strict=False)
        ]
        group_id = repo.upsert_group(
            {"name": "月租组", "enabled": True, "cycle": "month", "members": members}
        )
        return repo.get_group(group_id) or {}

    def _duty_label_at(self, group: dict, when: float) -> str:
        picked = shift.duty_at(group, when)
        self.assertIsNotNone(picked)
        return str(picked[0].get("label") or "")

    def test_three_members_31_day_month(self):
        """10 月 31 天：10/10/11 天，最后一位吃余数。"""
        group = self._group([10, 10, 11])
        self.assertEqual(group["cycle"], "month")
        expect = [
            (1, "香港-A"),
            (10, "香港-A"),
            (11, "香港-B"),
            (20, "香港-B"),
            (21, "香港-C"),
            (31, "香港-C"),
        ]
        for day, label in expect:
            self.assertEqual(self._duty_label_at(group, _ts(2026, 10, day, 12)), label, day)

    def test_february_shrinks_to_month_end(self):
        """2 月只有 28 天：15/15 变成 15/13，第二位排到月末。"""
        group = self._group([15, 15])
        self.assertEqual(self._duty_label_at(group, _ts(2026, 2, 15, 23)), "香港-A")
        self.assertEqual(self._duty_label_at(group, _ts(2026, 2, 16, 0)), "香港-B")
        self.assertEqual(self._duty_label_at(group, _ts(2026, 2, 28, 23)), "香港-B")

    def test_new_month_restarts_from_first(self):
        """跨月从第 1 位重新开始，slot 键跟着变（run 表按 slot 记）。"""
        group = self._group([10, 10, 11])
        last_of_oct = shift.duty_at(group, _ts(2026, 10, 31, 23))
        first_of_nov = shift.duty_at(group, _ts(2026, 11, 1, 0))
        self.assertEqual(str(last_of_oct[0]["label"]), "香港-C")
        self.assertEqual(str(first_of_nov[0]["label"]), "香港-A")
        self.assertNotEqual(last_of_oct[1], first_of_nov[1])
        self.assertEqual(first_of_nov[1], int(_ts(2026, 11, 1)))

    def test_next_switch_is_next_day_midnight(self):
        """下一班 = 这段最后一天的次日 0 点；月末那台落在下月 1 号。"""
        group = self._group([10, 10, 11])
        self.assertEqual(shift.next_switch_at(group, _ts(2026, 10, 5, 12)), int(_ts(2026, 10, 11)))
        self.assertEqual(shift.next_switch_at(group, _ts(2026, 10, 25, 12)), int(_ts(2026, 11, 1)))

    def test_span_text_uses_day_range(self):
        """通知里的「当前班次」写日期区间，不写时刻。"""
        group = self._group([10, 10, 11])
        duty = repo.get_server(self.a, decrypt_secret=True) or {}
        duty = {**duty, "_timezone": "Asia/Shanghai"}
        text = shift._shift_span(group, duty, {}, _ts(2026, 10, 5, 12))  # noqa: SLF001
        self.assertIn("10 月 1–10 日", text)
        self.assertNotIn("小时", text)

    def test_span_follows_current_segment_not_duty(self):
        """手动换班 / 故障转移后，「当前班次」仍按今天是哪一段来写。

        10/5 本该 A（1–10 日）当班，手动切到 B（11–20 日）之后，报告如果按
        「当班成员自己的排班段」去写，就会在 10/5 收到一条「10 月 11–20 日」，
        与今天自相矛盾。
        """
        group = self._group([10, 10, 11])
        duty_b = repo.get_server(self.b, decrypt_secret=True) or {}
        duty_b = {**duty_b, "_timezone": "Asia/Shanghai"}
        text = shift._shift_span(group, duty_b, {}, _ts(2026, 10, 5, 12))  # noqa: SLF001
        self.assertIn("10 月 1–10 日", text)
        self.assertNotIn("11–20", text)

    def test_disabled_member_does_not_take_days(self):
        """停用的成员不占日子：他的天数清零，剩下的启用成员重排。

        否则界面上按「三行都在排」算出来 1–10 / 11–20 / 21–31，实际只跑
        1–10 / 11–31，用户以为中间那段是那个人在顶。
        """
        group_id = repo.upsert_group(
            {
                "name": "月租组",
                "enabled": True,
                "cycle": "month",
                "members": [
                    {"server_id": self.a, "days": 10},
                    {"server_id": self.b, "days": 10, "enabled": False},
                    {"server_id": self.c, "days": 11},
                ],
            }
        )
        group = repo.get_group(group_id) or {}
        days = {int(m["server_id"]): int(m["days"]) for m in group["members"]}
        self.assertEqual(days[self.b], 0, "停用成员的天数应清零")
        layout = shift._month_layout(group, 2026, 10)  # noqa: SLF001
        self.assertEqual([(m["label"], s, e) for m, s, e in layout],
                         [("香港-A", 1, 10), ("香港-C", 11, 31)])

    def test_span_label_for_ui(self):
        """界面用的 span_label：月模式「1–10 日」，一天一轮的时刻表「08:00–20:00」。"""
        group = self._group([10, 10, 11])
        member = next(m for m in group["members"] if int(m["server_id"]) == self.a)
        self.assertEqual(shift.member_span_label(group, member, _ts(2026, 10, 5)), "1–10 日")
        self.assertEqual(shift.member_span_label(group, member, _ts(2026, 2, 5)), "1–10 日")

        day_group = {
            "members": [
                {"server_id": self.a, "enabled": 1, "hours": 12, "start_time": "08:00",
                 "end_time": "20:00"},
                {"server_id": self.b, "enabled": 1, "hours": 12, "start_time": "20:00",
                 "end_time": "00:00"},
            ]
        }
        first = day_group["members"][0]
        self.assertEqual(
            shift.member_span_label(day_group, first, _ts(2026, 10, 5)), "08:00–20:00"
        )
        self.assertEqual(
            shift.member_span_label(day_group, day_group["members"][1], _ts(2026, 10, 5)),
            "20:00–24:00",
        )

    def test_payload_carries_cycle_and_span_label(self):
        """界面拿到的就是引擎算出来的段：cycle + 每人的 span_label。"""
        group = self._group([10, 10, 11])
        payload = api._group_payload(group, repo.get_settings(), _ts(2026, 10, 5))  # noqa: SLF001
        self.assertEqual(payload["cycle"], "month")
        spans = {int(m["server_id"]): m["span_label"] for m in payload["members"]}
        self.assertEqual(spans[self.a], "1–10 日")
        self.assertEqual(spans[self.b], "11–20 日")
        self.assertEqual(spans[self.c], "21–31 日")

    def test_total_over_31_days_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._group([20, 20])
        self.assertIn("超过一个月最多 31 天", str(ctx.exception))

    def test_partially_filled_days_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            repo.upsert_group(
                {
                    "name": "月租组",
                    "enabled": True,
                    "cycle": "month",
                    "members": [{"server_id": self.a, "days": 10}, {"server_id": self.b, "days": 0}],
                }
            )
        self.assertIn("每台启用的成员都要填天数", str(ctx.exception))

    def test_handover_waits_until_successor_running(self):
        """上下班时序：8 号 24 点交班，要等 9 号那位确认 Running 才停前一位。

        组员 1 排 1–8 号：9 号 0 点整准时给组员 2 开机；在组员 2 变成 Running
        之前，组员 1 一直保持运行（「上班准时、下班等顶班」）。
        """
        group = self._group([8, 23])  # A：1–8 号，B：9–31 号
        config = {"timezone": "Asia/Shanghai", "ddns_enabled": "0"}
        status = {self.a: "Running", self.b: "Stopped"}
        starts: list[int] = []
        stops: list[int] = []

        def _fake_refresh(server: dict) -> None:
            state = status[int(server["id"])]
            server["instance_status"] = state
            server["stopped_mode"] = "StopCharging" if state == "Stopped" else ""
            server["public_ip"] = "1.2.3.4" if state == "Running" else ""
            server["traffic_used"] = 0.0

        def _fake_start(server: dict) -> None:
            starts.append(int(server["id"]))
            status[int(server["id"])] = "Starting"

        def _fake_stop(server: dict, mode: str = "StopCharging", fallback: bool = False) -> None:
            del mode, fallback
            stops.append(int(server["id"]))
            status[int(server["id"])] = "Stopped"

        origin = (shift._refresh, shift.aliyun.start_instance, shift.aliyun.stop_instance)  # noqa: SLF001
        shift._refresh = _fake_refresh  # noqa: SLF001
        shift.aliyun.start_instance = _fake_start
        shift.aliyun.stop_instance = _fake_stop
        try:
            # 8 号 23:59：A 当班、B 停着，不该有任何启停动作
            shift.engine.reconcile_group(
                group, config, dt.datetime(2026, 10, 8, 23, 59, tzinfo=TZ),
                _ts(2026, 10, 8, 23, 59), notify_enabled=False,
            )
            self.assertEqual((starts, stops), ([], []))

            # 9 号 00:00：准时给 B 开机；B 还在 Starting，A 必须继续跑
            shift.engine.reconcile_group(
                group, config, dt.datetime(2026, 10, 9, 0, 0, tzinfo=TZ),
                _ts(2026, 10, 9, 0, 0), notify_enabled=False,
            )
            self.assertEqual(starts, [self.b], "9 号 0 点应准时拉起接班的组员 2")
            self.assertEqual(stops, [], "接班还没确认运行，不能停组员 1")

            # 9 号 00:05：B 已 Running，这时才停 A
            status[self.b] = "Running"
            shift.engine.reconcile_group(
                group, config, dt.datetime(2026, 10, 9, 0, 5, tzinfo=TZ),
                _ts(2026, 10, 9, 0, 5), notify_enabled=False,
            )
            self.assertEqual(stops, [self.a], "接班确认运行后才节省停机组员 1")
        finally:
            (
                shift._refresh,  # noqa: SLF001
                shift.aliyun.start_instance,
                shift.aliyun.stop_instance,
            ) = origin


if __name__ == "__main__":
    unittest.main(verbosity=2)
