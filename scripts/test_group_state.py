#!/usr/bin/env python3
"""换班组徽章状态测试（纯函数，不碰数据库 / 阿里云）。

徽章要回答的是「此刻这个组到底在干什么」，所以判定看的是实例真实状态 +
本班次的覆盖来源，而不是上一轮写进库的 phase：

    正常排班中（绿） / 换班中（蓝） / 故障转移中（黄） / 手动换班中（蓝）
    恢复中（黄） / 已熔断停机（红） / 换班异常（红） / 已停用 / 待排班 / 状态未知

跑法：python scripts/test_group_state.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cdt-group-state-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"

from app import api  # noqa: E402


def _server(server_id: int, status: str) -> dict:
    return {"id": server_id, "instance_status": status}


class GroupState(unittest.TestCase):
    def _state(
        self,
        *,
        enabled: bool = True,
        run: dict | None = None,
        duty_status: str = "Running",
        other_status: str = "Stopped",
        members: int = 2,
    ) -> str:
        group = {"id": 1, "enabled": enabled}
        member_rows = [{"server_id": 1}, {"server_id": 2}, {"server_id": 3}][:members]
        picked = (member_rows[0], 0) if member_rows else None
        servers = {1: _server(1, duty_status), 2: _server(2, other_status)}
        if members >= 3:
            servers[3] = _server(3, "Stopped")
        return api.group_state(group, run or {}, picked, member_rows, servers)

    def test_all_matching_is_scheduled(self):
        self.assertEqual(self._state(), "scheduled")
        self.assertEqual(api.GROUP_STATES["scheduled"], "正常排班中")

    def test_duty_stopped_is_recovering(self):
        """该跑却没跑：被抢占或被手动关机，引擎正拉起。"""
        self.assertEqual(self._state(duty_status="Stopped"), "recovering")

    def test_duty_starting_is_switching(self):
        self.assertEqual(self._state(duty_status="Starting"), "switching")

    def test_standby_still_running_is_switching(self):
        """换班还没到位：下一台已经开了，上一台还没停完。"""
        self.assertEqual(self._state(other_status="Running"), "switching")

    def test_failover_stays_amber_for_whole_slot(self):
        self.assertEqual(self._state(run={"failover": 1, "phase": "failover"}), "failover")
        # 故障转移后重新就绪，仍然算这段班次的故障转移，不是「正常排班中」
        self.assertEqual(self._state(run={"failover": 1, "phase": "switching"}), "failover")

    def test_manual_switch_stays_blue_for_whole_slot(self):
        self.assertEqual(self._state(run={"manual": 1, "phase": "manual"}), "manual")
        self.assertEqual(self._state(run={"manual": 1, "phase": "switching"}), "manual")

    def test_next_normal_slot_clears_override(self):
        """下次正常换班开新班次，run 里没有 manual / failover，回到正常排班中。"""
        self.assertEqual(self._state(run={"duty_server_id": 2}), "scheduled")

    def test_breaker_and_error(self):
        self.assertEqual(self._state(run={"phase": "breaker"}), "breaker")
        self.assertEqual(self._state(run={"phase": "error"}), "error")

    def test_disabled_and_idle(self):
        self.assertEqual(self._state(enabled=False), "disabled")
        self.assertEqual(self._state(members=1), "idle")

    def test_unknown_when_status_missing(self):
        self.assertEqual(self._state(duty_status=""), "unknown")
        # 非当班状态没读到也不许报「正常排班中」：不知道它停没停
        self.assertEqual(self._state(other_status=""), "unknown")

    def test_labels_cover_every_state(self):
        """每个状态都得有中文文案，界面不能漏出英文 key。"""
        states = {
            "scheduled",
            "switching",
            "failover",
            "manual",
            "recovering",
            "breaker",
            "error",
            "disabled",
            "idle",
            "unknown",
        }
        self.assertEqual(set(api.GROUP_STATES), states)


if __name__ == "__main__":
    unittest.main(verbosity=2)
