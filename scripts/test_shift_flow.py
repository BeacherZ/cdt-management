#!/usr/bin/env python3
"""换班流程的端到端测试（dry-run 内存桩，不碰真实阿里云）。

锁死两条容易回归的规则：
  1. 一轮换班只产生 **一条** 事件（启动当班 / 停机非当班 / DDNS 都是同一条事件的进度），
     早期版本会给每一步各开一条，界面上一次换班冒出三条记录。
  2. DDNS 动作排在「当班 Running + 非当班全部停机」确认之后，且整条事件里只记一次，
     不会出现「DDNS：✅ 已同步」紧跟「DDNS：✅ 无变化」这种无意义进度。

跑法：python scripts/test_shift_flow.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前设好：数据目录指向临时目录，dry-run + 同步到位
_TMP = tempfile.mkdtemp(prefix="cdt-shift-flow-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, repo, settings, shift  # noqa: E402


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


class ShiftFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()
        cls.a_id = _make_server("机器A", "i-test-aaa")
        cls.b_id = _make_server("机器B", "i-test-bbb")
        cls.group_id = repo.upsert_group(
            {
                "name": "测试组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": cls.a_id, "hours": 12},
                    {"server_id": cls.b_id, "hours": 12},
                ],
            }
        )
        # 让 A 落在当班：把轮换起点挪到 1 小时前
        now_ts = time.time()
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?", (int(now_ts - 3600), cls.group_id)
        )
        # 换班前的真实样子：当班 A 还没起来，非当班 B 还在跑
        server_b = repo.get_server(cls.b_id, decrypt_secret=True)
        aliyun.start_instance(server_b)
        cls.now = now_ts

    def _reconcile(self) -> dict:
        group = repo.get_group(self.group_id) or {}
        config = repo.get_settings()
        return shift.engine.reconcile_group(
            group, config, _now_dt(), float(time.time())
        )

    def test_1_single_event_and_ddns_last(self):
        for _ in range(4):
            self._reconcile()

        events = repo.list_events(limit=50, group_id=self.group_id)
        # 一轮换班只留一条事件
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        event = events[0]
        self.assertEqual(event["kind"], "shift_done")
        self.assertEqual(event["status"], "done")
        self.assertTrue(event["title"].endswith("：换班完成"))

        texts = [step["text"] for step in event["progress"]]
        # 启动当班 / 停机非当班 都只是进度，不再各自开事件
        self.assertEqual(sum("已下发启动当班" in t for t in texts), 1, texts)
        self.assertEqual(sum("已下发停机" in t for t in texts), 1, texts)
        self.assertEqual(sum(t.startswith("DDNS：") for t in texts), 1, texts)
        # DDNS 排在停机之后，且是最后一步之前
        self.assertGreater(
            next(i for i, t in enumerate(texts) if t.startswith("DDNS：")),
            next(i for i, t in enumerate(texts) if "已下发停机" in t),
            texts,
        )
        self.assertEqual(texts[-1], "全部状态确认完毕")

    def test_2_recovery_after_settle(self):
        """已结算的班次里当班被停掉 → 单独一条恢复事件，不当成换班超时。"""
        self._reconcile()  # 先把班次结算掉
        slot_key = str(int(self.now))

        server_a = repo.get_server(self.a_id, decrypt_secret=True)
        aliyun.stop_instance(server_a, "StopCharging")
        for _ in range(4):
            self._reconcile()

        events = repo.list_events(limit=50, group_id=self.group_id)
        recovery = [e for e in events if e["kind"] == "recovery"]
        self.assertEqual(len(recovery), 1, [e["title"] for e in events])
        self.assertTrue(recovery[0]["title"].endswith("：当班已恢复运行"))
        self.assertEqual(recovery[0]["status"], "done")
        # 恢复不是换班，不该留下换班超时的去重标记
        self.assertFalse(repo.get_flag(f"timeout:{self.group_id}:{slot_key}"))


def _now_dt():
    import datetime as dt

    return dt.datetime.now(shift._timezone("Asia/Shanghai"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
