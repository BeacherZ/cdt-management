#!/usr/bin/env python3
"""通知文案的单元测试。

需求是「标题 / 内容 / 图标严格照 cdt.py」，那种「文案被顺手优化掉一个字」的回归
最容易漏，所以这里把脚本原文抄成期望值逐字比对；只有「成员多于两台」的分支
才按适配后的结构断言（多块、当前班次带机名）。

跑法：python scripts/test_messages.py
"""

from __future__ import annotations

import datetime as dt
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
        period = messages.shift_period(
            A["label"], 0, 3, dt.timezone(dt.timedelta(hours=8))
        )
        message = messages.shift_done(A, [B, C], period, "✅ 已同步")
        self.assertEqual(message.html.count("⚪ <b>停止账号 </b>"), 2)
        self.assertIn(f"🕒 <b>当前班次</b>：AM · AliCloud-CDT-AM\n", message.html)

    def test_two_members_period_is_plain_am_pm(self):
        tz = dt.timezone(dt.timedelta(hours=8))
        morning = dt.datetime(2026, 10, 26, 0, 0, tzinfo=tz).timestamp()
        afternoon = dt.datetime(2026, 10, 26, 12, 0, tzinfo=tz).timestamp()
        self.assertEqual(messages.shift_period(A["label"], int(morning), 2, tz), "AM")
        self.assertEqual(messages.shift_period(A["label"], int(afternoon), 2, tz), "PM")

    def test_no_usage_line_in_shift_report(self):
        # 脚本的换班报告里没有「已用」这一行，别被顺手加回来
        self.assertNotIn("已用", messages.shift_done(A, [B], "AM", "✅ 已同步").html)


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
    def test_two_members_wording(self):
        message = messages.breaker([A, B])
        self.assertTrue(message.html.startswith("🚨 <b>CDT 全局熔断报告</b>\n\n"))
        self.assertIn("⚠ <b>警告：双实例流量均已到达到阈值，“全局熔断”，均已节省停机。</b>", message.html)
        self.assertIn("🖥 AliCloud-CDT-AM\n┃ 📦 实例：<code>i-aaa111</code>\n┃ 📈 已用：12.00 GB\n", message.html)

    def test_three_members_wording(self):
        message = messages.breaker([A, B, C])
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
