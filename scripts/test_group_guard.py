#!/usr/bin/env python3
"""回归测试：本轮代码审查里修掉的几处健壮性问题。

逐条锁死：
  1. 停用的换班组不再压制组员的单机保活（以前组停用了，保活还一直「让位」，
     两边都不管，机器躺着）。
  2. 同一台服务器不能同时编进两个启用的换班组：否则两组互相把对方当待停机，
     每轮对账互相启停。停用的组不算，那是「先配好、暂不启用」。
  3. 时刻表必须刚好盖满一天、任何时刻只归一台；空档 / 重叠在保存时就被拒，
     而不是让引擎悄悄按登记顺序挑一台。
  4. 超限停机：指令被拒或状态迟迟不变时，120 秒内只发一次、只记一条事件，
     不再每轮刷一条。
  5. 当班 / 组员状态读不到时不下发启停指令，也不把这一班算成「已到位」。
  6. 退出登录真的吊销票据（会话无状态，光删 cookie 票据还能用）。
  7. 登录限速的客户端识别：直连时不认 X-Forwarded-For，反向代理后才认。

跑法：python scripts/test_group_guard.py
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

# 必须在 import app.* 之前设好
_TMP = tempfile.mkdtemp(prefix="cdt-group-guard-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"
os.environ["CDT_MOCK_OP_DELAY"] = "0"

from starlette.requests import Request  # noqa: E402

from app import aliyun, api, db, notify, repo, security, shift  # noqa: E402

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


def _request(client_host: str, forwarded: str = "") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": headers,
            "client": (client_host, 43210),
            "query_string": b"",
        }
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
            "revoked_sessions",
            "traffic_samples",
        ):
            db.execute(f"DELETE FROM {table}")
        repo.set_settings({"ddns_enabled": "0", "notify_on_shift": "0"})
        self.sent: list[str] = []
        self._orig_send = notify.send_all

        def _fake_send(config, message):  # noqa: ANN001, ARG002
            self.sent.append(message.title)
            return {}

        notify.send_all = _fake_send
        self._orig_refresh = shift._refresh  # noqa: SLF001
        self._orig_stop = aliyun.stop_instance
        self._orig_start = aliyun.start_instance

    def tearDown(self) -> None:
        notify.send_all = self._orig_send
        shift._refresh = self._orig_refresh  # noqa: SLF001
        aliyun.stop_instance = self._orig_stop
        aliyun.start_instance = self._orig_start

    def _reconcile(self, group_id: int) -> dict:
        group = repo.get_group(group_id) or {}
        return shift.engine.reconcile_group(
            group, repo.get_settings(), _now_dt(), float(time.time())
        )

    def _events(self) -> list[dict]:
        return repo.list_events(limit=100)


class DisabledGroupReleasesKeepalive(_Base):
    """停用组不该继续占着组员，单机保活要能接手。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("保活A")
        self.b = _make_server("保活B")
        self.group_id = repo.upsert_group(
            {
                "name": "停用组",
                "enabled": False,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )
        repo.add_keepalive(self.a, True)

    def test_disabled_group_does_not_block_keepalive(self):
        self.assertEqual(shift.engine._grouped_server_ids(), set())  # noqa: SLF001
        results = shift.engine.reconcile_keepalives(
            repo.get_settings(), _now_dt(), shift.engine._grouped_server_ids()  # noqa: SLF001
        )
        self.assertTrue(results, "停用组不该再压制保活")
        self.assertEqual(results[0]["keepalive"], self.a)
        self.assertFalse(
            [e for e in self._events() if "让位" in e["title"]],
            [e["title"] for e in self._events()],
        )

    def test_enabled_group_still_wins(self):
        """启用的组仍然优先：保活必须让位，两套流程不能同时管一台机器。"""
        repo.upsert_group(
            {
                "id": self.group_id,
                "name": "启用组",
                "enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )
        self.assertEqual(shift.engine._grouped_server_ids(), {self.a, self.b})  # noqa: SLF001


class CrossGroupConflict(_Base):
    """同一台服务器不能同时编进两个启用的换班组。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("冲突A")
        self.b = _make_server("冲突B")
        self.c = _make_server("冲突C")
        self.first = repo.upsert_group(
            {
                "name": "组一",
                "enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )

    def test_same_server_in_two_enabled_groups_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            repo.upsert_group(
                {
                    "name": "组二",
                    "enabled": True,
                    "members": [
                        {"server_id": self.b, "hours": 12},
                        {"server_id": self.c, "hours": 12},
                    ],
                }
            )
        message = str(ctx.exception)
        self.assertIn("冲突B", message)
        self.assertIn("组一", message)

    def test_disabled_group_can_reuse_members(self):
        group_id = repo.upsert_group(
            {
                "name": "组三",
                "enabled": False,
                "members": [
                    {"server_id": self.b, "hours": 12},
                    {"server_id": self.c, "hours": 12},
                ],
            }
        )
        self.assertTrue(group_id)

    def test_editing_the_same_group_is_allowed(self):
        repo.upsert_group(
            {
                "id": self.first,
                "name": "组一改名",
                "enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 8},
                    {"server_id": self.b, "hours": 8},
                ],
            }
        )
        self.assertEqual(repo.get_group(self.first)["name"], "组一改名")


class TimetableCoverage(_Base):
    """时刻表要刚好盖满一天。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("班A")
        self.b = _make_server("班B")
        self.c = _make_server("班C")

    def _payload(self, *slots) -> dict:
        members = [
            {"server_id": sid, "hours": 12, "start_time": start, "end_time": end, "enabled": True}
            for sid, start, end in slots
        ]
        return {"name": "时刻组", "enabled": True, "members": members}

    def test_overlap_rejected(self):
        payload = self._payload((self.a, "08:00", "20:00"), (self.b, "12:00", "00:00"))
        with self.assertRaises(ValueError) as ctx:
            api._validate_group_timetable(payload)  # noqa: SLF001
        self.assertIn("重叠", str(ctx.exception))

    def test_gap_rejected(self):
        payload = self._payload((self.a, "08:00", "20:00"), (self.b, "20:00", "04:00"))
        with self.assertRaises(ValueError) as ctx:
            api._validate_group_timetable(payload)  # noqa: SLF001
        self.assertIn("没有安排当班", str(ctx.exception))
        self.assertIn("04:00", str(ctx.exception))

    def test_exact_coverage_accepted(self):
        for slots in (
            ((self.a, "08:00", "20:00"), (self.b, "20:00", "08:00")),
            ((self.a, "06:00", "14:00"), (self.b, "14:00", "22:00"), (self.c, "22:00", "06:00")),
        ):
            db.execute("DELETE FROM shift_members")
            db.execute("DELETE FROM shift_groups")
            payload = self._payload(*slots)
            api._validate_group_timetable(payload)  # noqa: SLF001
            repo.upsert_group(payload)

    def test_midnight_can_be_written_as_24(self):
        payload = self._payload((self.a, "00:00", "20:00"), (self.b, "20:00", "24:00"))
        api._validate_group_timetable(payload)  # noqa: SLF001
        self.assertEqual(payload["members"][1]["end_time"], "00:00")
        repo.upsert_group(payload)
        group = repo.get_group(repo.list_groups()[0]["id"])
        self.assertEqual(group["members"][1]["end_time"], "00:00")

    def test_same_start_and_end_rejected(self):
        payload = self._payload((self.a, "08:00", "08:00"), (self.b, "20:00", "08:00"))
        with self.assertRaises(ValueError) as ctx:
            api._validate_group_timetable(payload)  # noqa: SLF001
        self.assertIn("起止时刻不能相同", str(ctx.exception))


class LimitStopThrottle(_Base):
    """超限停机不许每轮刷事件。"""

    def setUp(self) -> None:
        super().setUp()
        self.duty = _make_server("超限当班", "Running", traffic=195.0)
        self.standby = _make_server("超限休班", "Stopped")
        self.group_id = repo.upsert_group(
            {
                "name": "超限组",
                "enabled": True,
                "failover_enabled": False,  # 不换班 → 走「超限停机」
                "members": [
                    {"server_id": self.duty, "hours": 12},
                    {"server_id": self.standby, "hours": 12},
                ],
            }
        )
        db.execute(
            "UPDATE shift_groups SET anchor_at=? WHERE id=?",
            (int(time.time() - 3600), self.group_id),
        )

        def _fake_refresh(server):  # noqa: ANN001
            if int(server["id"]) == self.duty:
                server.update(
                    traffic_used=195.0,
                    instance_status="Running",
                    stopped_mode="",
                    public_ip="47.0.0.1",
                )
            else:
                server.update(
                    traffic_used=0.0,
                    instance_status="Stopped",
                    stopped_mode="StopCharging",
                    public_ip="",
                )

        shift._refresh = _fake_refresh  # noqa: SLF001

    def test_rejected_stop_records_one_event(self):
        calls: list[int] = []

        def _fail(server, mode="StopCharging", fallback=True):  # noqa: ANN001, ARG001
            calls.append(int(server["id"]))
            raise RuntimeError("IncorrectInstanceStatus")

        aliyun.stop_instance = _fail
        for _ in range(4):
            self._reconcile(self.group_id)

        self.assertEqual(len(calls), 1, "120 秒内只该下发一次停机指令")
        errors = [e for e in self._events() if e["title"].endswith("超限停机失败")]
        self.assertEqual(len(errors), 1, [e["title"] for e in self._events()])

    def test_stuck_instance_records_one_event(self):
        calls: list[int] = []

        def _ok(server, mode="StopCharging", fallback=True):  # noqa: ANN001, ARG001
            calls.append(int(server["id"]))

        aliyun.stop_instance = _ok
        for _ in range(4):
            self._reconcile(self.group_id)

        self.assertEqual(len(calls), 1)
        events = [e for e in self._events() if e["kind"] == "limit_stop"]
        self.assertEqual(len(events), 1, [e["title"] for e in self._events()])


class UnreadableStateNoCommand(_Base):
    """状态读不到就不下指令。"""

    def setUp(self) -> None:
        super().setUp()
        self.a = _make_server("读不到A")
        self.b = _make_server("读不到B")
        self.group_id = repo.upsert_group(
            {
                "name": "读不到组",
                "enabled": True,
                "members": [
                    {"server_id": self.a, "hours": 12},
                    {"server_id": self.b, "hours": 12},
                ],
            }
        )

    def test_duty_unreadable_skips_round(self):
        started: list[int] = []
        aliyun.start_instance = lambda server: started.append(int(server["id"]))

        def _boom(server):  # noqa: ANN001, ARG002
            raise RuntimeError("RequestTimeout")

        shift._refresh = _boom  # noqa: SLF001
        first = self._reconcile(self.group_id)
        second = self._reconcile(self.group_id)

        self.assertIn("state_unreadable", first)
        self.assertEqual(started, [], "状态读不到时不该发开机指令")
        warnings = [e for e in self._events() if "当班状态读取失败" in e["title"]]
        self.assertEqual(len(warnings), 1, "同一班次只提示一次，不要每轮刷")
        self.assertIn("state_unreadable", second)

    def test_member_unreadable_blocks_settle(self):
        """组员状态读不到时不能算「全部到位」，否则会带着一台可能还在跑的机器结算。"""

        def _partial(server):  # noqa: ANN001
            if int(server["id"]) == self.a:
                server.update(
                    traffic_used=10.0,
                    instance_status="Running",
                    stopped_mode="",
                    public_ip="47.0.0.1",
                )
            else:
                raise RuntimeError("RequestTimeout")

        stops: list[int] = []
        aliyun.stop_instance = lambda server, mode="StopCharging", fallback=True: stops.append(
            int(server["id"])
        )
        shift._refresh = _partial  # noqa: SLF001
        result = self._reconcile(self.group_id)

        self.assertFalse(result.get("ready"))
        self.assertEqual(stops, [], "状态读不到的成员不该收到停机指令")
        run = repo.get_run(self.group_id, str(result.get("slot"))) or {}
        self.assertEqual(run.get("phase"), shift.PHASE_SWITCHING)


class LogoutRevokesTicket(_Base):
    """退出登录要把这张票据拉黑。"""

    def test_revoked_token_is_rejected(self):
        token = security.issue_session("admin", 0)
        self.assertEqual(security.read_session(token, 0), "admin")
        self.assertFalse(repo.is_session_revoked(security.session_id(token)))

        repo.revoke_session(
            security.session_id(token), int(time.time()) + 60
        )
        self.assertTrue(repo.is_session_revoked(security.session_id(token)))
        # 另一张票据不受影响（别把手机端的登录一起踢了）
        other = security.issue_session("admin", 0) + "x"
        self.assertFalse(repo.is_session_revoked(security.session_id(other)))

    def test_expired_revocations_are_pruned(self):
        token = security.issue_session("admin", 0)
        repo.revoke_session(security.session_id(token), int(time.time()) - 1)
        self.assertFalse(repo.is_session_revoked(security.session_id(token)))
        rows = db.query("SELECT * FROM revoked_sessions WHERE expires_at <= ?", (int(time.time()),))
        self.assertEqual(rows, [])

    def test_garbage_token_has_no_id(self):
        self.assertEqual(security.session_id(""), "")
        self.assertEqual(security.session_id("no-dot"), "")


class LoginRateLimitClient(_Base):
    """限速按真实客户端算，但不给伪造留口子。"""

    def test_public_peer_ignores_forwarded_for(self):
        # 8.8.8.8 才是「公网直连」：192.0.2/198.51.100/203.0.113 这几段在
        # ipaddress 里也算 private，拿它们做样本会误判成「前面有代理」。
        request = _request("8.8.8.8", forwarded="198.51.100.7")
        self.assertEqual(api._client_ip(request), "8.8.8.8")  # noqa: SLF001

    def test_private_peer_uses_forwarded_for(self):
        request = _request("172.18.0.1", forwarded="198.51.100.7, 172.18.0.1")
        self.assertEqual(api._client_ip(request), "198.51.100.7")  # noqa: SLF001

    def test_invalid_forwarded_for_falls_back_to_peer(self):
        request = _request("127.0.0.1", forwarded="not-an-ip")
        self.assertEqual(api._client_ip(request), "127.0.0.1")  # noqa: SLF001

    def test_failures_expire(self):
        api._login_failures.clear()  # noqa: SLF001
        for _ in range(api.LOGIN_MAX_FAILURES):  # noqa: SLF001
            api._record_failure("198.51.100.7")  # noqa: SLF001
        self.assertTrue(api._rate_limited("198.51.100.7"))  # noqa: SLF001
        # 窗口过了就不再锁
        api._login_failures["198.51.100.7"] = [  # noqa: SLF001
            time.time() - api.LOGIN_WINDOW - 1  # noqa: SLF001
        ] * api.LOGIN_MAX_FAILURES  # noqa: SLF001
        self.assertFalse(api._rate_limited("198.51.100.7"))  # noqa: SLF001
        self.assertNotIn("198.51.100.7", api._login_failures)  # noqa: SLF001
        api._login_failures.clear()  # noqa: SLF001


if __name__ == "__main__":
    unittest.main(verbosity=2)
