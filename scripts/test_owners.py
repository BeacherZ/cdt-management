#!/usr/bin/env python3
"""实例归属互斥的回归测试（dry-run 内存数据，不碰真实阿里云与 Cloudflare）。

锁死三件事：
  1. 一台实例只能归单机保活 / 联合换班 / 流量均衡其中之一，
     四个方向（三个模块各自入组）都得拦住，不能只拦一边。
  2. 编辑已有组时，它自己的成员不算冲突 —— 否则改个名字都报错。
  3. 闲散实例随便进，别把校验写成「有归属就拒」。

跑法：python scripts/test_owners.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cdt-owners-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import db, owners, repo, shift  # noqa: E402


def _make_server(label: str, instance_id: str) -> int:
    return repo.upsert_server(
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


class OwnersTest(unittest.TestCase):
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

        self.shift_a = _make_server("换班A", "i-own-shift-a")
        self.shift_b = _make_server("换班B", "i-own-shift-b")
        self.bal_a = _make_server("均衡A", "i-own-bal-a")
        self.bal_b = _make_server("均衡B", "i-own-bal-b")
        self.keep = _make_server("保活机", "i-own-keep")
        self.idle = _make_server("闲散机", "i-own-idle")

        self.shift_id = repo.upsert_group(
            {
                "name": "换班组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": self.shift_a, "hours": 12},
                    {"server_id": self.shift_b, "hours": 12},
                ],
            }
        )
        self.balance_id = repo.upsert_balance_group(
            {
                "name": "均衡组",
                "enabled": True,
                "failover_enabled": True,
                "interval_minutes": 60,
                "diff_gb": 10,
                "min_duty_minutes": 30,
                "members": [
                    {"server_id": self.bal_a, "off_action": "keep"},
                    {"server_id": self.bal_b, "off_action": "keep"},
                ],
            }
        )
        repo.add_keepalive(self.keep, True)

    # -- 归属表 ---------------------------------------------------------

    def test_managed_map_marks_each_kind(self) -> None:
        managed = owners.managed_map()
        self.assertEqual(managed[self.shift_a]["kind"], "group")
        self.assertEqual(managed[self.bal_a]["kind"], "balance")
        self.assertEqual(managed[self.keep]["kind"], "keepalive")
        self.assertNotIn(self.idle, managed)

    # -- 均衡组当班标记 -------------------------------------------------

    def test_managed_map_marks_balance_duty(self) -> None:
        """实例卡片靠这个标记决定开关机按钮放不放行，标错就会锁错人。"""
        repo.upsert_balance_state(self.balance_id, duty_server_id=self.bal_a)
        managed = owners.managed_map()
        self.assertTrue(managed[self.bal_a]["duty"])
        self.assertFalse(managed[self.bal_b]["duty"])

    def test_manual_note_splits_balance_duty_and_off(self) -> None:
        repo.upsert_balance_state(self.balance_id, duty_server_id=self.bal_a)
        note, mode, gid = shift.engine._managed_note(self.bal_a)
        self.assertEqual(mode, "balance_duty")
        self.assertEqual(gid, self.balance_id)
        self.assertIn("当班", note)
        # 当班被手动关机 = 中断对外服务，提示要说清楚后面靠什么兜底
        self.assertIn("故障转移", shift.engine._managed_detail(note, mode))

        note, mode, _ = shift.engine._managed_note(self.bal_b)
        self.assertEqual(mode, "balance_off")
        self.assertIn("非当班", note)
        # 非当班是唯一允许手动开关机的托管成员：提示不能写成「会被纠正」，
        # 也不能再承诺「下次评估会按设置停」—— 评估点已不再动开关机
        detail = shift.engine._managed_detail(note, mode)
        self.assertIn("只比流量差值", detail)
        self.assertNotIn("按流程纠正", detail)

    def test_manual_action_refuses_while_balance_switching(self) -> None:
        repo.upsert_balance_state(self.balance_id, duty_server_id=self.bal_a, phase="switching")
        with self.assertRaises(ValueError) as ctx:
            shift.engine.manual_action(self.bal_b, "start")
        self.assertIn("正在切换", str(ctx.exception))

    # -- 四个方向都要拦 -------------------------------------------------

    def test_balance_member_cannot_join_shift(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            owners.ensure_free([self.bal_a], owners.SHIFT)
        self.assertIn("流量均衡", str(ctx.exception))

    def test_shift_member_cannot_join_balance(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            owners.ensure_free([self.shift_a], owners.BALANCE)
        self.assertIn("联合换班", str(ctx.exception))

    def test_shift_member_cannot_add_keepalive(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            owners.ensure_free([self.shift_a], owners.KEEPALIVE)
        self.assertIn("联合换班", str(ctx.exception))

    def test_balance_member_cannot_add_keepalive(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            owners.ensure_free([self.bal_b], owners.KEEPALIVE)
        self.assertIn("流量均衡", str(ctx.exception))

    # -- 编辑本组要放行 ------------------------------------------------

    def test_editing_own_group_is_allowed(self) -> None:
        owners.ensure_free([self.shift_a, self.shift_b], owners.SHIFT, self.shift_id)
        owners.ensure_free([self.bal_a, self.bal_b], owners.BALANCE, self.balance_id)

    # -- 闲散随便进 ----------------------------------------------------

    def test_idle_server_passes_every_flow(self) -> None:
        for kind in (owners.KEEPALIVE, owners.SHIFT, owners.BALANCE):
            owners.ensure_free([self.idle], kind)

    def test_conflict_message_carries_server_label(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            owners.ensure_free([self.bal_a], owners.SHIFT, labels={self.bal_a: "新加坡-均衡A"})
        self.assertIn("新加坡-均衡A", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
