"""通知文案：与用户自用脚本 cdt.py 逐字对齐。

文案是用户长期打磨的，措辞、emoji、分隔线、字段顺序一律照抄，不做「优化」。
唯一改动过的地方，都是「成员多于两台时脚本的两块结构装不下」：
  · 换班完成：停止账号按成员数逐个列出（脚本只有两台，写死一块）
  · 当前班次：两台用 AM / PM（脚本原样），三台以上补上当班机名，否则看不出是谁在跑
  · 临时换班：三台以上在「超限账号」之后再补「停止账号」
  · 全局熔断：两台说「双实例」，三台以上说「N 台实例」

本模块只做拼装，不查库、不调接口，方便和脚本对着看。
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import re
from typing import Any

from . import notify

SEP = "━━━━━━━━━━━━━━━"

_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# 字段取值
# --------------------------------------------------------------------------

def _label(server: dict[str, Any]) -> str:
    return html_lib.escape(str(server.get("label") or ""))


def _instance(server: dict[str, Any]) -> str:
    return html_lib.escape(str(server.get("instance_id") or ""))


def _ip(server: dict[str, Any]) -> str:
    # 脚本里 get_ecs_info 取不到 IP 时给的就是这个字符串
    return str(server.get("public_ip") or "N/A")


def _mode(server: dict[str, Any]) -> str:
    return str(server.get("stopped_mode") or "-")


def _status(server: dict[str, Any]) -> str:
    """脚本里的 disp：Running 时不带括号，其它状态带上停机模式。"""
    status = str(server.get("instance_status") or "Unknown")
    return status if status == "Running" else f"{status} ({_mode(server)})"


def _used(server: dict[str, Any]) -> float:
    return float(server.get("traffic_used") or 0)


def _message(kind: str, title_html: str, title: str, body: str) -> notify.Message:
    raw = f"{title_html}\n\n{body}"
    return notify.Message(kind=kind, title=title, html=raw, text=_TAG_RE.sub("", raw))


# --------------------------------------------------------------------------
# 块
# --------------------------------------------------------------------------

def running_block(heading: str, server: dict[str, Any], ddns_status: str) -> str:
    """线上运行的那台。脚本里叫「运行账号 / 接替账号」。"""
    return "\n".join(
        [
            f"🟢 <b>{heading} </b>",
            f"┃ 🏷 别称：{_label(server)}",
            f"┃ 📦 实例：<code>{_instance(server)}</code>",
            f"┃ 🌐 ＩＰ：{_ip(server)}",
            f"┃ ☁️ DDNS：{ddns_status}",
            "┃ 🟢 状态：Running",
        ]
    )


def stopped_block(
    heading: str, server: dict[str, Any], status_text: str, used: float | None = None
) -> str:
    """停机的那台。脚本里「停止账号」不带已用，「超限账号」带已用。"""
    lines = [
        f"⚪ <b>{heading} </b>",
        f"┃ 🏷 别称：{_label(server)}",
        f"┃ 📦 实例：<code>{_instance(server)}</code>",
        f"┃ 🌐 ＩＰ：{_ip(server)}",
    ]
    if used is not None:
        lines.append(f"┃ 📈 已用：{used:.2f} GB")
    lines.append(f"┃ ⚪ 状态：{status_text}")
    return "\n".join(lines)


def _join_blocks(parts: list[str]) -> str:
    return f"\n{SEP}\n".join(parts) + f"\n{SEP}"


# --------------------------------------------------------------------------
# 各类报告
# --------------------------------------------------------------------------

def shift_done(
    duty: dict[str, Any],
    stopped: list[dict[str, Any]],
    period: str,
    ddns_status: str,
    delayed: bool = False,
) -> notify.Message:
    """✅ 换班完成 / 换班成功（延迟恢复）。"""
    if delayed:
        return _message(
            "shift_done",
            "✅ <b>CDT 换班成功报告 (延迟恢复)</b>",
            "CDT 换班成功报告 (延迟恢复)",
            f"🕒 <b>当前班次</b>：{period}\n{SEP}\n"
            + _join_blocks(
                [running_block("运行账号", duty, ddns_status)]
                + [stopped_block("停止账号", s, "Stopped (StopCharging)") for s in stopped]
            ),
        )
    return _message(
        "shift_done",
        "✅ <b>CDT 换班完成报告</b>",
        "CDT 换班完成报告",
        f"🕒 <b>当前班次</b>：{period}\n{SEP}\n"
        + _join_blocks(
            [running_block("运行账号", duty, ddns_status)]
            + [stopped_block("停止账号", s, "Stopped (StopCharging)") for s in stopped]
        ),
    )


def shift_failover(
    duty: dict[str, Any],
    over: list[dict[str, Any]],
    standby: list[dict[str, Any]],
    ddns_status: str,
) -> notify.Message:
    """⚠️ 临时换班（故障转移）。两块是脚本原样，三台以上才补「停止账号」。"""
    parts = [running_block("接替账号", duty, ddns_status)]
    parts += [stopped_block("超限账号", s, _status(s), used=_used(s)) for s in over]
    parts += [stopped_block("停止账号", s, "Stopped (StopCharging)") for s in standby]
    return _message(
        "shift_failover",
        "⚠️ <b>CDT 临时换班报告</b>",
        "CDT 临时换班报告",
        f"📢 <b>故障转移</b>：当班实例流量超限，已启用备用实例。\n{SEP}\n" + _join_blocks(parts),
    )


def breaker(servers: list[dict[str, Any]]) -> notify.Message:
    """🚨 全局熔断。脚本原文含「到达到阈值」「“全局熔断”」，保持原样。"""
    if len(servers) == 2:
        warn = "双实例流量均已到达到阈值，“全局熔断”，均已节省停机。"
    else:
        warn = f"{len(servers)} 台实例流量均已到达到阈值，“全局熔断”，均已节省停机。"
    blocks = [
        "\n".join(
            [
                f"🖥 {_label(s)}",
                f"┃ 📦 实例：<code>{_instance(s)}</code>",
                f"┃ 📈 已用：{_used(s):.2f} GB",
                f"┃ 🚧 状态：{_status(s)}",
            ]
        )
        for s in servers
    ]
    return _message(
        "breaker",
        "🚨 <b>CDT 全局熔断报告</b>",
        "CDT 全局熔断报告",
        f"⚠ <b>警告：{warn}</b>\n\n" + _join_blocks(blocks),
    )


def limit_stop(server: dict[str, Any]) -> notify.Message:
    """🔴 超限停机。"""
    return _message(
        "limit_stop",
        "🔴 <b>CDT 超限停机报告</b>",
        "CDT 超限停机报告",
        "\n".join(
            [
                "当班实例流量超限，已强制节省停机。",
                "",
                f"🖥 {_label(server)}",
                f"┃ 📦 实例：<code>{_instance(server)}</code>",
                f"┃ 🌐 ＩＰ：{_ip(server)}",
                f"┃ 🔴 状态：{_status(server)}",
                SEP,
            ]
        ),
    )


def recovery(
    server: dict[str, Any],
    ddns_status: str,
    headline: str = "当班实例疑被抢占，已自动恢复运行。",
    kind: str = "recovery",
) -> notify.Message:
    """🟢 抢占恢复。单机保活拉起也用这套版式（脚本里没有单独的保活通知）。"""
    return _message(
        kind,
        "🟢 <b>CDT 抢占恢复报告</b>",
        "CDT 抢占恢复报告",
        "\n".join(
            [
                headline,
                "",
                f"🖥 {_label(server)}",
                f"┃ 📦 实例：<code>{_instance(server)}</code>",
                f"┃ 🌐 ＩＰ：{_ip(server)}",
                f"┃ ☁️ DDNS：{ddns_status}",
                "┃ 🟢 状态：Running",
                SEP,
            ]
        ),
    )


def shift_error(
    duty: dict[str, Any],
    stopped: list[dict[str, Any]],
    minutes: int,
) -> notify.Message:
    """⚠️ 换班异常（超时未就绪）。"""
    parts = [
        "\n".join(
            [
                "🚩 <b>应当运行账号 </b>",
                f"┃ 🏷 别称：{_label(duty)}",
                f"┃ 📦 实例：<code>{_instance(duty)}</code>",
                f"┃ 🌐 ＩＰ：{_ip(duty)}",
                f"┃ 🚧 状态：{_status(duty)}",
            ]
        )
    ]
    for server in stopped:
        parts.append(
            "\n".join(
                [
                    "🚩 <b>应当停止账号 </b>",
                    f"┃ 🏷 别称：{_label(server)}",
                    f"┃ 📦 实例：<code>{_instance(server)}</code>",
                    f"┃ 🌐 ＩＰ：{_ip(server)}",
                    f"┃ 🚧 状态：{_status(server)}",
                ]
            )
        )
    return _message(
        "shift_error",
        "⚠️ <b>CDT 换班异常报告</b>",
        "CDT 换班异常报告",
        f"⏳ <b>换班超时：换班指令已执行 {minutes} 分钟，但状态未就绪。</b>\n\n"
        + _join_blocks(parts),
    )


def report_block(server: dict[str, Any]) -> str:
    """脚本里的 build_report_block。额度取每台自己的配额，没填就按 200GB。"""
    used = _used(server)
    quota = float(server.get("quota_gb") or 0) or 200.0
    remain = quota - used
    percent = used / quota * 100 if quota > 0 else 0.0
    color = "🟢 正常" if percent < 60 else ("🟡 注意" if percent < 90 else "🔴 危险")
    return "\n".join(
        [
            f"🖥 {_label(server)}",
            f"┃ 📦 实例：<code>{_instance(server)}</code>",
            f"┃ 🌐 ＩＰ：{_ip(server)}",
            f"┃ 📍 地域：{server.get('region') or '-'}",
            f"┃ 📈 已用：{used:.2f} GB",
            f"┃ 💾 剩余：{remain:.2f} GB",
            f"┃ 🔥 使用率：{percent:.2f}%  {color}",
        ]
    )


def daily_report(servers: list[dict[str, Any]], timestamp: str) -> notify.Message:
    """📊 每日流量报告。"""
    return _message(
        "daily_report",
        "📊 <b>CDT 每日流量报告</b>",
        "CDT 每日流量报告",
        "\n\n".join(report_block(s) for s in servers) + f"\n\n⏰ 更新时间：{timestamp}",
    )


def shift_period(label: str, shift_start: int, member_count: int, tz: Any) -> str:
    """当前班次。两台照脚本写 AM / PM；三台以上补当班机名。"""
    hour = dt.datetime.fromtimestamp(shift_start, tz).hour
    period = "AM" if hour < 12 else "PM"
    return period if member_count <= 2 else f"{period} · {label}"
