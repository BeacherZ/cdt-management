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

    def _server_payload(self, **overrides):
        payload = {
            "label": "配额非法",
            "access_key_id": "LTAI5tFakeKeyForTest",
            "access_key_secret": "fake-secret-for-test",
            "region_id": "cn-hongkong",
            "instance_id": "i-bad-quota",
            "quota_gb": 200,
            "threshold_gb": 188,
        }
        payload.update(overrides)
        return payload

    def test_upsert_server_rejects_non_finite_quota(self):
        # inf 能绕过 quota>0 / threshold<=quota 落库，之后所有返回服务器的接口
        # 都会因为 JSON 不接受 Infinity 变成 400；nan 会触发 NOT NULL → 500。
        for raw in ("inf", "-inf", "nan", "1e309", float("inf"), float("nan")):
            with self.subTest(quota=raw), self.assertRaises(ValueError):
                repo.upsert_server(self._server_payload(quota_gb=raw))

    def test_upsert_server_rejects_non_finite_threshold(self):
        for raw in ("inf", "nan", float("inf")):
            with self.subTest(threshold=raw), self.assertRaises(ValueError):
                repo.upsert_server(self._server_payload(threshold_gb=raw))

    def test_upsert_server_rejects_non_numeric_quota(self):
        for raw in ("abc", {"a": 1}, [1]):
            with self.subTest(quota=raw), self.assertRaises(ValueError):
                repo.upsert_server(self._server_payload(quota_gb=raw))

    def test_upsert_server_accepts_string_numbers(self):
        sid = repo.upsert_server(self._server_payload(quota_gb="200", threshold_gb="188"))
        self.assertEqual(repo.get_server(sid)["quota_gb"], 200)

    def test_server_summary_survives_legacy_infinite_values(self):
        """老库里已有的 inf / nan 不能让服务器页和仪表盘整页 400。"""
        import json

        from app import api

        row = {
            "id": 1,
            "label": "老数据",
            "quota_gb": float("inf"),
            "threshold_gb": float("nan"),
            "traffic_used": 10.0,
            "instance_status": "Running",
            "stopped_mode": "",
            "public_ip": "",
            "remark": "",
            "site": "international",
            "stop_mode": "",
            "traffic_checked_at": 0,
            "status_checked_at": 0,
        }
        payload = api.server_summary(row)
        json.dumps(payload)  # 不能抛 ValueError: Out of range float values
        self.assertEqual(payload["quota_gb"], 0)
        self.assertEqual(payload["threshold_gb"], 0)
        self.assertIsNotNone(payload["traffic_percent"])


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
