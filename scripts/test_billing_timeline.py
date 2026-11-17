#!/usr/bin/env python3
"""费用查询失败 → 重试 → 恢复的事件时间线回归测试（dry-run，不碰真实阿里云）。

锁死四件事：

1. **一次连续故障只有一条事件**。首次失败建一条「进行中」事件，后续失败只
   覆盖概览里的时刻与次数，不再每次新开一条把时间线刷满（以前是「失败」
   「已恢复」两条互不相识，看不出是同一次故障）。
2. **只在里程碑留痕**。第 2/5/10/30/60 次才追加一行进度，中间那些轮次不写。
3. **提示只出现一次**。「缺 BSS 权限时可在设置里关闭费用查询」只在首次失败
   的概览里出现，后续概览与过程条目都不复述。
4. **恢复在同一条事件里收尾**。标题改「费用查询已恢复（别名）」、详情写
   「此前连续失败 N 次，持续约 M 分钟」、级别降回 info、状态 done —— 不能
   留下「红色错误图标 + 绿色完成徽章」，也不能凭空多一条没有失败前提的恢复。

跑法：python scripts/test_billing_timeline.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-billing-timeline-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"

from app import billing, db, repo  # noqa: E402

NET_ERR = "实例账单查询失败：网络不可达（business.aliyuncs.com）"
# 剥掉抬头后留在事件里的原因
_BRIEF = "网络不可达（business.aliyuncs.com）"
TTL = 600


def _make_server(label: str = "Aliyun-CDT") -> int:
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


def _reset() -> None:
    db.init_db()
    for table in ("events", "flags", "servers", "traffic_samples"):
        db.execute(f"DELETE FROM {table}")


def _events() -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM events ORDER BY id")]


def _progress(event: dict) -> list[str]:
    return [step["text"] for step in repo.get_event(int(event["id"]))["progress"]]


def _age_flag(key: str, minutes: int) -> None:
    """把标记的起始时刻往前挪，模拟「已经连续失败 M 分钟」。"""
    db.execute(
        "UPDATE flags SET created_at=? WHERE key=?",
        (int(time.time()) - minutes * 60, key),
    )


class BillingTimeline(unittest.TestCase):
    def setUp(self) -> None:
        _reset()

    def test_first_failure_creates_single_running_event(self) -> None:
        sid = _make_server()
        billing._billing_error(repo.get_server(sid), NET_ERR, TTL)

        events = _events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["title"], "费用查询异常（Aliyun-CDT）")
        self.assertEqual(event["status"], "running")
        self.assertEqual(event["level"], "warning")
        self.assertEqual(event["kind"], "warning", "与状态巡检同一套图标")
        self.assertEqual(event["pending_kind"], "billing_error")
        self.assertEqual(event["server_id"], sid)
        # 概览：失败时刻 + 原因 + 重试节奏 + 恢复后自动更新
        # 抬头只留一份：_brief_error 剥掉「实例账单查询失败：」，模板自带「查询失败：」
        self.assertIn("查询失败：网络不可达（business.aliyuncs.com）", event["detail"])
        self.assertNotIn("实例账单查询失败", event["detail"])
        self.assertIn("每 10 分钟重试", event["detail"])
        self.assertIn("恢复后自动更新", event["detail"])
        # 时间线首条是「第 1 次失败」，不与概览重复；条目不写时刻（左侧列已有）
        steps = _progress(event)
        self.assertEqual(len(steps), 2, steps)  # 第 1 次失败 + 权限提示
        self.assertEqual(steps[0], f"第 1 次失败：{_BRIEF}")
        self.assertNotRegex(steps[0], r"^\d{2}:\d{2}:\d{2}")

    def test_repeated_failures_stay_in_one_event(self) -> None:
        sid = _make_server()
        server = repo.get_server(sid)
        for _ in range(3):
            billing._billing_error(server, NET_ERR, TTL)

        events = _events()
        self.assertEqual(len(events), 1, "连续失败不该每次新开一条事件")
        event = events[0]
        self.assertEqual(event["status"], "running")
        self.assertIn("仍失败（连续 3 次）", event["detail"])
        # 第 2 次是里程碑、第 3 次不是：时间线只多一行（另有首条的权限提示）
        steps = _progress(event)
        self.assertEqual(len(steps), 3, steps)
        self.assertTrue(any(s.startswith("第 2 次失败") for s in steps))
        self.assertFalse(any("第 3 次失败" in s for s in steps))

    def test_milestones_only(self) -> None:
        sid = _make_server()
        server = repo.get_server(sid)
        for _ in range(6):
            billing._billing_error(server, NET_ERR, TTL)
        steps = _progress(_events()[0])
        # 1 / 2 / 5 三次留痕，3、4、6 不写；外加首条的权限提示
        self.assertEqual(len(steps), 4, steps)
        self.assertTrue(any(s.startswith("第 5 次失败") for s in steps))
        self.assertFalse(any("第 6 次失败" in s for s in steps))

    def test_both_errors_get_briefed(self) -> None:
        """账单 + 余额两段错误各自剥掉抬头，代码段原样用「；」串起来。"""
        sid = _make_server()
        billing._billing_error(
            repo.get_server(sid),
            "实例账单查询失败：网络不可达（business.aliyuncs.com）；"
            "账户余额查询失败：鉴权失败（AccessKey 无效或无权限）",
            TTL,
        )
        detail = _events()[0]["detail"]
        self.assertIn(
            "查询失败：网络不可达（business.aliyuncs.com）；鉴权失败（AccessKey 无效或无权限）",
            detail,
        )
        self.assertNotIn("实例账单查询失败", detail)
        self.assertNotIn("账户余额查询失败", detail)

    def test_tip_printed_once(self) -> None:
        """权限提示挂在过程里、整条事件只一次；概览每轮被覆盖，不放那儿。"""
        sid = _make_server()
        server = repo.get_server(sid)
        billing._billing_error(server, NET_ERR, TTL)
        self.assertNotIn("缺 BSS 权限", _events()[0]["detail"], "概览不放长提示")
        self.assertEqual(
            [s for s in _progress(_events()[0]) if "缺 BSS 权限" in s],
            ["提示：缺 BSS 权限时可在设置里关闭费用查询"],
        )

        billing._billing_error(server, NET_ERR, TTL)
        billing._billing_error(server, NET_ERR, TTL)
        event = _events()[0]
        self.assertNotIn("缺 BSS 权限", event["detail"])
        self.assertEqual(
            len([s for s in _progress(event) if "缺 BSS 权限" in s]),
            1,
            "后续失败不该再复述提示",
        )

    def test_recovery_finishes_same_event(self) -> None:
        sid = _make_server()
        server = repo.get_server(sid)
        billing._billing_error(server, NET_ERR, TTL)
        billing._billing_error(server, NET_ERR, TTL)
        billing._billing_error(server, NET_ERR, TTL)
        _age_flag(f"billing_err:{sid}", 22)

        billing._billing_recovered(server)

        events = _events()
        self.assertEqual(len(events), 1, "恢复不该另开一条")
        event = events[0]
        self.assertEqual(event["title"], "费用查询已恢复（Aliyun-CDT）")
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["level"], "info", "收尾必须降回 info，否则红图标配绿徽章")
        self.assertIn("已恢复", event["detail"])
        self.assertIn("此前连续失败 3 次", event["detail"])
        self.assertIn("持续约 22 分钟", event["detail"])
        self.assertTrue(_progress(event)[-1].endswith("已恢复"))
        # 计数随事件一起清掉，下一轮失败从 1 重新数
        self.assertEqual(repo.flag_value(f"billing_err:{sid}"), 0)

    def test_recovery_without_failure_records_nothing(self) -> None:
        sid = _make_server()
        billing._billing_recovered(repo.get_server(sid))
        self.assertEqual(_events(), [], "没有失败的「已恢复」是噪音")

    def test_failure_after_recovery_opens_new_event(self) -> None:
        sid = _make_server()
        server = repo.get_server(sid)
        billing._billing_error(server, NET_ERR, TTL)
        billing._billing_recovered(server)
        billing._billing_error(server, NET_ERR, TTL)

        events = _events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["status"], "done")
        self.assertEqual(events[1]["status"], "running")
        self.assertIn("第 1 次失败", _progress(events[1])[0], "新一轮应从 1 开始数")

    def test_transition_keeps_updating_on_repeat(self) -> None:
        """真实入口 `_mark_transition`：连续失败必须一路续写，不能停在第一轮。

        以前这里拿「上一轮有没有错误」做短路，第二轮起 was_error == now_error
        就直接 return，概览与计数都冻住了 —— 恢复时会写出「连续失败 1 次」。
        """
        sid = _make_server()
        for _ in range(3):
            # 模拟 refresh_server：先读旧快照，落库新错误，再动事件
            snapshot = repo.get_server(sid)
            repo.update_server_billing(sid, error=NET_ERR)
            billing._mark_transition(snapshot, NET_ERR, TTL)

        events = _events()
        self.assertEqual(len(events), 1, "连续失败不该每次新开一条事件")
        self.assertIn("仍失败（连续 3 次）", events[0]["detail"])
        self.assertEqual(repo.flag_value(f"billing_err:{sid}"), 3)

    def test_transition_recovery_closes_same_event(self) -> None:
        sid = _make_server()
        repo.update_server_billing(sid, error=NET_ERR)
        billing._mark_transition(repo.get_server(sid), NET_ERR, TTL)
        repo.update_server_billing(sid, error=NET_ERR)
        billing._mark_transition(repo.get_server(sid), NET_ERR, TTL)
        _age_flag(f"billing_err:{sid}", 18)

        # 恢复：旧快照里还带着错误，新的一轮没有错误
        snapshot = repo.get_server(sid)
        repo.update_server_billing(sid, monthly_cost=1.5, error="")
        billing._mark_transition(snapshot, "", TTL)

        events = _events()
        self.assertEqual(len(events), 1, "恢复要收在同一条里")
        self.assertEqual(events[0]["title"], "费用查询已恢复（Aliyun-CDT）")
        self.assertEqual(events[0]["status"], "done")
        self.assertIn("此前连续失败 2 次", events[0]["detail"])
        self.assertIn("持续约 18 分钟", events[0]["detail"])

    def test_success_without_failure_stays_quiet(self) -> None:
        sid = _make_server()
        for _ in range(3):
            snapshot = repo.get_server(sid)
            repo.update_server_billing(sid, monthly_cost=1.5, error="")
            billing._mark_transition(snapshot, "", TTL)
        self.assertEqual(_events(), [], "一直正常时每轮都不该产生事件")

    def test_stale_flag_resets_count(self) -> None:
        """事件被清理、flag 还留着时，新一轮仍从「第 1 次失败」开始数。"""
        sid = _make_server()
        repo.bump_flag(f"billing_err:{sid}")
        repo.bump_flag(f"billing_err:{sid}")

        billing._billing_error(repo.get_server(sid), NET_ERR, TTL)
        self.assertEqual(repo.flag_value(f"billing_err:{sid}"), 1)
        event = _events()[0]
        self.assertIn("第 1 次失败", _progress(event)[0])
        self.assertNotIn("仍失败", event["detail"])

    def test_orphan_closed(self) -> None:
        sid = _make_server()
        billing._billing_error(repo.get_server(sid), NET_ERR, TTL)
        billing.close_orphan_billing_errors(set())  # 机器已不在监控里

        event = _events()[0]
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["level"], "info")
        self.assertIn("自动收尾", event["detail"])
        self.assertEqual(repo.flag_value(f"billing_err:{sid}"), 0)

    def test_orphan_keeps_monitored(self) -> None:
        sid = _make_server()
        billing._billing_error(repo.get_server(sid), NET_ERR, TTL)
        billing.close_orphan_billing_errors({sid})
        self.assertEqual(_events()[0]["status"], "running", "还在监控里的不该被收掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
