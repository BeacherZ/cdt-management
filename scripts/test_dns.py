"""DNS 绑定逻辑单测。

不起服务、不连网：`ddns.sync` 全部打桩，只验证「谁该指向谁」和「什么时候才该真发请求」。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前指好数据目录：settings 在导入时就把路径算成常量了
_TMP = tempfile.mkdtemp(prefix="cdt-dns-test-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"

from app import db, ddns, dns, repo  # noqa: E402

CALLS: list[tuple[str, str]] = []


def _fake_sync(ip, config, force=False, record=None):  # noqa: ANN001, ARG001
    CALLS.append((record or "", ip))
    return ddns.DDNSResult(ddns.STATUS_SYNCED, f"{record} → {ip}", changed=True, ip=ip)


def _fake_sync_fail(ip, config, force=False, record=None):  # noqa: ANN001, ARG001
    CALLS.append((record or "", ip))
    return ddns.DDNSResult(ddns.STATUS_UPDATE_FAILED, "HTTP 500", ip=ip)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        CALLS.clear()
        for table in ("dns_records", "servers", "keepalives", "shift_groups", "shift_members", "events"):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM settings WHERE key LIKE 'ddns%'")
        repo.set_settings({"ddns_enabled": "1", "ddns_records": "[]", "ddns_record": ""})
        self._real_sync = ddns.sync
        ddns.sync = _fake_sync

    def tearDown(self) -> None:
        ddns.sync = self._real_sync

    # -- 造数据 ---------------------------------------------------------
    def add_server(self, label: str, status: str = "Running", ip: str = "47.243.10.10") -> int:
        server_id = repo.upsert_server(
            {
                "label": label,
                "access_key_id": "LTAI_FAKE_KEY",
                "access_key_secret": "fake-secret",
                "region_id": "cn-hongkong",
                "site": "china",
                "instance_id": f"i-{label}",
                "quota_gb": 200,
                "threshold_gb": 188,
            }
        )
        repo.update_server_runtime(server_id, status=status, stopped_mode="", public_ip=ip)
        return server_id

    def add_group(self, name: str, server_ids: list[int]) -> int:
        return repo.upsert_group(
            {
                "name": name,
                "enabled": True,
                "members": [
                    {"server_id": sid, "hours": 12, "enabled": True} for sid in server_ids
                ],
            }
        )

    def config(self) -> dict[str, str]:
        return repo.get_settings()


class Registry(Base):
    def test_settings_records_become_registry_rows(self):
        repo.set_settings({"ddns_records": '["a.example.com","b.example.com"]'})
        dns.sync_registry(self.config())
        self.assertEqual([row["record"] for row in repo.list_dns_records()],
                         ["a.example.com", "b.example.com"])

    def test_removed_record_is_dropped_with_its_binding(self):
        repo.set_settings({"ddns_records": '["a.example.com","b.example.com"]'})
        dns.sync_registry(self.config())
        server_id = self.add_server("香港-主力")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        repo.set_settings({"ddns_records": '["b.example.com"]'})
        dns.sync_registry(self.config())
        self.assertIsNone(repo.get_dns_record("a.example.com"))
        self.assertEqual(repo.dns_record_of(dns.TARGET_SERVER, server_id), "")

    def test_add_record_appends_and_keeps_order(self):
        repo.set_settings({"ddns_records": '["a.example.com"]'})
        dns.add_record("b.example.com", self.config())
        self.assertEqual(ddns.records(self.config()), ["a.example.com", "b.example.com"])
        # 重复添加（大小写不同）不产生第二条
        dns.add_record("B.example.com", self.config())
        self.assertEqual(len(ddns.records(self.config())), 2)

    def test_add_record_rejects_bad_names(self):
        for bad in ("", "  ", "no-dot", "has space.com"):
            with self.assertRaises(ValueError):
                dns.add_record(bad, self.config())


class Binding(Base):
    def setUp(self) -> None:
        super().setUp()
        repo.set_settings({"ddns_records": '["a.example.com","b.example.com"]'})
        dns.sync_registry(self.config())

    def test_unbound_record_never_syncs(self):
        """以前这里会拿第一条记录兜底，于是两台机器抢同一个域名。"""
        server_id = self.add_server("香港-主力")
        # 没绑：同步请求根本不该发出去
        result = dns.sync_record("a.example.com", self.config())
        self.assertEqual(result.status, dns.STATUS_UNBOUND)
        self.assertEqual(CALLS, [])
        # 目标在跑但记录没绑，巡检也不许动
        dns.reconcile(self.config())
        self.assertEqual(CALLS, [])
        self.assertEqual(repo.dns_record_of(dns.TARGET_SERVER, server_id), "")

    def test_bound_running_server_syncs_its_ip(self):
        server_id = self.add_server("香港-主力", ip="47.243.10.10")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        result = dns.sync_record("a.example.com", self.config())
        self.assertEqual(result.status, ddns.STATUS_SYNCED)
        self.assertEqual(CALLS, [("a.example.com", "47.243.10.10")])
        row = repo.get_dns_record("a.example.com")
        self.assertEqual(row["last_ip"], "47.243.10.10")

    def test_stopped_server_is_not_pointed_at(self):
        server_id = self.add_server("香港-备用", status="Stopped", ip="47.243.10.11")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        result = dns.sync_record("a.example.com", self.config())
        self.assertEqual(result.status, dns.STATUS_IDLE)
        self.assertEqual(CALLS, [])

    def test_one_target_holds_only_one_record(self):
        server_id = self.add_server("香港-主力")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.bind("b.example.com", dns.TARGET_SERVER, server_id)
        self.assertEqual(repo.dns_record_of(dns.TARGET_SERVER, server_id), "b.example.com")
        self.assertEqual(repo.get_dns_record("a.example.com")["target_kind"], "")

    def test_unbind(self):
        server_id = self.add_server("香港-主力")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.bind("a.example.com", "", 0)
        self.assertEqual(repo.dns_record_of(dns.TARGET_SERVER, server_id), "")

    def test_group_binding_follows_duty(self):
        a = self.add_server("香港-A", ip="47.243.10.21")
        b = self.add_server("香港-B", status="Stopped", ip="47.243.10.22")
        group_id = self.add_group("双机轮换", [a, b])
        dns.bind("a.example.com", dns.TARGET_GROUP, group_id)
        snapshot = dns.snapshot(self.config())
        row = next(item for item in snapshot["records"] if item["record"] == "a.example.com")
        self.assertEqual(row["target"]["kind"], "group")
        self.assertTrue(row["target"]["running"])
        self.assertEqual(row["desired_ip"], "47.243.10.21")

    def test_deleting_group_releases_binding(self):
        a = self.add_server("香港-A")
        b = self.add_server("香港-B", status="Stopped")
        group_id = self.add_group("双机轮换", [a, b])
        dns.bind("a.example.com", dns.TARGET_GROUP, group_id)
        repo.delete_group(group_id)
        self.assertEqual(repo.get_dns_record("a.example.com")["target_kind"], "")

    def test_deleting_server_releases_binding(self):
        server_id = self.add_server("香港-主力")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        repo.delete_server(server_id)
        self.assertEqual(repo.get_dns_record("a.example.com")["target_kind"], "")

    def test_removing_keepalive_releases_binding(self):
        server_id = self.add_server("香港-主力")
        keepalive_id = repo.add_keepalive(server_id)
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        repo.delete_keepalive(keepalive_id)
        self.assertEqual(repo.get_dns_record("a.example.com")["target_kind"], "")


class Sync(Base):
    def setUp(self) -> None:
        super().setUp()
        repo.set_settings({"ddns_records": '["a.example.com"]'})
        dns.sync_registry(self.config())

    def test_reconcile_syncs_once_then_stays_quiet(self):
        server_id = self.add_server("香港-主力", ip="47.243.10.10")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.reconcile(self.config())
        self.assertEqual(len(CALLS), 1)
        dns.reconcile(self.config())
        self.assertEqual(len(CALLS), 1, "IP 没变还在打接口就是白烧配额")

    def test_reconcile_follows_ip_change(self):
        server_id = self.add_server("香港-主力", ip="47.243.10.10")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.reconcile(self.config())
        repo.update_server_runtime(server_id, status="Running", public_ip="47.243.10.99")
        dns.reconcile(self.config())
        self.assertEqual(CALLS[-1], ("a.example.com", "47.243.10.99"))
        self.assertEqual(repo.get_dns_record("a.example.com")["last_ip"], "47.243.10.99")

    def test_private_ip_is_skipped(self):
        server_id = self.add_server("香港-主力", ip="10.0.0.8")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.reconcile(self.config())
        self.assertEqual(CALLS, [])

    def test_failure_keeps_last_good_ip(self):
        server_id = self.add_server("香港-主力", ip="47.243.10.10")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.sync_record("a.example.com", self.config())
        ddns.sync = _fake_sync_fail
        repo.update_server_runtime(server_id, public_ip="47.243.10.11")
        result = dns.sync_record("a.example.com", self.config())
        self.assertEqual(result.status, ddns.STATUS_UPDATE_FAILED)
        row = repo.get_dns_record("a.example.com")
        self.assertEqual(row["last_ip"], "47.243.10.10", "失败时界面上要还能看到实际解析到哪")
        self.assertEqual(row["last_status"], ddns.STATUS_UPDATE_FAILED)

    def test_disabled_ddns_does_nothing(self):
        server_id = self.add_server("香港-主力")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        repo.set_settings({"ddns_enabled": "0"})
        self.assertEqual(dns.reconcile(self.config()), [])
        self.assertEqual(CALLS, [])

    def test_snapshot_states(self):
        server_id = self.add_server("香港-主力")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        state = dns.snapshot(self.config())["records"][0]["state"]
        self.assertEqual(state, dns.STATE_PENDING)
        dns.sync_record("a.example.com", self.config())
        self.assertEqual(dns.snapshot(self.config())["records"][0]["state"], dns.STATE_SYNCED)


class Legacy(Base):
    def test_group_ddns_record_is_adopted_once(self):
        a = self.add_server("香港-A")
        b = self.add_server("香港-B", status="Stopped")
        group_id = self.add_group("双机轮换", [a, b])
        repo.set_settings({"ddns_records": '["legacy.example.com"]'})
        db.execute("UPDATE shift_groups SET ddns_record='legacy.example.com' WHERE id=?", (group_id,))
        dns.sync_registry(self.config())
        dns.adopt_legacy(self.config())
        self.assertEqual(repo.dns_record_of(dns.TARGET_GROUP, group_id), "legacy.example.com")

    def test_adopt_does_not_steal_existing_binding(self):
        a = self.add_server("香港-A")
        b = self.add_server("香港-B", status="Stopped")
        group_id = self.add_group("双机轮换", [a, b])
        server_id = self.add_server("香港-C")
        repo.set_settings({"ddns_records": '["legacy.example.com"]'})
        db.execute("UPDATE shift_groups SET ddns_record='legacy.example.com' WHERE id=?", (group_id,))
        dns.sync_registry(self.config())
        dns.bind("legacy.example.com", dns.TARGET_SERVER, server_id)
        dns.adopt_legacy(self.config())
        self.assertEqual(repo.dns_record_of(dns.TARGET_SERVER, server_id), "legacy.example.com")
        self.assertEqual(repo.dns_record_of(dns.TARGET_GROUP, group_id), "")


def tearDownModule() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
