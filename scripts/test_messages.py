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
    "region": "cn-hongkong",
    "quota_gb": 200,
    "traffic_used": 12.0,
    "instance_status": "Running",
    "stopped_mode": "Not-applicable",
}
B = {
    "label": "AliCloud-CDT-PM",
    "instance_id": "i-bbb222",
    "public_ip": "198.51.100.2",
    "region": "cn-hongkong",
    "quota_gb": 200,
    "traffic_used": 190.0,
    "instance_status": "Stopped",
    "stopped_mode": "StopCharging",
}
C = {
    "label": "第三台",
    "instance_id": "i-ccc333",
    "public_ip": "198.51.100.3",
    "region": "cn-hongkong",
    "quota_gb": 200,
    "traffic_used": 5.0,
    "instance_status": "Stopped",
    "stopped_mode": "StopCharging",
}


class ShiftDone(unittest.TestCase):
    def test_two_members_exactly_like_script(self):
        expected = (
            "✅ <b>CDT 换班完成报告</b>\n"
            "\n"
            f"🕒 <b>当前班次</b>：AM\n"
            f"{SEP}\n"
            "🟢 <b>运行账号 </b>\n"
            "┃ 🏷 别称：AliCloud-CDT-AM\n"
            "┃ 📦 实例：<code>i-aaa111</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ ☁️ DDNS：✅ 已同步\n"
            "┃ 🟢 状态：Running\n"
            f"{SEP}\n"
            "⚪ <b>停止账号 </b>\n"
            "┃ 🏷 别称：AliCloud-CDT-PM\n"
            "┃ 📦 实例：<code>i-bbb222</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.2\n"
            "┃ ⚪ 状态：Stopped (StopCharging)\n"
            f"{SEP}"
        )
        message = messages.shift_done(A, [B], "AM", "✅ 已同步")
        self.assertEqual(message.html, expected)
        self.assertEqual(message.title, "CDT 换班完成报告")

    def test_delayed_title(self):
        message = messages.shift_done(A, [B], "PM", "✅ 已同步", delayed=True)
        self.assertTrue(message.html.startswith("✅ <b>CDT 换班成功报告 (延迟恢复)</b>\n\n"))
        self.assertIn(f"🕒 <b>当前班次</b>：PM\n{SEP}", message.html)

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

    def test_period_marks_next_day(self):
        # 跨天班次写「次日」，否则 22:00–06:00 看着像时间倒流
        self.assertEqual(
            messages.shift_period("夜班", "22:00", "06:00"),
            "夜班（22:00–次日 06:00）",
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

    def test_no_usage_line_in_shift_report(self):
        # 脚本的换班报告里没有「已用」这一行，别被顺手加回来
        self.assertNotIn("已用", messages.shift_done(A, [B], "AM", "✅ 已同步").html)

    def test_group_name_in_title(self):
        # 标题插入组名；单机保活等无组名场景保持脚本原样
        message = messages.shift_done(A, [B], "AM", "✅ 已同步", group_name="主力组")
        self.assertTrue(message.html.startswith("✅ <b>CDT 主力组 换班完成报告</b>\n\n"))
        self.assertEqual(message.title, "CDT 主力组 换班完成报告")
        # 不传组名时保持原样
        plain = messages.shift_done(A, [B], "AM", "✅ 已同步")
        self.assertEqual(plain.title, "CDT 换班完成报告")


class DdnsStatus(unittest.TestCase):
    """域名没绑定时通知里也要如实写，不能悄悄显示成「已同步」。"""

    def test_unbound_is_shown_verbatim(self):
        message = messages.shift_done(A, [B], "AM", "⏸ 未绑定")
        self.assertIn("┃ ☁️ DDNS：⏸ 未绑定", message.html)

    def test_disabled_is_shown_verbatim(self):
        message = messages.shift_done(A, [B], "AM", "⏸ DDNS 未启用")
        self.assertIn("┃ ☁️ DDNS：⏸ DDNS 未启用", message.html)


class ShiftFailover(unittest.TestCase):
    def test_two_members_exactly_like_script(self):
        expected = (
            "⚠️ <b>CDT 临时换班报告</b>\n"
            "\n"
            f"📢 <b>故障转移</b>：当班实例流量超限，已启用备用实例。\n"
            f"{SEP}\n"
            "🟢 <b>接替账号 </b>\n"
            "┃ 🏷 别称：AliCloud-CDT-PM\n"
            "┃ 📦 实例：<code>i-bbb222</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.2\n"
            "┃ ☁️ DDNS：✅ 已同步\n"
            "┃ 🟢 状态：Running\n"
            f"{SEP}\n"
            "⚪ <b>超限账号 </b>\n"
            "┃ 🏷 别称：AliCloud-CDT-AM\n"
            "┃ 📦 实例：<code>i-aaa111</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ 📈 已用：190.00 GB\n"
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


class Breaker(unittest.TestCase):
    # 熔断发生时每台都已经停机，测试数据也按真实状态造，别拿 Running 的机器凑数
    OFF_A = {**A, "instance_status": "Stopped", "stopped_mode": "StopCharging"}
    OFF_C = {**C, "instance_status": "Stopped", "stopped_mode": "StopCharging"}

    def test_two_members_wording(self):
        message = messages.breaker([self.OFF_A, B])
        self.assertTrue(message.html.startswith("🚨 <b>CDT 全局熔断报告</b>\n\n"))
        self.assertIn("⚠ <b>警告：双实例流量均已到达到阈值，“全局熔断”，均已节省停机。</b>", message.html)
        self.assertIn("🖥 AliCloud-CDT-AM\n┃ 📦 实例：<code>i-aaa111</code>\n┃ 📈 已用：12.00 GB\n", message.html)

    def test_three_members_wording(self):
        message = messages.breaker([self.OFF_A, B, self.OFF_C])
        self.assertIn("3 台实例流量均已到达到阈值", message.html)
        self.assertEqual(message.html.count("🖥 "), 3)


class OtherReports(unittest.TestCase):
    def test_limit_stop(self):
        expected = (
            "🔴 <b>CDT 超限停机报告</b>\n"
            "\n"
            "当班实例流量超限，已强制节省停机。\n"
            "\n"
            "🖥 AliCloud-CDT-PM\n"
            "┃ 📦 实例：<code>i-bbb222</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.2\n"
            "┃ 🔴 状态：Stopped (StopCharging)\n"
            f"{SEP}"
        )
        self.assertEqual(messages.limit_stop(B).html, expected)

    def test_recovery(self):
        expected = (
            "🟢 <b>CDT 抢占恢复报告</b>\n"
            "\n"
            "当班实例疑被抢占，已自动恢复运行。\n"
            "\n"
            "🖥 AliCloud-CDT-AM\n"
            "┃ 📦 实例：<code>i-aaa111</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ ☁️ DDNS：✅ 已同步\n"
            "┃ 🟢 状态：Running\n"
            f"{SEP}"
        )
        self.assertEqual(messages.recovery(A, "✅ 已同步").html, expected)

    def test_keepalive_reuses_recovery_layout(self):
        message = messages.recovery(
            A, "⏸ DDNS 未启用", headline="实例疑被抢占，已自动恢复运行。", kind="keepalive"
        )
        self.assertTrue(message.html.startswith("🟢 <b>CDT 抢占恢复报告</b>\n\n实例疑被抢占"))
        self.assertEqual(message.kind, "keepalive")

    def test_shift_error(self):
        message = messages.shift_error(B, [A], 10)
        self.assertTrue(message.html.startswith("⚠️ <b>CDT 换班异常报告</b>\n\n"))
        self.assertIn("⏳ <b>换班超时：换班指令已执行 10 分钟，但状态未就绪。</b>", message.html)
        self.assertIn("🚩 <b>应当运行账号 </b>", message.html)
        self.assertIn("🚩 <b>应当停止账号 </b>", message.html)

    def test_daily_report(self):
        message = messages.daily_report([A, B], "2026-10-26 23:58")
        expected = (
            "📊 <b>CDT 每日流量报告</b>\n"
            "\n"
            "🖥 AliCloud-CDT-AM\n"
            "┃ 📦 实例：<code>i-aaa111</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.1\n"
            "┃ 📍 地域：cn-hongkong\n"
            "┃ 📈 已用：12.00 GB\n"
            "┃ 💾 剩余：188.00 GB\n"
            "┃ 🔥 使用率：6.00%  🟢 正常\n"
            "\n"
            "🖥 AliCloud-CDT-PM\n"
            "┃ 📦 实例：<code>i-bbb222</code>\n"
            "┃ 🌐 ＩＰ：198.51.100.2\n"
            "┃ 📍 地域：cn-hongkong\n"
            "┃ 📈 已用：190.00 GB\n"
            "┃ 💾 剩余：10.00 GB\n"
            "┃ 🔥 使用率：95.00%  🔴 危险\n"
            "\n"
            "⏰ 更新时间：2026-10-26 23:58"
        )
        self.assertEqual(message.html, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
