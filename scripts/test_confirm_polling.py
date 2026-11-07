#!/usr/bin/env python3
"""事件确认器：状态查询失败要脱敏 + 去重，保活收尾要带上总尝试次数。

确认器 5 秒一轮，比巡检密得多。它的文案一旦不脱敏、不去重：
  - 原始 SDK 异常里的 AccessKeyId / Signature 会直接写进事件（还可能进通知）；
  - 异常文本带对象地址，每次都不一样，躲过 add_event_progress 的相邻去重，
    一条跨天的保活事件能被刷到条数上限，把「已下发启动指令」这类开头步骤挤掉。

保活收尾那条也一样：概览会被「运行中」整条覆盖，用户想看「这次被抢之后一共
试了多少次才起来」就没地方看了。

跑法：python scripts/test_confirm_polling.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-confirm-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, repo, scheduler  # noqa: E402

# 和真实日志同构：整条签名 URL + 对象地址 + 错误号
_RAW = (
    "SDK.HttpError HTTPConnectionPool(host='cdt.aliyuncs.com', port=80): Max retries "
    "exceeded with url: /?Version=2021-08-13&Action=ListCdtInternetTraffic"
    "&AccessKeyId=LTAI5tDndQZB2LTXh2KQMuu&Signature=8FnJoqAWGDuiWVdDC1EI.OWU%3D "
    "(Caused by NewConnectionError('<aliyunsdkcore.vendored.requests.packages.urllib3."
    "connection.HTTPConnection object at 0x714c86631230>': Failed to establish a new "
    "connection: [Errno 101] Network is unreachable'))"
)


class ConfirmPolling(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        for table in (
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
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings(
            {
                "ddns_enabled": "0",
                "notify_on_keepalive": "0",
                "notify_on_error": "0",
            }
        )
        self._orig_get = aliyun.get_instance
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
            }
        )
        repo.add_keepalive(self.server_id, enabled=True)

    def tearDown(self) -> None:
        aliyun.get_instance = self._orig_get

    def _pending(self, tries: int) -> int:
        """造一条「保活进行中、等 Running」的事件，并把尝试次数放到 tries。"""
        event_id = repo.add_event(
            "start",
            "保活机：保活拉起",
            "已尝试拉起 1 次，仍未运行（已停机）",
            level="warning",
            server_id=self.server_id,
            status="running",
            pending_status="Running",
            pending_kind="keepalive",
        )
        db.execute(
            "INSERT INTO flags(key, created_at, value) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"keepalive_tries:{self.server_id}", int(time.time()), tries),
        )
        return event_id

    def test_query_failure_is_redacted_and_deduped(self) -> None:
        """连续两轮查询失败：只留一条，且不含凭据。"""

        def boom(server: dict) -> None:  # noqa: ANN001, ARG001
            raise aliyun.AliyunError(_RAW)

        aliyun.get_instance = boom
        event_id = self._pending(2)

        scheduler._confirm_pending_events()  # noqa: SLF001
        scheduler._confirm_pending_events()  # noqa: SLF001

        texts = [str(step["text"]) for step in repo.get_event(event_id)["progress"]]
        fails = [text for text in texts if "状态查询失败" in text]
        self.assertEqual(len(fails), 1, texts)
        self.assertIn("网络不可达（cdt.aliyuncs.com）", fails[0])
        self.assertNotIn("LTAI", fails[0])
        self.assertNotIn("Signature", fails[0])
        self.assertNotIn("0x714c", fails[0])

    def test_recovery_summary_carries_total_tries(self) -> None:
        """到位收尾：概览里补「共尝试 N 次」，别让次数随计数清空一起消失。"""
        aliyun.get_instance = lambda server: aliyun.InstanceInfo(  # noqa: ARG005
            "Running", "", "47.0.0.1", "cn-hongkong"
        )
        event_id = self._pending(4)

        scheduler._confirm_pending_events()  # noqa: SLF001

        event = repo.get_event(event_id)
        self.assertEqual(event["status"], "done")
        self.assertIn("共尝试 4 次", event["detail"])
        self.assertFalse(repo.get_flag(f"keepalive_tries:{self.server_id}"))

    def test_single_try_has_no_redundant_total(self) -> None:
        """第一次就起来：不必写「共尝试 1 次」，那是废话。"""
        aliyun.get_instance = lambda server: aliyun.InstanceInfo(  # noqa: ARG005
            "Running", "", "47.0.0.1", "cn-hongkong"
        )
        event_id = self._pending(1)

        scheduler._confirm_pending_events()  # noqa: SLF001

        self.assertNotIn("共尝试", repo.get_event(event_id)["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
