#!/usr/bin/env python3
"""手动换班一致性回归：引擎、界面、域名、通知必须认同一台机器。

踩过的坑（v1.1.10 用户实测）：
  1. 手动换完班，引擎在跑 B，界面「当前当班」还高亮 A（而且是已停机的那台）；
  2. 服务器卡片上的域名框还挂在 A 上，可 Cloudflare 里解析指的是 B；
  3. 同一个班次里连点三次「立即换班」，只收到一条通知 —— 去重键用了班次，
     同一个 slot 里手动切多少次都算「同一件事」。

根因是同一个：「此刻谁当班」这个判定散在四处，只有引擎那处认 run.manual。
现在统一走 shift.actual_duty()，这个文件就是钉住这件事。

跑法：python scripts/test_manual_consistency.py
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

# 必须在 import app.* 之前设好：临时数据目录 + dry-run + 同步到位
_TMP = tempfile.mkdtemp(prefix="cdt-manual-consistency-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, api, db, dns, notify, repo, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
RECORD = "shift.example.com"


def _now_dt() -> dt.datetime:
    return dt.datetime.now(TZ)


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


class ManualConsistency(unittest.TestCase):
    """固定时刻表模式（用户实际在用的 12 小时班）。"""

    def setUp(self) -> None:
        db.init_db()
        for table in (
            "events",
            "flags",
            "shift_runs",
            "shift_members",
            "shift_groups",
            "dns_records",
            "servers",
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings(
            {
                "timezone": "Asia/Shanghai",
                "notify_on_shift": "1",
                "notify_on_failover": "1",
                "ddns_enabled": "1",
            }
        )
        self.a = _make_server("手换A", "i-cons-aaa")
        self.b = _make_server("手换B", "i-cons-bbb")
        self.group_id = repo.upsert_group(
            {
                "name": "一致性组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12, "start_time": "00:00", "end_time": "12:00"},
                    {"server_id": self.b, "hours": 12, "start_time": "12:00", "end_time": "00:00"},
                ],
            }
        )
        repo.ensure_dns_record(RECORD)
        dns.bind(RECORD, dns.TARGET_GROUP, self.group_id)

        self.sent: list[str] = []
        self._orig_send = notify.send_all
        notify.send_all = self._fake_send

    def tearDown(self) -> None:
        notify.send_all = self._orig_send

    def _fake_send(self, config, message):  # noqa: ANN001, ARG002
        self.sent.append(message.title)
        return {"webhook": "ok"}

    # -- 工具 -----------------------------------------------------------
    def _reconcile(self) -> dict:
        return shift.engine.reconcile_group(
            repo.get_group(self.group_id) or {},
            repo.get_settings(),
            _now_dt(),
            float(time.time()),
        )

    def _settle(self, rounds: int = 8) -> None:
        for _ in range(rounds):
            if self._reconcile().get("ready"):
                return

    def _run(self) -> dict:
        group = repo.get_group(self.group_id) or {}
        picked = shift.duty_at(group, time.time())
        return repo.get_run(self.group_id, str(picked[1])) or {}

    def _duty_id(self) -> int:
        return int(self._run().get("duty_server_id") or 0)

    def _manual(self) -> None:
        shift.engine.manual_switch(self.group_id)
        self._settle()

    # -- 1. 界面必须跟引擎认同一台 --------------------------------------
    def test_dashboard_follows_manual_switch(self):
        self._settle()
        clock_duty = int((shift.duty_at(repo.get_group(self.group_id) or {}, time.time()))[0]["server_id"])
        self._manual()
        actual = self._duty_id()
        self.assertNotEqual(actual, clock_duty, "手动换班应该切到另一台，测试前提不成立")

        payload = api._group_payload(  # noqa: SLF001
            repo.get_group(self.group_id) or {}, repo.get_settings(), float(time.time())
        )
        self.assertEqual(
            payload["duty_server_id"],
            actual,
            "界面「当前当班」必须跟着手动指定走，不能还按时钟算",
        )
        duty_members = [m["server_id"] for m in payload["members"] if m["is_duty"]]
        self.assertEqual(duty_members, [actual])
        # 徽章不能永远停在「手动换班」，切完就是已完成
        self.assertEqual(payload["phase"], shift.PHASE_SETTLED, payload["phase_label"])

    # -- 2. 域名框 / 解析归属必须跟引擎一致 ------------------------------
    def test_dns_follows_manual_switch(self):
        self._settle()
        self._manual()
        actual = self._duty_id()

        cards = dns.books_for_cards(float(time.time()))
        self.assertEqual(cards.get(actual), [RECORD], f"域名框应该挂在当班 {actual} 上")
        for server_id, names in cards.items():
            if server_id != actual:
                self.assertNotIn(RECORD, names, f"非当班 {server_id} 不该显示这个域名")

        runner = dns._group_runner(  # noqa: SLF001
            self.group_id, float(time.time()), {s["id"]: s for s in repo.list_servers()}
        )
        self.assertIsNotNone(runner)
        self.assertEqual(int(runner["id"]), actual)

    # -- 3. 同一班次里点几次就该收到几条报告 -----------------------------
    def test_every_manual_switch_sends_its_own_report(self):
        self._settle()
        self.sent.clear()
        for _ in range(3):
            self._manual()

        self.assertEqual(len(self.sent), 3, self.sent)
        for title in self.sent:
            self.assertEqual(title, "CDT 手动换班完成报告（换班组：一致性组）")

        events = repo.list_events(limit=50, group_id=self.group_id)
        manual = [e for e in events if e["kind"] == "manual"]
        self.assertEqual(len(manual), 3, [e["title"] for e in manual])
        for event in manual:
            self.assertEqual(event["status"], "done", event["title"])
            self.assertEqual(event["title"], "一致性组：手动换班完成")

    # -- 4. 上一轮没切完就再点：旧事件不能永远转圈 -----------------------
    def test_rapid_second_click_closes_previous_event(self):
        self._settle()
        shift.engine.manual_switch(self.group_id)  # 不等它走完
        shift.engine.manual_switch(self.group_id)  # 立刻再点一次
        self._settle()

        events = repo.list_events(limit=50, group_id=self.group_id)
        running = [e for e in events if e["status"] == "running"]
        self.assertEqual(running, [], [e["title"] for e in events])

        manual = [e for e in events if e["kind"] == "manual"]
        texts = [" | ".join(step["text"] for step in e["progress"]) for e in manual]
        self.assertTrue(any("已被新的手动换班取代" in t for t in texts), texts)
        # 被顶掉的那条明确写「已取消」，别留一条永远「进行中」的假进度；
        # 后点的那次正常走完，两条都必须是终态。
        titles = sorted(e["title"] for e in manual)
        self.assertEqual(
            titles, ["一致性组：手动换班完成", "一致性组：手动换班已取消"], titles
        )

    # -- 5. 手动换完，域名确实指向当班机 ---------------------------------
    def test_ddns_points_at_actual_duty(self):
        self._settle()
        self._manual()
        actual = self._duty_id()
        server = repo.get_server(actual) or {}
        row = repo.get_dns_record(RECORD) or {}
        self.assertEqual(row.get("target_kind"), dns.TARGET_GROUP)
        self.assertEqual(
            row.get("last_ip"),
            server.get("public_ip"),
            f"解析应该指向当班 {server.get('label')} 的 IP",
        )

    # -- 6. 手动开机不能绕开流量阈值 -------------------------------------
    def test_manual_start_blocked_when_over_threshold(self):
        """已到阈值的机器不许手动点着：点着了巡检下一轮才停，中间白烧的流量补不回来。"""
        server = repo.get_server(self.a, decrypt_secret=True)
        aliyun._mock_set(server, traffic="200.00", status="Stopped", stopped_mode="StopCharging")

        with self.assertRaises(ValueError) as ctx:
            shift.engine.manual_action(self.a, "start")
        self.assertIn("阈值", str(ctx.exception))
        self.assertFalse(
            [e for e in repo.list_events(limit=20) if e["kind"] == "manual"],
            "被拒绝时不该留下开机事件",
        )

    def test_manual_start_under_threshold_still_works(self):
        server = repo.get_server(self.a, decrypt_secret=True)
        aliyun._mock_set(server, traffic="12.00", status="Stopped", stopped_mode="StopCharging")

        result = shift.engine.manual_action(self.a, "start")

        self.assertEqual(result.get("status"), "pending")
        self.assertEqual(result.get("target_status"), "Running")


def tearDownModule() -> None:
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
