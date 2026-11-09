#!/usr/bin/env python3
"""单机保活的运行时段 / 运行日期（dry-run 内存桩，不碰真实阿里云）。

锁死这几条：

1. **窗口外主动停机，不只是不拉起**。「只在 08:00–23:00 运行」要成立，23:00
   到点必须把机器停掉；只做到「不拉起」的话它会一直跑到第二天早上。
2. **窗口外一律不推通知**。到点停机、到点开机都是预期的，推一条「实例疑被抢占，
   已自动恢复运行」会让人以为机器被阿里云回收了。
3. **事件里能看到「单机保活窗口暂停 / 单机保活窗口恢复」**，概览一句结论，展开是过程。
4. **停机模式按实例设置走**：选普通停机就发 Stop，不再写死节省停机。
5. 配置写坏（半截时段、跨零点）时按「不限」处理，不能把机器钉死在停机状态。

跑法：python scripts/test_keepalive_window.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-keepalive-window-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, notify, repo, scheduler, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
TABLES = (
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
)


def _at(hour: int, minute: int = 0, day: int = 15, month: int = 6) -> dt.datetime:
    return dt.datetime(2026, month, day, hour, minute, tzinfo=TZ)


class WindowMath(unittest.TestCase):
    """纯函数：窗口判定与摘要。"""

    def _item(self, **kwargs) -> dict:
        base = {
            "start_time": "",
            "end_time": "",
            "start_day": 0,
            "end_day": 0,
        }
        base.update(kwargs)
        return base

    def test_no_window_means_always_on(self):
        inside, summary = shift.keepalive_window(self._item(), _at(3))
        self.assertTrue(inside)
        self.assertEqual(summary, "")

    def test_time_window(self):
        item = self._item(start_time="08:00", end_time="23:00")
        self.assertTrue(shift.keepalive_window(item, _at(8, 0))[0], "起点含在窗口内")
        self.assertTrue(shift.keepalive_window(item, _at(22, 59))[0])
        self.assertFalse(shift.keepalive_window(item, _at(23, 0))[0], "终点不含")
        self.assertFalse(shift.keepalive_window(item, _at(7, 59))[0])
        self.assertEqual(shift.keepalive_window(item, _at(12))[1], "每天 08:00–23:00")

    def test_time_window_ending_at_midnight(self):
        """结束填 00:00 = 到 24:00：与联合换班一致，不能当成零长度而忽略窗口。"""
        item = self._item(start_time="08:00", end_time="00:00")
        self.assertTrue(shift.keepalive_window(item, _at(12))[0])
        self.assertTrue(shift.keepalive_window(item, _at(23, 59))[0])
        self.assertFalse(shift.keepalive_window(item, _at(3))[0], "凌晨不在窗口内")
        self.assertEqual(shift.keepalive_window(item, _at(12))[1], "每天 08:00–24:00")

    def test_day_window(self):
        item = self._item(start_day=1, end_day=15)
        self.assertTrue(shift.keepalive_window(item, _at(12, day=1))[0])
        self.assertTrue(shift.keepalive_window(item, _at(12, day=15))[0])
        self.assertFalse(shift.keepalive_window(item, _at(12, day=16))[0])
        self.assertEqual(shift.keepalive_window(item, _at(12, day=15))[1], "1–15 日")

    def test_both_dimensions_are_intersection(self):
        item = self._item(start_time="08:00", end_time="23:00", start_day=1, end_day=15)
        self.assertTrue(shift.keepalive_window(item, _at(12, day=10))[0])
        self.assertFalse(shift.keepalive_window(item, _at(23, day=10))[0], "时刻外")
        self.assertFalse(shift.keepalive_window(item, _at(12, day=20))[0], "日期外")

    def test_end_day_clamped_to_month_length(self):
        """2 月填 1–31 日不能整月不干活：按月末 28 号截断。"""
        item = self._item(start_day=1, end_day=31)
        self.assertTrue(shift.keepalive_window(item, _at(12, day=28, month=2))[0])
        self.assertTrue(shift.keepalive_window(item, _at(12, day=30, month=4))[0], "30 天的小月也截到月末")
        self.assertTrue(shift.keepalive_window(item, _at(12, day=31, month=5))[0])

    def test_broken_config_falls_back_to_unlimited(self):
        """半截 / 跨零点配置按「不限」处理：宁可多跑，也别把机器钉死在停机。"""
        for item in (
            self._item(start_time="08:00"),  # 只有开始
            self._item(start_time="23:00", end_time="06:00"),  # 跨零点
            self._item(start_time="oops", end_time="23:00"),  # 格式错
            self._item(start_day=10),  # 只有开始日
            self._item(start_day=20, end_day=5),  # 跨月
            self._item(start_day="abc", end_day=5),  # 手改库写脏的文本，int() 不能抛
        ):
            inside, summary = shift.keepalive_window(item, _at(12))
            self.assertTrue(inside, item)
            self.assertEqual(summary, "", item)


class KeepaliveWindowEngine(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        for table in TABLES:
            db.execute(f"DELETE FROM {table}")
        repo.set_settings({"ddns_enabled": "0", "notify_on_keepalive": "1", "timezone": "Asia/Shanghai"})

        self.sent: list[notify.Message] = []
        self._orig_send = notify.send_all
        notify.send_all = lambda config, message: (  # noqa: ANN001, ARG005
            self.sent.append(message) or {}
        )

        self.state = "Running"
        self.mode = "StopCharging"
        self.started: list[int] = []
        self.stopped: list[tuple[str, bool]] = []
        self.stop_fail = 0  # >0 时下一次停机抛错，用来验证「失败后补结果」
        self._orig_refresh = shift._refresh  # noqa: SLF001
        self._orig_start = aliyun.start_instance
        self._orig_stop = aliyun.stop_instance
        self._orig_get = aliyun.get_instance

        def _fake_refresh(server: dict) -> None:  # noqa: ANN001
            server["instance_status"] = self.state
            server["stopped_mode"] = self.mode if self.state == "Stopped" else ""
            server["public_ip"] = "47.0.0.1" if self.state == "Running" else ""

        def _fake_start(server: dict) -> None:  # noqa: ANN001
            self.started.append(int(server["id"]))

        def _fake_stop(account: dict, mode: str = "StopCharging", fallback: bool = False) -> str:  # noqa: ANN001, ARG001
            if self.stop_fail > 0:
                self.stop_fail -= 1
                raise RuntimeError("StopInstance 调用失败")
            self.stopped.append((mode, fallback))
            self.state = "Stopped"
            self.mode = mode
            return mode

        shift._refresh = _fake_refresh  # noqa: SLF001
        aliyun.start_instance = _fake_start
        aliyun.stop_instance = _fake_stop
        aliyun.get_instance = lambda server: aliyun.InstanceInfo(  # noqa: ARG005
            self.state,
            self.mode if self.state == "Stopped" else "",
            "47.0.0.1" if self.state == "Running" else "",
            "cn-hongkong",
        )

        self.server_id = repo.upsert_server(
            {
                "label": "保活机",
                "access_key_id": "LTAI5tFakeKeyForTest",
                "access_key_secret": "fake-secret-for-test",
                "region_id": "cn-hongkong",
                "site": "international",
                "instance_id": "i-keepalive",
                "quota_gb": 200,
                "threshold_gb": 188,
                "stop_mode": "StopCharging",
            }
        )
        repo.update_server_runtime(self.server_id, status="Running", stopped_mode="")

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        shift._refresh = self._orig_refresh  # noqa: SLF001
        aliyun.start_instance = self._orig_start
        aliyun.stop_instance = self._orig_stop
        aliyun.get_instance = self._orig_get

    def _keepalive(self, **kwargs) -> int:
        return repo.add_keepalive(self.server_id, enabled=True, **kwargs)

    def _tick(self, now: dt.datetime | None = None) -> None:
        shift.engine.reconcile_keepalives(
            repo.get_settings(), now or dt.datetime.now(TZ), set()
        )

    def _confirm(self) -> None:
        scheduler._confirm_pending_events()  # noqa: SLF001

    def _settle(self) -> None:
        """巡检下发停机 + 确认器看到停稳：一条「暂停」事件的完整两轮。"""
        self._tick(dt.datetime.now(TZ))
        self._confirm()

    def _events(self) -> list[dict]:
        return repo.list_events(limit=50)

    def _titles(self) -> list[str]:
        return [str(e["title"]) for e in self._events()]

    def _texts(self, event: dict) -> list[str]:
        return [str(step.get("text")) for step in event["progress"]]

    # -- 窗口外 ---------------------------------------------------------

    def test_outside_window_stops_and_logs_pause(self):
        self._keepalive(start_time="08:00", end_time="23:00")
        self._tick(_at(23, 30))

        self.assertEqual(self.stopped, [("StopCharging", True)], "到点必须真的停掉")
        self.assertEqual(self.started, [], "窗口外不许拉起")
        self.assertEqual(len(self._events()), 1)
        event = self._events()[0]
        self.assertEqual(event["title"], "单机保活窗口暂停（保活机）")
        # 指令下去了但还没确认停稳：事件必须还是「进行中」，不能先写「已暂停」
        self.assertEqual(event["status"], "running")
        self.assertEqual(event["detail"], "等待停机到位")
        self.assertEqual(
            self._texts(event),
            ["已离开运行窗口（每天 08:00–23:00），窗口外不再自动拉起", "已下发停机指令（节省停机）"],
            self._texts(event),
        )
        self.assertEqual(self.sent, [], "到点停机是预期动作，不该推通知")

        # 确认器看到 Stopped 才收尾：过程里有结果，不是「下发完就不管了」
        self._confirm()
        event = self._events()[0]
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["detail"], "已暂停（节省停机）")
        self.assertIn("已确认 Stopped（节省停机）", self._texts(event))

    def test_pause_completion_needs_stopped_confirmation(self):
        """机器没停稳（Stopping）时事件不能收尾：等它真停下来才算完。"""
        self._keepalive(start_time="08:00", end_time="23:00")

        def slow_stop(account, mode="StopCharging", fallback=False):  # noqa: ARG001
            self.stopped.append((mode, fallback))
            self.state = "Stopping"
            return mode

        aliyun.stop_instance = slow_stop
        self._tick(_at(23, 30))
        self._confirm()
        self.assertEqual(self._events()[0]["status"], "running", "还在停，不能写完成")

        self.state = "Stopped"
        self.mode = "StopCharging"
        self._confirm()
        event = self._events()[0]
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["detail"], "已暂停（节省停机）")

    def test_pause_logged_once_per_rest(self):
        """同一段休止时间里只记一条，不每分钟刷一条。"""
        self._keepalive(start_time="08:00", end_time="23:00")
        for _ in range(3):
            self._tick(_at(23, 30))

        self.assertEqual(len(self._events()), 1, self._titles())
        self.assertEqual(len(self.stopped), 1, "停过一次就够了，停机状态下不该重发指令")

    def test_rest_closes_running_pull_event(self):
        """进入休止时段时，那条还在转的「保活拉起」不能再转圈：改成「等停机」。"""
        self._keepalive(start_time="08:00", end_time="23:00")
        self.state = "Stopped"
        self._tick(_at(12))  # 窗口内：正常拉起，建一条 running 事件
        running = [e for e in self._events() if e["status"] == "running"]
        self.assertEqual(len(running), 1, self._titles())

        self.state = "Running"
        self._tick(_at(23, 30))  # 出窗口

        self.assertNotIn("单机保活窗口恢复中（保活机）", self._titles(), "原事件被复用改名")
        pause = [e for e in self._events() if e["title"] == "单机保活窗口暂停（保活机）"][0]
        self.assertEqual(pause["status"], "running", "等停机确认")
        self.assertEqual(self.stopped, [("StopCharging", True)])

        self._confirm()
        event = [e for e in self._events() if e["title"] == "单机保活窗口暂停（保活机）"][0]
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["detail"], "已暂停（节省停机）")
        self.assertEqual([e for e in self._events() if e["status"] == "running"], [])

    def test_outside_window_skips_limit_stop(self):
        """窗口外先按休止处理，不因为超阈值再推一条超限停机报告。"""
        self._keepalive(start_time="08:00", end_time="23:00")
        repo.update_server_runtime(self.server_id, traffic=199)
        self._tick(_at(23, 30))

        self.assertEqual(self.sent, [])
        self.assertEqual(self._titles(), ["单机保活窗口暂停（保活机）"])

    # -- 回到窗口 -------------------------------------------------------

    def test_resume_logs_and_starts_without_notification(self):
        self._keepalive(start_time="08:00", end_time="23:00")
        self._tick(_at(23, 30))
        self._confirm()
        self.state = "Stopped"
        self.mode = "StopCharging"
        events_before = len(self._events())

        self._tick(_at(8, 30))
        self.assertEqual(self.started, [self.server_id], "进窗口该把机器拉起来")
        new_events = self._events()[: len(self._events()) - events_before]
        self.assertEqual(len(new_events), 1, self._titles())
        event = new_events[0]
        self.assertEqual(event["title"], "单机保活窗口恢复中（保活机）")
        self.assertEqual(event["status"], "running", "等确认到位")
        self.assertEqual(event["detail"], "等待开机到位")
        self.assertEqual(
            self._texts(event),
            ["已进入运行窗口（每天 08:00–23:00），恢复自动拉起", "已下发开机指令（原状态 已停机）"],
            self._texts(event),
        )
        self.assertEqual(self.sent, [], "到点开机不是被抢占，不推通知")

    def test_resume_completion_keeps_title_and_stays_quiet(self):
        """确认器把这条事件收尾时：标题变成「窗口恢复」，且不推抢占恢复。"""
        self._keepalive(start_time="08:00", end_time="23:00")
        self._tick(_at(23, 30))
        self._confirm()
        self.state = "Stopped"
        self._tick(_at(8, 30))

        self.state = "Running"
        self._confirm()

        self.assertEqual(self.sent, [], "计划内开机不推「抢占恢复」")
        event = [e for e in self._events() if e["title"] == "单机保活窗口恢复（保活机）"][0]
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["detail"], "运行中")
        self.assertIn("已确认 Running（运行中）", self._texts(event))

    def test_resume_event_when_already_running(self):
        """休止时段里被人手动开机：回窗口只记一条「窗口恢复」，不再发指令。"""
        self._keepalive(start_time="08:00", end_time="23:00")
        self._tick(_at(23, 30))
        self._confirm()
        self.state = "Running"  # 有人在窗口外手动开回来了
        repo.update_server_runtime(self.server_id, status="Running", stopped_mode="")

        self._tick(_at(8, 30))
        self.assertEqual(self.started, [], "已经在跑，不该再下发开机")
        event = [e for e in self._events() if e["title"] == "单机保活窗口恢复（保活机）"][0]
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["detail"], "运行中")

    def test_mid_state_resume_does_not_fall_back_to_normal_pull(self):
        """窗口刚开时实例还在 Stopping：下一轮真正拉起也不能退回普通「保活拉起」。

        恢复那一轮发不出开机指令（实例还在停），真正拉起发生在下一轮。
        不把这次中断记成「计划内」，下一轮会建一条普通 start 事件，
        确认器到位后推「实例疑被抢占」——到点开机根本不是被抢占。
        """
        self._keepalive(start_time="08:00", end_time="23:00")

        def slow_stop(account, mode="StopCharging", fallback=False):  # noqa: ARG001
            self.stopped.append((mode, fallback))
            self.state = "Stopping"
            return mode

        aliyun.stop_instance = slow_stop
        self._tick(_at(23, 30))
        self.assertEqual(self.state, "Stopping")

        self._tick(_at(8, 30))  # 窗口开了，但实例还在停的路上
        self.assertEqual(self.started, [], "还在停，这一轮发不出开机")

        self.state = "Stopped"
        self.mode = "StopCharging"
        self._tick(_at(8, 31))  # 停稳了，这一轮才真正拉起
        self.assertEqual(self.started, [self.server_id])

        starts = [e for e in self._events() if e["kind"] == "start"]
        self.assertEqual(starts, [], f"不该退回普通保活拉起：{self._titles()}")
        quiet = [e for e in self._events() if e["kind"] == "keepalive_quiet"]
        self.assertEqual(len(quiet), 1, self._titles())
        self.assertEqual(self.sent, [])

    def test_stopping_state_does_not_resend_stop(self):
        """实例还在 Stopping：每分钟一轮不能重复下发停机指令。"""
        self._keepalive(start_time="08:00", end_time="23:00")

        def slow_stop(account, mode="StopCharging", fallback=False):  # noqa: ARG001
            self.stopped.append((mode, fallback))
            self.state = "Stopping"
            return mode

        aliyun.stop_instance = slow_stop
        for _ in range(3):
            self._tick(_at(23, 30))
        self.assertEqual(len(self.stopped), 1, f"重复下发 {len(self.stopped)} 次")

    def test_confirm_converts_pull_to_stop_wait(self):
        """确认器先发现出窗口：把还在转的拉起改成「等停机」，不能就地写「已暂停」。"""
        self._keepalive(start_time="08:00", end_time="23:00")
        self.state = "Stopped"
        self._tick(_at(12))  # 建一条还在转的「保活拉起」
        self.state = "Running"
        repo.update_server_runtime(self.server_id, status="Running", stopped_mode="")
        # 确认器用真实时间判窗口，把窗口挪到已经过去的时段
        repo.update_keepalive(
            int(repo.list_keepalives()[0]["id"]),
            enabled=True,
            start_time="00:00",
            end_time="00:01",
        )
        self._confirm()
        event = [e for e in self._events() if e["title"] == "单机保活窗口暂停（保活机）"][0]
        self.assertEqual(event["status"], "running", "停机指令还没下发，不能写完成")
        self.assertEqual(event["detail"], "等待停机到位")

        self._tick(dt.datetime.now(TZ))  # 巡检这一轮才真正下发停机
        event = repo.get_event(int(event["id"]))
        self.assertIn("已下发停机指令（节省停机）", self._texts(event))
        self.assertEqual(event["status"], "running", "指令下发≠停稳，等确认器")

        self._confirm()
        event = repo.get_event(int(event["id"]))
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["detail"], "已暂停（节省停机）")
        self.assertIn("已确认 Stopped（节省停机）", self._texts(event))

    def test_confirm_closes_pull_when_window_closes(self):
        """确认器先发现出窗口：转成「等停机」，全程不推通知，巡检不再重复记。"""
        self._keepalive(start_time="08:00", end_time="23:00")
        self.state = "Stopped"
        self._tick(_at(12))  # 拉起事件，等 Running
        self.assertEqual(len(self.sent), 0)

        self.state = "Running"
        # 确认器用的是「现在」的时间，这里直接把窗口改成已经过去的时段
        repo.update_keepalive(
            int(repo.list_keepalives()[0]["id"]),
            enabled=True,
            start_time="00:00",
            end_time="00:01",
        )
        self._confirm()

        self.assertEqual(self.sent, [], "窗口外到位也不许推抢占恢复")
        titles = self._titles()
        self.assertIn("单机保活窗口暂停（保活机）", titles, titles)
        # 巡检再来一轮不会重复记一条，且把停机结果补进同一条
        self._tick(_at(23, 30))
        self.assertEqual(len([t for t in self._titles() if t == "单机保活窗口暂停（保活机）"]), 1)
        self._confirm()
        event = [e for e in self._events() if e["title"] == "单机保活窗口暂停（保活机）"][0]
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["detail"], "已暂停（节省停机）")

    def test_first_pull_after_adding_keepalive_notifies(self):
        """保活刚加上、机器本来就停着：窗口内第一次拉起也要推通知。

        规则只有一条：窗口到点的那条（kind=keepalive_quiet）安静，其余拉起全部
        走「单机保活抢占恢复」并推通知。以前按「上一轮是否看到它在跑」判断，
        手动关机后重新开保活 / 刚加进保活都会被当成计划内，用户明明要求它在线、
        机器却没起来，反而收不到消息。
        """
        self._keepalive(start_time="00:00", end_time="23:59")  # 现在就在窗口内
        self.state = "Stopped"
        self.mode = "StopCharging"
        self._tick(_at(12))

        self.assertEqual(self.started, [self.server_id])
        self.state = "Running"
        self._confirm()

        self.assertEqual(len(self.sent), 1, "非窗口起点的拉起要走「抢占恢复」渠道")
        event = [e for e in self._events() if e["status"] == "done"][0]
        self.assertEqual(event["title"], "单机保活抢占恢复（保活机）")

    def test_real_preemption_still_notifies(self):
        """人在跑着被收回：照旧推通知，且事件走同一套标题。"""
        self._keepalive()  # 没有运行窗口
        self.state = "Running"
        self._tick(_at(12))  # 正常在跑：不该产生任何事件
        self.assertEqual(self._events(), [], "正常运行不该产生事件")

        self.state = "Stopped"  # 被阿里云收回
        self.mode = "StopCharging"
        self._tick(_at(12, 1))
        self.state = "Running"
        self._confirm()

        self.assertEqual(len(self.sent), 1, "真抢占必须推「抢占恢复」")
        self.assertIn("抢占恢复", self.sent[0].title)

    def test_readd_after_manual_stop_notifies(self):
        """移除保活 → 手动关机 → 重新开 24 小时保活：拉起后要推通知。

        回归（用户实测报的）：以前按 keepalive_live 判断「是不是被抢占」，手动
        关机把它清掉了，于是重新加入保活后的第一次拉起被当成计划内，事件静默
        完成、通知一条没有 —— 用户明确要求它在线，机器没起来却收不到消息。
        规则改成：只有窗口到点那条安静（kind=keepalive_quiet），其余全走抢占恢复。
        """
        kid = self._keepalive(start_time="00:00", end_time="23:59")
        self._tick(_at(12))
        repo.set_keepalive_enabled(kid, False)  # 移除保活

        self.state = "Stopped"  # 手动关机
        self.mode = "StopCharging"
        repo.update_server_runtime(self.server_id, status="Stopped", stopped_mode="StopCharging")

        repo.add_keepalive(self.server_id, enabled=True, start_time="00:00", end_time="23:59")
        self._tick(_at(12, 1))
        self.assertEqual(self.started, [self.server_id], "重新开保活要立刻拉起")
        self.state = "Running"
        self._confirm()

        self.assertEqual(len(self.sent), 1, "非窗口起点的拉起必须推通知")
        event = [e for e in self._events() if "抢占恢复" in e["title"]][0]
        self.assertEqual(event["title"], "单机保活抢占恢复（保活机）")
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["detail"], "运行中")

    def test_stop_failure_event_gets_its_result(self):
        """停机失败一次、下一轮停成：那条失败事件要补上「重试成功」。

        以前只清 fail 标记，事件永远停在「停机指令失败」，用户点开不知道后来
        到底停成没有 —— 与「凡是要等外部状态的动作都得有结果」这条规则相悖。
        """
        self._keepalive(start_time="08:00", end_time="23:00")
        self.stop_fail = 1
        self._tick(_at(23, 30))  # 第一次：指令失败，只记一条
        failed = [e for e in self._events() if e["title"].startswith("单机保活停机失败")]
        self.assertEqual(len(failed), 1, self._titles())
        self.assertIn("停机指令失败", failed[0]["detail"])

        self._tick(_at(23, 31))  # 第二次：停成了
        event = [e for e in self._events() if e["title"].startswith("单机保活停机失败")][0]
        self.assertEqual(event["detail"], "重试成功，已停机（节省停机）")
        self.assertIn("重试成功，已停机（节省停机）", self._texts(event))

    # -- 停机模式 -------------------------------------------------------

    def test_stop_mode_preference_is_used(self):
        repo.upsert_server(
            {
                "id": self.server_id,
                "label": "保活机",
                "region_id": "cn-hongkong",
                "site": "international",
                "instance_id": "i-keepalive",
                "quota_gb": 200,
                "threshold_gb": 188,
                "stop_mode": "Stop",
            }
        )
        self._keepalive(start_time="08:00", end_time="23:00")
        self._tick(_at(23, 30))

        self.assertEqual(self.stopped, [("Stop", False)], "选普通停机就别 fallback")
        self._confirm()
        self.assertEqual(self._events()[0]["detail"], "已暂停（普通停机）")

    def test_manual_stop_reports_real_mode(self):
        """手动停机的事件摘要写实际生效的模式，不再写死节省停机。"""
        repo.upsert_server(
            {
                "id": self.server_id,
                "label": "保活机",
                "region_id": "cn-hongkong",
                "site": "international",
                "instance_id": "i-keepalive",
                "quota_gb": 200,
                "threshold_gb": 188,
                "stop_mode": "Stop",
            }
        )
        shift.engine.manual_action(self.server_id, "stop")
        event = self._events()[0]
        self.assertEqual(event["detail"], "等待停机到位（普通停机）")


class StopModeStore(unittest.TestCase):
    """实例设置里的停机模式：默认节省停机、脏值归一、不传时不被重置。"""

    def setUp(self) -> None:
        db.init_db()
        for table in TABLES:
            db.execute(f"DELETE FROM {table}")

    def _payload(self, **kwargs) -> dict:
        base = {
            "label": "机器",
            "access_key_id": "LTAI5tFakeKeyForTest",
            "access_key_secret": "fake-secret-for-test",
            "region_id": "cn-hongkong",
            "site": "international",
            "instance_id": "i-x",
            "quota_gb": 200,
            "threshold_gb": 188,
        }
        base.update(kwargs)
        return base

    def test_defaults_to_saving_shutdown(self):
        server_id = repo.upsert_server(self._payload())
        self.assertEqual(repo.get_server(server_id)["stop_mode"], "StopCharging")

    def test_dirty_value_falls_back(self):
        server_id = repo.upsert_server(self._payload(stop_mode="whatever"))
        self.assertEqual(repo.get_server(server_id)["stop_mode"], "StopCharging")
        self.assertEqual(repo.normalize_stop_mode("Stop"), "Stop")

    def test_editing_other_fields_keeps_mode(self):
        """其它入口（刷新、改配额）不带 stop_mode 时不能把它重置回默认。"""
        server_id = repo.upsert_server(self._payload(stop_mode="Stop"))
        repo.upsert_server({**self._payload(), "id": server_id, "quota_gb": 300})
        self.assertEqual(repo.get_server(server_id)["stop_mode"], "Stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
