#!/usr/bin/env python3
"""通知文案的单元测试。

需求是「标题 / 内容 / 图标严格照 cdt.py」，那种「文案被顺手优化掉一个字」的回归
最容易漏，所以这里把脚本原文抄成期望值逐字比对；只有「成员多于两台」的分支
才按适配后的结构断言（多块、当前班次带机名）。

跑法：python scripts/test_messages.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import messages  # noqa: E402

SEP = "━━━━━━━━━━━━━━━"

A = {
    "label": "AliCloud-CDT-AM",
    "instance_id": "i-aaa111",
    "public_ip": "198.51.100.1",
    "region_id": "cn-hongkong",
    "quota_gb": 200,
    "threshold_gb": 188,
    "traffic_used": 12.0,
    "instance_status": "Running",
    "stopped_mode": "Not-applicable",
}
B = {
    "label": "AliCloud-CDT-PM",
    "instance_id": "i-bbb222",
    "public_ip": "198.51.100.2",
    "region_id": "cn-hongkong",
    "quota_gb": 200,
    "threshold_gb": 188,
    "traffic_used": 190.0,
    "instance_status": "Stopped",
    "stopped_mode": "StopCharging",
}
C = {
    "label": "第三台",
    "instance_id": "i-ccc333",
    "public_ip": "198.51.100.3",
    "region_id": "cn-hongkong",
    "quota_gb": 200,
    "threshold_gb": 188,
    "traffic_used": 5.0,
    "instance_status": "Stopped",
    "stopped_mode": "StopCharging",
}


class ShiftDone(unittest.TestCase):
    def test_two_members_exactly_like_script(self):
        expected = (
            "✅ <b>CDT 换班完成报告（换班组）</b>\n"
            "\n"
            f"🕒 <b>当前班次</b>：AM\n"
            f"{SEP}\n"
            "🟢 <b>运行账号 </b>\n"
            "┃ 🏷 别名：AliCloud-CDT-AM\n"
            "┃ 📦 实例：<code>i-aaa111</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ 📍 地域：中国香港\n"
            "┃ ☁️ DDNS：✅ 已同步\n"
            "┃ 📈 流量：12.00 GB / 200GB\n"
            "┃ 🟢 状态：Running\n"
            f"{SEP}\n"
            "⚪ <b>停止账号 </b>\n"
            "┃ 🏷 别名：AliCloud-CDT-PM\n"
            "┃ 📦 实例：<code>i-bbb222</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.2\n"
            "┃ 📍 地域：中国香港\n"
            "┃ 📈 流量：190.00 GB / 200GB\n"
            "┃ ⚪ 状态：Stopped (StopCharging)\n"
            f"{SEP}"
        )
        message = messages.shift_done(A, [B], "AM", "✅ 已同步")
        self.assertEqual(message.html, expected)
        self.assertEqual(message.title, "CDT 换班完成报告（换班组）")

    def test_delayed_title(self):
        message = messages.shift_done(A, [B], "PM", "✅ 已同步", delayed=True)
        self.assertTrue(
            message.html.startswith("✅ <b>CDT 换班成功报告（换班组，延迟恢复）</b>\n\n")
        )
        self.assertIn(f"🕒 <b>当前班次</b>：PM\n{SEP}", message.html)

    def test_manual_switch_title(self):
        """手动换班的报告用独立事件名「手动换班完成」，正文与自动换班一致。"""
        message = messages.shift_done(A, [B], "AM", "✅ 已同步", group_name="港新联合", manual=True)
        self.assertEqual(message.title, "CDT 手动换班完成报告（换班组：港新联合）")
        self.assertTrue(
            message.html.startswith("✅ <b>CDT 手动换班完成报告（换班组：港新联合）</b>\n\n")
        )
        self.assertNotIn("故障转移", message.html)
        # 正文和自动换班完全一致，只是标题不同
        auto = messages.shift_done(A, [B], "AM", "✅ 已同步", group_name="港新联合")
        self.assertEqual(
            message.html.split("\n", 1)[1], auto.html.split("\n", 1)[1]
        )

    def test_three_members_adds_block_and_names_duty(self):
        period = messages.shift_period("白班", "00:00", "12:00")
        message = messages.shift_done(A, [B, C], period, "✅ 已同步")
        self.assertEqual(message.html.count("⚪ <b>停止账号 </b>"), 2)
        # 当前班次用「班次名称（时间段）」，不再只写 AM/PM
        self.assertIn(
            f"🕒 <b>当前班次</b>：白班（00:00–12:00）\n", message.html
        )

    def test_period_with_slot_name(self):
        self.assertEqual(messages.shift_period("白班", "06:00", "18:00"), "白班（06:00–18:00）")
        self.assertEqual(messages.shift_period("", "06:00", "18:00"), "06:00–18:00")

    def test_period_shows_midnight_as_24(self):
        # 结束 00:00 是「当天到午夜」，显示成 24:00 才不会读成第二天凌晨
        self.assertEqual(
            messages.shift_period("白班", "12:00", "00:00"),
            "白班（12:00–24:00）",
        )
        # 起始就是 0 点的那一段照常写 00:00
        self.assertEqual(
            messages.shift_period("凌晨班", "00:00", "06:00"),
            "凌晨班（00:00–06:00）",
        )

    def test_stopped_block_uses_real_mode(self):
        # 普通停机照常收规格费，不能跟着脚本写死 StopCharging
        keep = {**B, "stopped_mode": "KeepCharging"}
        self.assertIn(
            "┃ ⚪ 状态：Stopped (KeepCharging)",
            messages.shift_done(A, [keep], "AM", "✅ 已同步").html,
        )

    def test_breaker_says_real_mode(self):
        keep = {**A, "instance_status": "Stopped", "stopped_mode": "KeepCharging"}
        # 两台模式不一致时不下结论，只说「均已停机」
        message = messages.breaker([keep, B])
        self.assertIn("，均已停机。", message.html)
        self.assertNotIn("均已节省停机", message.html)
        # 全是普通停机时不能跟着脚本说「均已节省停机」
        message = messages.breaker([keep, {**B, "stopped_mode": "KeepCharging"}])
        self.assertIn("均已普通停机", message.html)
        self.assertNotIn("均已节省停机", message.html)

    def test_stop_tail_unknown_mode_never_prints_dash(self):
        # _mode 读不到模式时给的是「-」，以前会拼出「均已-」这种车祸；
        # 有机器读不到就退成「均已停机」，不能替它猜「节省停机」
        unknown = {"label": "C1", "instance_id": "i-ccc333", "instance_status": "Stopped"}
        self.assertEqual(messages.stop_tail([unknown]), "均已停机")
        self.assertEqual(messages.stop_tail([{**unknown, "stopped_mode": ""}]), "均已停机")
        message = messages.breaker([{**unknown, "traffic_used": 200.0}])
        self.assertNotIn("均已-", message.html)
        self.assertIn("均已停机", message.html)

    def test_not_applicable_shown_as_normal_stop(self):
        # Not-applicable = 本实例不支持停机不收费，停机后照常收费，语义等同普通停机，
        # 不能原样漏成英文 Not-applicable
        na = {**B, "stopped_mode": "Not-applicable"}
        message = messages.shift_done(A, [na], "AM", "✅ 已同步")
        self.assertIn("┃ ⚪ 状态：Stopped (KeepCharging)", message.html)
        self.assertNotIn("Not-applicable", message.html)
        # 尾巴也要说「普通停机」，不能说「节省停机」
        message = messages.breaker([{**na, "traffic_used": 200.0}, {**na, "label": "C2"}])
        self.assertIn("均已普通停机", message.html)
        self.assertNotIn("均已节省停机", message.html)

    def test_traffic_line_before_status(self):
        """每个账号块都要有流量行，且固定落在「状态」上面（换班 / 均衡一致）。

        早期照抄脚本时换班报告是没有「已用」这一行的，加了流量行之后，读报告的人
        才能直接比出「这台还剩多少额度」。
        """
        html = messages.shift_done(A, [B], "AM", "✅ 已同步").html
        self.assertEqual(html.count("┃ 📈 流量："), 2)
        self.assertIn("┃ 📈 流量：12.00 GB / 200GB\n┃ 🟢 状态：Running", html)
        self.assertIn(
            "┃ 📈 流量：190.00 GB / 200GB\n┃ ⚪ 状态：Stopped (StopCharging)",
            html,
        )

    def test_traffic_line_without_quota_or_threshold(self):
        """没填额度 / 阈值时不留空壳，也不要写「额度 0 GB」。"""
        bare = {**A, "quota_gb": 0, "threshold_gb": 0}
        html = messages.shift_done(bare, [B], "AM", "✅ 已同步").html
        # 只看运行账号那一块（停止账号 B 有自己的额度和阈值）
        block = html.split(SEP)[1]
        self.assertIn("┃ 📈 流量：12.00 GB\n", block)
        self.assertNotIn("额度", block)
        self.assertNotIn("（阈值", block)

    def test_traffic_line_unknown_usage(self):
        """流量还没查过时写「—」，不能写 0.00 GB 冒充「一点没用到」。"""
        html = messages.shift_done({**A, "traffic_used": None}, [B], "AM", "✅ 已同步").html
        self.assertIn("┃ 📈 流量：— / 200GB", html)

    def test_group_name_in_title(self):
        # 标题带组名 + 全角范围括号；不传组名时退回「（换班组）」
        message = messages.shift_done(A, [B], "AM", "✅ 已同步", group_name="主力组")
        self.assertTrue(
            message.html.startswith("✅ <b>CDT 换班完成报告（换班组：主力组）</b>\n\n")
        )
        self.assertEqual(message.title, "CDT 换班完成报告（换班组：主力组）")
        # 不传组名时退回通用范围
        plain = messages.shift_done(A, [B], "AM", "✅ 已同步")
        self.assertEqual(plain.title, "CDT 换班完成报告（换班组）")


class DdnsStatus(unittest.TestCase):
    """域名没绑定时通知里也要如实写，不能悄悄显示成「已同步」。"""

    def test_unbound_is_shown_verbatim(self):
        message = messages.shift_done(A, [B], "AM", "⏸ 未绑定")
        self.assertIn("┃ ☁️ DDNS：⏸ 未绑定", message.html)

    def test_disabled_is_shown_verbatim(self):
        message = messages.shift_done(A, [B], "AM", "⏸ 未启用")
        self.assertIn("┃ ☁️ DDNS：⏸ 未启用", message.html)


class ShiftFailover(unittest.TestCase):
    def test_two_members_exactly_like_script(self):
        expected = (
            "⚠️ <b>CDT 故障转移报告（换班组）</b>\n"
            "\n"
            f"📢 <b>故障转移</b>：当班实例流量超限，已启用备用实例。\n"
            f"{SEP}\n"
            "🟢 <b>接替账号 </b>\n"
            "┃ 🏷 别名：AliCloud-CDT-PM\n"
            "┃ 📦 实例：<code>i-bbb222</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.2\n"
            "┃ 📍 地域：中国香港\n"
            "┃ ☁️ DDNS：✅ 已同步\n"
            "┃ 📈 流量：190.00 GB / 200GB\n"
            "┃ 🟢 状态：Running\n"
            f"{SEP}\n"
            "⚪ <b>超限账号 </b>\n"
            "┃ 🏷 别名：AliCloud-CDT-AM\n"
            "┃ 📦 实例：<code>i-aaa111</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ 📍 地域：中国香港\n"
            "┃ 📈 流量：190.00 GB / 200GB\n"
            "┃ ⚪ 状态：Stopped (StopCharging)\n"
            f"{SEP}"
        )
        over = {
            **A,
            "traffic_used": 190.0,
            "instance_status": "Stopped",
            "stopped_mode": "StopCharging",
        }
        message = messages.shift_failover(B, [over], [], "✅ 已同步")
        self.assertEqual(message.html, expected)

    def test_three_members_add_standby_block(self):
        message = messages.shift_failover(B, [A], [C], "✅ 已同步")
        self.assertIn("⚪ <b>超限账号 </b>", message.html)
        self.assertIn("⚪ <b>停止账号 </b>", message.html)


class BalanceReports(unittest.TestCase):
    """均衡组报告：正文照换班报告写，只有标题和概览那行不同。

    换班完成 = 运行账号 + 停止账号；故障转移 = 接替账号 + 超限账号 + 停止账号。
    均衡报告用同一套块标题，不另造「接替 / 交班 / 待机」——同一件事两种叫法，
    读通知的人得先在脑子里对一遍。

    均衡组的成员 dict 来自 balance_members 联表（服务器 id 在 server_id 上），
    换班那边是 servers 行（id），两边的去重键不一样，这里按真实形状造数据。
    """

    DUTY = {
        **A,
        "id": 11,
        "server_id": 1,
        "label": "新加坡-均衡A",
        "public_ip": "198.51.100.11",
        "instance_status": "Running",
        "traffic_used": 12.0,
    }
    PREV = {
        **B,
        "id": 12,
        "server_id": 2,
        "label": "新加坡-均衡B",
        "public_ip": "198.51.100.12",
        "traffic_used": 190.0,
    }
    REST = {
        **C,
        "id": 13,
        "server_id": 3,
        "label": "新加坡-均衡C",
        "public_ip": "198.51.100.13",
        "traffic_used": 5.0,
    }

    def test_manual_switch_lists_every_member(self):
        message = messages.balance_switch(
            self.DUTY,
            [self.DUTY, self.PREV, self.REST],
            "✅ 已同步",
            group_name="新加坡",
            prev=self.PREV,
            manual=True,
        )
        html = message.html
        self.assertEqual(message.title, "CDT 手动均衡完成报告（均衡组：新加坡）")
        # 和换班完成报告同一套块标题：运行账号 1 块 + 停止账号 2 块
        self.assertIn("🟢 <b>运行账号 </b>", html)
        self.assertEqual(html.count("<b>运行账号 </b>"), 1)
        self.assertEqual(html.count("<b>停止账号 </b>"), 2)
        self.assertNotIn("接替账号", html)
        self.assertNotIn("交班账号", html)
        self.assertNotIn("待机账号", html)
        # 每块都带流量行，且在三块里各出现一次
        self.assertEqual(html.count("┃ 📈 流量："), 3)
        self.assertIn("┃ 📈 流量：12.00 GB / 200GB", html)
        self.assertIn("┃ 📈 流量：190.00 GB / 200GB", html)
        self.assertIn("┃ 📈 流量：5.00 GB / 200GB", html)

    def test_auto_switch_same_shape_as_manual(self):
        """自动 / 手动只差标题和概览那行，正文块完全一样。"""
        message = messages.balance_switch(
            self.DUTY, [self.DUTY, self.PREV], "✅ 已同步", group_name="新加坡", prev=self.PREV
        )
        self.assertEqual(message.title, "CDT 均衡完成报告（均衡组：新加坡）")
        self.assertIn("🟢 <b>运行账号 </b>", message.html)
        self.assertIn("⚪ <b>停止账号 </b>", message.html)
        self.assertNotIn("上一台", message.html)

    def test_switch_block_heads_match_shift_done(self):
        """换班完成报告有哪些块标题，均衡完成就有哪些（只允许标题和概览不同）。"""
        shift = messages.shift_done(A, [B], "AM", "✅ 已同步").html
        balance = messages.balance_switch(
            self.DUTY, [self.DUTY, self.PREV], "✅ 已同步", prev=self.PREV
        ).html
        for head in ("运行账号", "停止账号"):
            self.assertIn(f"<b>{head} </b>", shift)
            self.assertIn(f"<b>{head} </b>", balance)
        # 概览：换班写班次，均衡写为什么切
        self.assertIn("🕒 <b>当前班次</b>", shift)
        self.assertIn("📊 <b>流量均衡</b>", balance)
        # 每块的字段顺序也要一致（流量行在状态行上面）
        for html in (shift, balance):
            self.assertIn("┃ 📈 流量：", html)
            self.assertLess(html.index("┃ 📈 流量："), html.index("状态："))

    def test_member_listed_once_even_if_passed_twice(self):
        """members 传了全部成员、prev 又在里面，同一台不能列两遍。"""
        html = messages.balance_switch(
            self.DUTY,
            [self.DUTY, self.PREV, self.PREV],
            "✅ 已同步",
            prev=self.PREV,
        ).html
        self.assertEqual(html.count("新加坡-均衡B"), 1)

    def test_failover_lists_remaining_members(self):
        message = messages.balance_failover(
            self.DUTY,
            [self.PREV],
            [self.DUTY, self.PREV, self.REST],
            "✅ 已同步",
            group_name="新加坡",
            reason="traffic",
        )
        html = message.html
        self.assertEqual(message.title, "CDT 均衡转移报告（均衡组：新加坡）")
        self.assertIn("⚪ <b>超限账号 </b>", html)
        # 剩下的第三台也要出现，标成「停止账号」（与故障转移报告一致）
        self.assertIn("⚪ <b>停止账号 </b>", html)
        self.assertIn("新加坡-均衡C", html)
        self.assertNotIn("待机账号", html)
        self.assertEqual(html.count("┃ 📈 流量："), 3)

    def test_stall_says_offline(self):
        message = messages.balance_failover(
            self.DUTY, [self.PREV], [], "✅ 已同步", reason="stall"
        )
        self.assertIn("⚪ <b>离线账号 </b>", message.html)
        self.assertIn("疑被抢占", message.html)


class Breaker(unittest.TestCase):
    # 熔断发生时每台都已经停机，测试数据也按真实状态造，别拿 Running 的机器凑数
    OFF_A = {**A, "instance_status": "Stopped", "stopped_mode": "StopCharging"}
    OFF_C = {**C, "instance_status": "Stopped", "stopped_mode": "StopCharging"}

    def test_two_members_wording(self):
        message = messages.breaker([self.OFF_A, B])
        self.assertTrue(message.html.startswith("🚨 <b>CDT 全局熔断报告（换班组）</b>\n\n"))
        self.assertIn("⚠ <b>警告：双实例流量均已到达到阈值，“全局熔断”，均已节省停机。</b>", message.html)
        self.assertIn(
            "🖥 AliCloud-CDT-AM\n┃ 📦 实例：<code>i-aaa111</code>\n┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ 📍 地域：中国香港\n┃ 📈 已用：12.00 GB（阈值 188 GB）\n",
            message.html,
        )

    def test_three_members_wording(self):
        message = messages.breaker([self.OFF_A, B, self.OFF_C])
        self.assertIn("3 台实例流量均已到达到阈值", message.html)
        self.assertEqual(message.html.count("🖥 "), 3)

    def test_each_block_shows_its_own_threshold(self):
        """阈值是每台机器自己在设置里填的，不能拿第一台的或组里统一值套。"""
        html = messages.breaker(
            [{**self.OFF_A, "threshold_gb": 180}, {**B, "threshold_gb": 150}]
        ).html
        self.assertIn("┃ 📈 已用：12.00 GB（阈值 180 GB）", html)
        self.assertIn("┃ 📈 已用：190.00 GB（阈值 150 GB）", html)

    def test_missing_threshold_omits_parenthesis(self):
        """没设阈值就别写「阈值 0 GB」，那比不写更误导。"""
        html = messages.breaker([{**B, "threshold_gb": 0}]).html
        self.assertIn("┃ 📈 已用：190.00 GB\n", html)
        self.assertNotIn("（阈值", html)

    def test_block_has_ip_between_instance_and_region(self):
        """块内字段顺序与其它报告一致：实例 → ＩＰ → 地域 → 已用 → 状态。"""
        block = messages.breaker([B]).html.split("🖥 ")[1]
        order = [block.index(k) for k in ("📦 实例", "🌐 ＩＰ", "📍 地域", "📈 已用", "⚪ 状态")]
        self.assertEqual(order, sorted(order))


class OtherReports(unittest.TestCase):
    def test_limit_stop(self):
        expected = (
            "🚨 <b>CDT 全局熔断报告（换班组）</b>\n"
            "\n"
            "当班实例流量超限，该换班组没有启用故障转移，已强制节省停机。\n"
            "如有需要，请开启故障转移或者去管理后台手动点击“立即换班”。\n"
            "\n"
            "🖥 AliCloud-CDT-PM\n"
            "┃ 📦 实例：<code>i-bbb222</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.2\n"
            "┃ 📍 地域：中国香港\n"
            "┃ 📈 已用：190.00 GB（阈值 188 GB）\n"
            "┃ ⚪ 状态：Stopped (StopCharging)\n"
            f"{SEP}"
        )
        self.assertEqual(messages.limit_stop(B).html, expected)

    def test_over_limit_keepalive(self):
        message = messages.over_limit(B, messages.SCOPE_KEEPALIVE)
        self.assertEqual(message.title, "CDT 超限停机报告（单机保活）")
        self.assertTrue(
            message.html.startswith("🚨 <b>CDT 超限停机报告（单机保活）</b>\n\n")
        )
        # 停机模式必须写出来：只写「已暂停保活」看不出这台是省钱停了还是照常计费
        self.assertIn("已节省停机，并暂停保活", message.html)
        self.assertIn("┃ 📈 已用：190.00 GB（阈值 188 GB）", message.html)

    def test_over_limit_idle(self):
        message = messages.over_limit(B, messages.SCOPE_IDLE)
        self.assertEqual(message.title, "CDT 超限停机报告（闲散手动）")
        self.assertIn("已节省停机", message.html)
        self.assertNotIn("已暂停保活", message.html)

    def test_recovery(self):
        expected = (
            "🟢 <b>CDT 抢占恢复报告（换班组）</b>\n"
            "\n"
            "当班实例疑被抢占，已自动恢复运行。\n"
            "\n"
            "🖥 AliCloud-CDT-AM\n"
            "┃ 📦 实例：<code>i-aaa111</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ 📍 地域：中国香港\n"
            "┃ ☁️ DDNS：✅ 已同步\n"
            "┃ 🟢 状态：Running\n"
            f"{SEP}"
        )
        self.assertEqual(messages.recovery(A, "✅ 已同步").html, expected)

    def test_keepalive_reuses_recovery_layout(self):
        message = messages.recovery(
            A, "⏸ 未启用", headline="实例疑被抢占，已自动恢复运行。", kind="keepalive"
        )
        # 单机保活标题后面挂「（单机保活）」，换班组是「（换班组：组名）」
        self.assertTrue(
            message.html.startswith(
                "🟢 <b>CDT 抢占恢复报告（单机保活）</b>\n\n实例疑被抢占"
            )
        )
        self.assertEqual(message.kind, "keepalive")
        self.assertEqual(message.title, "CDT 抢占恢复报告（单机保活）")
        self.assertNotIn("当班实例", message.html)

    def test_recovery_group_name(self):
        message = messages.recovery(A, "✅ 已同步", group_name="港新联合")
        self.assertEqual(message.title, "CDT 抢占恢复报告（换班组：港新联合）")

    def test_shift_error(self):
        # duty=B 是 Stopped（该运行没运行 → ⚪），stopped=[A] 是 Running（该停没停 → 🟢）
        message = messages.shift_error(B, [A], 10, ddns_status="⏸ 目标未运行")
        self.assertTrue(message.html.startswith("⚠️ <b>CDT 换班异常报告（换班组）</b>\n\n"))
        self.assertIn("⏳ <b>换班超时：换班指令已执行 10 分钟，但状态未就绪。</b>", message.html)
        self.assertIn("🚩 <b>应当运行账号 </b>", message.html)
        self.assertIn("🚩 <b>应当停止账号 </b>", message.html)
        # 状态图标跟着真实状态走，不再写死 🚧
        self.assertIn("┃ ⚪ 状态：Stopped (StopCharging)", message.html)
        self.assertIn("┃ 🟢 状态：Running", message.html)
        # 域名的状态行：其它换班报告都有，异常报告同样要有（只挂在应当运行那台）
        self.assertIn("┃ ☁️ DDNS：⏸ 目标未运行", message.html)

    def test_error_reports_have_traffic_line(self):
        """异常报告的账号块也要有流量行：卡住时用户要同时看「用掉多少」判断还能撑多久。"""
        shift = messages.shift_error(B, [A], 10).html
        self.assertEqual(shift.count("┃ 📈 流量："), 2)
        balance = messages.balance_error(B, 10).html
        self.assertEqual(balance.count("┃ 📈 流量："), 1)

    def test_every_notification_has_region_line(self):
        # 所有通知的实例块都要跟每日报告一样带「地域」行（IP 下面一行；熔断块没有 IP 行，
        # 就跟在实例行后面）
        cases = {
            "换班完成": messages.shift_done(A, [B], "AM", "✅ 已同步"),
            "故障转移": messages.shift_failover(B, [A], [], "✅ 已同步"),
            "全局熔断": messages.breaker([{**A, "instance_status": "Stopped"}, B]),
            "全局熔断（单台）": messages.limit_stop(B),
            "抢占恢复": messages.recovery(A, "✅ 已同步"),
            "换班异常": messages.shift_error(B, [A], 10),
        }
        for name, message in cases.items():
            with self.subTest(kind=name):
                self.assertIn("┃ 📍 地域：中国香港", message.html)
        # 地域缺失时给「-」，不能留空
        bare = messages.recovery({k: v for k, v in A.items() if k != "region_id"}, "✅ 已同步")
        self.assertIn("┃ 📍 地域：-", bare.html)

    def test_region_code_is_translated(self):
        # 阿里云国际站的 RegionId 是 ap-northeast-1 这类代码，通知里要翻成人话，
        # 不能把代码直接摆给用户看（前端卡片用的是同一张映射表）
        cases = {
            "ap-northeast-1": "日本（东京）",
            "cn-hongkong": "中国香港",
            "cn-wulanchabu": "华北 6（乌兰察布）",
            "cn-heyuan": "华南 2（河源）",
            "ap-southeast-8": "马来西亚（柔佛）",
            "na-south-1": "墨西哥",
            "sa-east-1": "巴西（圣保罗）",
            "me-central-1": "沙特阿拉伯（利雅得）",
        }
        for code, label in cases.items():
            with self.subTest(region=code):
                message = messages.recovery({**A, "region_id": code}, "✅ 已同步")
                self.assertIn(f"┃ 📍 地域：{label}", message.html)
        # 映射表里没有的代码原样显示，不猜测
        unknown = {**A, "region_id": "xx-made-up-1"}
        self.assertIn(
            "┃ 📍 地域：xx-made-up-1", messages.recovery(unknown, "✅ 已同步").html
        )

    def test_daily_report(self):
        a = {
            **A,
            "site": "china",
            "monthly_cost": 12.34,
            "balance_amount": 1234.56,
            "billing_currency": "CNY",
        }
        b = {
            **B,
            "site": "international",
            "monthly_cost": 3.21,
            # 国际站后付费账号 AvailableAmount 恒 0，直接显示 $0.00，不再写「后付费」
            "balance_amount": 0.0,
            "billing_currency": "USD",
        }
        message = messages.daily_report([a, b], "2026-10-26 23:58")
        expected = (
            "📊 <b>CDT-Management 每日报告</b>\n"
            "\n"
            "🖥 AliCloud-CDT-AM\n"
            "┃ 📦 实例：<code>i-aaa111</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ 📍 地域：中国香港\n"
            "┃ 💰 费用：¥12.34\n"
            "┃ 💳 余额：¥1234.56\n"
            "┃ 📈 流量：12.00 GB\n"
            "┃ 💾 剩余：188.00 GB\n"
            "┃ 🔥 使用率：6.00%  🟢 正常\n"
            "\n"
            "🖥 AliCloud-CDT-PM\n"
            "┃ 📦 实例：<code>i-bbb222</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.2\n"
            "┃ 📍 地域：中国香港\n"
            "┃ 💰 费用：$3.21\n"
            "┃ 💳 余额：$0.00\n"
            "┃ 📈 流量：190.00 GB\n"
            "┃ 💾 剩余：10.00 GB\n"
            "┃ 🔥 使用率：95.00%  🔴 危险\n"
            "\n"
            "⏰ 更新时间：2026-10-26 23:58"
        )
        self.assertEqual(message.html, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
