#!/usr/bin/env python3
"""换班流程的端到端测试（dry-run 内存桩，不碰真实阿里云）。

锁死两条容易回归的规则：
  1. 一轮换班只产生 **一条** 事件（启动当班 / 停机非当班 / DDNS 都是同一条事件的进度），
     早期版本会给每一步各开一条，界面上一次换班冒出三条记录。
  2. DDNS 动作排在「当班 Running + 非当班全部停机」确认之后，且整条事件里只记一次，
     不会出现两条重复的「DDNS：✅ 已同步」（IP 没变时的旧文案是「✅ 无变化」，
     现已统一成「已同步」，进度靠去重保证只记一次）。

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

from app import aliyun, db, ddns, dns, notify, repo, settings, shift  # noqa: E402


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


class ManualSwitch(unittest.TestCase):
    """手动换班不发通知：两种模式（小时轮换 / 固定时刻表）行为要一致。

    界面点完按钮有 toast、事件里也有记录，再推一条「换班完成」只会让人以为
    自己不小心触发了自动换班。
    """

    def setUp(self) -> None:
        db.init_db()
        for table in (
            "events",
            "flags",
            "shift_runs",
            "shift_members",
            "shift_groups",
            "servers",
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings({"notify_on_shift": "1", "notify_on_failover": "1"})
        self.a = _make_server("手换A", "i-man-aaa")
        self.b = _make_server("手换B", "i-man-bbb")
        self.group_id = repo.upsert_group(
            {
                "name": "手换组",
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
        aliyun.start_instance(repo.get_server(self.a, decrypt_secret=True))
        self.sent: list[str] = []
        self._orig_send = notify.send_all
        notify.send_all = self._fake_send

    def tearDown(self) -> None:
        notify.send_all = self._orig_send

    def _fake_send(self, config, message):  # noqa: ANN001, ARG002
        self.sent.append(message.title)
        return {"webhook": "ok"}

    def _reconcile(self) -> None:
        shift.engine.reconcile_group(
            repo.get_group(self.group_id) or {}, repo.get_settings(), _now_dt(), float(time.time())
        )

    def test_manual_switch_is_silent(self):
        for _ in range(3):
            self._reconcile()
        self.assertTrue(self.sent, "正常结算应该发一条换班完成，测试前提不成立")
        self.sent.clear()

        shift.engine.manual_switch(self.group_id)
        for _ in range(5):
            self._reconcile()

        self.assertEqual(self.sent, [], "手动换班不该再推通知")
        manual = [
            e
            for e in repo.list_events(limit=50, group_id=self.group_id)
            if e["kind"] == "manual"
        ]
        self.assertEqual(len(manual), 1, [e["title"] for e in manual])


class PartialDdns(unittest.TestCase):
    """一台机器挂两条域名、其中一条同步失败时，换班不能判完成。

    combine_status 拼出的「✅ 已同步 / ❌ 更新失败」以 ✅ 开头，早期代码用
    startswith("✅") 判断，于是把「有一条没指过去」当成全部成功：事件打完成、
    通知照发，用户以为域名已经切过来了。
    """

    def setUp(self) -> None:
        db.init_db()
        for table in (
            "events",
            "flags",
            "shift_runs",
            "shift_members",
            "shift_groups",
            "servers",
            "dns_records",
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings(
            {
                "notify_on_shift": "1",
                "ddns_enabled": "1",
                "ddns_records": '["ok.example.com","bad.example.com"]',
                "ddns_record": "ok.example.com",
            }
        )
        dns.sync_registry(repo.get_settings())
        self.a = _make_server("域名A", "i-ddns-aaa")
        self.b = _make_server("域名B", "i-ddns-bbb")
        self.group_id = repo.upsert_group(
            {
                "name": "域名组",
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
        aliyun.start_instance(repo.get_server(self.a, decrypt_secret=True))
        dns.bind("ok.example.com", dns.TARGET_GROUP, self.group_id)
        dns.bind("bad.example.com", dns.TARGET_SERVER, self.a)

        self.sent: list[str] = []
        self._orig_send = notify.send_all
        self._orig_sync = ddns.sync
        notify.send_all = self._fake_send
        ddns.sync = self._fake_sync

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        ddns.sync = self._orig_sync

    def _fake_send(self, config, message):  # noqa: ANN001, ARG002
        self.sent.append(message.title)
        return {"webhook": "ok"}

    def _fake_sync(self, ip, config, force=False, record=None):  # noqa: ANN001, ARG002
        if record == "bad.example.com":
            return ddns.DDNSResult(ddns.STATUS_UPDATE_FAILED, "HTTP 500", ip=ip)
        return ddns.DDNSResult(ddns.STATUS_SYNCED, f"{record} → {ip}", changed=True, ip=ip)

    def test_partial_failure_blocks_settle(self):
        for _ in range(4):
            shift.engine.reconcile_group(
                repo.get_group(self.group_id) or {},
                repo.get_settings(),
                _now_dt(),
                float(time.time()),
            )

        run = db.query_one(
            "SELECT * FROM shift_runs WHERE group_id=? ORDER BY updated_at DESC LIMIT 1",
            (self.group_id,),
        )
        self.assertIsNotNone(run)
        self.assertNotEqual(run["phase"], shift.PHASE_SETTLED, dict(run))
        self.assertEqual(self.sent, [], "就绪判定没通过，不该发换班报告")
        events = repo.list_events(limit=50, group_id=self.group_id)
        # 事件是「进行中」建的，kind 一开始就叫 shift_done，看的是它有没有被结算
        self.assertFalse([e for e in events if e["status"] == "done"], [e["title"] for e in events])
        texts = [step["text"] for e in events for step in e.get("progress") or []]
        self.assertNotIn("全部状态确认完毕", texts)


class TrafficBreaker(unittest.TestCase):
    """当班超限、又找不到替班时，必须停机，不能「保守维持当班」。

    两种情况都要停：
      1. 其他成员也超限
      2. 其他成员状态读不到（早先的实现会保留当班继续跑，等于限流阈值失效）
    """

    def setUp(self) -> None:
        db.init_db()
        for table in (
            "events",
            "flags",
            "shift_runs",
            "shift_members",
            "shift_groups",
            "servers",
            "dns_records",
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings(
            {"notify_on_breaker": "1", "notify_on_shift": "1", "notify_on_failover": "1"}
        )
        self.a = _make_server("超限A", "i-over-aaa")
        self.b = _make_server("超限B", "i-over-bbb")
        self.group_id = repo.upsert_group(
            {
                "name": "超限组",
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
        # 两台都跑起来：A 当班、B 待机
        for server_id in (self.a, self.b):
            aliyun.start_instance(repo.get_server(server_id, decrypt_secret=True))
            for _ in range(3):
                aliyun.get_instance(repo.get_server(server_id, decrypt_secret=True))

        self.stopped: list[str] = []
        self.traffic = 199.0
        self._orig_stop = aliyun.stop_instance
        self._orig_traffic = aliyun.get_traffic_gb
        self._orig_instance = aliyun.get_instance
        aliyun.stop_instance = self._fake_stop
        aliyun.get_traffic_gb = self._fake_traffic

    def tearDown(self) -> None:
        aliyun.stop_instance = self._orig_stop
        aliyun.get_traffic_gb = self._orig_traffic
        aliyun.get_instance = self._orig_instance

    def _fake_stop(self, account, mode="StopCharging"):  # noqa: ANN001, ARG002
        self.stopped.append(str(account.get("label") or ""))
        return self._orig_stop(account, mode)

    def _fake_traffic(self, account):  # noqa: ANN001, ARG002
        # 阈值是 188 GB，默认两台都超；用例可以改 self.traffic 模拟降下来
        return self.traffic

    def _reconcile(self) -> dict:
        return shift.engine.reconcile_group(
            repo.get_group(self.group_id) or {}, repo.get_settings(), _now_dt(), float(time.time())
        )

    def _run_phase(self) -> str:
        group = repo.get_group(self.group_id) or {}
        picked = shift.duty_at(group, time.time())
        assert picked is not None
        run = repo.get_run(self.group_id, str(int(picked[1]))) or {}
        return str(run.get("phase") or "")

    def test_all_over_stops_everyone(self):
        result = self._reconcile()
        self.assertTrue(result.get("breaker"), result)
        self.assertEqual(sorted(self.stopped), sorted(["超限A", "超限B"]))
        self.assertEqual(self._run_phase(), shift.PHASE_BREAKER)

    def test_unreadable_others_still_stops_duty(self):
        """其他成员状态读不到 → 不再保守维持当班，当班也停机。"""
        orig = self._orig_instance

        def _raise_for_b(account):  # noqa: ANN001
            if str(account.get("label")) == "超限B":
                raise RuntimeError("DescribeInstances 超时")
            return orig(account)

        aliyun.get_instance = _raise_for_b
        result = self._reconcile()
        self.assertTrue(result.get("breaker"), result)
        self.assertIn("超限A", self.stopped, "当班超限却还在跑，等于限流阈值没生效")
        events = repo.list_events(limit=50, group_id=self.group_id)
        joined = " ".join(
            [str(e.get("title") or "") for e in events]
            + [str(e.get("detail") or "") for e in events]
            + [step["text"] for e in events for step in e.get("progress") or []]
        )
        self.assertIn("状态读取失败", joined, joined)
        self.assertNotIn("保守维持当班", joined, joined)
        # 熔断原因要如实写成「读不到」，不能编成「全员超限」
        run = db.query_one(
            "SELECT note FROM shift_runs WHERE group_id=? ORDER BY updated_at DESC LIMIT 1",
            (self.group_id,),
        )
        self.assertIn("读取失败", str(dict(run or {})["note"] or ""))


    def test_breaker_note_clears_after_recovery(self):
        """熔断原因只在异常期间有意义：流量降下来后不能还挂在界面上。"""
        self._reconcile()
        self.assertEqual(self._run_phase(), shift.PHASE_BREAKER)

        self.traffic = 10.0
        for _ in range(3):
            self._reconcile()

        run = db.query_one(
            "SELECT phase, note FROM shift_runs WHERE group_id=? ORDER BY updated_at DESC LIMIT 1",
            (self.group_id,),
        )
        row = dict(run or {})
        self.assertEqual(row.get("phase"), shift.PHASE_SETTLED, row)
        self.assertEqual(row.get("note"), "", "恢复正常后不该还挂着熔断原因")


def _now_dt():
    import datetime as dt

    return dt.datetime.now(shift._timezone("Asia/Shanghai"))


class Timezone(unittest.TestCase):
    """设置里的时区要能被所有调用点读到，不能只有引擎知道。

    以前只有 reconcile_group 往 group 上塞 `_timezone`，界面当班高亮、DNS 指向判定、
    手动换班拿到的都是裸字典，一律回落硬编码的 +08:00 —— 时区配成别的就整体错位。
    """

    def setUp(self) -> None:
        db.init_db()
        shift._TZ_CACHE.update({"at": 0.0, "name": ""})

    def tearDown(self) -> None:
        repo.set_settings({"timezone": "Asia/Shanghai"})
        shift._TZ_CACHE.update({"at": 0.0, "name": ""})

    def test_bare_group_follows_setting(self):
        repo.set_settings({"timezone": "UTC"})
        shift._TZ_CACHE.update({"at": 0.0, "name": ""})
        group = {"id": 999, "members": [{"server_id": 1, "position": 0, "enabled": 1}]}
        self.assertEqual(shift._group_timezone(group), "UTC")
        # 引擎当次读到的配置优先于设置缓存
        self.assertEqual(
            shift._group_timezone({**group, "_timezone": "Asia/Tokyo"}), "Asia/Tokyo"
        )

    def test_timetable_follows_setting(self):
        """UTC 10:00 在 06:00–18:00 这一班里；按 +08:00 算就是 18:00，会判给另一台。"""
        import calendar

        try:
            from zoneinfo import ZoneInfo

            ZoneInfo("UTC")
        except Exception:  # noqa: BLE001
            self.skipTest("本机没有 tzdata，ZoneInfo 不可用")

        a = _make_server("时区A", "i-tz-aaa")
        b = _make_server("时区B", "i-tz-bbb")
        group_id = repo.upsert_group(
            {
                "name": "时区组",
                "enabled": True,
                "members": [
                    {"server_id": a, "hours": 12, "start_time": "06:00", "end_time": "18:00"},
                    {"server_id": b, "hours": 12, "start_time": "18:00", "end_time": "06:00"},
                ],
            }
        )
        repo.set_settings({"timezone": "UTC"})
        shift._TZ_CACHE.update({"at": 0.0, "name": ""})

        picked = shift.duty_at(repo.get_group(group_id) or {}, calendar.timegm((2026, 10, 27, 10, 0, 0)))
        self.assertIsNotNone(picked)
        self.assertEqual(int(picked[0]["server_id"]), a, "UTC 10:00 属于 06:00–18:00 的班")


if __name__ == "__main__":
    unittest.main(verbosity=2)
