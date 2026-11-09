#!/usr/bin/env python3
"""读取异常的时间线 + 换班超时锚点回归测试（dry-run 内存桩，不碰真实阿里云）。

锁死四件事：

1. **单次读失败不再定性成错误**。第一次只建一条「进行中」事件，详情写清
   失败时刻、重试间隔（默认 60 秒）与「恢复后自动更新」；时间线首条记
   「第 1 次失败」而不是照抄概览，展开后不与概览重复。后续失败只刷新
   概览里的最近时刻与次数，不每分钟新开一条。
2. **恢复要写恢复时刻**。巡检重新读通后，同一条事件补上「已恢复」并把标题
   改成「…读取已恢复」、状态收尾为 done、级别降回 info（否则会留下红色错误
   图标配绿色完成徽章），而不是留一条永远「已完成」的失败。
3. **连续失败到阈值才升格**。默认 3 次（≈3 分钟）后事件级别变 error，避免
   网络抖一下就红一片吓人；升格那一次必须在时间线里留痕。
4. **已结算班次被打回 switching 时重置超时锚点**。否则沿用建行时（可能是
   几天前）的 started_at，一算几千分钟，必报「换班超时」，而机器其实都正常。

跑法：python scripts/test_read_error_timeline.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-read-error-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, notify, repo, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))

_RAW_NET = (
    "HTTPConnectionPool(host='cdt.aliyuncs.com', port=80): Max retries exceeded with "
    "url: /?Version=2021-08-13&Action=ListCdtInternetTraffic&AccessKeyId=LTAI5tDndQZB2LTXh2KQMuu"
    "&Signature=8FnJoqAWGDuiWVdDC1EI.OWU%3D (Caused by NewConnectionError("
    "'<aliyunsdkcore.vendored.requests.packages.urllib3.connection.HTTPConnection object "
    "at 0x714c86631230>': Failed to establish a new connection: "
    "[Errno 101] Network is unreachable'))"
)


def _make_server(label: str, status: str = "Stopped") -> int:
    server_id = repo.upsert_server(
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
    repo.update_server_runtime(
        server_id,
        status=status,
        stopped_mode="StopCharging" if status == "Stopped" else "",
        public_ip="47.0.0.1" if status == "Running" else "",
    )
    return server_id


def _reset() -> None:
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


class BriefError(unittest.TestCase):
    """SDK 原始异常 → 一句人话，且不泄露凭据。"""

    def test_network_error_is_short_and_redacted(self) -> None:
        out = aliyun.brief_error(_RAW_NET)
        self.assertEqual(out, "网络不可达（cdt.aliyuncs.com）")
        self.assertNotIn("LTAI", out)
        self.assertNotIn("Signature", out)
        self.assertLess(len(out), 40)

    def test_generic_error_redacts_secret_params(self) -> None:
        out = aliyun.brief_error(Exception("boom Signature=abc123 AccessKeyId=LTAIXYZ tail"))
        self.assertNotIn("abc123", out)
        self.assertNotIn("LTAIXYZ", out)
        self.assertLessEqual(len(out), 140)


class ReadErrorTimeline(unittest.TestCase):
    """失败 → 重试 → 恢复 的完整时间线。"""

    def setUp(self) -> None:
        _reset()
        repo.set_settings({"notify_on_error": "1"})

    def _event(self, server_id: int) -> dict:
        event = repo.pending_event(server_id, "read_error")
        self.assertIsNotNone(event)
        return event

    def test_first_failure_is_running_not_error(self) -> None:
        sid = _make_server("Ali-CDT-02")
        shift._read_error(  # noqa: SLF001
            server_id=sid,
            title="状态读取异常（Ali-CDT-02）",
            exc="CDT 流量查询失败：网络不可达（cdt.aliyuncs.com）",
        )
        event = self._event(sid)
        self.assertEqual(event["status"], "running", "单次失败不能定性成已完成/错误")
        self.assertEqual(event["level"], "warning")
        # 有失败时刻、重试间隔、恢复承诺
        self.assertRegex(event["detail"], r"^\d{2}:\d{2}:\d{2} 读取失败")
        self.assertIn("60 秒重试", event["detail"])
        self.assertIn("恢复后自动更新", event["detail"])
        # 时间线首条是「第 1 次失败」，与概览（带重试节奏）不同，不重复
        steps = repo.get_event(event["id"])["progress"]
        self.assertEqual(len(steps), 1, steps)
        self.assertIn("第 1 次失败", steps[0]["text"])
        self.assertNotEqual(steps[0]["text"], event["detail"])

    def test_escalation_milestone_is_recorded(self) -> None:
        """第 3 次既是升格点、又不在常规里程碑里，必须照样留痕。"""
        sid = _make_server("Ali-CDT-02")
        for _ in range(3):
            shift._read_error(  # noqa: SLF001
                server_id=sid,
                title="状态读取异常（Ali-CDT-02）",
                exc="CDT 流量查询失败：网络不可达（cdt.aliyuncs.com）",
            )
        texts = [step["text"] for step in repo.get_event(self._event(sid)["id"])["progress"]]
        self.assertTrue(
            any("第 3 次失败" in text for text in texts),
            f"升格那一次要进时间线：{texts}",
        )

    def test_repeat_failure_updates_count_and_escalates(self) -> None:
        sid = _make_server("Ali-CDT-02")
        for _ in range(3):
            shift._read_error(  # noqa: SLF001
                server_id=sid,
                title="状态读取异常（Ali-CDT-02）",
                exc="CDT 流量查询失败：网络不可达（cdt.aliyuncs.com）",
            )
        event = self._event(sid)
        self.assertEqual(repo.flag_value(f"read_err:{sid}"), 3)
        self.assertIn("连续 3 次", event["detail"])
        # 连续到阈值才升格
        self.assertEqual(event["level"], "error")

    def test_recovery_records_time_and_closes_event(self) -> None:
        sid = _make_server("Ali-CDT-02")
        # 连失败 3 次先升到 error，才能验证恢复时级别有没有降回来
        for _ in range(3):
            shift._read_error(  # noqa: SLF001
                server_id=sid,
                title="状态读取异常（Ali-CDT-02）",
                exc="CDT 流量查询失败：网络不可达（cdt.aliyuncs.com）",
            )
        eid = self._event(sid)["id"]
        self.assertEqual(repo.get_event(eid)["level"], "error")
        shift._read_recovered(  # noqa: SLF001
            server_id=sid,
            title="状态读取已恢复（Ali-CDT-02）",
        )
        event = repo.get_event(eid) or {}
        self.assertEqual(event["status"], "done")
        self.assertTrue(event["title"].startswith("状态读取已恢复"), event["title"])
        self.assertRegex(event["detail"], r"^\d{2}:\d{2}:\d{2} 恢复正常")
        self.assertIn("连续失败 3 次", event["detail"])
        # 收尾成「完成」就不能再挂红色错误图标
        self.assertEqual(event["level"], "info", "恢复后级别要降回 info")
        texts = [step["text"] for step in event["progress"]]
        self.assertTrue(any("已恢复" in text for text in texts), texts)
        # flag 清掉，下一轮不会再误发
        self.assertFalse(repo.get_flag(f"read_err:{sid}"))

    def test_recovery_without_failure_is_silent(self) -> None:
        sid = _make_server("Ali-CDT-02")
        shift._read_recovered(  # noqa: SLF001
            server_id=sid,
            title="状态读取已恢复（Ali-CDT-02）",
        )
        self.assertIsNone(repo.pending_event(sid, "read_error"))
        self.assertEqual(repo.list_events(group_id=0), [])

    def test_retry_interval_follows_user_setting(self) -> None:
        """重试节奏要写用户实际配的巡检间隔，不是环境默认的 60。"""
        repo.set_settings({"reconcile_interval": "300"})
        try:
            sid = _make_server("Ali-CDT-02")
            shift._read_error(  # noqa: SLF001
                server_id=sid,
                title="状态读取异常（Ali-CDT-02）",
                exc="CDT 流量查询失败：连接超时",
            )
            event = self._event(sid)
            self.assertIn("每 300 秒重试", event["detail"])
            self.assertNotIn("每 60 秒", event["detail"])
        finally:
            repo.set_settings({"reconcile_interval": "60"})

    def test_orphan_close_clears_counter(self) -> None:
        """移出监控时事件和计数一起收：否则重新纳管会从旧值续数。"""
        sid = _make_server("Ali-CDT-02")
        for _ in range(2):
            shift._read_error(  # noqa: SLF001
                server_id=sid,
                title="状态读取异常（Ali-CDT-02）",
                exc="CDT 流量查询失败：连接超时",
            )
        self.assertEqual(repo.flag_value(f"read_err:{sid}"), 2)

        shift._close_orphan_read_errors(set())  # 已不在任何监控里  # noqa: SLF001

        self.assertIsNone(repo.pending_event(sid, "read_error"))
        self.assertFalse(repo.get_flag(f"read_err:{sid}"), "孤儿收尾要把计数一起清掉")

        # 重新纳入监控后是全新的一次中断：从第 1 次重新数，而不是接着 3
        shift._read_error(  # noqa: SLF001
            server_id=sid,
            title="状态读取异常（Ali-CDT-02）",
            exc="CDT 流量查询失败：连接超时",
        )
        self.assertEqual(repo.flag_value(f"read_err:{sid}"), 1)
        self.assertEqual(self._event(sid)["level"], "warning", "不能一上来就变红")


class _ShiftBase(unittest.TestCase):
    def setUp(self) -> None:
        _reset()
        repo.set_settings(
            {
                "ddns_enabled": "0",
                "notify_on_shift": "0",
                "notify_on_failover": "0",
                "notify_on_error": "1",
            }
        )
        self.sent: list = []
        self._orig_send = notify.send_all
        notify.send_all = lambda config, message: (  # noqa: ANN001, ARG005
            self.sent.append(message) or {}
        )
        self._orig_refresh = shift._refresh  # noqa: SLF001
        self._orig_start = aliyun.start_instance
        self._orig_stop = aliyun.stop_instance
        self.status: dict[int, str] = {}
        self.unreadable: set[int] = set()

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        shift._refresh = self._orig_refresh  # noqa: SLF001
        aliyun.start_instance = self._orig_start
        aliyun.stop_instance = self._orig_stop

    def _wire(self) -> None:
        def _fake_refresh(server: dict) -> None:  # noqa: ANN001
            sid = int(server["id"])
            if sid in self.unreadable:
                raise aliyun.AliyunError("CDT 流量查询失败：网络不可达（cdt.aliyuncs.com）")
            state = self.status[sid]
            server["instance_status"] = state
            server["stopped_mode"] = "StopCharging" if state == "Stopped" else ""
            server["public_ip"] = "47.0.0.1" if state == "Running" else ""

        def _fake_start(server: dict) -> None:  # noqa: ANN001
            self.status[int(server["id"])] = "Running"

        def _fake_stop(server: dict, mode: str = "StopCharging", fallback: bool = False) -> str:  # noqa: ANN001, ARG001
            self.status[int(server["id"])] = "Stopped"
            return "StopCharging"

        shift._refresh = _fake_refresh  # noqa: SLF001
        aliyun.start_instance = _fake_start
        aliyun.stop_instance = _fake_stop

    def _group(self, members: list[dict]) -> int:
        group_id = repo.upsert_group(
            {
                "name": "换班组",
                "enabled": True,
                "failover_enabled": True,
                "members": members,
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), group_id),
        )
        return group_id

    def _reconcile(self, group_id: int) -> dict:
        return shift.engine.reconcile_group(
            repo.get_group(group_id) or {},
            repo.get_settings(),
            dt.datetime.now(TZ),
            float(time.time()),
        )

    def _settle(self, group_id: int) -> None:
        for _ in range(4):
            self._reconcile(group_id)

    def _titles(self, group_id: int) -> list[str]:
        return [e["title"] for e in repo.list_events(limit=100, group_id=group_id)]


class SettledShiftNoFalseTimeout(_ShiftBase):
    """已结算班次因待机读失败被打回 switching：不许沿用几天前的锚点报超时。"""

    def test_settled_shift_does_not_report_false_timeout(self) -> None:
        a = _make_server("当班A", "Running")
        b = _make_server("待机B", "Running")
        self.status = {a: "Running", b: "Running"}
        self._wire()
        group_id = self._group([{"server_id": a, "hours": 12}, {"server_id": b, "hours": 12}])

        self._settle(group_id)
        runs = [r for r in repo.recent_runs(limit=10) if int(r["group_id"]) == group_id]
        self.assertTrue(runs)
        self.assertIn(runs[0]["phase"], ("settled", "failover"), runs[0]["phase"])

        # 模拟 11/1 建的班次：锚点手动拨到 4 天前
        old = int(time.time()) - 4 * 24 * 3600
        db.execute(
            "UPDATE shift_runs SET started_at=? WHERE group_id=?",
            (old, group_id),
        )

        # 11/4 待机读不到 → 被打回 switching
        self.unreadable = {b}
        self._reconcile(group_id)

        titles = self._titles(group_id)
        self.assertFalse(
            any("换班超时" in title for title in titles),
            f"不该因单次读失败报换班超时：{titles}",
        )
        self.assertEqual(self.sent, [], "不该发换班超时通知")
        # 锚点已重置到「这次掉回切换中」的时刻
        runs = [r for r in repo.recent_runs(limit=5) if int(r["group_id"]) == group_id]
        self.assertGreater(int(runs[0]["started_at"]), old + 3600)


class UnsettledTimeoutStillWorks(_ShiftBase):
    """没结算过的班次：锚点老旧时，超时仍要正常报（别把正常逻辑改坏）。"""

    def test_unsettled_shift_still_reports_timeout(self) -> None:
        a = _make_server("当班A", "Running")
        b = _make_server("待机B", "Running")
        self.status = {a: "Running", b: "Running"}
        self._wire()
        group_id = self._group([{"server_id": a, "hours": 12}, {"server_id": b, "hours": 12}])

        self._settle(group_id)
        # 人为把它退回「未结算」，并把锚点拨旧
        old = int(time.time()) - 4 * 24 * 3600
        db.execute(
            "UPDATE shift_runs SET phase='switching', started_at=? WHERE group_id=?",
            (old, group_id),
        )

        self.unreadable = {b}
        self._reconcile(group_id)

        titles = self._titles(group_id)
        self.assertTrue(
            any("换班超时" in title for title in titles),
            f"未结算班次的超时应该照常报：{titles}",
        )


class IdleReadFailure(unittest.TestCase):
    """闲散机器读不到状态也要留痕。

    以前这里是静默 continue：卡片一直挂着上一轮的旧状态和旧 IP，用户以为机器
    在跑。保活和换班组都有读取事件，闲散机不该是盲区。
    """

    def setUp(self) -> None:
        _reset()
        self.sid = _make_server("闲散-01", status="Running")
        self._real_traffic = aliyun.get_traffic_gb
        self._real_instance = aliyun.get_instance

    def tearDown(self) -> None:
        aliyun.get_traffic_gb = self._real_traffic
        aliyun.get_instance = self._real_instance

    def _reconcile(self) -> list[dict]:
        config = repo.get_settings()
        return shift.engine.reconcile_idle_limits(config, dt.datetime.now(TZ), set(), set())

    def _break_traffic(self) -> None:
        def boom(server):  # noqa: ANN001, ARG001
            raise aliyun.AliyunError("CDT 流量查询失败：网络不可达（cdt.aliyuncs.com）")

        aliyun.get_traffic_gb = boom

    def test_failure_opens_event_and_recovery_closes_it(self) -> None:
        self._break_traffic()
        self._reconcile()

        event = repo.pending_event(self.sid, "read_error")
        self.assertIsNotNone(event, "闲散机读失败也应该有事件")
        self.assertIn("读取失败", event["detail"])
        self.assertIn("网络不可达", event["detail"])

        aliyun.get_traffic_gb = self._real_traffic
        self._reconcile()

        self.assertIsNone(repo.pending_event(self.sid, "read_error"))
        latest = repo.list_events(limit=1)[0]
        self.assertEqual(latest["status"], "done")
        self.assertTrue(latest["title"].startswith("状态读取已恢复"), latest["title"])
        self.assertFalse(repo.get_flag(f"read_err:{self.sid}"))

    def test_recovery_without_failure_is_silent(self) -> None:
        self._reconcile()
        self.assertEqual(repo.list_events(group_id=0), [])

    def test_event_survives_orphan_sweep(self) -> None:
        """整轮对账末尾的孤儿收尾不能把闲散机的读取事件关掉。

        孤儿判据必须是「还在库里」。只认换班组+保活的话，闲散机器刚建好的
        读取事件会在同一轮末尾被当成孤儿关掉，然后下一轮再建一条 —— 每分钟
        开关一次，用户只看到一片闪烁的历史记录。
        """
        self._break_traffic()
        shift.engine.reconcile_all()
        self.assertIsNotNone(
            repo.pending_event(self.sid, "read_error"),
            "闲散机的读取事件被孤儿收尾误关了",
        )

    def test_event_closed_after_server_deleted(self) -> None:
        self._break_traffic()
        shift.engine.reconcile_all()
        repo.delete_server(self.sid)
        shift.engine.reconcile_all()

        self.assertIsNone(repo.pending_event(self.sid, "read_error"))
        latest = repo.list_events(limit=1)[0]
        self.assertEqual(latest["status"], "done")
        self.assertFalse(repo.get_flag(f"read_err:{self.sid}"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
