#!/usr/bin/env python3
"""回归测试：CDT 两个流量包（中国内地 20 GB / 非中国内地 200 GB）的整套口径。

背景：配额以前是用户自己填的一个数，默认 200 GB。现在改成「由实例所在地域决定」：
中国内地 20 GB、非中国内地（含中国香港）200 GB，阈值自由填但不得超过配额。
这个改动会一路影响流量保护、单机保活、联合换班、流量均衡、故障转移 —— 它们全靠
`threshold_gb` 判断，一旦配额/阈值被写错，结果就是「该停不停」（烧钱）或
「没到额度就停」（误停）。

这里锁死四件事：
  1. 地域 → 额度：cn- 开头且不是 cn-hongkong 的 20 GB，其余 200 GB；
     带可用区后缀的 cn-hongkong-b 必须是 200（判成 20 会误停香港机器）。
  2. 流量读数是「本池合计」：同池各地域相加（阿里按账号共享额度），
     国内 / 海外两个池互不串数；明细为空 / 地域缺失 / 分页没取全
     一律返回 None（当成 0 会把流量保护关掉）。
  3. 写入侧：配额不取用户输入、阈值不得超配额、编辑不带阈值时沿用原值、
     运行态回填地域时配额跟着变。
  4. 迁移：只修「阈值 > 配额」这种非法值，用户主动填的合法阈值不能每次启动都被压。

跑法：python scripts/test_traffic_pool.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cdt-traffic-pool-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "0"

from app import aliyun, db, messages, repo, settings  # noqa: E402

db.init_db()

GIB = 1024 ** 3


def _detail(region: str, gb: float) -> dict:
    return {"BusinessRegionId": region, "Traffic": gb * GIB}


class FakeClient:
    """按页返回预设响应，并记录每次调用是否带了 NextToken。"""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls = 0
        self.tokens: list[str] = []

    def do_action_with_exception(self, request):  # noqa: ANN001
        self.tokens.append(request.get_query_params().get("NextToken", ""))
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return json.dumps(page).encode("utf-8")


class PoolQuotaTest(unittest.TestCase):
    def test_region_to_quota(self) -> None:
        cases = {
            "cn-hongkong": 200.0,
            # 带可用区后缀：宁可判成海外。判成国内会把 200 砍成 20，香港机器会在
            # 远没到额度时被停机。
            "cn-hongkong-b": 200.0,
            "ap-southeast-1": 200.0,
            "us-west-1": 200.0,
            "cn-zhangjiakou": 20.0,
            "cn-beijing": 20.0,
            "cn-beijing-h": 20.0,
            "cn-shanghai": 20.0,
            "": 200.0,
        }
        for region, expect in cases.items():
            with self.subTest(region=region):
                self.assertEqual(aliyun.pool_quota_gb(region), expect)


class TrafficFilteringTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dry = settings.DRY_RUN
        settings.DRY_RUN = False

    def tearDown(self) -> None:
        settings.DRY_RUN = self._dry

    def _use(self, pages: list[dict]) -> FakeClient:
        client = FakeClient(pages)
        aliyun._client = lambda *_a, **_k: client  # type: ignore[assignment]
        return client

    def test_instances_read_their_own_pool_only(self) -> None:
        """同账号国内 + 海外并存：各读各的池，绝不互相串数。"""
        self._use(
            [
                {
                    "TrafficDetails": [
                        _detail("cn-hongkong", 163.77),
                        _detail("ap-southeast-1", 12.0),
                        _detail("cn-zhangjiakou", 9.41),
                        _detail("cn-beijing", 5.0),
                    ]
                }
            ]
        )
        # 海外包合计 163.77 + 12.0；国内包合计 9.41 + 5.0
        self.assertAlmostEqual(aliyun.get_traffic_gb({"region_id": "cn-hongkong"}), 175.77)
        self.assertAlmostEqual(aliyun.get_traffic_gb({"region_id": "ap-southeast-1"}), 175.77)
        self.assertAlmostEqual(aliyun.get_traffic_gb({"region_id": "cn-zhangjiakou"}), 14.41)
        self.assertAlmostEqual(aliyun.get_traffic_gb({"region_id": "cn-beijing"}), 14.41)

    def test_same_pool_regions_are_summed(self) -> None:
        """同包跨地域必须相加：16 + 5 = 21 已超 20，只看本地域会漏判。"""
        self._use(
            [
                {
                    "TrafficDetails": [
                        _detail("cn-zhangjiakou", 16.0),
                        _detail("cn-beijing", 5.0),
                    ]
                }
            ]
        )
        self.assertAlmostEqual(aliyun.get_traffic_gb({"region_id": "cn-zhangjiakou"}), 21.0)
        self.assertGreater(aliyun.get_traffic_gb({"region_id": "cn-beijing"}) or 0, 20.0)

    def test_zone_suffix_uses_pool_not_exact_match(self) -> None:
        """库里存成可用区（cn-hongkong-b）也要读到香港那条：精确匹配会读出 0。"""
        self._use([{"TrafficDetails": [_detail("cn-hongkong", 163.77)]}])
        self.assertAlmostEqual(aliyun.get_traffic_gb({"region_id": "cn-hongkong-b"}), 163.77)

    def test_missing_region_is_none_not_all_pools(self) -> None:
        """地域缺失分不清池：全加会把 20 GB 的机器按 220 GB 判，宁可当读数不可用。"""
        self._use([{"TrafficDetails": [_detail("cn-hongkong", 20), _detail("cn-beijing", 5)]}])
        self.assertIsNone(aliyun.get_traffic_gb({"region_id": ""}))
        self.assertIsNone(aliyun.get_traffic_gb({}))

    def test_region_withzero_usage_is_zero_not_none(self) -> None:
        """账号有量、本池没量：是 0（本月没跑），不是「读不到」。"""
        self._use([{"TrafficDetails": [_detail("cn-hongkong", 20)]}])
        self.assertEqual(aliyun.get_traffic_gb({"region_id": "cn-beijing"}), 0.0)

    def test_empty_details_is_none(self) -> None:
        self._use([{"TrafficDetails": []}])
        self.assertIsNone(aliyun.get_traffic_gb({"region_id": "cn-hongkong"}))

    def test_follows_next_token(self) -> None:
        client = self._use(
            [
                {"TrafficDetails": [_detail("cn-hongkong", 10)], "NextToken": "t1"},
                {"TrafficDetails": [_detail("cn-zhangjiakou", 5)]},
            ]
        )
        self.assertAlmostEqual(aliyun.get_traffic_gb({"region_id": "cn-zhangjiakou"}), 5.0)
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.tokens, ["", "t1"])

    def test_incomplete_pagination_is_none_not_zero(self) -> None:
        """5 页都没取全、又没看到本池地域：不能当成 0，否则保护失效。"""
        self._use([{"TrafficDetails": [_detail("cn-hongkong", 1)], "NextToken": "loop"}])
        self.assertIsNone(aliyun.get_traffic_gb({"region_id": "cn-beijing"}))

    def test_nested_data_payload(self) -> None:
        self._use([{"Data": {"TrafficDetails": [_detail("cn-beijing", 3)]}}])
        self.assertAlmostEqual(aliyun.get_traffic_gb({"region_id": "cn-beijing"}), 3.0)


def _payload(**overrides) -> dict:
    payload = {
        "label": "池测试",
        "access_key_id": "LTAI5tFakeKeyForTest",
        "access_key_secret": "fake-secret-for-test",
        "region_id": "cn-hongkong",
        "site": "china",
        "instance_id": "i-pool",
    }
    payload.update(overrides)
    return payload


class ServerQuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        db.execute("DELETE FROM servers")
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('traffic_threshold_pct','90')")

    def _row(self, server_id: int) -> dict:
        return db.query_one(
            "SELECT region_id, quota_gb, threshold_gb FROM servers WHERE id=?", (server_id,)
        )

    def test_new_server_gets_pool_quota_and_default_threshold(self) -> None:
        hk = repo.upsert_server(_payload(region_id="cn-hongkong", instance_id="i-hk"))
        zj = repo.upsert_server(_payload(region_id="cn-zhangjiakou", instance_id="i-zj"))
        self.assertEqual(self._row(hk)["quota_gb"], 200.0)
        self.assertEqual(self._row(hk)["threshold_gb"], 180.0)  # 90%
        self.assertEqual(self._row(zj)["quota_gb"], 20.0)
        self.assertEqual(self._row(zj)["threshold_gb"], 18.0)

    def test_quota_from_payload_is_ignored(self) -> None:
        sid = repo.upsert_server(
            _payload(region_id="cn-zhangjiakou", instance_id="i-dom", quota_gb=200, threshold_gb=20)
        )
        self.assertEqual(self._row(sid)["quota_gb"], 20.0)

    def test_threshold_above_quota_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            repo.upsert_server(
                _payload(region_id="cn-beijing", instance_id="i-bad", threshold_gb=25)
            )
        # 等于配额是允许的（用户想把保护压到最后一兆）
        sid = repo.upsert_server(
            _payload(region_id="cn-beijing", instance_id="i-eq", threshold_gb=20)
        )
        self.assertEqual(self._row(sid)["threshold_gb"], 20.0)

    def test_update_without_threshold_keeps_it(self) -> None:
        sid = repo.upsert_server(
            _payload(region_id="cn-hongkong", instance_id="i-keep", threshold_gb=200)
        )
        repo.upsert_server(_payload(id=sid, label="改名", region_id="cn-hongkong", instance_id="i-keep"))
        self.assertEqual(self._row(sid)["threshold_gb"], 200.0)
        self.assertEqual(self._row(sid)["quota_gb"], 200.0)

    def test_region_change_recomputes_when_over_quota(self) -> None:
        sid = repo.upsert_server(
            _payload(region_id="cn-hongkong", instance_id="i-move", threshold_gb=180)
        )
        repo.upsert_server(
            _payload(id=sid, region_id="cn-shanghai", instance_id="i-move")
        )
        row = self._row(sid)
        self.assertEqual(row["quota_gb"], 20.0)
        self.assertEqual(row["threshold_gb"], 18.0)

    def test_runtime_region_backfill_updates_quota(self) -> None:
        sid = repo.upsert_server(
            _payload(region_id="cn-hongkong", instance_id="i-rt", threshold_gb=180)
        )
        repo.update_server_runtime(sid, region_id="cn-zhangjiakou")
        row = self._row(sid)
        self.assertEqual(row["quota_gb"], 20.0)
        self.assertEqual(row["threshold_gb"], 18.0)
        # 回到海外：配额回 200，阈值 18 是合法值，不该被改成 180
        repo.update_server_runtime(sid, region_id="cn-hongkong")
        row = self._row(sid)
        self.assertEqual(row["quota_gb"], 200.0)
        self.assertEqual(row["threshold_gb"], 18.0)

    def test_migration_keeps_legal_thresholds(self) -> None:
        """迁移只修「阈值 > 配额」，否则用户主动填的 190 / 200 每次重启都会被压到 180。"""
        db.execute(
            "INSERT INTO servers(label,access_key_id,access_key_secret,region_id,site,instance_id,"
            "quota_gb,threshold_gb,stop_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,0)",
            ("满额", "LTAIfake", "x", "cn-hongkong", "china", "i-max", 200, 200, "StopCharging"),
        )
        db.execute(
            "INSERT INTO servers(label,access_key_id,access_key_secret,region_id,site,instance_id,"
            "quota_gb,threshold_gb,stop_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,0)",
            ("老国内", "LTAIfake", "x", "cn-zhangjiakou", "china", "i-old", 20, 180, "StopCharging"),
        )
        # 香港可用区：后缀带 -b，迁移不能把它当 `cn-` 国内包压成 20/18
        db.execute(
            "INSERT INTO servers(label,access_key_id,access_key_secret,region_id,site,instance_id,"
            "quota_gb,threshold_gb,stop_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,0)",
            ("香港可用区", "LTAIfake", "x", "cn-hongkong-b", "china", "i-hkz", 200, 180, "StopCharging"),
        )
        db._migrate(db.connect())
        rows = {
            r["instance_id"]: (r["quota_gb"], r["threshold_gb"])
            for r in db.query("SELECT instance_id, quota_gb, threshold_gb FROM servers")
        }
        self.assertEqual(rows["i-max"], (200.0, 200.0))
        self.assertEqual(rows["i-old"], (20.0, 18.0))
        self.assertEqual(rows["i-hkz"], (200.0, 180.0))


class NotificationTest(unittest.TestCase):
    def test_report_block_uses_pool_when_quota_missing(self) -> None:
        server = {
            "label": "国内机",
            "instance_id": "i-dom",
            "region_id": "cn-zhangjiakou",
            "traffic_used": 9.41,
            "quota_gb": None,
            "threshold_gb": 18,
        }
        block = messages.report_block(server)
        # 9.41 / 20 = 47.05%，按 200 算会变成 4.71%
        self.assertIn("使用率：47.05%", block)
        self.assertIn("剩余：10.59 GB", block)

    def test_limit_notification_follows_pool(self) -> None:
        """超限通知/事件文案跟着地域走：国内按 20 GB 口径，海外按 200 GB。"""
        base = {
            "instance_id": "i-1",
            "public_ip": "1.2.3.4",
            "instance_status": "Stopped",
            "stopped_mode": "StopCharging",
        }
        domestic = messages.over_limit(
            {**base, "label": "国内机", "region_id": "cn-zhangjiakou",
             "traffic_used": 19.0, "quota_gb": 20, "threshold_gb": 18},
            messages.SCOPE_KEEPALIVE,
            "StopCharging",
        )
        self.assertIn("已用：19.00 GB（阈值 18 GB）", domestic.text)
        self.assertNotIn("200", domestic.text)

        overseas = messages.over_limit(
            {**base, "label": "海外机", "region_id": "cn-hongkong",
             "traffic_used": 199.0, "quota_gb": 200, "threshold_gb": 180},
            messages.SCOPE_KEEPALIVE,
            "StopCharging",
        )
        self.assertIn("已用：199.00 GB（阈值 180 GB）", overseas.text)
        self.assertIn("剩余：1.00 GB", messages.report_block(
            {**base, "label": "海外机", "region_id": "cn-hongkong",
             "traffic_used": 199.0, "quota_gb": 200, "threshold_gb": 180}
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
