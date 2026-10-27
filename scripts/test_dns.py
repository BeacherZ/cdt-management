"""DNS 绑定逻辑单测。

不起服务、不连网：`ddns.sync` 全部打桩，只验证「谁该指向谁」和「什么时候才该真发请求」。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
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

    def test_one_target_can_hold_many_records(self):
        """一台机器可以挂多条域名（www、api…），彼此不顶掉。"""
        server_id = self.add_server("香港-主力")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.bind("b.example.com", dns.TARGET_SERVER, server_id)
        self.assertEqual(
            repo.dns_records_of(dns.TARGET_SERVER, server_id),
            ["a.example.com", "b.example.com"],
        )
        self.assertEqual(repo.get_dns_record("a.example.com")["target_id"], server_id)
        self.assertEqual(repo.get_dns_record("b.example.com")["target_id"], server_id)

    def test_moving_one_record_keeps_the_others(self):
        """把其中一条改绑到别的机器，只挪这一条，剩下的不动。"""
        server_id = self.add_server("香港-主力")
        other_id = self.add_server("香港-备用")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.bind("b.example.com", dns.TARGET_SERVER, server_id)
        dns.bind("b.example.com", dns.TARGET_SERVER, other_id)
        self.assertEqual(repo.dns_records_of(dns.TARGET_SERVER, server_id), ["a.example.com"])
        self.assertEqual(repo.dns_records_of(dns.TARGET_SERVER, other_id), ["b.example.com"])

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

    def test_removing_keepalive_keeps_binding(self):
        """移出单机保活不再顺手清掉 DNS 绑定：绑定是用户在 DNS 页显式建的，
        这台机器可能还给别的用途挂着域名。"""
        server_id = self.add_server("香港-主力")
        keepalive_id = repo.add_keepalive(server_id)
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        repo.delete_keepalive(keepalive_id)
        self.assertEqual(repo.dns_record_of(dns.TARGET_SERVER, server_id), "a.example.com")


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


class Regression(Base):
    """代码审查发现的问题，逐条钉住。"""

    def setUp(self) -> None:
        super().setUp()
        repo.set_settings({"ddns_records": '["a.example.com"]', "ddns_record": "a.example.com"})
        dns.sync_registry(self.config())

    def test_failed_sync_is_reported_as_error_state(self):
        """同步失败要显示「失败」，不能因为 IP 对不上就降级成「待同步」。

        失败路径故意保留上次成功的 IP（界面上要能看出实际指向哪），
        于是 last_ip 永远不等于目标 IP —— 先比 IP 就会把失败藏起来。
        """
        server_id = self.add_server("香港-主力", ip="47.243.10.10")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.sync_record("a.example.com", self.config())
        repo.update_server_runtime(server_id, public_ip="47.243.10.11")
        ddns.sync = _fake_sync_fail
        dns.sync_record("a.example.com", self.config())

        record = dns.snapshot(self.config())["records"][0]
        self.assertEqual(record["state"], dns.STATE_ERROR, record)
        self.assertEqual(record["last_status"], ddns.STATUS_UPDATE_FAILED)

    def test_success_clears_every_failure_flag(self):
        """成功的记录要把历史上所有失败标记都清掉，否则同一记录再失败就不报警。"""
        server_id = self.add_server("香港-主力", ip="47.243.10.10")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        ddns.sync = _fake_sync_fail
        dns.reconcile(self.config())
        dns.reconcile(self.config())
        self.assertEqual(self.error_events(), 1, "同一类失败不该每分钟刷一条")

        ddns.sync = _fake_sync
        dns.reconcile(self.config(), force=True)
        ddns.sync = _fake_sync_fail
        dns.reconcile(self.config(), force=True)
        self.assertEqual(self.error_events(), 2, "恢复过一次之后再次失败应该重新提醒")

    def test_mixed_status_is_not_success(self):
        """「✅ 已同步 / ❌ 更新失败」开头是 ✅，但整体不算成功。"""
        self.assertFalse(dns.is_success("✅ 已同步 / ❌ 更新失败"))
        self.assertFalse(dns.is_success("✅ 已同步 / ⚠️ IP 无效跳过"))
        self.assertTrue(dns.is_success("✅ 已同步（2 条）"))
        self.assertFalse(dns.is_success(""))

    def test_transient_empty_ip_keeps_previous(self):
        """阿里云瞬时返回空 IP 时，别把库里已存的公网 IP 抹掉。"""
        server_id = self.add_server("香港-主力", ip="47.243.10.10")
        repo.update_server_runtime(server_id, status="Running", public_ip="")
        self.assertEqual(
            (repo.get_server(server_id) or {}).get("public_ip"),
            "47.243.10.10",
            "运行中查不到 IP 多半是接口抖动，先留着上一轮读数",
        )
        repo.update_server_runtime(server_id, status="Stopped", public_ip="")
        self.assertEqual(
            (repo.get_server(server_id) or {}).get("public_ip"), "", "停机后 IP 确实释放了"
        )

    def error_events(self) -> int:
        return len([e for e in repo.list_events(limit=100) if e["kind"] == "error"])


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


class SyncTarget(Base):
    """一台机器/一个换班组挂多条域名时，开机要把它们一次全同步掉。"""

    def setUp(self) -> None:
        super().setUp()
        repo.set_settings({"ddns_records": '["a.example.com","b.example.com"]'})
        dns.sync_registry(self.config())

    def test_syncs_every_record_of_a_target(self):
        server_id = self.add_server("香港-主力", ip="47.243.10.10")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.bind("b.example.com", dns.TARGET_SERVER, server_id)
        names, statuses = dns.sync_target(dns.TARGET_SERVER, server_id, self.config())
        self.assertEqual(names, ["a.example.com", "b.example.com"])
        self.assertEqual(dns.combine_status(statuses), "✅ 已同步（2 条）")
        self.assertEqual(
            CALLS,
            [("a.example.com", "47.243.10.10"), ("b.example.com", "47.243.10.10")],
        )

    def test_no_record_means_unbound(self):
        server_id = self.add_server("香港-主力")
        names, statuses = dns.sync_target(dns.TARGET_SERVER, server_id, self.config())
        self.assertEqual(names, [])
        self.assertEqual(dns.combine_status(statuses), dns.STATUS_UNBOUND)
        self.assertEqual(CALLS, [])

    def test_failure_is_not_hidden_by_success(self):
        server_id = self.add_server("香港-主力")
        dns.bind("a.example.com", dns.TARGET_SERVER, server_id)
        dns.bind("b.example.com", dns.TARGET_SERVER, server_id)

        def mixed(ip, config, force=False, record=None):  # noqa: ANN001, ARG001
            if record == "b.example.com":
                return _fake_sync_fail(ip, config, force, record)
            return _fake_sync(ip, config, force, record)

        ddns.sync = mixed
        _names, statuses = dns.sync_target(dns.TARGET_SERVER, server_id, self.config())
        # 一条成功一条失败，不能汇成一句「已同步」
        self.assertEqual(dns.combine_status(statuses), "✅ 已同步 / ❌ 更新失败")


class DutyDdns(Base):
    """换班轮到某台机器时，换班组绑的域名 + 这台机器自己绑的域名都要指过去。"""

    def setUp(self) -> None:
        super().setUp()
        repo.set_settings({"ddns_records": '["group.example.com","own.example.com"]'})
        dns.sync_registry(self.config())

    def test_duty_syncs_group_records_and_its_own(self):
        from app import shift

        a = self.add_server("香港-A", ip="47.243.10.21")
        b = self.add_server("香港-B", status="Stopped", ip="47.243.10.22")
        group_id = self.add_group("双机轮换", [a, b])
        dns.bind("group.example.com", dns.TARGET_GROUP, group_id)
        dns.bind("own.example.com", dns.TARGET_SERVER, a)

        server = repo.get_server(a, decrypt_secret=True) or {}
        status = shift.engine._sync_ddns(server, self.config(), dns.TARGET_GROUP, group_id)

        self.assertEqual(status, "✅ 已同步（2 条）")
        self.assertEqual(
            sorted(CALLS),
            [("group.example.com", "47.243.10.21"), ("own.example.com", "47.243.10.21")],
        )

    def test_explicit_ip_overrides_stale_snapshot(self):
        """调用方拿到实时 IP 时，不能用库里可能为空的旧快照。

        抢占恢复的确认轮询里，server 是轮询开始前取的快照：实例刚停机时
        public_ip 是空的，拿它去同步只会得到「⚠️ IP 无效跳过」。
        """
        from app import shift

        a = self.add_server("香港-A", ip="47.243.10.21")
        b = self.add_server("香港-B", status="Stopped", ip="47.243.10.22")
        group_id = self.add_group("双机轮换", [a, b])
        dns.bind("group.example.com", dns.TARGET_GROUP, group_id)

        stale = {**(repo.get_server(a, decrypt_secret=True) or {}), "public_ip": ""}
        status = shift.engine._sync_ddns(
            stale, self.config(), dns.TARGET_GROUP, group_id, ip="47.243.10.99"
        )
        self.assertEqual(status, "✅ 已同步")
        self.assertEqual(CALLS, [("group.example.com", "47.243.10.99")])

    def test_duty_without_any_binding_is_quiet(self):
        from app import shift

        a = self.add_server("香港-A", ip="47.243.10.21")
        b = self.add_server("香港-B", status="Stopped", ip="47.243.10.22")
        group_id = self.add_group("双机轮换", [a, b])
        server = repo.get_server(a, decrypt_secret=True) or {}
        status = shift.engine._sync_ddns(server, self.config(), dns.TARGET_GROUP, group_id)
        self.assertEqual(status, dns.STATUS_UNBOUND)
        self.assertEqual(CALLS, [])


class Cards(Base):
    """服务器卡片上的域名框：换班组绑定的域名只挂在「此刻当班」那台的卡上。"""

    def setUp(self) -> None:
        super().setUp()
        repo.set_settings({"ddns_records": '["a.example.com","b.example.com"]'})
        dns.sync_registry(self.config())

    def add_pair(self) -> tuple[int, int, int]:
        a = self.add_server("香港-A", ip="47.243.10.21")
        b = self.add_server("香港-B", status="Stopped", ip="47.243.10.22")
        group_id = self.add_group("双机轮换", [a, b])
        return a, b, group_id

    def test_group_binding_shows_only_on_duty_card(self):
        a, b, group_id = self.add_pair()
        dns.bind("a.example.com", dns.TARGET_GROUP, group_id)

        cards = dns.books_for_cards(time.time())
        self.assertEqual(cards.get(a), ["a.example.com"])
        # 非当班成员不该显示组域名，否则会被误认为它也在占着这条记录
        self.assertNotIn(b, cards)

    def test_server_binding_stays_on_its_own_card(self):
        a, b, group_id = self.add_pair()
        dns.bind("a.example.com", dns.TARGET_GROUP, group_id)
        dns.bind("b.example.com", dns.TARGET_SERVER, b)

        cards = dns.books_for_cards(time.time())
        # 当班机只有组域名，非当班机只有自己绑的那条，两边互不串味
        self.assertEqual(cards.get(a), ["a.example.com"])
        self.assertEqual(cards.get(b), ["b.example.com"])

    def test_duty_merges_own_and_group_records(self):
        a, _b, group_id = self.add_pair()
        dns.bind("a.example.com", dns.TARGET_GROUP, group_id)
        dns.bind("b.example.com", dns.TARGET_SERVER, a)

        cards = dns.books_for_cards(time.time())
        # 自己的记录在前，组绑定的接在后面
        self.assertEqual(cards.get(a), ["b.example.com", "a.example.com"])

    def test_failover_moves_the_chip_to_standby(self):
        a, b, group_id = self.add_pair()
        dns.bind("a.example.com", dns.TARGET_GROUP, group_id)

        from app import shift

        now_ts = time.time()
        group = repo.get_group(group_id) or {}
        slot = str(shift.duty_at(group, now_ts)[1])
        repo.upsert_run(group_id, slot, failover=1, duty_server_id=b)

        # 本班次已经故障转移到 B：域名框跟着实际在跑的机器走，跟界面高亮一致
        cards = dns.books_for_cards(now_ts)
        self.assertEqual(cards.get(b), ["a.example.com"])
        self.assertNotIn(a, cards)


def tearDownModule() -> None:
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
