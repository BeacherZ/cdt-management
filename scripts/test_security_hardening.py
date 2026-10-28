#!/usr/bin/env python3
"""第一批健壮性/安全加固的回归测试。

锁死这几条规则：
  1. add_keepalive 是原子 upsert：重复添加不报 UNIQUE 冲突，只覆盖 enabled；
  2. manual_action 幂等：同一台机器同一目标状态已有进行中事件时不再重复下发；
  3. upsert_server 配额显式校验：quota 传 0/负数直接拒绝，不再被 or 默认值吞掉；
  4. _expected_setup_token 从数据目录 init.token 读令牌。

跑法：python scripts/test_security_hardening.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cdt-security-hardening-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import api, db, repo, settings, shift  # noqa: E402

db.init_db()


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


class SecurityHardening(unittest.TestCase):
    def setUp(self) -> None:
        for table in (
            "events",
            "flags",
            "shift_runs",
            "shift_members",
            "shift_groups",
            "keepalives",
            "servers",
        ):
            db.execute(f"DELETE FROM {table}")

    def test_add_keepalive_upsert(self):
        sid = _make_server("保活机", "i-ka-upsert")
        first = repo.add_keepalive(sid, True)
        second = repo.add_keepalive(sid, False)
        # 重复添加不再 UNIQUE 冲突，返回同一条记录
        self.assertEqual(first, second)
        rows = repo.list_keepalives()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["enabled"], 0)

    def test_manual_action_idempotent(self):
        sid = _make_server("幂等机", "i-idempotent")
        first = shift.engine.manual_action(sid, "start")
        self.assertFalse(first.get("duplicate"))
        # 同机同目标状态再来一次 → 幂等，不再下发指令、不再多记事件
        second = shift.engine.manual_action(sid, "start")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(len(repo.list_events()), 1)

    def test_upsert_server_quota_guard(self):
        with self.assertRaises(ValueError):
            repo.upsert_server(
                {
                    "label": "配额0",
                    "access_key_id": "LTAI5tFakeKeyForTest",
                    "access_key_secret": "fake-secret-for-test",
                    "region_id": "cn-hongkong",
                    "instance_id": "i-quota0",
                    "quota_gb": 0,
                    "threshold_gb": 188,
                }
            )

    def test_expected_setup_token_from_file(self):
        # 环境变量没设时，令牌来自数据目录下的 init.token
        settings.SETUP_TOKEN = ""
        settings.SETUP_TOKEN_PATH.write_text("tok-abc-123")
        try:
            self.assertEqual(api._expected_setup_token(), "tok-abc-123")
        finally:
            settings.SETUP_TOKEN_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
