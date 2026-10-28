"""通知文案：与用户自用脚本 cdt.py 逐字对齐。

文案是用户长期打磨的，措辞、emoji、分隔线、字段顺序一律照抄，不做「优化」。
唯一改动过的地方，都是「成员多于两台时脚本的两块结构装不下」：
  · 换班完成：停止账号按成员数逐个列出（脚本只有两台，写死一块）
  · 当前班次：两台用 AM / PM（脚本原样）；三台以上一律写成「机名（起–止）」，
    因为三台轮班时每台都可能落在 AM 段，只写 AM / PM 根本看不出是谁
  · 故障转移：三台以上在「超限账号」之后再补「停止账号」
  · 全局熔断：两台说「双实例」，三台以上说「N 台实例」；单台（超限无处可换）
    沿用同一个标题，正文保留 ＩＰ 行

停机状态一律读实例真实返回的 StoppedMode（StopCharging 节省停机 / KeepCharging 普通停机），
不按脚本写死「Stopped (StopCharging)」——普通停机照常收规格费，写错会误导。

唯一在脚本之外统一加的一行是「📍 地域」：跟着 ＩＰ 行的下一行，取 ECS DescribeInstances
返回的真实 RegionId（如 cn-hongkong），和每日流量报告字段顺序一致。其余字段位置不动。

本模块只做拼装，不查库、不调接口，方便和脚本对着看。
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from . import notify

SEP = "━━━━━━━━━━━━━━━"

# 阿里云 StoppedMode 的取值，面板与通知统一用这套说法。
# Not-applicable = 本实例不支持停机不收费，停机后照常收费，语义等同普通停机。
MODE_LABELS = {
    "StopCharging": "节省停机",
    "KeepCharging": "普通停机",
    "Stop": "普通停机",
    "Not-applicable": "普通停机",
}

_TAG_RE = re.compile(r"<[^>]+>")


def mode_label(mode: str | None) -> str:
    """StopCharging → 节省停机；KeepCharging → 普通停机；空值返回空串。"""
    mode = str(mode or "").strip()
    return MODE_LABELS.get(mode, mode) if mode else ""


# 实例状态的中文说法，事件进度文案用（Running 到位 = 运行中；停机到位 = 真实停机模式）。
STATUS_LABELS = {
    "Running": "运行中",
    "Stopped": "已停机",
    "Starting": "启动中",
    "Stopping": "停止中",
    "Pending": "等待中",
}


def status_suffix(status: str | None, stopped_mode: str | None) -> str:
    """给「已确认 / 当前」后面的状态一个括号后缀。

    - Running → （运行中），不带停机模式（运行时 StoppedMode 可能是上次停机的残留值，
      硬拼「普通停机」是误导）
    - Stopped → （节省停机 / 普通停机 / 已停机），按真实 StoppedMode
    - 中间态 → （启动中 / 停止中 …）
    """
    status = str(status or "").strip()
    if not status:
        return ""
    if status == "Running":
        return "（运行中）"
    if status == "Stopped":
        label = mode_label(stopped_mode)
        return f"（{label}）" if label else "（已停机）"
    label = STATUS_LABELS.get(status)
    return f"（{label}）" if label else ""


def stop_tail(servers: list[dict[str, Any]]) -> str:
    """一句话说明这批机器到底是怎么停的，按实例返回的真实 StoppedMode。"""
    modes = {_mode(s) for s in servers}
    if modes == {"StopCharging"}:
        return "均已节省停机"
    if len(modes) == 1 and "" not in modes:
        return f"均已{mode_label(next(iter(modes)))}"
    return "均已停机"


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
    # Not-applicable = 本实例不支持停机不收费，停机后照常收费，语义等同普通停机
    # （KeepCharging）。归一化后再走 mode_label / _status，避免漏成英文。
    mode = str(server.get("stopped_mode") or "-")
    return "KeepCharging" if mode == "Not-applicable" else mode


def _status(server: dict[str, Any]) -> str:
    """脚本里的 disp：Running 时不带括号，其它状态带上停机模式。

    模式还没读到（刚下发指令、实例还在 Stopping）时只写状态，不补空括号。
    脚本里模式名是英文原值（StopCharging / KeepCharging），保持一致；唯独
    Not-applicable 是「不支持节省停机」的说明性值，映射成「普通停机」以免漏英文。
    """
    status = str(server.get("instance_status") or "Unknown")
    if status == "Running":
        return status
    mode = _mode(server)
    return f"{status} ({mode})" if mode and mode != "-" else status


def _state_icon(server: dict[str, Any]) -> str:
    """状态行图标：运行中 🟢，其余（停机 / 中间态）⚪。

    换班异常报告里当班「应当运行」却超时未就绪，停机的那台该 ⚪；
    应当停止的那台若还赖在 Running，该 🟢。写死 🚧 会让图标和状态文字打架。
    """
    return "🟢" if str(server.get("instance_status") or "") == "Running" else "⚪"


def _used(server: dict[str, Any]) -> float:
    return float(server.get("traffic_used") or 0)


def _threshold_text(server: dict[str, Any]) -> str:
    """阈值取这台机器自己在服务器设置里填的 threshold_gb，不是组里共用的那个。

    每台机器的套餐和阈值可以不一样，用组阈值会张冠李戴。
    没填（0 或缺失）时返回空串，调用方就别写括号 —— 「阈值 0 GB」比不写更糟。
    """
    try:
        value = float(server.get("threshold_gb") or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _used_line(server: dict[str, Any]) -> str:
    """「已用」行带上这台机器自己的阈值，一眼能看出超了多少。"""
    line = f"┃ 📈 已用：{_used(server):.2f} GB"
    threshold = _threshold_text(server)
    return f"{line}（阈值 {threshold} GB）" if threshold else line


# --------------------------------------------------------------------------
# 标题：CDT {事件}报告（{范围}）
# --------------------------------------------------------------------------
# 三种托管范围，标题末尾永远带一个全角括号说明「这条报告属于谁」：
#   换班组   → （换班组：港新联合保活）
#   单机保活 → （单机保活）
#   闲散手动 → （闲散手动）
# 统一之后，推送列表里一眼能认出事件归属，不用去点开正文。
SCOPE_GROUP = "换班组"
SCOPE_KEEPALIVE = "单机保活"
SCOPE_IDLE = "闲散手动"


def scope_tag(group_name: str = "", scope: str = SCOPE_GROUP, note: str = "") -> str:
    """标题末尾的范围括号。

    note 是给同一事件补的修饰（目前只有「延迟恢复」），跟在范围后面共用
    一个括号：`（换班组：X，延迟恢复）`。排两个括号看着像两件事。
    """
    inner = f"{SCOPE_GROUP}：{group_name}" if group_name else (scope or SCOPE_GROUP)
    if note:
        inner = f"{inner}，{note}"
    return f"（{inner}）"


def title_of(
    event: str,
    group_name: str = "",
    scope: str = SCOPE_GROUP,
    note: str = "",
) -> str:
    """统一拼标题。event 不带「报告」二字，例如「换班完成」「全局熔断」。"""
    return f"CDT {event}报告{scope_tag(group_name, scope, note)}"


def _message(kind: str, title_html: str, title: str, body: str) -> notify.Message:
    raw = f"{title_html}\n\n{body}"
    return notify.Message(kind=kind, title=title, html=raw, text=_TAG_RE.sub("", raw))


def _titled(kind: str, emoji: str, title: str, body: str) -> notify.Message:
    """标题只写一份：html 版加 emoji 与 <b>，text 版用纯文本，省得两处改漏。"""
    return _message(kind, f"{emoji} <b>{title}</b>", title, body)


# --------------------------------------------------------------------------
# 块
# --------------------------------------------------------------------------

REGION_LABELS = {
    # -- 中国内地 --
    "cn-qingdao": "华北 1（青岛）",
    "cn-beijing": "华北 2（北京）",
    "cn-zhangjiakou": "华北 3（张家口）",
    "cn-huhehaote": "华北 5（呼和浩特）",
    "cn-wulanchabu": "华北 6（乌兰察布）",
    "cn-hangzhou": "华东 1（杭州）",
    "cn-shanghai": "华东 2（上海）",
    "cn-nanjing": "华东 5（南京-本地地域）",
    "cn-fuzhou": "华东 6（福州-本地地域）",
    "cn-shenzhen": "华南 1（深圳）",
    "cn-heyuan": "华南 2（河源）",
    "cn-guangzhou": "华南 3（广州）",
    "cn-chengdu": "西南 1（成都）",
    "cn-wuhan-lr": "华中 1（武汉-本地地域）",
    # -- 中国港澳台 --
    "cn-hongkong": "中国香港",
    "cn-macau": "中国澳门",
    # -- 亚太 --
    "ap-southeast-1": "新加坡",
    "ap-southeast-2": "澳大利亚（悉尼）",
    "ap-southeast-3": "马来西亚（吉隆坡）",
    "ap-southeast-5": "印度尼西亚（雅加达）",
    "ap-southeast-6": "菲律宾（马尼拉）",
    "ap-southeast-7": "泰国（曼谷）",
    "ap-southeast-8": "马来西亚（柔佛）",
    "ap-northeast-1": "日本（东京）",
    "ap-northeast-2": "韩国（首尔）",
    "ap-south-1": "印度（孟买）",
    # -- 欧洲 --
    "eu-central-1": "德国（法兰克福）",
    "eu-west-1": "英国（伦敦）",
    # -- 美洲 --
    "us-west-1": "美国（硅谷）",
    "us-east-1": "美国（弗吉尼亚）",
    "na-south-1": "墨西哥",
    "sa-east-1": "巴西（圣保罗）",
    # -- 中东 --
    "me-east-1": "阿联酋（迪拜）",
    "me-central-1": "沙特阿拉伯（利雅得）",
}


def _region(server: dict[str, Any]) -> str:
    """地域：ECS DescribeInstances 返回的 RegionId 翻成中文名，没读到给「-」。

    RegionId 本身是 API 原值（cn-hongkong / ap-northeast-1 这种代码），
    直接摆进通知用户看不懂，这里翻译成人话，清单外的代码原样返回、不猜。
    表里是阿里云全局地域（含已关停的本地地域，仅用于翻译老旧机器）；
    创建服务器时的可选列表是 web/src/regions.ts，那边只管能买到的地域。
    """
    raw = str(server.get("region_id") or "").strip()
    if not raw:
        return "-"
    return REGION_LABELS.get(raw, raw)


def running_block(heading: str, server: dict[str, Any], ddns_status: str) -> str:
    """线上运行的那台。脚本里叫「运行账号 / 接替账号」。"""
    return "\n".join(
        [
            f"🟢 <b>{heading} </b>",
            f"┃ 🏷 别名：{_label(server)}",
            f"┃ 📦 实例：<code>{_instance(server)}</code>",
            f"┃ 🌐 ＩＰ：{_ip(server)}",
            f"┃ 📍 地域：{_region(server)}",
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
        f"┃ 🏷 别名：{_label(server)}",
        f"┃ 📦 实例：<code>{_instance(server)}</code>",
        f"┃ 🌐 ＩＰ：{_ip(server)}",
        f"┃ 📍 地域：{_region(server)}",
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
    group_name: str = "",
    delayed: bool = False,
    manual: bool = False,
) -> notify.Message:
    """✅ 换班完成 / 换班成功（延迟恢复）/ 手动换班完成。

    手动换班走完整流程（确认成员状态 → DDNS → 结算），报告照发，事件名改成
    「手动换班完成」而不是加后缀 —— 标题统一为 `CDT {事件}报告（范围）` 之后，
    后缀那一串只能塞进范围括号里，读起来像范围本身有毛病。
    """
    body = f"🕒 <b>当前班次</b>：{period}\n{SEP}\n" + _join_blocks(
        [running_block("运行账号", duty, ddns_status)]
        + [stopped_block("停止账号", s, _status(s)) for s in stopped]
    )
    if delayed:
        return _titled(
            "shift_done",
            "✅",
            title_of("换班成功", group_name, note="延迟恢复"),
            body,
        )
    event = "手动换班完成" if manual else "换班完成"
    return _titled("shift_done", "✅", title_of(event, group_name), body)


def shift_failover(
    duty: dict[str, Any],
    over: list[dict[str, Any]],
    standby: list[dict[str, Any]],
    ddns_status: str,
    group_name: str = "",
) -> notify.Message:
    """⚠️ 故障转移（原「临时换班」）。两块是脚本原样，三台以上才补「停止账号」。"""
    parts = [running_block("接替账号", duty, ddns_status)]
    parts += [stopped_block("超限账号", s, _status(s), used=_used(s)) for s in over]
    parts += [stopped_block("停止账号", s, _status(s)) for s in standby]
    return _titled(
        "shift_failover",
        "⚠️",
        title_of("故障转移", group_name),
        f"📢 <b>故障转移</b>：当班实例流量超限，已启用备用实例。\n{SEP}\n" + _join_blocks(parts),
    )


def breaker(
    servers: list[dict[str, Any]], group_name: str = "", note: str = ""
) -> notify.Message:
    """🚨 全局熔断。脚本原文含「到达到阈值」「“全局熔断”」，保持原样。

    尾巴按真实停机模式写：脚本默认全是节省停机，但有的规格不支持，
    会退化成普通停机（照常收规格费），这时候还说「均已节省停机」就是骗人。

    note 非空表示「不是因为全员超限、而是找不到替班」（其他成员状态读不到）：
    此时我们并不知道别人用了多少流量，不能照抄「N 台流量均已到达到阈值」。
    """
    count = "双实例" if len(servers) == 2 else f"{len(servers)} 台实例"
    if note:
        warn = f"当班流量已到达到阈值，{note}，按“全局熔断”处理，{stop_tail(servers)}。"
    else:
        warn = f"{count}流量均已到达到阈值，“全局熔断”，{stop_tail(servers)}。"
    blocks = [
        "\n".join(
            [
                f"🖥 {_label(s)}",
                f"┃ 📦 实例：<code>{_instance(s)}</code>",
                f"┃ 🌐 ＩＰ：{_ip(s)}",
                f"┃ 📍 地域：{_region(s)}",
                _used_line(s),
                f"┃ 🚧 状态：{_status(s)}",
            ]
        )
        for s in servers
    ]
    return _titled(
        "breaker",
        "🚨",
        title_of("全局熔断", group_name),
        f"⚠ <b>警告：{warn}</b>\n\n" + _join_blocks(blocks),
    )


def limit_stop(server: dict[str, Any], group_name: str = "") -> notify.Message:
    """🚨 全局熔断（单台场景）：当班超限，这台机器只停它自己。

    两种情况：组里关掉了「允许流量故障转移」，或当班自身状态都读不到、无从判断。
    和 breaker() 共用同一个标题「全局熔断报告」——两者都是「流量到阈值被迫停机」，
    只是范围不同（单台 / 全组），分两个标题在推送里只会让人以为是两件事。
    正文保留 ＩＰ 行，与其它通知的实例块字段顺序一致。
    """
    return _titled(
        "limit_stop",
        "🚨",
        title_of("全局熔断", group_name),
        "\n".join(
            [
                "当班实例流量超限，该换班组没有启用故障转移，已强制节省停机。",
                "如有需要，请开启故障转移或者去管理后台手动点击“立即换班”。",
                "",
                f"🖥 {_label(server)}",
                f"┃ 📦 实例：<code>{_instance(server)}</code>",
                f"┃ 🌐 ＩＰ：{_ip(server)}",
                f"┃ 📍 地域：{_region(server)}",
                _used_line(server),
                f"┃ 🔴 状态：{_status(server)}",
                SEP,
            ]
        ),
    )


def over_limit(server: dict[str, Any], scope: str) -> notify.Message:
    """🚨 超限停机：单机保活 / 闲散手动的机器到阈值，被迫节省停机。

    标题不叫「全局熔断」——那套说的是换班组里「没得换所以全停」，单机场景
    底下没有「全局」可言，混用会让人以为换班组出了事。范围括号标明是谁被停了。
    """
    paused = "已暂停保活" if scope == SCOPE_KEEPALIVE else "已节省停机"
    return _titled(
        "limit_stop",
        "🚨",
        title_of("超限停机", scope=scope),
        "\n".join(
            [
                f"流量已到阈值，{paused}，不再自动开机。",
                "流量回落到阈值以下（流量包重置或调高阈值）后自动恢复。",
                "",
                f"🖥 {_label(server)}",
                f"┃ 📦 实例：<code>{_instance(server)}</code>",
                f"┃ 🌐 ＩＰ：{_ip(server)}",
                f"┃ 📍 地域：{_region(server)}",
                _used_line(server),
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
    group_name: str = "",
) -> notify.Message:
    """🟢 抢占恢复。单机保活拉起也用这套版式（脚本里没有单独的保活通知）。

    范围靠标题括号区分：单机保活「（单机保活）」、换班组「（换班组：组名）」。
    之前联合换班把组名塞在事件名前面、单机保活没有前缀，两个标题长得太像，不好认。
    """
    if kind == "keepalive":
        title = title_of("抢占恢复", scope=SCOPE_KEEPALIVE)
    else:
        title = title_of("抢占恢复", group_name)
    lines = [
        headline,
        "",
        f"🖥 {_label(server)}",
        f"┃ 📦 实例：<code>{_instance(server)}</code>",
        f"┃ 🌐 ＩＰ：{_ip(server)}",
        f"┃ 📍 地域：{_region(server)}",
        f"┃ ☁️ DDNS：{ddns_status}",
        "┃ 🟢 状态：Running",
        SEP,
    ]
    return _titled(kind, "🟢", title, "\n".join(lines))


def shift_error(
    duty: dict[str, Any],
    stopped: list[dict[str, Any]],
    minutes: int,
    group_name: str = "",
) -> notify.Message:
    """⚠️ 换班异常（超时未就绪）。"""
    parts = [
        "\n".join(
            [
                "🚩 <b>应当运行账号 </b>",
                f"┃ 🏷 别名：{_label(duty)}",
                f"┃ 📦 实例：<code>{_instance(duty)}</code>",
                f"┃ 🌐 ＩＰ：{_ip(duty)}",
                f"┃ 📍 地域：{_region(duty)}",
                f"┃ {_state_icon(duty)} 状态：{_status(duty)}",
            ]
        )
    ]
    for server in stopped:
        parts.append(
            "\n".join(
                [
                    "🚩 <b>应当停止账号 </b>",
                    f"┃ 🏷 别名：{_label(server)}",
                    f"┃ 📦 实例：<code>{_instance(server)}</code>",
                    f"┃ 🌐 ＩＰ：{_ip(server)}",
                    f"┃ 📍 地域：{_region(server)}",
                    f"┃ {_state_icon(server)} 状态：{_status(server)}",
                ]
            )
        )
    return _titled(
        "shift_error",
        "⚠️",
        title_of("换班异常", group_name),
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
            f"┃ 📍 地域：{_region(server)}",
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


def shift_period(
    slot_name: str,
    start: str,
    end: str,
) -> str:
    """当前班次：班次名称（起–止）。跨天时结束标「次日」。

    起止是 HH:MM 字符串（时刻表模式直接从成员字段拿）；没有名称时退回只写时间段。
    """
    slot_name = (slot_name or "").strip()
    start = (start or "").strip()
    end = (end or "").strip()
    if not start and not end:
        return slot_name or "—"
    if not end:
        return f"{slot_name}（{start}）" if slot_name else start
    # 跨天：结束时刻 <= 开始时刻，标「次日」
    tail = end
    if _hhmm_minutes(end) <= _hhmm_minutes(start):
        tail = f"次日 {end}"
    span = f"{start}–{tail}"
    return f"{slot_name}（{span}）" if slot_name else span


def _hhmm_minutes(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 2:
        return -1
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return -1
