#!/usr/bin/env python3
"""回归测试：CDT 流量读数「成功但明细为空」时的兜底。

背景：`ListCdtInternetTraffic` 偶尔调用成功却不返回任何 `TrafficDetails`。
以前这里会被折算成 0.0 GB，直接覆盖库里的累计值 —— 一台已经烧到阈值的机器
于是被判成「没超限」，下一分钟就被重新开机，流量保护形同虚设。

现在的口径（shift._resolve_empty_traffic）：
  1. 本计费月读过流量、这次突然空了  → 视为读数不可用，沿用旧值继续判定，
     不写样本、只记一条预警；旧值超限就照样停机，不会被拉起；
  2. 新的计费月还没读到过（上次采样在上个月）→ 空就是真的 0，照常启动；
  3. 从来没有采样（新机器）          → 同样按 0，照常启动；
  4. 读数恢复正常                      → 自动清掉预警标记，不长期挂着。

跑法：python scripts/test_traffic_guard.py
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

_TMP = tempfile.mkdtemp(prefix="cdt-traffic-guard-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from app import aliyun, db, repo, shift  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))


def _now_dt() -> dt.datetime:
    return dt.datetime.now(TZ)


def _make_server(label: str, status: str = "Stopped", traffic: float = 0.0) -> int:
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
    db.execute("UPDATE servers SET traffic_used=? WHERE id=?", (traffic, server_id))
    return server_id


def _add_sample_at(server_id: int, used: float, ts: int) -> None:
    db.insert(
        "INSERT INTO traffic_samples(server_id, ts, used, status) VALUES(?,?,?,?)",
        (server_id, ts, used, "Stopped"),
    )


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
            "traffic_samples",
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings({"ddns_enabled": "0", "notify_on_shift": "0", "notify_on_limit_stop": "0"})
        self.started: list[int] = []
        self._orig_start = aliyun.start_instance
        self._orig_traffic = aliyun.get_traffic_gb
        aliyun.start_instance = lambda server: self.started.append(int(server["id"]))
        aliyun.get_traffic_gb = lambda server: None  # 默认：接口成功但明细为空

    def tearDown(self) -> None:
        aliyun.start_instance = self._orig_start
        aliyun.get_traffic_gb = self._orig_traffic

    def _keepalive_round(self) -> None:
        shift.engine.reconcile_keepalives(repo.get_settings(), _now_dt(), set())

    def _warnings(self) -> list[dict]:
        return [e for e in repo.list_events(limit=50) if "流量读数异常" in e["title"]]

    def _traffic_of(self, server_id: int) -> float:
        row = db.query_one("SELECT traffic_used FROM servers WHERE id=?", (server_id,))
        return float(row["traffic_used"] or 0)


class EmptyReadingSameMonth(_Base):
    """同月读过、这次空 → 保留旧值，超限机器绝不被拉起。"""

    def test_keeps_old_value_and_never_starts(self):
        sid = _make_server("已超限", traffic=195.0)
        _add_sample_at(sid, 195.0, int(time.time()) - 3600)  # 一小时前读过，同月
        repo.add_keepalive(sid)

        self._keepalive_round()

        self.assertEqual(self.started, [], "读数异常时不该拉起")
        self.assertAlmostEqual(self._traffic_of(sid), 195.0, msg="累计值不能被改写成 0")
        self.assertTrue(repo.get_flag(f"traffic_empty:{sid}"), "应打上读数异常标记")

    def test_no_fake_sample_written(self):
        sid = _make_server("已超限", traffic=195.0)
        _add_sample_at(sid, 195.0, int(time.time()) - 3600)
        repo.add_keepalive(sid)
        before = len(repo.list_samples(sid, hours=24 * 365))

        self._keepalive_round()

        after = len(repo.list_samples(sid, hours=24 * 365))
        self.assertEqual(after, before, "空读数不该往曲线里插一个假 0")

    def test_warning_only_once(self):
        sid = _make_server("已超限", traffic=195.0)
        _add_sample_at(sid, 195.0, int(time.time()) - 3600)
        repo.add_keepalive(sid)

        for _ in range(3):
            self._keepalive_round()

        self.assertEqual(len(self._warnings()), 1, "连续多轮只留一条预警")
        self.assertEqual(self.started, [])

    def test_under_limit_still_starts(self):
        """没超限的机器不该被这条保护连坐：照常拉起，值也照旧。"""
        sid = _make_server("没超限", traffic=10.0)
        _add_sample_at(sid, 10.0, int(time.time()) - 3600)
        repo.add_keepalive(sid)

        self._keepalive_round()

        self.assertEqual(self.started, [sid])
        self.assertAlmostEqual(self._traffic_of(sid), 10.0)


class EmptyReadingNewMonth(_Base):
    """上月读过、本月第一次空 → 是真的 0，照常启动。"""

    def test_new_month_treated_as_zero(self):
        sid = _make_server("上月超限", traffic=195.0)
        last_month = int(time.time()) - 40 * 24 * 3600
        _add_sample_at(sid, 195.0, last_month)
        repo.add_keepalive(sid)

        self._keepalive_round()

        self.assertEqual(self.started, [sid], "新计费月空读数应视为 0 并正常拉起")
        self.assertAlmostEqual(self._traffic_of(sid), 0.0)

    def test_first_ever_reading_is_zero(self):
        sid = _make_server("新机器", traffic=0.0)
        repo.add_keepalive(sid)

        self._keepalive_round()

        self.assertEqual(self.started, [sid])


class Recovery(_Base):
    """读数恢复 → 清标记，后续按真实值走。"""

    def test_clears_flag_when_reading_returns(self):
        sid = _make_server("已超限", traffic=195.0)
        _add_sample_at(sid, 195.0, int(time.time()) - 3600)
        repo.add_keepalive(sid)
        self._keepalive_round()
        self.assertTrue(repo.get_flag(f"traffic_empty:{sid}"))

        aliyun.get_traffic_gb = lambda server: 12.0  # 接口恢复
        self._keepalive_round()

        self.assertFalse(repo.get_flag(f"traffic_empty:{sid}"), "读数恢复后标记该清掉")
        self.assertAlmostEqual(self._traffic_of(sid), 12.0)


class GroupPath(_Base):
    """换班组走同一套 _refresh：读数异常时同样不许把超限当班开起来。"""

    def _group(self, duty: int, standby: int) -> int:
        group_id = repo.upsert_group(
            {
                "name": "读数组",
                "enabled": True,
                "failover_enabled": True,
                "members": [
                    {"server_id": duty, "hours": 12},
                    {"server_id": standby, "hours": 12},
                ],
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), group_id),
        )
        return group_id

    def test_over_limit_duty_not_started_on_empty_reading(self):
        duty = _make_server("当班A", traffic=195.0)
        standby = _make_server("休班B", traffic=10.0)
        _add_sample_at(duty, 195.0, int(time.time()) - 3600)
        _add_sample_at(standby, 10.0, int(time.time()) - 3600)
        group_id = self._group(duty, standby)

        group = repo.get_group(group_id) or {}
        shift.engine.reconcile_group(group, repo.get_settings(), _now_dt(), float(time.time()))

        self.assertNotIn(duty, self.started, "超限当班不该被拉起")
        self.assertAlmostEqual(self._traffic_of(duty), 195.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
