#!/usr/bin/env python3
"""换班组的建立与解散（dry-run 内存桩，不碰真实阿里云）。

锁死三条规则：
  1. 新建换班组保存后立刻按排班表校正组员状态：该当班的先开机，等它 Running
     再把其余成员节省停机，全部到位后才同步 DDNS —— 顺序和自动换班一致。
     整个过程只有一条「组名：新建换班组」事件，结算时标题也不改叫「换班完成」。
  2. 新建组那一班不发结算通知（用户刚点完保存，不需要再收一份报告）。
  3. 解散换班组不给实例下发任何启停指令：跑着的继续跑、停着的继续停着，
     回到闲散手动；组员的单机保活一并暂停，免得解散后被保活顺手拉起来。

另外校验「任何事件点开都要有过程」：一次性完成的即时事件也会把详情
拆成进度条目，不会只有一句摘要。

跑法：python scripts/test_group_lifecycle.py
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

# 必须在 import app.* 之前设好：数据目录指向临时目录，dry-run + 同步到位
_TMP = tempfile.mkdtemp(prefix="cdt-group-life-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, dns, notify, repo, shift  # noqa: E402


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


def _now_dt() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def _status(server_id: int) -> tuple[str, str]:
    """读一次真实状态（dry-run 桩）并落库，返回 (status, stopped_mode)。"""
    server = repo.get_server(server_id, decrypt_secret=True)
    shift._refresh(server)  # noqa: SLF001
    fresh = repo.get_server(server_id) or {}
    return str(fresh.get("instance_status") or ""), str(fresh.get("stopped_mode") or "")


class _Base(unittest.TestCase):
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
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings({"notify_on_shift": "1", "notify_on_failover": "1", "ddns_enabled": "0"})
        self.sent: list[str] = []
        self._orig_send = notify.send_all

        def _fake_send(config, message):  # noqa: ANN001, ARG002
            self.sent.append(message.title)
            return {"webhook": "ok"}

        notify.send_all = _fake_send

    def tearDown(self) -> None:
        notify.send_all = self._orig_send

    def _reconcile(self, group_id: int) -> None:
        group = repo.get_group(group_id) or {}
        shift.engine.reconcile_group(
            group, repo.get_settings(), _now_dt(), float(time.time())
        )

    def _events(self, group_id: int = 0) -> list[dict]:
        return repo.list_events(limit=100, group_id=group_id)


class NewGroupBootstrap(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("整队A", "i-boot-aaa")
        self.b = _make_server("整队B", "i-boot-bbb")
        self.group_id = repo.upsert_group(
            {
                "name": "整队组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12, "slot_name": "白班"},
                    {"server_id": self.b, "hours": 12, "slot_name": "夜班"},
                ],
            }
        )
        # 让 A 落在当班：轮换起点挪到 1 小时前
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), self.group_id),
        )
        # 真实乱象：该当班的 A 停着，该休班的 B 在跑
        aliyun.start_instance(repo.get_server(self.b, decrypt_secret=True))
        repo.set_settings({"ddns_enabled": "1"})
        repo.ensure_dns_record("life.example.com")
        repo.bind_dns_record("life.example.com", dns.TARGET_GROUP, self.group_id)

    def test_bootstrap_aligns_members_in_one_event(self):
        shift.engine.bootstrap_group(self.group_id)
        # 开机是跨轮的：多跑几轮模拟后台对账推进
        for _ in range(4):
            self._reconcile(self.group_id)

        # 组员回到排班表该有的位置：当班 Running，休班节省停机
        self.assertEqual(_status(self.a)[0], "Running")
        self.assertEqual(_status(self.b), ("Stopped", "StopCharging"))

        events = self._events(self.group_id)
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        event = events[0]
        self.assertEqual(event["kind"], "group_new")
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["title"], "整队组：新建换班组")

        texts = [step["text"] for step in event["progress"]]
        self.assertTrue(any(t.startswith("按排班表核对组员状态") for t in texts), texts)
        # 核对阶段就点出哪台不符合，用户不用等动作下发完
        self.assertTrue(any("需要开机" in t for t in texts), texts)
        self.assertTrue(any("需要节省停机" in t for t in texts), texts)
        self.assertEqual(sum("已下发启动当班" in t for t in texts), 1, texts)
        self.assertEqual(sum("已下发停机" in t for t in texts), 1, texts)
        # DDNS 排在停机之后：状态没全部到位不碰域名
        ddns_at = next(i for i, t in enumerate(texts) if t.startswith("DDNS："))
        self.assertGreater(ddns_at, next(i for i, t in enumerate(texts) if "已下发停机" in t), texts)
        self.assertEqual(texts[-1], "全部状态确认完毕")

        # 新建组这一班不发报告
        self.assertEqual(self.sent, [], self.sent)

    def test_no_reconcile_creates_second_event(self):
        """bootstrap 之后例行对账沿用同一条事件，不会多出一条「换班进行中」。"""
        shift.engine.bootstrap_group(self.group_id)
        for _ in range(5):
            self._reconcile(self.group_id)
        titles = [e["title"] for e in self._events(self.group_id)]
        self.assertEqual(titles, ["整队组：新建换班组"], titles)

    def test_disabled_group_only_records_event(self):
        repo.upsert_group(
            {
                "id": self.group_id,
                "name": "整队组",
                "enabled": False,
                "failover_enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )
        result = shift.engine.bootstrap_group(self.group_id)
        self.assertEqual(result.get("skipped"), "disabled")
        self.assertEqual(_status(self.b)[0], "Running")  # 原样不动
        event = self._events(self.group_id)[0]
        self.assertEqual(event["title"], "整队组：新建换班组")
        self.assertEqual(event["status"], "done")
        self.assertIn("未启用", event["detail"])


class DissolveGroup(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("解散A", "i-dis-aaa")
        self.b = _make_server("解散B", "i-dis-bbb")
        self.group_id = repo.upsert_group(
            {
                "name": "解散组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), self.group_id),
        )
        repo.set_settings({"ddns_enabled": "1"})
        repo.ensure_dns_record("dissolve.example.com")
        repo.bind_dns_record("dissolve.example.com", dns.TARGET_GROUP, self.group_id)
        # A 在跑、B 停着：解散后必须原样保持
        aliyun.start_instance(repo.get_server(self.a, decrypt_secret=True))

    def test_dissolve_keeps_state_and_releases_keepalive(self):
        keepalive_id = repo.add_keepalive(self.a, True)
        self.assertTrue(repo.list_keepalives()[0]["enabled"])

        shift.engine.dissolve_group(self.group_id)

        # 状态原样保留：没有开机也没有停机
        self.assertEqual(_status(self.a)[0], "Running")
        self.assertEqual(_status(self.b)[0], "Stopped")

        # 组没了、域名解绑了、保活暂停了（记录还在，只是不启用）
        self.assertIsNone(repo.get_group(self.group_id))
        self.assertEqual(repo.dns_records_of(dns.TARGET_GROUP, self.group_id), [])
        keepalives = repo.list_keepalives()
        self.assertEqual(len(keepalives), 1)
        self.assertEqual(int(keepalives[0]["id"]), keepalive_id)
        self.assertFalse(keepalives[0]["enabled"])

        events = self._events()
        self.assertEqual(len(events), 1, [e["title"] for e in events])
        event = events[0]
        self.assertEqual(event["kind"], "group_dissolve")
        self.assertEqual(event["status"], "done")
        self.assertEqual(event["title"], "解散组：解散换班组")
        texts = [step["text"] for step in event["progress"]]
        self.assertTrue(any("解散A" in t and "保持不动" in t for t in texts), texts)
        self.assertTrue(any("解散B" in t and "保持不动" in t for t in texts), texts)
        self.assertTrue(any("单机保活已暂停" in t for t in texts), texts)
        self.assertTrue(any("DDNS 绑定已解除" in t for t in texts), texts)
        self.assertEqual(texts[-1], "组员均已回到闲散手动，可随时手动开关机")
        # 解散不发通知
        self.assertEqual(self.sent, [], self.sent)

    def test_dissolve_after_keepalive_would_not_start_stopped_member(self):
        """B 停着、又挂着保活：解散后也不能被保活拉起来。"""
        repo.add_keepalive(self.b, True)
        shift.engine.dissolve_group(self.group_id)
        shift.engine.reconcile_keepalives(
            repo.get_settings(), _now_dt(), shift.engine._grouped_server_ids()
        )
        self.assertEqual(_status(self.b)[0], "Stopped")


class EventProgressAlways(unittest.TestCase):
    """所有事件点开都要有过程：即时完成的事件把详情拆成进度条目。"""

    def setUp(self) -> None:
        db.init_db()
        db.execute("DELETE FROM events")

    def test_instant_event_records_progress(self):
        repo.add_event(
            "dns",
            "DNS 记录名已添加",
            "a.example.com\nb.example.com",
        )
        event = repo.list_events(limit=10)[0]
        self.assertEqual(event["status"], "done")
        self.assertEqual([s["text"] for s in event["progress"]], ["a.example.com", "b.example.com"])

    def test_error_event_records_progress(self):
        repo.add_event("error", "换班组同步异常", "连接超时", level="error")
        event = repo.list_events(limit=10)[0]
        self.assertEqual([s["text"] for s in event["progress"]], ["连接超时"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
