#!/usr/bin/env python3
"""第一批健壮性/安全加固的回归测试。

锁死这几条规则：
  1. add_keepalive 是原子 upsert：重复添加不报 UNIQUE 冲突，只覆盖 enabled；
  2. manual_action 幂等：同一台机器同一目标状态已有进行中事件时不再重复下发；
  3. upsert_server 配额显式校验：quota 传 0/负数直接拒绝，不再被 or 默认值吞掉。

跑法：python scripts/test_security_hardening.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cdt-security-hardening-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import db, repo, security, settings, shift  # noqa: E402

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


class ManualReconcileBusy(unittest.TestCase):
    """后台正忙时点「立即对账」，接口必须能说出「这轮没跑」。"""

    def test_lock_held_returns_none(self):
        engine = shift.Engine()
        engine._lock.acquire()  # noqa: SLF001 - 模拟上一轮还没结束
        try:
            self.assertIsNone(engine.reconcile_all())
        finally:
            engine._lock.release()


class MasterKeyFirstCall(unittest.TestCase):
    """冷启动并发首调只能产生一把密钥。

    以前是「先判文件不存在、再 write_bytes」：两个调用方各生成一把，后写的
    覆盖先写的，用另一把加密的凭据从此解不开。
    """

    def test_concurrent_first_call_yields_one_key(self):
        key_path = settings.MASTER_KEY_PATH
        if key_path.exists():
            key_path.unlink()
        security._MASTER_KEY = None  # noqa: SLF001
        keys: list[bytes] = []
        errors: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def grab() -> None:
            barrier.wait()
            try:
                key = security.master_key()
                with lock:
                    keys.append(key)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(keys), 8)
        self.assertEqual(len(set(keys)), 1, "并发首调生成了不同的密钥")
        self.assertEqual(key_path.read_bytes()[:32], keys[0])
        self.assertEqual(len(key_path.read_bytes()), 32)

    def test_text_mode_key_file_is_repaired(self):
        """1.2.8 在 Windows 上写出过 33 字节的密钥文件（文本模式把 0x0A 变 \\r\\n）。

        前 32 字节不是真密钥，拿去解密会把所有凭据判成「master.key 被替换」。
        加载时要按插入位置还原成 32 字节并写回，不能直接截前 32 字节。
        """
        key_path = settings.MASTER_KEY_PATH
        real = bytes(range(200, 232))
        real = real[:5] + b"\n" + real[6:]
        key_path.write_bytes(real.replace(b"\n", b"\r\n", 1))
        security._MASTER_KEY = None  # noqa: SLF001

        self.assertEqual(security.master_key(), real)
        self.assertEqual(len(key_path.read_bytes()), 32, "还原后要写回磁盘")


if __name__ == "__main__":
    unittest.main()
