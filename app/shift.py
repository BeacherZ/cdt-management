"""核心引擎：联合换班（N 台按小时轮换）+ 单机保活。

对账（reconcile）幂等：同一班次内反复执行不会重复操作，也不会重复推送。
所有「已做过」的标记都落 SQLite，容器重启不丢。
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
import threading
import time
from typing import Any

from . import aliyun, db as dbmod, ddns, dns, messages, notify, repo, settings

log = logging.getLogger("cdt-management.engine")

PHASE_PENDING = "pending"
PHASE_SWITCHING = "switching"
PHASE_SETTLED = "settled"
PHASE_FAILOVER = "failover"
# 人工点了「立即换班」并已就绪：内部当班是 run.duty_server_id 覆盖出来的，
# 和流量超限的 failover 共用一个覆盖字段，但对外必须说「手动换班」。
PHASE_MANUAL = "manual"
PHASE_BREAKER = "breaker"
PHASE_ERROR = "error"

# 这些阶段的班次「这一班已经处理过」：当班被重新拉起时是恢复，不是一次新换班。
# breaker 也在内 —— 熔断 / 超限停机后流量回落，重新拉起当班属于恢复，不该记成
# 一次新换班，更不该拿建行时那个可能已过去几小时的 started_at 去误报「换班超时」。
_SETTLED_LIKE_PHASES = (PHASE_SETTLED, PHASE_FAILOVER, PHASE_MANUAL, PHASE_BREAKER)

# shift_runs.origin：本班次是怎么来的。
# 新建换班组后的第一次对齐单独标出来 —— 那次事件要一直叫「组名：新建换班组」，
# 不能被结算逻辑改名成「换班完成」，否则用户点保存后看到的事件对不上自己刚做的事。
ORIGIN_NEW_GROUP = "new_group"

TRANSIENT_STATUS = {"Starting", "Pending", "Stopping"}

# 换班组事件的全部 kind。组停用 / 排不出当班时按这份清单收尾，
# 否则「换班进行中」会一直转圈，直到 14 天清理才消失。
_GROUP_EVENT_KINDS = (
    "shift_done",
    "manual",
    "group_new",
    "failover",
    "shift_failover",
    "recovery",
)


def _close_group_events(group_id: int, note: str) -> None:
    """流程走不下去了（组停用 / 启用成员不足）：把该组还在转的事件收尾。"""
    for event in repo.running_events(group_id, _GROUP_EVENT_KINDS):
        event_id = int(event["id"])
        repo.add_event_progress(event_id, note)
        repo.finish_event(event_id, "error", note)


def _close_running_event(event_id: int, note: str) -> bool:
    """把一条还在转的事件收尾；已经收尾的不动，返回是否真的收掉了。

    熔断 / 超限停机拿 run.event_id 直接 finish 的话，会把本班次早就结算完的
    「换班完成」翻成 error —— 事件里红叉和「换班完成」四个字同时出现。
    只有 status='running' 的事件才该被这里收掉。
    """
    if not event_id:
        return False
    event = repo.get_event(event_id)
    if not event or str(event.get("status") or "") != "running":
        return False
    repo.add_event_progress(event_id, note)
    repo.finish_event(event_id, "error", note)
    return True


# 同一台机器在同一个班次里重复下发停机指令的最小间隔（秒）。
# 只有在「状态读不到、库里看着还在跑」时才会重复触发，给它一个节流。
_BREAKER_STOP_COOLDOWN = 120

# 换了班但 DDNS 一直同步不上时，最多等这么久就照常结算（秒）。
# 以前是无上限地等：绑定记录在 Cloudflare 侧被删/凭证过期时永远等不到 ✅，
# 10 分钟一到只发一条「换班超时」，看着像机器出故障，实际机器早切好了。
# 失败本身已经写进事件进度和结算报告，不阻塞才是对的。
_DDNS_WAIT_SECONDS = 180

# 保活重试的里程碑：一台机器一天都起不来时，事件只有一条（可以横跨一整天），
# 进度里只在这几个「已尝试次数」上补一行，其余轮次只改事件概要那句
# 「等待开机到位（已尝试 N 次）」—— 每分钟一行同义句会把展开的事件糊成一片。
_KEEPALIVE_RETRY_MARK = "仍未运行（"
_KEEPALIVE_MILESTONES = (3, 10, 30, 100, 300, 1000, 3000)


# --------------------------------------------------------------------------
# 轮换计算
# --------------------------------------------------------------------------

def _hhmm_to_minutes(value: str) -> int:
    """'06:00' → 360。非法输入返回 -1。"""
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return -1
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return -1
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return -1
    return hour * 60 + minute


def keepalive_window(item: dict[str, Any], now: dt.datetime) -> tuple[bool, str]:
    """单机保活的运行窗口：(此刻能不能跑, 人话摘要)。

    两个维度取交集：时段（每天几点到几点，**不跨零点**，结束写 00:00 表示到 24:00）
    与日期（每月几号到几号，**不跨月**）。都为空 = 全天全月，等价于没设窗口。

    日期超过当月天数时按月末截断：2 月填「1–31 日」就是 1 号到 28/29 号，
    否则那个月一天都匹配不上，机器整个月不干活。

    配置读不懂（手改库写坏的半截配置）时按「不限」处理并照常保活 —— 宁可多跑，
    不能因为一行坏配置把用户的机器钉死在停机状态。
    """
    start_raw = str(item.get("start_time") or "").strip()
    end_raw = str(item.get("end_time") or "").strip()
    start = _hhmm_to_minutes(start_raw) if start_raw else -1
    end = _hhmm_to_minutes(end_raw) if end_raw else -1
    # 结束 00:00 = 当天到午夜（24:00），和联合换班是同一套写法：
    # 「08:00–00:00」表示整段跑到午夜，不是零长度。
    if end == 0 and start > 0:
        end = 24 * 60
    timed = start >= 0 and end > start

    # 手改库写坏的半截配置按「不限」处理：int() 遇到文本会抛，不能让一行坏配置
    # 把整轮巡检掀翻（注释承诺的是「照常保活」，不是让机器全停）。
    try:
        day_start = int(item.get("start_day") or 0)
        day_end = int(item.get("end_day") or 0)
    except (TypeError, ValueError):
        day_start = day_end = 0
    dated = 1 <= day_start <= day_end <= 31

    parts: list[str] = []
    if timed:
        # 摘要按面板习惯写 24:00：写「每天 08:00–00:00」会被读成零长度
        end_label = "24:00" if end == 24 * 60 else end_raw
        parts.append(f"每天 {start_raw}–{end_label}")
    if dated:
        parts.append(f"{day_start}–{day_end} 日")
    summary = " · ".join(parts)

    if timed:
        minute = now.hour * 60 + now.minute
        if not (start <= minute < end):
            return False, summary
    if dated:
        month_days = calendar.monthrange(now.year, now.month)[1]
        if not (min(day_start, month_days) <= now.day <= min(day_end, month_days)):
            return False, summary
    return True, summary


def _window_bracket(window_text: str) -> str:
    """事件进度里紧跟「运行窗口」的括号说明；没配窗口时给空串，别留一对空括号。"""
    return f"（{window_text}）" if window_text else ""


def _is_timetable_group(group: dict[str, Any]) -> bool:
    """组内所有启用成员都填了合法起止时刻，才走「固定时刻表」模式。"""
    members = [m for m in group.get("members") or [] if m.get("enabled")]
    if len(members) < 2:
        return False
    return all(
        _hhmm_to_minutes(m.get("start_time")) >= 0 and _hhmm_to_minutes(m.get("end_time")) >= 0
        for m in members
    )


def _duty_at_timetable(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int]:
    """固定时刻表模式：按每天真实时钟判定当班。

    每个成员占据一天内的 [start, end) 分钟区间，全组刚好把 0–24 点分完；
    end == 0（保存时的 00:00 / 面板上的 24:00）表示这一段到午夜为止。
    真正的跨天配置已在保存时拒绝，这里保留 [start, 1440) ∪ [0, end) 的写法
    只为兼容历史数据。

    返回 (当班成员, 该班次开始时间戳)。开始时间戳用于 slot_key 去重与通知时间段显示。
    """
    members = active_members(group)
    tz_name = _group_timezone(group)
    now = dt.datetime.fromtimestamp(now_ts, _timezone(tz_name))
    minute_of_day = now.hour * 60 + now.minute

    # 逐个成员判断当前分钟落在谁家。跨天成员优先处理边界。
    for member in members:
        start = _hhmm_to_minutes(member.get("start_time"))
        end = _hhmm_to_minutes(member.get("end_time"))
        if start < 0 or end < 0:
            continue
        if end > start:
            # 普通区间 [start, end)
            if start <= minute_of_day < end:
                return member, _shift_start_ts(now, start)
        else:
            # 跨天区间 [start, 1440) ∪ [0, end)
            if minute_of_day >= start or minute_of_day < end:
                return member, _shift_start_ts(now, start)

    # 没命中（理论上不该发生），退回第一台
    first = members[0]
    return first, _shift_start_ts(now, _hhmm_to_minutes(first.get("start_time")))


def _shift_start_ts(now: dt.datetime, start_minutes: int) -> int:
    """今天 start_minutes 时刻的时间戳。

    班次都在一天 0–24 点内分完（结束 00:00 表示到午夜），命中的一定是今天开的班，
    不需要回退；只有历史遗留的跨天配置（18:00–06:00）才走下面这层兜底。
    """
    start_minutes = max(0, start_minutes)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = today_start + dt.timedelta(minutes=start_minutes)
    if candidate > now:
        candidate -= dt.timedelta(days=1)
    return int(candidate.timestamp())


def is_month_cycle(group: dict[str, Any]) -> bool:
    """排班周期是不是「一月一轮」（组员按天轮换）。

    默认 day：一天一轮，按小时累加或按固定时刻表 —— 两种都在 24 小时内换完。
    """
    return str(group.get("cycle") or "day") == "month"


def active_members(group: dict[str, Any]) -> list[dict[str, Any]]:
    """启用的成员，按登记顺序。

    一月一轮看天数，一天一轮看工时：两个字段各自代表「这台能上班」的凭据，
    混着判会让月模式的成员（hours 可能是 0）整组消失。
    """
    month = is_month_cycle(group)
    members = [
        member
        for member in group.get("members") or []
        if member.get("enabled")
        and (float(member.get("days") or 0) > 0 if month else float(member.get("hours") or 0) > 0)
    ]
    return sorted(members, key=lambda item: int(item.get("position") or 0))


def _month_layout(
    group: dict[str, Any], year: int, month: int
) -> list[tuple[dict[str, Any], int, int]]:
    """当月排班表：[(成员, 起始日, 结束日)]，日号从 1 开始、含首尾。

    前面的人各上自己填的天数，最后一位一直排到月末 —— 28/29/30/31 天的月份
    因此能自动伸缩，不用每月改配置。前面的人把整月排完了，后面的人这段就是
    空的（不返回），历史数据填了超额天数时才可能出现。
    """
    members = active_members(group)
    days_in_month = calendar.monthrange(year, month)[1]
    layout: list[tuple[dict[str, Any], int, int]] = []
    cursor = 1
    for index, member in enumerate(members):
        remaining = days_in_month - cursor + 1
        if remaining <= 0:
            break
        span = remaining if index == len(members) - 1 else int(member.get("days") or 0)
        span = min(max(span, 0), remaining)
        if span <= 0:
            continue
        layout.append((member, cursor, cursor + span - 1))
        cursor += span
    return layout


def _duty_at_month(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int] | None:
    """一月一轮：按天排，从当月 1 号 0 点起依次铺开，不看小时边界。

    slot 起点用「某人这段的第一天 0 点」，跨月自然产生新 slot，run 表不用改。
    """
    members = active_members(group)
    if not members:
        return None
    now = dt.datetime.fromtimestamp(now_ts, _timezone(_group_timezone(group)))
    layout = _month_layout(group, now.year, now.month)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for member, start_day, end_day in layout:
        if start_day <= now.day <= end_day:
            return member, int((month_start + dt.timedelta(days=start_day - 1)).timestamp())
    # 排班表没盖住今天（前面的人把整月排完、最后一位没位置）：让第一位兜底，
    # 至少保证有一台在跑，不要出现「谁都不当班」
    if not layout:
        return None
    member, start_day, _ = layout[0]
    return member, int((month_start + dt.timedelta(days=start_day - 1)).timestamp())


def duty_at(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int] | None:
    """返回 (当班成员, 该班次开始时间戳)。按 position 顺序循环轮换。"""
    members = active_members(group)
    if len(members) < 2:
        return None

    if is_month_cycle(group):
        return _duty_at_month(group, now_ts)

    if _is_timetable_group(group):
        return _duty_at_timetable(group, now_ts)

    total = sum(float(m["hours"]) for m in members) * 3600
    anchor = float(group.get("anchor_at") or group.get("created_at") or now_ts)
    elapsed = max(0.0, now_ts - anchor)
    cycle_index = int(elapsed // total)
    offset = elapsed - cycle_index * total

    accumulated = 0.0
    for member in members:
        span = float(member["hours"]) * 3600
        if offset < accumulated + span:
            return member, int(anchor + cycle_index * total + accumulated)
        accumulated += span
    last = members[-1]
    return last, int(anchor + cycle_index * total + accumulated - float(last["hours"]) * 3600)


def actual_duty(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int] | None:
    """此刻**真正**当班的是谁：返回 (当班成员, 该班次开始时间戳)。

    时钟只是默认答案。本班次里如果发生过故障转移（run.failover）或者人工点过
    立即换班（run.manual），实际当班是 run.duty_server_id 覆盖出来的那台。

    这个判定以前散在四个地方各写一遍，其中界面、DNS 两处只认 failover、忘了
    manual，于是手动换完班：引擎在跑 A，界面和域名却还认为当班是 B。
    统一收到这里，谁要问「现在谁当班」都必须走这个函数。
    """
    picked = duty_at(group, now_ts)
    if picked is None:
        return None
    member, shift_start = picked
    run = repo.get_run(int(group["id"]), str(shift_start)) or {}
    if not (run.get("failover") or run.get("manual")):
        return member, shift_start
    override = int(run.get("duty_server_id") or 0)
    if not override:
        return member, shift_start
    for candidate in active_members(group):
        if int(candidate["server_id"]) == override:
            return candidate, shift_start
    # 被指定的那台已经被移出组 / 删掉了：按时钟算，别拿一个不存在的当班
    return member, shift_start


def groups_of_server(server_id: int) -> list[int]:
    """这台机器被哪些**启用中**的换班组收着（DNS 巡检判断换班是否进行中用）。"""
    groups: list[int] = []
    for group in repo.list_groups():
        if not group.get("enabled"):
            continue
        for member in group.get("members") or []:
            if member.get("enabled") and int(member.get("server_id") or 0) == int(server_id):
                groups.append(int(group["id"]))
                break
    return groups


def dns_snapshot(group_id: int, duty_id: int) -> str:
    """换班开始那一刻，相关域名各自指向哪个 IP（JSON）。

    换完班要把域名指到新当班机，事件里那一步得写清楚「旧 IP → 新 IP」。
    不能等同步完再读当前值：中途巡检（dns.reconcile）可能先一步把记录改掉，
    那时 last_ip 已经是新值，旧地址就再也找不回来了。
    """
    names = list(repo.dns_records_of(dns.TARGET_GROUP, int(group_id)))
    for name in repo.dns_records_of(dns.TARGET_SERVER, int(duty_id)):
        if name not in names:
            names.append(name)
    snapshot: dict[str, str] = {}
    for name in names:
        row = repo.get_dns_record(name) or {}
        snapshot[name] = str(row.get("last_ip") or "")
    return json.dumps(snapshot, ensure_ascii=False)


def _day_key(group: dict[str, Any], now_ts: float) -> str:
    """组时区下的自然日（YYYY-MM-DD）。"""
    return dt.datetime.fromtimestamp(
        now_ts, _timezone(_group_timezone(group))
    ).strftime("%Y-%m-%d")


def _reset_month_day_failover(
    group: dict[str, Any], group_id: int, now_ts: float
) -> dict[str, Any] | None:
    """一月一轮：隔天 0 点把「起不来代班」清掉，重新请排班表上的当班上线。

    为什么只对一月一轮做：一天一轮（含 0–24 点时刻表）一段只有几小时，代班顶满
    一段也就几个小时，下一个换班点自然复位；一月一轮一段是几天甚至整月，一次代班
    就把这几天的机器费全压到顶班那台上 —— 用户排班的目的是「每台都摊一点、谁都
    不超月费上限」，顶班吃掉整段等于这个目的作废。

    复位时机是**组时区的自然日 0 点**，不是「转移后满 24 小时」：后者会跟着第一次
    转移的时刻往后漂，越滚越偏离排班的日界。

    只清 stall（起不来）。traffic（当班流量超限）不清：原当班已经烧到阈值，
    整段都不该再让它跑，代班必须顶满这一段。
    """
    if not is_month_cycle(group):
        return None
    scheduled = duty_at(group, now_ts)
    if scheduled is None:
        return None
    slot_key = str(scheduled[1])
    run = repo.get_run(group_id, slot_key) or {}
    if not run.get("failover") or str(run.get("failover_reason") or "") != "stall":
        return None
    if run.get("manual"):
        # 人指定了这一班由谁顶，隔天也不替人改回去
        return None

    today = _day_key(group, now_ts)
    done_day = str(run.get("duty_day") or "")
    if done_day == today:
        return None
    if not done_day:
        # 升级前发生的转移没有这个字段（老库刚补的列）。补成今天，
        # 免得升级那一瞬间把正在代班、跑得好好的机器换下来。
        return repo.upsert_run(group_id, slot_key, duty_day=today)

    # 跨日了：把代班状态和黑白名单一起清掉，本轮从「排班表当班」重新走一遍。
    # 不当代班、原当班恢复不了，180 秒后会按今天的流量重新挑一台顶 —— 每天重挑，
    # 三台以上的组会自然轮换，不会认死同一台。
    #
    # 复位等于「这一节重新开始」：上一节那条还在转圈的事件必须就地收尾，否则
    # 界面永远转圈，事件列表还会同时挂着两条「换班进行中」。
    old_event_id = int(run.get("event_id") or 0)
    old_event = repo.get_event(old_event_id) if old_event_id else None
    if old_event and str(old_event.get("status")) == "running":
        repo.add_event_progress(old_event_id, "跨日复位，本节重新尝试原当班")
        repo.finish_event(old_event_id, "done", "跨日复位，本节重新尝试原当班")
    for prefix in (
        f"duty_stall:{group_id}:{slot_key}",
        f"stall_nocand:{group_id}:{slot_key}",
        f"stall_disabled:{group_id}:{slot_key}",
        f"failover_tried:{group_id}:{slot_key}:",
        f"recovery:{group_id}:{slot_key}",
        f"start_err:{group_id}:{slot_key}",
    ):
        repo.clear_flags_like(prefix)
    log.info("一月一轮按天复位代班：组 %s 班次 %s 回到排班表当班", group_id, slot_key)
    return repo.upsert_run(
        group_id,
        slot_key,
        failover=0,
        failover_from=0,
        failover_reason="",
        duty_day="",
        duty_server_id=0,
        phase=PHASE_SWITCHING,
        stage="",
        event_id=0,
        started_at=int(now_ts),
    )


def next_switch_at(group: dict[str, Any], now_ts: float) -> float | None:
    members = active_members(group)
    if len(members) < 2:
        return None

    if is_month_cycle(group):
        return _next_switch_month(group, now_ts)

    if _is_timetable_group(group):
        return _next_switch_timetable(group, now_ts)

    total = sum(float(m["hours"]) for m in members) * 3600
    anchor = float(group.get("anchor_at") or group.get("created_at") or now_ts)
    elapsed = max(0.0, now_ts - anchor)
    cycle_index = int(elapsed // total)
    offset = elapsed - cycle_index * total
    accumulated = 0.0
    for member in members:
        accumulated += float(member["hours"]) * 3600
        if offset < accumulated:
            return anchor + cycle_index * total + accumulated
    return anchor + (cycle_index + 1) * total


def _next_switch_month(group: dict[str, Any], now_ts: float) -> float:
    """一月一轮：下一班开始 = 当前这段的最后一天结束（次日 0 点）。"""
    tz_name = _group_timezone(group)
    now = dt.datetime.fromtimestamp(now_ts, _timezone(tz_name))
    layout = _month_layout(group, now.year, now.month)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _member, start_day, end_day in layout:
        if start_day <= now.day <= end_day:
            # end_day 是月末时，加一天正好落到下月 1 号 0 点
            return int((month_start + dt.timedelta(days=end_day)).timestamp())
    # 兜底：下个月 1 号 0 点
    return int(
        (month_start + dt.timedelta(days=calendar.monthrange(now.year, now.month)[1])).timestamp()
    )


def _next_switch_timetable(group: dict[str, Any], now_ts: float) -> float:
    """固定时刻表模式：下一班开始时刻 = 当前班次的结束时刻。"""
    members = active_members(group)
    tz_name = _group_timezone(group)
    now = dt.datetime.fromtimestamp(now_ts, _timezone(tz_name))
    minute_of_day = now.hour * 60 + now.minute

    for member in members:
        start = _hhmm_to_minutes(member.get("start_time"))
        end = _hhmm_to_minutes(member.get("end_time"))
        if start < 0 or end < 0:
            continue
        if end > start:
            if start <= minute_of_day < end:
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                return int((today_start + dt.timedelta(minutes=end)).timestamp())
        else:
            if minute_of_day >= start or minute_of_day < end:
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                candidate = today_start + dt.timedelta(minutes=end)
                # 跨天班次结束在今天（比如现在 20:00 属于 18:00–06:00，结束在明天 06:00）
                if candidate <= now:
                    candidate += dt.timedelta(days=1)
                return int(candidate.timestamp())
    # 兜底
    return now_ts + 3600


def current_member_index(group: dict[str, Any], now_ts: float) -> int:
    members = active_members(group)
    picked = actual_duty(group, now_ts)
    if picked is None:
        return -1
    target = int(picked[0].get("server_id") or 0)
    for index, member in enumerate(members):
        if int(member.get("server_id") or 0) == target:
            return index
    return -1


# --------------------------------------------------------------------------
# 文案
# --------------------------------------------------------------------------
# 全部挪到 messages.py：那里逐字对齐用户脚本 cdt.py，改文案只改那一个文件。


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------

class Engine:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    # -- 总入口 -----------------------------------------------------------
    def reconcile_all(self) -> list[dict[str, Any]] | None:
        if not self._lock.acquire(blocking=False):
            log.debug("上一轮还没结束，跳过本次")
            # 返回 None 而不是空列表：「这一轮没跑」和「跑了但没事可做」必须能区分，
            # 否则手动点「立即对账」时接口会说「已触发」，实际什么都没发生。
            return None
        try:
            config = repo.get_settings()
            now = dt.datetime.now(_timezone(config.get("timezone") or "Asia/Shanghai"))
            now_ts = now.timestamp()

            grouped = self._grouped_server_ids()
            keepalive_ids = repo.keepalive_server_ids()
            results: list[dict[str, Any]] = []

            try:
                results.extend(self.reconcile_keepalives(config, now, grouped))
            except Exception as exc:  # noqa: BLE001
                log.exception("单机保活对账失败")
                repo.add_event(
                    "error", messages.ev("单机保活同步异常"), aliyun.brief_error(exc), level="error"
                )

            try:
                results.extend(
                    self.reconcile_idle_limits(config, now, grouped, keepalive_ids)
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("闲散机器超限对账失败")
                repo.add_event(
                    "error", messages.ev("闲散超限检查异常"), aliyun.brief_error(exc), level="error"
                )

            for group in repo.list_groups():
                try:
                    results.append(self.reconcile_group(group, config, now, now_ts))
                except Exception as exc:  # noqa: BLE001
                    log.exception("换班组 %s 对账失败", group.get("id"))
                    repo.add_event(
                        "error",
                        messages.ev("换班组同步异常", messages.obj_group(str(group.get('name') or ''))),
                        aliyun.brief_error(exc),
                        level="error",
                        group_id=int(group["id"]),
                    )
                    results.append({"group_id": group["id"], "error": str(exc)})

            # 读取告警收尾：机器被删或移出监控后不会再有恢复回调，
            # 统一在每轮末尾把这类「进行中」事件收掉，避免它永远转圈。
            # 判据是「还在库里」而不是「在换班组/保活里」：闲散机器这一轮也会
            # 读状态、也会建读取事件，只认换班组+保活的话，它的告警会被刚建好
            # 就当成孤儿关掉，每分钟开关一次。
            try:
                _close_orphan_read_errors(
                    {int(server["id"]) for server in repo.list_servers()}
                )
            except Exception:  # noqa: BLE001
                log.exception("读取告警收尾失败")
            return results
        finally:
            self._lock.release()

    def _grouped_server_ids(self) -> set[int]:
        """被换班组接管的机器。

        只算**启用的**组：组停用后成员就该回到闲散状态，单机保活要能接手。
        以前漏了这个判断，停用组 + 组员挂保活 → 保活认为「已被换班组接管」直接
        让位，结果两边都不管，机器就躺着没人开机。

        还要算**真能排出当班**的组（启用成员 ≥ 2）：只有一台的组排不出当班，
        `reconcile_group` 会在 `duty_at` 为 None 时直接 return，根本走不到流量
        判断。如果这里仍把它记成「已被接管」，那台机器既不被换班引擎管、又被
        保活和闲散检查跳过 —— 在跑且超阈值也没人停。少一台就交还给闲散检查。
        """
        ids: set[int] = set()
        for group in repo.list_groups():
            if not group.get("enabled"):
                continue
            members = active_members(group)
            if len(members) < 2:
                continue
            for member in members:
                ids.add(int(member["server_id"]))
        return ids

    # ==================================================================
    # 单机保活
    # ==================================================================
    def reconcile_keepalives(
        self,
        config: dict[str, str],
        now: dt.datetime,
        grouped: set[int],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in repo.list_keepalives():
            server_id = int(item["server_id"])
            if not item.get("enabled"):
                continue
            if server_id in grouped:
                # 换班组会接管这台机器，单机保活让位，避免两边互相拉扯。
                # 正在转的保活事件必须当场收掉：确认器对保活不设超时，留着它会
                # 一直每 5 秒查一次；等换班组把它拉起来，还会补发一条「疑被抢占」，
                # 而这次开机其实是换班组干的。
                _close_keepalive_event(
                    server_id,
                    str(item.get("label") or f"#{server_id}"),
                    "已由换班组接管，停止保活拉起",
                )
                repo.clear_flag(f"keepalive_resume_pending:{server_id}")
                flag = f"keepalive_conflict:{server_id}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "warning",
                        messages.ev("单机保活已停用", messages.obj_server(str(item.get('label') or ''))),
                        "已由换班组接管，单机保活自动停用，避免两边互相拉扯",
                        level="warning",
                        server_id=server_id,
                    )
                continue

            # 已经交回保活（换班组解散 / 停用 / 成员不足）：清掉「已停用」的去重标记，
            # 下次再被接管才会重新提醒一次，否则这条提示一辈子只出现一次。
            repo.clear_flag(f"keepalive_conflict:{server_id}")

            server = repo.get_server(server_id, decrypt_secret=True)
            if server is None:
                continue
            tz_name = config.get("timezone") or "Asia/Shanghai"
            label = str(server.get("label") or f"#{server_id}")
            try:
                _refresh(server)
            except Exception as exc:  # noqa: BLE001
                # 同一条事件里持续记录，带失败时刻与重试节奏；恢复时补上恢复时刻
                _read_error(
                    server_id=server_id,
                    title=messages.ev("状态读取异常", messages.obj_server(label)),
                    exc=exc,
                    tz_name=tz_name,
                )
                results.append({"keepalive": server_id, "error": str(exc)})
                continue
            _read_recovered(
                server_id=server_id,
                title=messages.ev("状态读取已恢复", messages.obj_server(label)),
                tz_name=tz_name,
            )

            # 运行窗口（可选）：时段 / 日期之外不拉起，还要主动停掉 ——
            # 只「不拉起」的话「只在 08:00–23:00 运行」根本不成立，机器会一直跑。
            in_window, window_text = keepalive_window(item, now)
            rest_flag = f"keepalive_rest:{server_id}"
            if not in_window:
                self._keepalive_rest(server, window_text)
                results.append(
                    {
                        "keepalive": server_id,
                        "label": server.get("label"),
                        "status": server.get("instance_status"),
                        "resting": True,
                    }
                )
                continue

            # 流量超限 → 按实例停机模式停机并暂停保活，不再无脑拉起。
            # 这是 1.1.15 补上的功能：之前单机保活只判断「停了就拉」，从不看阈值，
            # 机器超了阈值还在烧流量。停一次打一个 limit_stop 标记，标记在就不再
            # 碰它（否则下轮又被拉起）；流量回落到阈值以下（包重置 / 调高阈值）后
            # 自动清标记、恢复保活。
            if _over_threshold(server):
                if self._limit_stop_server(server_id, server, config, messages.SCOPE_KEEPALIVE):
                    results.append({"keepalive": server_id, "over_limit": True})
                # 超限期间保活是暂停的：正在转的「保活拉起」必须当场收掉，否则它
                # 永远 running（确认器对保活不设超时），流量回落后还会被继续续写。
                _close_keepalive_event(server_id, label, "流量超限，已停机并暂停保活")
                # 超限停机的机器不再走「停了就拉」：流量回落前它就该保持停机。
                continue
            # 流量回落到阈值以下（流量包重置 / 调高阈值）：清掉超限标记，
            # 恢复正常的「停了就拉」。否则标记会永久钉住这台机器。
            if repo.get_flag(f"limit_stop:{messages.SCOPE_KEEPALIVE}:{server_id}"):
                repo.clear_flag(f"limit_stop:{messages.SCOPE_KEEPALIVE}:{server_id}")

            status = str(server.get("instance_status") or "")
            # 刚从休止时段回来：先记一条「单机保活窗口恢复中」，开机指令也在这里发。
            # planned=True 时本轮不再走下面的「Stopped 就拉起」，否则一条恢复事件
            # 后面又会紧跟着建一条「保活拉起」，同一件事被记两遍。
            planned = False
            if repo.get_flag(rest_flag):
                repo.clear_flag(rest_flag)
                repo.clear_flag(f"keepalive_rest_fail:{server_id}")
                _keepalive_resume_note(server, window_text, status)
                planned = True

            if status == "Stopped" and not planned:
                # 保活就是「一分钟一轮」：这一轮看到它还是 Stopped，就说明上一轮没成，
                # 立刻再下发一次开机，不做退避。只有在 Stopped 才下发，Starting 之类的
                # 中间态不进这个分支，正在启动的实例不会被重复开。
                # 事件只保留一条：一台机器一直起不来，重试次数续写进同一条事件的进度，
                # 不再每次重试新开一条「保活拉起」。
                #
                # 走到这里就一定是「非窗口起点」的拉起：窗口到点那条走的是上面的
                # _keepalive_resume_note，压根不进这个分支。所以这里不再判断「是不是
                # 上一轮还看着它在跑」—— 掉线被抢占、手动关机后重新开保活、刚加进
                # 保活，对用户来说都是「我要求它在线，现在它不在」，一律走抢占恢复
                # 渠道并推通知；窗口自动开关那两端才是安静的。
                #
                # 唯一例外是上一轮窗口恢复没能开出机（还在 Stopping / 指令失败），
                # 那一次留了标记：这一轮补拉仍算「窗口到点」，继续安静。
                resume_pending = bool(repo.get_flag(f"keepalive_resume_pending:{server_id}"))
                repo.clear_flag(f"keepalive_resume_pending:{server_id}")
                state_label = messages.STATUS_LABELS.get(status, status or "未知")
                pending = repo.pending_event(server_id, "keepalive")
                try:
                    aliyun.start_instance(server)
                    # 保活拉起是异步的：建 running 事件，交给确认器轮询到 Running 后
                    # 补发「抢占恢复」通知并打完成标志，不在这里同步等状态。
                    #
                    # 一直起不来时：事件始终是那一条（可以横跨一整天），只把
                    # 「试了多少次」累加进标记，概要行跟着改 —— 收起的事件列表里
                    # 一眼就能看到「这台今天试了几百次还是没起来」，不用点开。
                    _keepalive_retry_note(
                        server_id=server_id,
                        label=str(server.get("label") or f"#{server_id}"),
                        state_label=state_label,
                        pending=pending,
                        quiet=resume_pending,
                    )
                    if repo.get_flag(f"keepalive_fail:{server_id}"):
                        repo.clear_flag(f"keepalive_fail:{server_id}")
                except Exception as exc:  # noqa: BLE001
                    # 开机指令没发出去（库存不足 / 限流 / 欠费）同样算一次尝试：
                    # 以前这里只建一条 error 事件，之后每分钟静默重试，用户在界面上
                    # 看不出「还在试、试了多少次」，那条 error 还挂着「完成」徽章。
                    # 现在并进同一条 running 事件，概览写清次数与失败原因。
                    _keepalive_retry_note(
                        server_id=server_id,
                        label=str(server.get("label") or f"#{server_id}"),
                        state_label=state_label,
                        pending=pending,
                        # _brief_exc 会剥掉异常自带的「启动实例失败：」前缀，
                        # 否则概览里会出现「启动指令失败：启动实例失败：…」两层失败
                        error=_brief_exc(exc),
                        quiet=resume_pending,
                    )
                # 已经 Running 时不再下发：抢占恢复的通知由确认器在「Running 到位」
                # 那一刻统一发出（scheduler._confirm_pending_events）。
            elif status == "Running":
                # 真的跑起来了：计数归零，下一次中断从 1 重新数。
                # 注意只认 Running——Starting / Pending 这类中间态不动计数，
                # 否则「停→启动中→又被抢→停」会来回清零，里程碑永远到不了。
                repo.clear_flag(f"keepalive_tries:{server_id}")
                # 窗口恢复那轮若是 Starting（还没停稳就重开），会留一个
                # keepalive_resume_pending：「这次是窗口到点」只对紧邻的下一轮有效。
                # 现在已经跑起来了，标记必须作废，否则将来真被抢占时会被当成
                # 窗口恢复 —— 不推通知、标题也错成「窗口恢复」。
                repo.clear_flag(f"keepalive_resume_pending:{server_id}")
            results.append(
                {
                    "keepalive": server_id,
                    "label": server.get("label"),
                    "status": server.get("instance_status"),
                    "traffic": server.get("traffic_used"),
                }
            )
        return results

    # -- 休止时段（单机保活的运行窗口外） ---------------------------------
    def _keepalive_rest(self, server: dict[str, Any], window_text: str) -> None:
        """窗口外：主动停机，只落事件、**不发任何通知**。

        用户在保活里设了「每天 08:00–23:00」，23:00 到点自动停掉是预期内的动作，
        推一条「实例被停」只会变成噪音；同理窗口开了自动拉起也不发「抢占恢复」。
        这些事在事件列表里能看到就够了。

        事件只在真正停稳（或本来就停着）之后才落一条，措辞与换班/超限停机一致：
        概览一句结论，展开看得到「离开运行窗口 → 下发停机 → 结果」。
        """
        server_id = int(server["id"])
        label = str(server.get("label") or f"#{server_id}")
        rest_flag = f"keepalive_rest:{server_id}"
        fail_flag = f"keepalive_rest_fail:{server_id}"
        # 确认器抢先发现「已出窗口」时会把那条还在转的事件改成「等停机」
        # 并置上这个标记，这里就别再记一条同名的。
        logged = bool(repo.get_flag(rest_flag))
        # 之前记过「停机失败」吗？成功后要把那条事件的结论补回去，
        # 否则它在列表里永远写着「停机指令失败」，用户不知道后来停成没有。
        was_failed = bool(repo.get_flag(fail_flag))

        if _is_stopped_any_mode(server):
            if logged:
                # 确认器先把事件转成了「保活暂停」：机器本来就已经停着，把真实模式补回去
                _keepalive_pause_progress(server_id, str(server.get("stopped_mode") or ""))
                return
            repo.set_flag(rest_flag)
            repo.clear_flag(fail_flag)
            mode = str(server.get("stopped_mode") or "")
            if was_failed:
                _keepalive_rest_recovered(server_id, mode)
            _keepalive_pause_note(
                server_id,
                label,
                window_text,
                f"已暂停（{messages.mode_label(mode) or '停机'}）",
                [f"实例已是停机状态（{messages.mode_label(mode) or '已停机'}），未重复下发指令"],
            )
            return

        # 已经在停的路上：等它停稳，别每分钟重复下发同一条停机指令。
        if str(server.get("instance_status") or "") == "Stopping":
            return

        try:
            mode = stop_server_by_pref(server)
        except Exception as exc:  # noqa: BLE001
            error = _brief_exc(exc)
            # 没停成：不落「已暂停」（那是假的），下一轮接着试；失败原因只记一次，
            # 否则每分钟一条「停机失败」能把最近事件刷满。
            if not logged and not repo.get_flag(fail_flag):
                repo.set_flag(fail_flag)
                _keepalive_rest_fail_note(server_id, label, error)
            return

        repo.clear_flag(fail_flag)
        if was_failed:
            _keepalive_rest_recovered(server_id, mode)
        if logged:
            # 确认器先把事件转成了「等停机」，这轮才真正停机：把指令补回去，
            # 收尾等确认器看到 Stopped。
            _keepalive_pause_progress(server_id, mode)
            return
        repo.set_flag(rest_flag)
        _keepalive_pause_note(
            server_id,
            label,
            window_text,
            "等待停机到位",
            [f"已下发停机指令（{messages.mode_label(mode)}）"],
            await_stop=True,
        )

    # -- 超限停机（保活 / 闲散共用） ---------------------------------------
    def _limit_stop_server(
        self,
        server_id: int,
        server: dict[str, Any],
        config: dict[str, str],
        scope: str,
    ) -> bool:
        """单台机器超限：节省停机一次 + 打标记 + 推报告。返回是否真的发了指令。

        标记 key 里带 scope，保活和闲散各记各的（同一台机器不会同时处于两种状态，
        但分开写更清楚，也避免一种范围的清除误伤另一种）。
        """
        flag = f"limit_stop:{scope}:{server_id}"
        if repo.get_flag(flag):
            return False

        # 已经物理停机（含不支持节省停机的 Not-applicable）就不重发指令
        mode = str(server.get("stopped_mode") or "")
        if not _is_stopped_any_mode(server):
            try:
                mode = stop_server_by_pref(server)
            except Exception as exc:  # noqa: BLE001
                err = f"limit_stop_err:{scope}:{server_id}"
                if not repo.get_flag(err):
                    repo.set_flag(err)
                    repo.add_event(
                        "error",
                        messages.ev("流量超限停机失败", messages.obj_server(str(server.get('label') or ''))),
                        aliyun.brief_error(exc),
                        level="error",
                        server_id=server_id,
                    )
                return False
            repo.clear_flag(f"limit_stop_err:{scope}:{server_id}")

        repo.set_flag(flag)
        # 标题与通知口径一致：单机保活还要说明保活已暂停，否则事件只说「已停机」，
        # 用户不知道这台还会不会被自动拉起。
        # 标题只留一个逗号：保活超限的结论是「保活已暂停」，停机模式交给概览；
        # 闲散超限的结论才是停机本身。三个短句堆在一个括号前读不过来。
        if scope == messages.SCOPE_KEEPALIVE:
            tail = "保活已暂停"
        else:
            tail = messages.stop_phrase(mode)
        reason = f"已用 {_used_gb(server):.2f} GB（阈值 {float(server.get('threshold_gb') or 0):g} GB）"
        stopped_already = _is_stopped_any_mode(server)
        event_id = repo.add_event(
            "limit_stop",
            messages.ev(f"流量超限，{tail}", messages.obj_server(str(server.get('label') or ''))),
            f"等待停机到位 / {reason}",
            level="warning",
            server_id=server_id,
            status="running",
            pending_status="Stopped",
            pending_kind="limit_stop",
        )
        repo.add_event_progress(event_id, f"已用 {_used_gb(server):.2f} GB，超过阈值 {float(server.get('threshold_gb') or 0):g} GB")
        # 已经停着的机器没有「下发指令」这一步，写清楚，别让过程看起来断了一截
        repo.add_event_progress(
            event_id,
            f"实例已是停机状态（{messages.mode_label(mode) or '已停机'}），未重复下发指令"
            if stopped_already
            else f"已下发停机指令（{messages.mode_label(mode)}）",
        )
        # 事件等到 Stopped 才由确认器收尾：指令下发 ≠ 真停了，这里不能先写「已完成」
        if config.get("notify_on_limit_stop") == "1":
            results = notify.send_all(config, messages.over_limit(server, scope, mode=mode))
            notify.note_notify(event_id, results)
        return True

    # ==================================================================
    # 闲散机器超限停机
    # ==================================================================
    def reconcile_idle_limits(
        self,
        config: dict[str, str],
        now: dt.datetime,
        grouped: set[int],
        keepalive_ids: set[int],
    ) -> list[dict[str, Any]]:
        """闲散机器（没进任何换班组、也没挂单机保活）也要超限停机。

        这是 1.1.15 补上的：之前闲散机器完全没人巡检，超了阈值一直在跑。
        """
        results: list[dict[str, Any]] = []
        tz_name = config.get("timezone") or "Asia/Shanghai"
        for server in repo.list_servers(decrypt_secret=True):
            server_id = int(server["id"])
            if server_id in grouped or server_id in keepalive_ids:
                continue
            label = str(server.get("label") or f"#{server_id}")
            try:
                _refresh(server)
            except Exception as exc:  # noqa: BLE001
                # 以前这里是静默 continue：卡片一直挂着上一轮的旧状态和旧 IP，
                # 用户以为机器在跑。保活和换班组都有读取事件，闲散机不该是盲区。
                _read_error(
                    server_id=server_id,
                    title=messages.ev("状态读取异常", messages.obj_server(label)),
                    exc=exc,
                    tz_name=tz_name,
                )
                continue
            _read_recovered(
                server_id=server_id,
                title=messages.ev("状态读取已恢复", messages.obj_server(label)),
                tz_name=tz_name,
            )
            if _over_threshold(server):
                if self._limit_stop_server(server_id, server, config, messages.SCOPE_IDLE):
                    results.append({"idle": server_id, "over_limit": True})
            elif repo.get_flag(f"limit_stop:{messages.SCOPE_IDLE}:{server_id}"):
                # 流量回落：清标记，之后这台机器再超限还能再停
                repo.clear_flag(f"limit_stop:{messages.SCOPE_IDLE}:{server_id}")
        return results

    # ==================================================================
    # 联合换班
    # ==================================================================
    def reconcile_group(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        now: dt.datetime,
        now_ts: float,
        notify_enabled: bool = True,
    ) -> dict[str, Any]:
        group_id = int(group["id"])
        name = group.get("name") or f"#{group_id}"

        if not group.get("enabled"):
            # 停用组时把仍在转的事件收尾：否则保存停用后，最近事件里那条
            # 「换班进行中」会一直转圈，看不出是被停用中止的。
            _close_group_events(group_id, "换班组已停用，本节流程中止")
            return {"group_id": group_id, "skipped": "disabled"}

        # 时刻表模式需要知道时区
        group["_timezone"] = config.get("timezone") or "Asia/Shanghai"

        # 一月一轮：隔天 0 点先把「起不来代班」复位，再算当班。
        # 顺序不能反 —— actual_duty 会读 run.failover 覆盖当班，先算就还是代班那台。
        _reset_month_day_failover(group, group_id, now_ts)

        picked = actual_duty(group, now_ts)
        if picked is None:
            # 所有成员都停用 / 只剩一台：排不出当班，正在转的事件同样要收尾
            _close_group_events(group_id, "启用成员不足两台，排不出当班，流程中止")
            return {"group_id": group_id, "skipped": "成员不足两台"}
        duty_member, shift_start = picked
        slot_key = str(shift_start)
        run = repo.get_run(group_id, slot_key) or {}

        # manual 和 failover 是两个独立的含义：manual 是「人切的」，failover 是
        # 「流量超限自动转的」。当班覆盖已经由 actual_duty 统一处理，这里只读标志
        # 决定后续怎么描述这次换班（事件标题 / 报告标题 / 阶段）。
        manual_switch = bool(run.get("manual"))
        failed_over = bool(run.get("failover"))
        # 上一轮如果已经因「起不来」转过，这两项要从库里接着用：
        # 结算通知里「被接管账号」靠 failover_from 取，原因靠 failover_reason 说。
        failover_from = int(run.get("failover_from") or 0)
        failover_reason = str(run.get("failover_reason") or "")

        duty = repo.get_server(int(duty_member["server_id"]), decrypt_secret=True)
        if duty is None:
            return {"group_id": group_id, "skipped": "当班服务器已被删除"}

        others: list[dict[str, Any]] = []
        for member in active_members(group):
            if int(member["server_id"]) == int(duty_member["server_id"]):
                continue
            server = repo.get_server(int(member["server_id"]), decrypt_secret=True)
            if server is not None:
                others.append(server)
        if not others:
            return {"group_id": group_id, "skipped": "没有其他成员"}

        # 1) 读取全部成员实时状态
        duty_error = ""
        try:
            _refresh(duty)
        except Exception as exc:  # noqa: BLE001
            duty_error = str(exc)
        healthy_others: list[dict[str, Any]] = []
        unreadable: set[int] = set()
        tz_name = config.get("timezone") or "Asia/Shanghai"
        for server in others:
            member_id = int(server["id"])
            member_label = str(server.get("label") or f"#{member_id}")
            try:
                _refresh(server)
                healthy_others.append(server)
                _read_recovered(
                    server_id=member_id,
                    title=messages.ev("状态读取已恢复", messages.obj_member(str(name), member_label)),
                    tz_name=tz_name,
                )
            except Exception as exc:  # noqa: BLE001
                unreadable.add(member_id)
                _read_error(
                    server_id=member_id,
                    title=messages.ev("状态读取异常", messages.obj_member(str(name), member_label)),
                    exc=exc,
                    group_id=group_id,
                    tz_name=tz_name,
                )

        # 当班状态读不到：本轮不许凭库里的旧快照发指令。
        # 旧快照说「停着」而机器其实在跑，就会给运行中的实例发开机，阿里云回
        # IncorrectInstanceStatus，事件里冒出一条「当班启动失败」，看着像系统坏了；
        # 反过来旧快照说「跑着」而机器已经停了，就漏掉一次该有的拉起。
        # 读不到就等下一轮 —— 拿不到状态时「什么都不做」比「猜着做」安全。
        if duty_error:
            _read_error(
                server_id=int(duty["id"]),
                title=messages.ev("状态读取异常", messages.obj_duty(str(name), str(duty.get('label') or ''))),
                exc=duty_error,
                group_id=group_id,
                tz_name=tz_name,
            )
            return {"group_id": group_id, "state_unreadable": duty_error}
        _read_recovered(
            server_id=int(duty["id"]),
            title=messages.ev("状态读取已恢复", messages.obj_duty(str(name), str(duty.get('label') or ''))),
            tz_name=tz_name,
        )

        # 2) 当班超限 → 找一台没超的接班；找不到就停机，绝不让超限的当班继续跑
        failover = False
        breaker = False
        breaker_reason = ""
        if _over_threshold(duty) and group.get("failover_enabled") and not duty_error:
            candidates = [s for s in healthy_others if not _over_threshold(s)]
            if candidates:
                replacement = min(candidates, key=lambda s: float(s.get("traffic_used") or 0))
                # 决策和结果写进同一条事件：这条先以「进行中」建出来，后续启动、
                # 停待机、DDNS 的每一步都往它里面追加，结算时改成「故障转移」。
                # 以前这里建的是一条 0 秒完成的决策事件，结算再建一条结果事件，
                # 一次转移在事件列表里留下两条标题几乎一样的记录。
                decision_event = repo.add_event(
                    "failover",
                    messages.ev("当班超限，故障转移", messages.obj_duty(str(name), str(duty.get('label') or ''))),
                    f"等待接管到位（{replacement.get('label')}）",
                    level="warning",
                    group_id=group_id,
                    server_id=int(replacement["id"]),
                    status="running",
                )
                # 摘要结算时会被改写成结论（当班 B / 待机 N 台），「谁烧超了、换给谁」
                # 这件事必须留在展开的步骤里，否则事件点开就只剩结果、没有原因。
                repo.add_event_progress(
                    decision_event,
                    f"当班 {duty.get('label')} 流量超限（{_used_gb(duty):.2f} GB / "
                    f"阈值 {float(duty.get('threshold_gb') or 0):g} GB），改由 "
                    f"{replacement.get('label')} 接班",
                )
                others = [s for s in others if int(s["id"]) != int(replacement["id"])]
                others.append(duty)
                failover_from = int(duty["id"])
                failover_reason = "traffic"
                duty = replacement
                failover = True
                # 和被接管者一起落库：结算通知里「被接管账号」按它取，
                # 多台都超限时才不会挑错人。
                #
                # 同时把这班次当成「开始切换」：新当班要开机、要等 IP、要停别人，
                # 这是一段新的换班过程，10 分钟超时也该从新当班重新算。
                repo.upsert_run(
                    group_id,
                    slot_key,
                    phase=PHASE_SWITCHING,
                    stage="",
                    event_id=decision_event,
                    started_at=int(time.time()),
                    failover_from=failover_from,
                    failover_reason=failover_reason,
                )
                # 本地 run 快照同步过来：下面 previously_settled 按它判断，
                # 还拿着旧的 settled 会把这次转移当成「已结算班次的恢复」，
                # 又开一条「当班自动拉起中」。
                run = repo.get_run(group_id, slot_key) or {}
            else:
                # 找不到替班的两种情形：其他成员都超了，或者其他成员状态读不到。
                # 两种都不该让已经超限的当班继续跑 —— 早先「读不到就保守维持当班」，
                # 等于让一台已经到阈值的机器接着烧流量，与「限流阈值」本身就矛盾了。
                # 一律按全局熔断处理：全部节省停机。
                breaker = True
                if not healthy_others:
                    breaker_reason = "其他成员状态读取失败，无法确认可接班的成员"
                elif len(healthy_others) < len(others):
                    breaker_reason = "可读取的成员流量均已超限，其余成员状态读取失败"
                else:
                    breaker_reason = "全部成员流量超限"

        if breaker:
            return self._handle_breaker(
                group, config, now, slot_key, duty, others, notify_enabled, breaker_reason
            )

        if _over_threshold(duty) and not failover:
            return self._handle_limit_stop(group, config, now, slot_key, duty, others, notify_enabled)

        # 抢占恢复跨多轮，用 flag 记住「这次是恢复而不是换班」（本地变量活不过一轮）；
        # event_id 是本班次正在进行的那条事件，后面「起不来」要复用它改成故障转移结论。
        recovery_flag = f"recovery:{group_id}:{slot_key}"
        event_id = int(run.get("event_id") or 0)

        # 2.5) 当班起不来 → 和「当班流量超限」走同一套故障转移。
        #      被抢占后拉不起来、或启动指令一直卡在中间态，超时后换一台顶这一班；
        #      否则整班空着只能等人工，与「只留一台在线」的目的相悖。
        #
        #      计时用独立 flag 而不是 run.started_at：后者的语义是「本班次从什么时候
        #      开始处理」（换班超时按它算），正常换班里它不等同于「这次拉起多久了」。
        #
        #      阈值按状态分两档：Stopped 是「确实没在跑」，180 秒足够判死；Starting /
        #      Pending 是「正在起」，Windows 实例正常开机就要 2–3 分钟，用 180 秒去卡
        #      等于把慢启动判成起不来 —— 换完人还会把正在启动的原当班主动停机，白启动
        #      一次。这一段给 DUTY_START_GRACE（默认 480 秒，比 10 分钟的换班超时
        #      早一档，先转移再报警），真卡死照样转移。
        stall_flag = f"duty_stall:{group_id}:{slot_key}"
        duty_now = str(duty.get("instance_status") or "")
        stall_limit = (
            settings.DUTY_START_GRACE
            if duty_now in TRANSIENT_STATUS
            else settings.DUTY_STALL_SECONDS
        )
        stall = False
        if not failover and not manual_switch and duty_now != "Running":
            if repo.flag_age(stall_flag) < 0:
                # 第一次发现当班没在跑：先记时，本轮照常尝试拉起（第 3 步）。
                repo.set_flag(stall_flag)
            elif repo.flag_age(stall_flag) >= stall_limit:
                stall = True
        else:
            # 当班已经在跑，或者这次是人手动切的（人盯着界面，系统别自作主张）
            repo.clear_flag(stall_flag)

        if stall:
            if not group.get("failover_enabled"):
                # 组里关了「允许流量故障转移」：不替人做决定，但要留一条痕迹，
                # 否则用户只会看到「这台一直起不来」而不知道系统其实动过手。
                flag = f"stall_disabled:{group_id}:{slot_key}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "warning",
                        messages.ev("当班未就绪", messages.obj_duty(str(name), str(duty.get('label') or ''))),
                        f"保持等待（未启用故障转移） / 已等待 {stall_limit} 秒",
                        level="warning",
                        group_id=group_id,
                        server_id=int(duty["id"]),
                    )
            else:
                # 同一个班次里已经顶过班的不再重复挑，否则几台都起不来时会来回踢皮球。
                tried = {
                    int(s["id"])
                    for s in healthy_others
                    if repo.get_flag(f"failover_tried:{group_id}:{slot_key}:{int(s['id'])}")
                }
                candidates = [
                    s
                    for s in healthy_others
                    if int(s["id"]) not in tried and not _over_threshold(s)
                ]
                status_text = messages.STATUS_LABELS.get(
                    str(duty.get("instance_status") or ""), str(duty.get("instance_status") or "未知")
                )
                if candidates:
                    # 和流量超限挑人一致：优先流量用得最少的，接着省流量额度
                    replacement = min(candidates, key=lambda s: float(s.get("traffic_used") or 0))
                    repo.set_flag(f"failover_tried:{group_id}:{slot_key}:{int(duty['id'])}")
                    title = messages.ev(
                        "当班未就绪，故障转移",
                        messages.obj_duty(str(name), str(duty.get('label') or '')),
                    )
                    detail = f"等待接管到位（{replacement.get('label')}）"
                    if event_id:
                        # 本班次已经有一条在进行的事件（抢占恢复的「已自动拉起」，
                        # 或跨日复位后新建的「换班进行中」）。当班换人不是另开一件事，
                        # 而是同一件事的下一段：就地改题、继续往里记，结算时再统一
                        # 改成「故障转移（保活超时）」。—— 以前在这里 finish 掉、
                        # 另开一条，一次转移在事件列表里留下两条标题相近的记录。
                        repo.update_event(
                            event_id, title=title, detail=detail, kind="shift_failover"
                        )
                        repo.add_event_progress(
                            event_id, f"{duty.get('label')}（{status_text}）已 {stall_limit} 秒未就绪，改由 {replacement.get('label')} 接班"
                        )
                    else:
                        event_id = repo.add_event(
                            "shift_failover",
                            title,
                            detail,
                            level="warning",
                            group_id=group_id,
                            server_id=int(replacement["id"]),
                            status="running",
                        )
                    repo.clear_flag(recovery_flag)
                    others = [s for s in others if int(s["id"]) != int(replacement["id"])]
                    others.append(duty)
                    failover_from = int(duty["id"])
                    failover_reason = "stall"
                    duty = replacement
                    failover = True
                    repo.clear_flag(stall_flag)
                    # 当班换人等于开了一段新的换班过程：阶段回到切换中、计时与
                    # 10 分钟超时都从新当班重新算，但事件还是同一条（继续往下记）。
                    # 一月一轮额外记下「今天是哪一天代班的」，隔天 0 点靠它判断
                    # 该不该把班交回排班表；一天一轮段本来就短，用不到这个字段。
                    day_fields = (
                        {"duty_day": _day_key(group, now_ts)}
                        if is_month_cycle(group)
                        else {}
                    )
                    repo.upsert_run(
                        group_id,
                        slot_key,
                        phase=PHASE_SWITCHING,
                        stage="",
                        event_id=event_id,
                        started_at=int(time.time()),
                        failover=1,
                        failover_from=failover_from,
                        failover_reason=failover_reason,
                        **day_fields,
                    )
                    # 把本地的 run 快照同步过来：后面「previously_settled」按它判断，
                    # 还拿着旧的 settled 会再走一遍「恢复」分支，多记一条事件。
                    run = repo.get_run(group_id, slot_key) or {}
                    event_id = 0
                else:
                    flag = f"stall_nocand:{group_id}:{slot_key}"
                    if not repo.get_flag(flag):
                        repo.set_flag(flag)
                        repo.add_event(
                            "error",
                            messages.ev("当班未就绪，无成员接替", messages.obj_group(str(name))),
                            f"{duty.get('label')}（{status_text}）等待 {stall_limit} 秒仍未运行，"
                            "其余成员要么流量也已超限，要么状态读取失败",
                            level="error",
                            group_id=group_id,
                            server_id=int(duty["id"]),
                        )

        # 故障转移标志要在「启动当班」之前就确定并持久化，否则转移后下一轮会
        # 因为 run.failover 没写进去而重新判定超限、重复记「故障转移」事件。
        failover_flag = 1 if (failover or failed_over) else 0

        # 3) 保证当班运行 —— 只做「启动当班」，不碰非当班。
        #    整个换班过程（启动当班 → 停非当班 → DDNS）都记在同一条事件里：
        #    以前每一步都单开一条事件，界面上一轮换班会冒出三条互不相干的记录。
        previously_settled = run.get("phase") in _SETTLED_LIKE_PHASES
        # 已结算的班次被打回 switching（典型是待机成员状态读不到），换班超时不能
        # 再用建行时写死的 started_at —— 那可能是几天前，一算几千分钟、必报超时。
        # 打回时把锚点重置为当前时刻，之后仍按阈值正常计；真持续异常照样会报。
        restart_clock = {"started_at": int(now_ts)} if previously_settled else {}
        duty_status = str(duty.get("instance_status") or "")
        duty_not_running = duty_status not in ("Running",) and duty_status not in TRANSIENT_STATUS

        if duty_not_running:
            if previously_settled:
                # 已结算的班次里当班又不跑了（被抢占 / 被手动停掉）→ 单独记一条恢复事件。
                # 这不是「换班」，不计换班超时：started_at 从这次拉起重新计时。
                repo.set_flag(recovery_flag)
                event_id = repo.add_event(
                    "recovery",
                    messages.ev("当班自动拉起中", messages.obj_duty(str(name), str(duty.get('label') or ''))),
                    # 概览只写「在等什么」，和单机保活的运行中事件同一套说法；
                    # 原来是什么状态放在展开的步骤里（检测到当班 X 未运行（Stopped））
                    "等待运行到位",
                    level="warning",
                    group_id=group_id,
                    server_id=int(duty["id"]),
                    status="running",
                )
                repo.add_event_progress(
                    event_id, f"检测到当班 {duty.get('label')} 未运行（{duty_status or '未知'}）"
                )
                repo.upsert_run(
                    group_id, slot_key, event_id=event_id, started_at=int(time.time())
                )
            else:
                event_id = self._shift_event(
                    group_id, slot_key, name, duty.get("label") or "", int(duty["id"])
                )

            try:
                aliyun.start_instance(duty)
                repo.clear_flag(f"start_err:{group_id}:{slot_key}")
                repo.add_event_progress(event_id, f"已下发启动当班 {duty.get('label')}")
                # 上一轮失败时概览写的是失败原因，这里开机成功就把它拨回「等待」，
                # 别让列表里刚发起的这次启动还挂着上次的「当班启动失败」。
                repo.update_event(event_id, detail="等待开机到位")
                duty_status = "Starting"
            except Exception as exc:  # noqa: BLE001
                flag = f"start_err:{group_id}:{slot_key}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "error",
                        messages.ev("当班启动失败", messages.obj_duty(str(name), str(duty.get('label') or ''))),
                        aliyun.brief_error(exc),
                        level="error",
                        group_id=group_id,
                        server_id=int(duty["id"]),
                    )
                # 启动失败不代表这场换班结束：下一轮还会再试。事件保持 running 别 finish、
                # 别把 event_id 清零，否则每轮巡检都会新开一条「换班进行中」，持续欠费 /
                # 库存不足时一小时就能堆出 60 条，把别的机器记录全挤掉。失败原因追加进
                # 进度（once 去重），概览改成失败态，点开能看到，收起也能一眼看出没起来。
                repo.update_event(event_id, detail=f"当班启动失败，等待重试：{_brief_exc(exc)}")
                repo.add_event_progress(event_id, f"当班启动失败：{_brief_exc(exc)}", once=True)
                repo.upsert_run(
                    group_id,
                    slot_key,
                    phase=PHASE_ERROR,
                    stage="",
                    event_id=event_id,
                    duty_server_id=int(duty["id"]),
                    standby_server_ids=_encode_ids(others),
                    failover=failover_flag,
                    note=str(exc)[:200],
                )
                return {"group_id": group_id, "error": str(exc)}
        elif not previously_settled:
            # 当班本来就在跑：先把本班次的换班事件建好，后续进度往里追加
            event_id = self._shift_event(
                    group_id, slot_key, name, duty.get("label") or "", int(duty["id"])
                )

        # 当班还在启动中（Starting/Pending），本轮不做任何停非当班的动作，等它 Running
        if duty_status in TRANSIENT_STATUS:
            repo.upsert_run(
                group_id,
                slot_key,
                phase=PHASE_SWITCHING,
                stage="starting_duty",
                event_id=event_id,
                duty_server_id=int(duty["id"]),
                standby_server_ids=_encode_ids(others),
                failover=failover_flag,
                attempts=int(run.get("attempts") or 0) + 1,
                **restart_clock,
            )
            self._maybe_report_timeout(
                group, config, now_ts, slot_key, shift_start, duty, others, notify_enabled
            )
            return {"group_id": group_id, "slot": slot_key, "ready": False, "stage": "starting_duty"}

        # 4) 当班已 Running：先确认公网 IP 就绪（DDNS 启用时）。
        #    ECS 开机后 EIP 绑定有几秒延迟，IP 没就绪就等下一轮，避免把空 IP 写进解析。
        ddns_enabled = config.get("ddns_enabled") == "1"
        has_ip = bool(str(duty.get("public_ip") or "").strip())
        if ddns_enabled and not has_ip:
            repo.upsert_run(
                group_id,
                slot_key,
                phase=PHASE_SWITCHING,
                stage="starting_duty",
                event_id=event_id,
                duty_server_id=int(duty["id"]),
                standby_server_ids=_encode_ids(others),
                failover=failover_flag,
                attempts=int(run.get("attempts") or 0) + 1,
                **restart_clock,
            )
            self._maybe_report_timeout(
                group, config, now_ts, slot_key, shift_start, duty, others, notify_enabled
            )
            return {"group_id": group_id, "slot": slot_key, "ready": False, "stage": "waiting_ip"}

        if not previously_settled:
            repo.add_event_progress(event_id, f"当班 {duty.get('label')} 已开机", once=True)

        # 5) 停非当班（此时才停）。物理停机（StopCharging / KeepCharging / Stop /
        #    Not-applicable）就算停好，不再重发；普通停机照常收费但已到位，无需死循环。
        all_stopped = True
        for server in others:
            if int(server["id"]) in unreadable:
                # 状态读不到的成员：不下发停机（不知道它现在是什么状态，重发也没用），
                # 但也不能算「已到位」—— 否则这一班会带着一台可能还在跑的机器结算。
                all_stopped = False
                continue
            if _is_stopped_any_mode(server):
                continue
            if server.get("instance_status") in TRANSIENT_STATUS:
                # 已在停机过程中，等它落地。
                # 例外：本班次里「起不来被换掉」的那台，卡在启动中不能再等 ——
                # 它已经不是当班了，等下去它真跑起来就是两台同时在线。
                stalled_out = (
                    failover_reason == "stall" and int(server["id"]) == failover_from
                )
                if not stalled_out:
                    all_stopped = False
                    continue
            try:
                stop_mode = stop_server_by_pref(server)
                repo.clear_flag(f"stop_err:{server['id']}")
                # 停机模式按实例设置走，事件里写实际生效的那个：选了节省停机但规格
                # 不支持会退化成普通停机，这里不写清就是在骗人。
                repo.add_event_progress(
                    event_id,
                    f"已下发停机 {server.get('label')}（{messages.mode_label(stop_mode)}）",
                )
            except Exception as exc:  # noqa: BLE001
                flag = f"stop_err:{server['id']}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "error",
                        messages.ev("非当班停机失败", messages.obj_member(str(name), str(server.get('label') or ''))),
                        aliyun.brief_error(exc),
                        level="error",
                        group_id=group_id,
                        server_id=int(server["id"]),
                    )
            all_stopped = False

        if all_stopped and not previously_settled:
            repo.add_event_progress(event_id, f"非当班{messages.stop_tail(others)}", once=True)

        # 6) 全部实例状态确认到位之后才动 DDNS。
        #    以前是「当班一开机就同步」，非当班还在跑的时候域名已经指过去了；而且
        #    停机那一轮还会再同步一次，进度里冒出重复的「DDNS：✅ 已同步」这种没意义的一行。
        ddns_text = ""
        # 概览和通知是两套文案：status 给通知（短），brief 给事件概览（带「已更新 / IP 未变化」）。
        # 换班概览以前错用了 status，列表里就只剩「✅ 已同步」，和保活、手动开机不一致。
        ddns_brief = ""
        ddns_report: dns.DDNSReport | None = None
        ddns_ok = True
        if all_stopped:
            # 首次结算去 Cloudflare 查一次真实值（用户明确要求的时间点）；
            # 之后每轮结算仍走本地缓存，避免每分钟打一次读请求。
            ddns_report = self._sync_ddns(
                duty,
                config,
                dns.TARGET_GROUP,
                group_id,
                verify=not previously_settled,
            )
            ddns_text = ddns_report.status
            ddns_brief = ddns_report.brief
            # DDNS 只有「已设置且已绑定」时才要求同步成功，否则不卡流程：
            #   - 未启用 / 未绑定（⏸ 开头）→ 跳过确认，不阻塞
            #   - 已绑定但同步失败（❌ / ⚠️ 开头）→ 本轮不算就绪，等下一轮重试
            # 换班组绑的域名和当班机器自己绑的域名都算：任何一条失败都要重试
            ddns_bound = bool(repo.dns_records_of(dns.TARGET_GROUP, group_id)) or bool(
                repo.dns_records_of(dns.TARGET_SERVER, int(duty["id"]))
            )
            # 一台机器可能挂着好几条域名，汇总文案「✅ 已同步 / ❌ 更新失败」开头也是 ✅，
            # 单看前缀会把「有一条没指过去」当成全部成功，于是事件打完成、通知照发。
            ddns_ok = (not ddns_enabled) or (not ddns_bound) or dns.is_success(ddns_text)

            # DDNS 失败不能无限期卡住结算：绑定记录被删、Token 过期这类配置问题
            # 重试到天亮也不会好，而每轮都「不算就绪」→ 10 分钟超时报告，看着像
            # 机器没切过去，实际早切好了。等满 _DDNS_WAIT_SECONDS 就照常往下走，
            # 失败原因仍在进度和报告里（ddns_text 带 ❌）。
            wait_key = f"ddns_wait:{group_id}:{slot_key}"
            if ddns_ok:
                repo.clear_flag(wait_key)
            else:
                waited = repo.flag_age(wait_key)
                if waited < 0:
                    repo.set_flag(wait_key)
                elif waited >= _DDNS_WAIT_SECONDS:
                    ddns_ok = True
                    repo.clear_flag(wait_key)
                    repo.add_event(
                        "error",
                        messages.ev("DDNS 未同步，换班照常结算", messages.obj_group(str(name))),
                        f"已放行结算 / 等待 {_DDNS_WAIT_SECONDS // 60} 分钟仍未同步（{ddns_text}），"
                        "请检查记录名、Token 与 Zone",
                        level="warning",
                        group_id=group_id,
                    )
            # 自动换班只在「开了 DDNS 且确实挂了域名」时记这一步 —— 定时换班
            # 每分钟都可能来一轮，没绑域名时写「DDNS：⏸ 未绑定」纯噪音。
            # 手动换班例外：用户点了按钮，就想要一份完整的流程，
            # 没绑域名也留一行「DDNS：⏸ 未绑定」，跟以前一样。
            if not previously_settled and (
                manual_switch or (ddns_enabled and ddns_bound)
            ):
                # 这一步要连「指到哪个 IP」一起写：域名从旧当班机换到新当班机，
                # 光一句「已同步」看不出变化，而且巡检有可能抢先同步过，当时的
                # 旧 IP 只能靠换班开始时存的快照还原。
                # 详情带前后 IP（来自 Cloudflare 实时值），概览由 report 给「已更新/未变化」。
                # 一条换班事件可能结算多轮：首次一定有这一行；之后只有这次真的改了
                # 才补，免得好几行「（IP 未变化）」把关键的前后对比挤走。
                text = f"DDNS：{ddns_report.detail}"
                existing = repo.get_event(event_id) or {}
                logged = any(
                    "DDNS：" in str(step.get("text") or "")
                    for step in (existing.get("progress") or [])
                )
                if not logged or any(r.changed for r in ddns_report.results):
                    # 跨轮可能先失败后成功，这里不用 once：文案变了就该补一条，
                    # 完全相同的相邻文案 add_event_progress 自己会吞掉。
                    repo.add_event_progress(event_id, text)

        ready = duty_status == "Running" and all_stopped and (has_ip or not ddns_enabled) and ddns_ok

        # 抢占恢复用 flag 标记；恢复过程里某轮启动失败会把 phase 打成 error，
        # 下一轮 previously_settled 变 False，若不把恢复优先于「新换班」判断，
        # 好不容易拉起来的那条恢复事件会被误改题成「换班完成」。
        # 但也要排除故障转移 / 手动换班这两条同 slot 的结算路径，别让残留的恢复
        # 标记把一次真转移的结算误写成「当班已恢复运行」。
        recovered = (
            ready
            and bool(repo.get_flag(recovery_flag))
            and not failover_flag
            and not manual_switch
        )
        first_settle = ready and not previously_settled and not recovered
        if not ready:
            phase = PHASE_SWITCHING
        elif failover_flag:
            # 真·流量超限转移优先：手动切的班次后续又超限，那也是故障转移
            phase = PHASE_FAILOVER
        else:
            # 手动切完也归「已完成」：徽章写「手动换班」会让人以为还在切，
            # 这次是人切的这件事，事件标题和报告标题里已经说清楚了。
            phase = PHASE_SETTLED
        stage = "" if ready else "stopping_standby"

        repo.upsert_run(
            group_id,
            slot_key,
            phase=phase,
            stage=stage,
            event_id=event_id,
            duty_server_id=int(duty["id"]),
            standby_server_ids=_encode_ids(others),
            failover=failover_flag,
            attempts=int(run.get("attempts") or 0) + 1,
            **(restart_clock if not ready else {}),
        )

        summary: dict[str, Any] = {
            "group_id": group_id,
            "slot": slot_key,
            "duty": duty.get("label"),
            "mode": "manual" if manual_switch else ("failover" if failover_flag else "normal"),
            "ready": ready,
            "ddns": ddns_text,
        }

        if not ready:
            self._maybe_report_timeout(
                group, config, now_ts, slot_key, shift_start, duty, others, notify_enabled
            )
            return summary

        # 换班完成：确认全部到位，把同一条事件改写为最终标题并打上完成标志
        # （只在首次 settle 时做，之后每轮 reconcile 维持 settled 状态，不再重复追加、重复 finish）。
        # 复用「换班进行中」这条 running 事件而不是另开一条，这样事件全程耗时才是真实的换班过程。
        if first_settle and event_id:
            repo.add_event_progress(event_id, "全部状态确认完毕", once=True)
            # 标题按来源判：真故障转移 > 手动换班 > 新建换班组 > 普通换班。
            # 手动切班的事件从「手动换班进行中」一路走到「手动换班完成」，
            # 新建组的事件从头到尾都叫「新建换班组」，点进去能看到完整过程。
            if failover_flag:
                # 起不来那种在标题里带上原因：否则事件列表里跟「流量超限」长得一模一样，
                # 事后分不清这台是被抢了还是烧超了。
                # 原因写进结论里而不是套第二个括号：标题末尾的括号固定留给对象，
                # 「故障转移（保活超时）（换班组：X）」会变成两层括号。
                # 与创建时（「当班未就绪，故障转移」/「当班超限，故障转移」）同一套词：
                # 同一条事件从头到尾只有一个说法，事后不用在两句话之间对照。
                # 原因缺失（升级前留下的老 run）不做猜测，退回中性说法。
                if failover_reason == "stall":
                    label = "当班未就绪，故障转移"
                elif failover_reason == "traffic":
                    label = "当班超限，故障转移"
                else:
                    label = "故障转移"
                kind = "shift_failover"
            elif manual_switch:
                label, kind = "手动换班完成", "manual"
            elif str(run.get("origin") or "") == ORIGIN_NEW_GROUP:
                label, kind = "新建换班组", "group_new"
            else:
                label, kind = "换班完成", "shift_done"
            repo.update_event(
                event_id,
                title=messages.ev(label, messages.obj_group(str(name))),
                kind=kind,
            )
            repo.finish_event(event_id, "done")
            # 本班次的临时标记到此作废：粘性计时、候选黑名单、告警去重都只在这一班里有效，
            # 不清掉会随着班次累积（下一班 slot 不同本来就用不上，留着只是堆垃圾）。
            for prefix in (
                f"duty_stall:{group_id}:{slot_key}",
                f"stall_nocand:{group_id}:{slot_key}",
                f"stall_disabled:{group_id}:{slot_key}",
                f"failover_tried:{group_id}:{slot_key}:",
            ):
                repo.clear_flags_like(prefix)

        # 抢占恢复：当班被拉起并重新就绪，收掉那条恢复事件。
        # flag 跨轮持久化，所以「这轮才拉起、下一轮才就绪」也能收尾。
        if recovered:
            repo.clear_flag(recovery_flag)
            if event_id:
                repo.add_event_progress(event_id, "当班已恢复运行", once=True)
                repo.update_event(
                    event_id,
                    title=messages.ev(
                        "当班已恢复运行",
                        messages.obj_duty(str(name), str(duty.get('label') or '')),
                    ),
                    detail="运行中",
                    kind="recovery",
                )
                repo.finish_event(event_id, "done")

        if not notify_enabled:
            # 不发通知，但结算结论照样要写：新建换班组走的就是这条路
            # （notify_enabled=False 只摆状态、不推报告）。以前这里直接 return，
            # 事件的概览永远停在「等待当班 X 到位」这种过程句上，点开才知道
            # 这一班到底跑没跑起来。通知是加分项，事件是流水，不能混。
            if first_settle and event_id:
                self._record_settled(
                    group_id,
                    slot_key,
                    kind,
                    name,
                    duty,
                    others,
                    str(ddns_brief),
                    is_failover=failover_flag,
                    is_manual=manual_switch,
                )
            return summary

        # 全部确认（当班 Running + 非当班节省停机 + DDNS 已同步）后才发通知。
        # 手动换班同样走这条：报告标题带「(手动换班)」后缀，和自动换班、故障转移分开。
        if first_settle or failover:
            self._notify_settled(
                group, config, duty, others, ddns_text, failover_flag, slot_key,
                manual=manual_switch, shift_event_id=event_id, now_ts=now_ts,
                ddns_brief=ddns_brief,
            )
        elif recovered and config.get("notify_on_recovery") == "1":
            results = notify.send_all(
                config, messages.recovery(duty, ddns_text, group_name=name)
            )
            # 通知是流程的最后一步，写在事件最后，点开就能确认报告到底发出去没有
            notify.note_notify(event_id, results)

        return summary

    # -- 熔断 -------------------------------------------------------------
    def _handle_breaker(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        now: dt.datetime,
        slot_key: str,
        duty: dict[str, Any],
        others: list[dict[str, Any]],
        notify_enabled: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        group_id = int(group["id"])
        name = group.get("name")
        everyone = [duty, *others]
        # 为什么判熔断：其他成员都超了，或者根本读不到别人的状态。
        # 两者都必须停机，只是给用户看的说法不能一样 —— 读不到时我们并不知道
        # 别人用了多少流量，写成「N 台流量均已达到阈值」就是编造。
        why = reason or "全部成员流量超限"

        # 本班次若已有进行中的换班事件，熔断要把它关掉，否则前端永远转圈
        run = repo.get_run(group_id, slot_key) or {}
        _close_running_event(
            int(run.get("event_id") or 0), f"{why}，触发全局熔断"
        )

        for server in everyone:
            # 已经物理停机就不再下指令（含 Not-applicable，避免死循环）
            if _is_stopped_any_mode(server):
                continue
            server_id = int(server["id"])
            # 状态读不到的成员会一直「看起来在跑」：不加节流，每轮巡检都要重发一次
            # 停机指令、再刷一条失败事件。同一台 120 秒内只发一次。
            stamp = f"breaker_stop:{group_id}:{slot_key}:{server_id}"
            age = repo.flag_age(stamp)
            if 0 <= age < _BREAKER_STOP_COOLDOWN:
                continue
            try:
                stop_server_by_pref(server)
                repo.clear_flag(f"breaker_stop_err:{group_id}:{slot_key}:{server_id}")
            except Exception as exc:  # noqa: BLE001
                flag = f"breaker_stop_err:{group_id}:{slot_key}:{server_id}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "error",
                        messages.ev("熔断停机失败", messages.obj_member(str(name), str(server.get('label') or ''))),
                        aliyun.brief_error(exc),
                        level="error",
                        group_id=group_id,
                        server_id=server_id,
                    )
            finally:
                # 无论成败都重新计时，避免每轮都往阿里云打一次
                repo.clear_flag(stamp)
                repo.set_flag(stamp)

        repo.upsert_run(
            group_id,
            slot_key,
            phase=PHASE_BREAKER,
            event_id=0,
            duty_server_id=int(duty["id"]),
            standby_server_ids=_encode_ids(others),
            note=why,
        )

        flag = f"breaker:{group_id}:{slot_key}"
        if notify_enabled and config.get("notify_on_breaker") == "1" and not repo.get_flag(flag):
            # 全部成员物理停机到位才发通知。everyone 是 reconcile 开头 _refresh
            # 实时拉过的状态：本轮刚下发停机、实例可能还在 Stopping 中间态，
            # _all_stopped_any_mode 判定为 False，本轮不发，等下一轮 _refresh
            # 经 API 确认全部 Stopped 后再发，避免「指令下发≠状态到位」就误报。
            if _all_stopped_any_mode(everyone):
                results = notify.send_all(
                    config, messages.breaker(everyone, group_name=name, note=reason)
                )
                repo.set_flag(flag)
                event_id = repo.add_event(
                    "breaker",
                    messages.ev("全局熔断", messages.obj_group(str(name))),
                    f"{messages.stop_tail(everyone)} / {why}",
                    level="error",
                    group_id=group_id,
                )
                # 熔断是一条结论性事件，但用户点开要知道「凭什么说全停了」：
                # 逐台写实际状态与停机模式，最后一句给出确认口径。
                for member in everyone:
                    member_label = str(member.get("label") or f"#{member.get('id')}")
                    repo.add_event_progress(
                        event_id,
                        f"{member_label}：{messages.stop_phrase(member.get('stopped_mode'))}",
                    )
                repo.add_event_progress(event_id, "已确认全部停机，本轮未再下发指令")
                notify.note_notify(event_id, results)

        return {"group_id": group_id, "breaker": True}

    # -- 超限停机 ---------------------------------------------------------
    def _handle_limit_stop(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        now: dt.datetime,
        slot_key: str,
        duty: dict[str, Any],
        others: list[dict[str, Any]],
        notify_enabled: bool,
    ) -> dict[str, Any]:
        group_id = int(group["id"])
        name = group.get("name")
        duty_status = str(duty.get("instance_status") or "")

        # 本班次若已有进行中的换班事件，超限停机要把它关掉，否则前端永远转圈
        run = repo.get_run(group_id, slot_key) or {}
        _close_running_event(int(run.get("event_id") or 0), "当班流量超限，触发超限停机")

        # 只有物理停机（Stopped 且 mode 合法）才算到位；否则（运行中/中间态）下指令
        if not _is_stopped_any_mode(duty):
            if duty_status not in TRANSIENT_STATUS:
                # 同一台机器 120 秒内只发一次指令、只记一条事件。
                # 以前每轮都发、每轮都记：指令被拒（IncorrectInstanceStatus）时
                # 「超限停机失败」一分钟一条，14 天保留期内能把「最近事件」刷满；
                # 指令成功了但状态迟迟不变时也一样刷。
                stamp = f"limit_stop_cmd:{group_id}:{slot_key}"
                age = repo.flag_age(stamp)
                if not (0 <= age < _BREAKER_STOP_COOLDOWN):
                    try:
                        mode = stop_server_by_pref(duty)
                        repo.clear_flag(f"limit_stop_err:{group_id}:{slot_key}")
                        event_flag = f"limit_stop_evt:{group_id}:{slot_key}"
                        if not repo.get_flag(event_flag):
                            repo.set_flag(event_flag)
                            used = _used_gb(duty)
                            thr = float(duty.get('threshold_gb') or 0)
                            event_id = repo.add_event(
                                "limit_stop",
                                messages.ev(
                                    f"当班流量超限，{messages.stop_phrase(mode)}",
                                    messages.obj_duty(str(name), str(duty.get('label') or '')),
                                ),
                                f"等待停机到位 / 已用 {used:.2f} GB（阈值 {thr:g} GB）",
                                level="warning",
                                group_id=group_id,
                                server_id=int(duty["id"]),
                                status="running",
                                pending_status="Stopped",
                                pending_kind="limit_stop",
                            )
                            repo.add_event_progress(event_id, f"已用 {used:.2f} GB，超过阈值 {thr:g} GB")
                            repo.add_event_progress(event_id, f"已下发停机指令（{messages.mode_label(mode)}）")
                    except Exception as exc:  # noqa: BLE001
                        err_flag = f"limit_stop_err:{group_id}:{slot_key}"
                        if not repo.get_flag(err_flag):
                            repo.set_flag(err_flag)
                            repo.add_event(
                                "error",
                                messages.ev("当班流量超限停机失败", messages.obj_duty(str(name), str(duty.get('label') or ''))),
                                aliyun.brief_error(exc),
                                level="error",
                                group_id=group_id,
                                server_id=int(duty["id"]),
                            )
                    finally:
                        # 无论成败都重新计时，避免每轮都往阿里云打一次
                        repo.clear_flag(stamp)
                        repo.set_flag(stamp)
        else:
            # 已经停机到位：把本轮次的「已下发 / 失败 / 节流」标记清掉，
            # 这个班次之后再超限（重新开机后）还能正常记一条新事件。
            for prefix in ("limit_stop_evt", "limit_stop_err", "limit_stop_cmd"):
                repo.clear_flag(f"{prefix}:{group_id}:{slot_key}")
            flag = f"limit_stop:{group_id}:{slot_key}"
            if (
                notify_enabled
                and config.get("notify_on_limit_stop") == "1"
                and _is_stopped_any_mode(duty)
                and not repo.get_flag(flag)
            ):
                # duty 是 reconcile 开头 _refresh 实时拉过的状态，物理停机到位才发；
                # 不再用 repo.get_server 的旧快照覆盖，确保「API 确认到位」后才报告。
                results = notify.send_all(config, messages.limit_stop(duty, group_name=name))
                repo.set_flag(flag)
                # 报告算这条超限停机事件的最后一步（停机那条事件是上一轮记的）
                notify.note_notify(repo.latest_event_id("limit_stop", group_id), results)

        repo.upsert_run(
            group_id,
            slot_key,
            phase=PHASE_BREAKER,
            event_id=0,
            duty_server_id=int(duty["id"]),
            standby_server_ids=_encode_ids(others),
            note="当班流量超限且无法换班",
        )
        return {"group_id": group_id, "limit_stop": True}

    # -- 换班超时 ---------------------------------------------------------
    def _maybe_report_timeout(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        now_ts: float,
        slot_key: str,
        shift_start: int,
        duty: dict[str, Any],
        others: list[dict[str, Any]],
        notify_enabled: bool,
    ) -> None:
        if not notify_enabled or config.get("notify_on_error") != "1":
            return
        # 手动换班没有「班次开始时刻」的概念：shift_start 是本班次的起点（可能
        # 已经过去好几个小时），用它算 elapsed 会把刚发起的切换误判成超时。
        # 手动换班是用户主动操作，界面会盯着结果，这里跳过超时报告。
        run = repo.get_run(group["id"], slot_key) or {}
        if run.get("manual"):
            return
        # 超时从「第一次开始处理这个班次」计时（run.started_at），而不是班次开始
        # 时刻。时刻表模式下 shift_start 是当天班次起点（如 18:00），容器在班次
        # 中途重启后第一次 reconcile 时 elapsed 已经很大，会被误判成超时。
        started_at = int(run.get("started_at") or shift_start)
        elapsed = (now_ts - started_at) / 60
        if elapsed < settings.SHIFT_TIMEOUT_MINUTES:
            return
        flag = f"timeout:{group['id']}:{slot_key}"
        if repo.get_flag(flag):
            return

        results = notify.send_all(
            config,
            messages.shift_error(
                duty, others, settings.SHIFT_TIMEOUT_MINUTES, group_name=group.get("name") or ""
            ),
        )
        repo.set_flag(flag)
        event_id = repo.add_event(
            "error",
            messages.ev("换班超时", messages.obj_group(str(group.get('name') or ''))),
            f"已等待 {elapsed:.0f} 分钟仍未就绪",
            level="error",
            group_id=int(group["id"]),
        )
        notify.note_notify(event_id, results)

    # -- DDNS -------------------------------------------------------------
    def _sync_ddns(
        self,
        server: dict[str, Any],
        config: dict[str, str],
        kind: str,
        target_id: int,
        ip: str | None = None,
        verify: bool = False,
    ) -> dns.DDNSReport:
        """把「这台机器正在跑」写进它该占的记录，返回三种说法（详情/概览/通知）。

        同步的不止一条：
          - 目标自己绑的（换班组 → 该组绑的域名；单机 → 这台机器绑的域名）
          - 换班组当班时，还要把**这台机器自己**绑的域名一起指过来 ——
            一台机器可能既在换班组里、又单独挂了别的域名，开机了就该都指向它
        没绑任何记录时返回「⏸ 未绑定」。

        ip 由调用方从刚查到的实时状态传进来：`server` 往往是轮询开始前的数据库
        快照，实例停机时快照里的 public_ip 是空的，拿它去同步只会得到「IP 无效跳过」。

        verify=True 只给「刚换班/刚开机」那一次用：去 Cloudflare 查真实值再决定
        改不改。之后每轮结算仍走本地缓存，不会每分钟打一次读请求。
        """
        ip = str(ip or server.get("public_ip") or "")
        names, results = dns.sync_target_full(
            kind, target_id, config, ip=ip, verify=verify
        )
        if kind == dns.TARGET_GROUP:
            own_names, own_results = dns.sync_target_full(
                dns.TARGET_SERVER, int(server.get("id") or 0), config, ip=ip, verify=verify
            )
            names = names + own_names
            results = results + own_results
        return dns.report(names, results)

    # -- 换班进行中的事件（动态化）----------------------------------------
    def _shift_event(
        self, group_id: int, slot_key: str, name: str, duty_label: str, duty_id: int = 0
    ) -> int:
        """取本班次「进行中」的换班事件 ID；没有就新建一条 running 事件。

        换班跨多轮 reconcile，这里用 shift_runs.event_id 记住事件，后续轮次
        追加进度（add_event_progress），最终 finish_event 打完成标志。
        """
        run = repo.get_run(group_id, slot_key) or {}
        event_id = int(run.get("event_id") or 0)
        if event_id:
            # 只复用还在转的那条。已经收尾的事件（组停用后重启、恢复事件已结算）
            # 再往上写，会把它改题成「换班完成」，时间戳还停在上一次。
            current = repo.get_event(event_id)
            if current and str(current.get("status") or "") == "running":
                return event_id
        event_id = repo.add_event(
            "shift_done",
            messages.ev("换班进行中", messages.obj_group(str(name))),
            f"等待当班 {duty_label} 到位",
            level="info",
            group_id=group_id,
            status="running",
        )
        # 开工前记下各域名此刻指向哪，留作排查依据；事件里的「旧 → 新」以同步时
        # 从 Cloudflare 读到的实际值为准（见 dns.report），不依赖这份快照。
        repo.upsert_run(
            group_id,
            slot_key,
            event_id=event_id,
            dns_before=dns_snapshot(group_id, int(duty_id or 0)),
        )
        repo.add_event_progress(event_id, f"换班开始：当班 {duty_label}")
        return event_id

    # -- 结算通知 ---------------------------------------------------------
    def _notify_settled(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        duty: dict[str, Any],
        others: list[dict[str, Any]],
        ddns_status: str,
        failover: int,
        slot_key: str,
        manual: bool = False,
        shift_event_id: int = 0,
        now_ts: float = 0.0,
        ddns_brief: str = "",
    ) -> None:
        # ddns_status 进通知（短），ddns_brief 进事件概览（带「已更新 / IP 未变化」）。
        # 没传 brief 的旧调用退回 status，概览不至于缺一段。
        ddns_brief = ddns_brief or str(ddns_status)
        group_id = int(group["id"])
        name = group.get("name") or ""
        is_failover = bool(failover)
        is_manual = bool(manual) and not is_failover
        kind = "shift_failover" if is_failover else "shift_done"
        # 换班完成 / 手动换班 / 临时换班共用同一个 slot，去重键必须分开，
        # 否则「已结算过又临时换班」时这条报告会被当成重复直接吞掉。
        #
        # 手动换班再细一层：同一个 slot 里可以连点好几次（切过去又切回来），
        # 用 slot 做键会让第二次起全被当成重复静默吞掉 —— 用户点了三次只收到
        # 一条报告。手动换班每次都会新建一条事件，就用事件 ID 做键。
        scope = "failover" if is_failover else ("manual" if is_manual else "settled")
        if is_manual and shift_event_id:
            flag = f"notify:{group_id}:manual:{shift_event_id}"
        elif is_failover and is_month_cycle(group):
            # 一月一轮按天代班：同一个段（slot）里每天都可能转移一次，去重键必须带上
            # 日期。否则 1 号那条发完，后面几天全被当成重复静默吞掉 —— 用户明确要的
            # 是「每天起不来都要有一条」，事件有、通知也得有。
            day = _day_key(group, float(now_ts or time.time()))
            flag = f"notify:{group_id}:{slot_key}:failover:{day}"
        else:
            flag = f"notify:{group_id}:{slot_key}:{scope}"
        if repo.get_flag(flag):
            # 通知已经发过、这次不再重发，但事件摘要还是要写 —— 否则同一个 slot 里
            # 第二次结算（典型：熔断停机后流量回落重新拉起）时，事件概览会永远停在
            # 「等待当班 X 到位」的过程句，点开才知道这一班到底跑没跑起来。
            self._record_settled(
                group_id,
                slot_key,
                kind,
                name,
                duty,
                others,
                str(ddns_brief),
                is_failover=is_failover,
                is_manual=is_manual,
            )
            return

        enabled_key = "notify_on_failover" if is_failover else "notify_on_shift"
        if config.get(enabled_key) != "1":
            # 通知开关只决定「发不发」，不决定「记不记」。以前这里直接 return，
            # 关掉通知后那条 running 事件就永远停在「正在启动当班 X」：界面看着像
            # 系统卡死，其实班早就换完了。
            repo.set_flag(flag)
            repo.clear_flag(f"timeout:{group_id}:{slot_key}")
            self._record_settled(
                group_id,
                slot_key,
                kind,
                name,
                duty,
                others,
                str(ddns_brief),
                is_failover=is_failover,
                is_manual=is_manual,
            )
            return

        if is_failover:
            # 被接管的那台按 run.failover_from 精确取。以前靠「谁超限」反推，
            # 保活超时场景下原当班根本没超限，会退化成「第一台」，
            # 把一台无关的成员写成被接管（通知里就是错的信息）。
            run = repo.get_run(group_id, slot_key) or {}
            from_id = int(run.get("failover_from") or 0)
            reason = str(run.get("failover_reason") or "")
            over = [server for server in others if int(server["id"]) == from_id] if from_id else []
            if not over:
                over = [server for server in others if _over_threshold(server)]
            if not over and others:
                over = [others[0]]
            # 按 id 比较，不要用内置 id()：后者比的是 dict 对象地址，一旦哪天
            # 有人 copy 一份 server 传进来，这里就会静默算错。
            over_ids = {int(server["id"]) for server in over}
            standby = [server for server in others if int(server["id"]) not in over_ids]
            message = messages.shift_failover(
                duty, over, standby, str(ddns_status), group_name=name, reason=reason
            )
        else:
            # 本班次如果先发过「换班异常」，成功时按脚本换标题；
            # 手动切班的报告标题带「(手动换班)」，与自动换班区分
            delayed = bool(repo.get_flag(f"timeout:{group_id}:{slot_key}"))
            period = _shift_span(group, duty, config, now_ts or None)
            message = messages.shift_done(
                duty,
                others,
                period,
                str(ddns_status),
                group_name=name,
                delayed=delayed,
                manual=is_manual,
            )
            if delayed:
                repo.clear_flag(f"timeout:{group_id}:{slot_key}")

        results = notify.send_all(config, message)
        repo.set_flag(flag)
        self._record_settled(
            group_id,
            slot_key,
            kind,
            name,
            duty,
            others,
            str(ddns_brief),
            is_failover=is_failover,
            is_manual=is_manual,
            results=results,
        )

    def _record_settled(
        self,
        group_id: int,
        slot_key: str,
        kind: str,
        name: str,
        duty: dict[str, Any],
        others: list[dict[str, Any]],
        ddns_brief: str,
        *,
        is_failover: bool,
        is_manual: bool,
        results: list[Any] | None = None,
    ) -> dict[str, Any] | None:
        """把结算结论写进本班次那条事件：改写概览摘要，并把通知结果追加到最后一步。

        换班过程的那条 running 事件已经在 reconcile 里改题、finish 过了，这里只补
        摘要，不再另开一条 0 秒完成的 done 事件。results 为 None 表示这次没发通知
        （通知开关关着或调用方没发）—— 摘要照样写，事件是系统自己的流水。
        DDNS 这段用 brief（「✅ DDNS已同步（…）」），与保活、手动开机的概览一致；
        通知里仍用 status，保持短句。
        """
        detail = f"当班 {duty.get('label')} / 待机 {len(others)} 台 / {ddns_brief}"
        run = repo.get_run(group_id, slot_key) or {}
        run_event_id = int(run.get("event_id") or 0)
        if run_event_id:
            repo.update_event(run_event_id, detail=detail)
            # 报告是整条换班流程的最后一步：写进事件详情，事后能确认发没发出去
            if results is not None:
                notify.note_notify(run_event_id, results)
            return None

        # 兜底：理论上不会走到（_shift_event 一定先建了 running 事件），
        # 万一 run 里丢了 event_id，仍补一条结果事件，别让这次换班无迹可寻
        event_id = repo.add_event(
            kind,
            messages.ev(
                f"{'故障转移' if is_failover else ('手动换班完成' if is_manual else '换班完成')}",
                messages.obj_group(str(name)),
            ),
            detail,
            group_id=group_id,
            server_id=int(duty["id"]),
        )
        if results is not None:
            notify.note_notify(event_id, results)
        return event_id

    # ==================================================================
    # 换班组的建立与解散
    # ==================================================================
    def bootstrap_group(self, group_id: int) -> dict[str, Any]:
        """新建换班组后，立刻按排班表把组员摆到该有的状态。

        用户点「保存」时组员的真实状态往往和排班表对不上：该当班的停着，
        该休班的还在跑。这里马上对一次，顺序和自动换班完全一致 ——
        先开当班 → 等它 Running → 其余成员节省停机 → 状态全部到位后同步 DDNS。

        开机要几十秒，跨轮的等待交给后面的例行对账继续推进（run.event_id 已指向
        这条事件），所以一条「组名：新建换班组」事件能从「下发启动」一路追加到
        「全部状态确认完毕」，点进去就是完整过程。

        本班次不发结算通知：用户刚点完保存，不需要再收一份自己刚做过的事的报告。
        """
        if not self._lock.acquire(timeout=30):
            raise ValueError("后台正在同步，请稍后再试")
        try:
            group = repo.get_group(group_id)
            if group is None:
                raise ValueError("换班组不存在")
            name = group.get("name") or f"#{group_id}"
            config = repo.get_settings()
            group["_timezone"] = config.get("timezone") or "Asia/Shanghai"
            now = dt.datetime.now(_timezone(group["_timezone"]))
            now_ts = now.timestamp()

            if not group.get("enabled"):
                repo.add_event(
                    "group_new",
                    messages.ev("新建换班组", messages.obj_group(str(name))),
                    "换班组未启用，没有对组员下发任何启停指令",
                    group_id=group_id,
                )
                return {"group_id": group_id, "skipped": "disabled"}

            picked = actual_duty(group, now_ts)
            if picked is None:
                repo.add_event(
                    "group_new",
                    messages.ev("新建换班组", messages.obj_group(str(name))),
                    "启用成员不足两台，排不出当班，未下发任何启停指令",
                    group_id=group_id,
                )
                return {"group_id": group_id, "skipped": "成员不足两台"}

            duty_member, shift_start = picked
            slot_key = str(shift_start)
            duty = repo.get_server(int(duty_member["server_id"]), decrypt_secret=True)
            if duty is None:
                repo.add_event(
                    "group_new",
                    messages.ev("新建换班组", messages.obj_group(str(name))),
                    "排班表指定的当班服务器已被删除，未下发任何启停指令",
                    group_id=group_id,
                )
                return {"group_id": group_id, "skipped": "当班服务器已被删除"}

            others: list[dict[str, Any]] = []
            for member in active_members(group):
                if int(member["server_id"]) == int(duty_member["server_id"]):
                    continue
                server = repo.get_server(int(member["server_id"]), decrypt_secret=True)
                if server is not None:
                    others.append(server)

            slot_name = str(duty_member.get("slot_name") or "").strip()
            when = slot_name or "当前时刻"
            event_id = repo.add_event(
                "group_new",
                messages.ev("新建换班组", messages.obj_group(str(name))),
                f"等待当班 {duty.get('label')} 到位 / 其余 {len(others)} 台待停机",
                group_id=group_id,
                server_id=int(duty["id"]),
                status="running",
            )
            repo.add_event_progress(
                event_id, f"按排班表核对组员状态：{when} / 当班 {duty.get('label')}"
            )
            # 事件先挂进 run：这一轮之后的巡检沿用同一条事件追加进度，
            # origin 让结算时把标题固定成「新建换班组」，不改叫「换班完成」
            repo.upsert_run(
                group_id,
                slot_key,
                event_id=event_id,
                origin=ORIGIN_NEW_GROUP,
                phase=PHASE_SWITCHING,
                duty_server_id=int(duty["id"]),
                standby_server_ids=_encode_ids(others),
            )
            # 占掉本班次的换班通知去重键：新建组只摆状态，不推报告
            repo.set_flag(f"notify:{group_id}:{slot_key}:settled")

            # 逐台核对现状，把「符不符合排班表」写进事件里，
            # 用户不用等动作下发完就能看出哪几台要动
            mismatched = 0
            targets = [(duty, True)] + [(server, False) for server in others]
            for server, should_run in targets:
                label = server.get("label") or f"#{server.get('id')}"
                try:
                    _refresh(server)
                except Exception as exc:  # noqa: BLE001
                    mismatched += 1
                    repo.add_event_progress(
                        event_id, f"{label}：状态读取失败（{_brief_exc(exc)}），交给下一轮同步重试"
                    )
                    continue
                status = str(server.get("instance_status") or "")
                suffix = messages.status_suffix(status, server.get("stopped_mode"))
                if should_run:
                    if status == "Running":
                        repo.add_event_progress(
                            event_id, f"当班 {label}：{status}{suffix}，符合排班表", once=True
                        )
                    else:
                        mismatched += 1
                        repo.add_event_progress(
                            event_id, f"当班 {label}：{status}{suffix}，需要开机", once=True
                        )
                elif _is_stopped_any_mode(server):
                    repo.add_event_progress(
                        event_id, f"{label}：{status}{suffix}，符合排班表", once=True
                    )
                else:
                    mismatched += 1
                    repo.add_event_progress(
                        event_id, f"{label}：{status}{suffix}，需要节省停机", once=True
                    )

            if not mismatched:
                repo.add_event_progress(
                    event_id, "组员状态已符合排班表，无需下发启停指令", once=True
                )

            # 交给换班流程真正下发指令：启动当班 → 停非当班 → DDNS → 结算。
            # 状态没到位（还在开机）时它只推进一轮就返回，事件保持 running，
            # 后面的例行对账接着走，过程一直追加在同一条事件上。
            result = self.reconcile_group(group, config, now, now_ts, notify_enabled=False)
            if not result.get("ready"):
                # 还没摆到位（多半在等开机）：申请一段加密对账窗口，
                # 让调度线程把间隔临时压到 10 秒，别让用户对着「进行中」等满一分钟
                from . import scheduler  # 延迟导入：scheduler 顶层 import 了本模块

                scheduler.request_fast_reconcile()
            return {"group_id": group_id, "event_id": event_id, **result}
        finally:
            self._lock.release()

    def dissolve_group(self, group_id: int) -> dict[str, Any]:
        """解散换班组：组员保持当前状态，回到闲散手动。

        解散不是停机 —— 跑着的继续跑，停着的继续停着，全程不给实例下发任何
        启停指令，之后怎么用由用户手动决定。

        组员如果挂着单机保活，一并暂停：解散后它们不归任何流程管，被保活顺手
        拉起来就不叫「保持当前状态」了，也就不是「闲散手动」了。记录本身留着，
        需要的话在单机保活页一键开回来。
        """
        if not self._lock.acquire(timeout=30):
            raise ValueError("后台正在同步，请稍后再试")
        try:
            group = repo.get_group(group_id)
            if group is None:
                raise ValueError("换班组不存在")
            name = group.get("name") or f"#{group_id}"
            member_ids = [int(m["server_id"]) for m in (group.get("members") or [])]
            bound = repo.dns_records_of(dns.TARGET_GROUP, group_id)
            keepalives = {int(item["server_id"]): item for item in repo.list_keepalives()}

            event_id = repo.add_event(
                "group_dissolve",
                messages.ev("解散换班组", messages.obj_group(str(name))),
                f"已解散 / {len(member_ids)} 台保持原状，未下发指令",
                group_id=0,  # 组马上就没了，事件别再挂到不存在的组上
                status="running",
            )

            for server_id in member_ids:
                server = repo.get_server(server_id, decrypt_secret=True)
                if server is None:
                    repo.add_event_progress(event_id, f"#{server_id}：服务器已被删除，跳过")
                    continue
                label = server.get("label") or f"#{server_id}"
                try:
                    _refresh(server)
                    status = str(server.get("instance_status") or "")
                    suffix = messages.status_suffix(status, server.get("stopped_mode"))
                    repo.add_event_progress(event_id, f"{label}：{status}{suffix}，保持不动")
                except Exception as exc:  # noqa: BLE001
                    repo.add_event_progress(
                        event_id, f"{label}：状态读取失败（{_brief_exc(exc)}），保持不动"
                    )

                item = keepalives.get(server_id)
                if item is not None and item.get("enabled"):
                    repo.set_keepalive_enabled(int(item["id"]), False)
                    repo.add_event_progress(
                        event_id, f"{label}：单机保活已暂停，不会被自动拉起"
                    )

            # 该组可能正有换班事件在转圈：解散后没人再推进它，先把这类事件收掉，
            # 否则「最近事件」里会永远挂着一条进行中
            for row in dbmod.query(
                "SELECT id FROM events WHERE group_id=? AND status='running'", (group_id,)
            ):
                repo.add_event_progress(
                    int(row["id"]), "换班组已解散，本次换班流程中止"
                )
                repo.finish_event(int(row["id"]), "done", "换班组已解散，流程未走完")

            repo.delete_group(group_id)
            if bound:
                repo.add_event_progress(event_id, f"DDNS 绑定已解除：{'、'.join(bound)}")
            repo.add_event_progress(event_id, "组员均已回到闲散手动，可随时手动开关机")
            repo.finish_event(event_id, "done")
            return {"ok": True, "event_id": event_id, "servers": len(member_ids)}
        finally:
            self._lock.release()

    # -- 手动操作 ---------------------------------------------------------
    def manual_switch(self, group_id: int) -> dict[str, Any]:
        """立即跳到下一台当班。

        一天一轮的 hours 模式：把轮换起点 anchor_at 平移到下一班的开头。
        按真实时钟决定当班的模式（固定时刻表、一月一轮）：anchor 不起作用，
        改走 shift_runs 的 manual/duty_server_id 覆盖 —— reconcile_group 会沿用
        这个手动指定，把下一台拉起来、停掉原当班，走完整流程后发报告。

        两种模式都只开一条「手动换班」事件，后续进度全部追加到这条上。
        """
        # 和后台调度抢同一把锁：否则两边会同时给同一批实例下指令
        if not self._lock.acquire(timeout=30):
            raise ValueError("后台正在同步，请稍后再试")
        try:
            group = repo.get_group(group_id)
            if group is None:
                raise ValueError("换班组不存在")
            config = repo.get_settings()
            now = dt.datetime.now(_timezone(config.get("timezone") or "Asia/Shanghai"))
            now_ts = now.timestamp()

            members = active_members(group)
            if len(members) < 2:
                raise ValueError("组内启用的成员不足两台")

            if is_month_cycle(group) or _is_timetable_group(group):
                return self._finish_manual(
                    self._manual_switch_timetable(group, config, now, now_ts)
                )

            # 「当前谁当班」必须走 actual_duty：本班次可能已经故障转移过、
            # 也被手动切过，那时 run.duty_server_id 才是真相。以前这里按偏移量
            # 从时钟推当前成员，一旦发生过转移，「下一台」算出来的正是那个已经被
            # 覆盖上去的当班机 —— 用户点「立即换班」看着按钮转了一圈，机器没变。
            picked_now = actual_duty(group, now_ts)
            current_member = picked_now[0] if picked_now else members[0]

            size = len(members)
            current_index = next(
                (
                    index
                    for index, member in enumerate(members)
                    if int(member["server_id"]) == int(current_member["server_id"])
                ),
                -1,
            )
            # current_index = -1（实际当班不在启用成员里：被移出组或已停用）时，
            # next_index 落回 0，从队首重新开始，不会取到不存在的下一个。
            next_index = (current_index + 1) % size
            # 「下一台」在一个周期里的累计起点；把 anchor 挪到 now - 该起点，
            # 此刻就正好落在下一班的开头。
            cum_before_next = sum(float(m["hours"]) for m in members[:next_index]) * 3600

            new_anchor = int(now_ts - cum_before_next)
            dbmod.execute(
                "UPDATE shift_groups SET anchor_at=? WHERE id=?", (new_anchor, group_id)
            )
            fresh = repo.get_group(group_id) or group
            picked = duty_at(fresh, now_ts)
            if picked is not None:
                slot_key = str(picked[1])
                # manual 与 failover 分开记：手动切的班次不该被当成故障转移。
                # duty_server_id 必须显式写成目标机：anchor 精确到秒，新 slot 的键
                # 有可能和旧 slot 同值（同一秒内连点、或 elapsed 正好落在整秒），
                # 那一行上还留着上一次的当班，而 actual_duty 见到 manual=1 就直接
                # 采信 duty_server_id —— 于是「点了换班，当班没动」。
                repo.upsert_run(
                    group_id,
                    slot_key,
                    manual=1,
                    failover=0,
                    duty_server_id=int(picked[0]["server_id"]),
                )
                self._manual_event(
                    group_id, slot_key, group.get("name") or "", current_member, picked[0]
                )
            return self._finish_manual(self.reconcile_group(fresh, config, now, now_ts))
        finally:
            self._lock.release()

    def _finish_manual(self, result: dict[str, Any]) -> dict[str, Any]:
        """手动换班还没走完（多在等开机 / 等原当班停稳）就申请加密对账窗口。

        不这么做，用户点完「立即换班」只能盯着「进行中」等满一个对账间隔。
        """
        if isinstance(result, dict) and not result.get("ready"):
            from . import scheduler  # 延迟导入：scheduler 顶层 import 了本模块

            scheduler.request_fast_reconcile()
        return result

    def _manual_event(
        self,
        group_id: int,
        slot_key: str,
        name: str,
        current: dict[str, Any],
        target: dict[str, Any],
    ) -> int:
        """开一条「手动换班」事件，并把 ID 记进本班次 run。

        整轮换班（确认成员状态 → 停原当班 → DDNS → 结算）都追加到这一条上，
        点进去就是完整流程；和自动换班「一次换班只留一条事件」保持一致。
        """
        from_label = current.get("label") or current.get("server_id")
        to_label = target.get("label") or target.get("server_id")
        # 上一次换班还没走完就又被点了一次：那条事件马上会被新事件顶掉
        # （run.event_id 被覆盖），界面上它就永远停在「进行中」转圈。
        # 先把它收掉并写明原因，别留一条转不完的假进度。
        # 新建组 / 故障转移 / 恢复这类事件同样会被顶掉，一并收尾。
        for stale in repo.running_events(group_id, _GROUP_EVENT_KINDS):
            stale_id = int(stale["id"])
            repo.add_event_progress(
                stale_id, f"已被新的手动换班取代（改为 {to_label}）"
            )
            if str(stale.get("kind") or "") in ("manual", "shift_done"):
                repo.update_event(
                    stale_id, title=messages.ev("手动换班已取消", messages.obj_group(str(name)))
                )
            repo.finish_event(stale_id, "done")
        event_id = repo.add_event(
            "manual",
            messages.ev("手动换班进行中", messages.obj_group(str(name))),
            f"等待接管到位（{to_label}，原当班 {from_label}）",
            level="info",
            group_id=group_id,
            status="running",
        )
        repo.add_event_progress(event_id, f"手动换班：{from_label} → {to_label}")
        # 切之前记下各域名的当前指向，留作排查依据（事件里的前后 IP 来自同步时
        # 从 Cloudflare 读到的真实值）
        repo.upsert_run(
            group_id,
            slot_key,
            event_id=event_id,
            dns_before=dns_snapshot(group_id, int(target.get("server_id") or 0)),
        )
        return event_id

    def _manual_switch_timetable(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        now: dt.datetime,
        now_ts: float,
    ) -> dict[str, Any]:
        """按真实时钟决定当班的模式（固定时刻表 / 一月一轮）下的手动换班：
        把「下一台」写进本班次 run 的 duty_server_id，
        借 reconcile_group 的 manual 分支完成切换（开下一台 → 停原当班 → DDNS → 发报告）。"""
        group_id = int(group["id"])
        members = active_members(group)
        # 当前实际当班以运行记录为准（本班次可能已经被手动切过 / 故障转移过）。
        # 只按时钟推「下一台」会永远算出同一台 —— 用户第二次点按钮时，时钟还是那个
        # 成员，算出来的「下一台」还是同一台，于是怎么点都换不回来。
        picked = actual_duty(group, now_ts)
        if picked is None:
            raise ValueError("组内启用的成员不足两台")
        current_member, shift_start = picked
        slot_key = str(shift_start)

        # 按 position 顺序取当前当班的下一台（循环到队首）
        current_index = next(
            (i for i, m in enumerate(members) if int(m["server_id"]) == int(current_member["server_id"])),
            -1,
        )
        next_member = members[(current_index + 1) % len(members)] if current_index >= 0 else members[0]

        # 上一轮手动换班还没走完就又点了一次：旧事件不能就这么挂着。
        # run.event_id 马上会被下面的 _manual_event 覆盖，界面上那条事件就永远
        # 停在「进行中」转圈，看着像系统卡死。这件事统一在 _manual_event 里处理。

        repo.upsert_run(
            group_id,
            slot_key,
            phase=PHASE_SWITCHING,
            stage="",
            duty_server_id=int(next_member["server_id"]),
            failover=0,
            manual=1,
        )
        # 只开一条事件，后面启动 / 停机 / DDNS / 结算全部追加到它上面
        self._manual_event(group_id, slot_key, group.get("name") or "", current_member, next_member)
        return self.reconcile_group(repo.get_group(group_id) or group, config, now, now_ts)

    def _managed_note(self, server_id: int) -> str:
        """这台机器是否被自动化流程托管。托管时返回一句说明，闲散机器返回空串。"""
        for group in repo.list_groups():
            if not group.get("enabled"):
                continue
            for member in group.get("members") or []:
                if member.get("enabled") and int(member["server_id"]) == server_id:
                    return f"换班组「{group.get('name')}」"
        for item in repo.list_keepalives():
            if int(item["server_id"]) == server_id and item.get("enabled"):
                return "单机保活"
        return ""

    def manual_action(self, server_id: int, action: str) -> dict[str, Any]:
        """手动开关机。闲散服务器（没编进任何自动化流程）可以随便用；
        托管中的机器依旧允许操作，但会提示下一轮对账会按流程纠正。

        流程：下发指令 → 创建 running 事件 → 立即返回。实例状态由后台
        事件确认器每 5 秒轮询，到位后把事件标记为完成。
        """
        if action not in ("start", "stop"):
            raise ValueError("只支持 start / stop")
        server = repo.get_server(server_id, decrypt_secret=True)
        if server is None:
            raise ValueError("服务器不存在")
        if not str(server.get("instance_id") or "").strip():
            raise ValueError("这台服务器还没填实例 ID，无法启停")

        label = "开机" if action == "start" else "停机"
        target_status = "Running" if action == "start" else "Stopped"
        note = self._managed_note(server_id)

        # 幂等：同一台机器同一目标状态已有进行中的手动事件，直接返回，不再重复
        # 下发指令。否则快速连点会连发两次启停、产生多条转圈事件。
        if repo.pending_manual_exists(server_id, target_status):
            return {
                "status": "pending",
                "duplicate": True,
                "target_status": target_status,
                "managed_note": note,
                "instance_status": server.get("instance_status") or "Unknown",
                "stopped_mode": server.get("stopped_mode") or "",
            }

        # 1) 下发指令。停机按实例设置里的停机模式（默认节省停机，不支持时退化普通停机）。
        stop_mode = ""
        if action == "start":
            # 手动开机也不能绕过流量阈值：以前这里不判断，点一下就把一台已经到
            # 阈值的机器点起来，下一轮巡检才把它停掉 —— 中间那几十秒白烧的流量
            # 补不回来。先拉一次实时流量再判，库里的快照最多可能旧一分钟。
            try:
                _refresh(server)
            except Exception:  # noqa: BLE001
                # 读不到就按库里的旧值判断：宁可少拦一次，也别把人挡在门外
                pass
            if _over_threshold(server):
                raise ValueError(
                    f"已用 {_used_gb(server):.2f} GB，已达阈值 "
                    f"{float(server.get('threshold_gb') or 0):g} GB，不能开机；"
                    "请先调高阈值，或等流量包重置后再试"
                )
            aliyun.start_instance(server)
        else:
            stop_mode = stop_server_by_pref(server)

        # 2) 建 running 事件，交给后台确认器轮询确认，不在这里同步等状态。
        detail = f"等待{label}到位"
        if action == "stop" and stop_mode:
            # 写实际生效的模式：选了节省停机但规格不支持会退化成普通停机，
            # 事件里再写死「节省停机」就是骗人（账单照扣规格费）。
            detail += f"（{messages.mode_label(stop_mode)}）"
        if note:
            detail += f" / 由{note}托管，下一轮同步会按流程纠正"

        event_id = repo.add_event(
            "manual",
            messages.ev(f"手动{label}", messages.obj_server(str(server.get('label') or ''))),
            detail,
            level="info",
            server_id=server_id,
            status="running",
            pending_status=target_status,
            pending_kind="manual",
        )
        repo.add_event_progress(event_id, f"已下发{label}指令")

        # 3) 立即返回。卡片状态由确认器轮询到位后刷新。
        return {
            "status": "pending",
            "event_id": event_id,
            "target_status": target_status,
            "managed_note": note,
            "instance_status": server.get("instance_status") or "Unknown",
            "stopped_mode": server.get("stopped_mode") or "",
        }

    def refresh(self, server_id: int) -> dict[str, Any]:
        server = repo.get_server(server_id, decrypt_secret=True)
        if server is None:
            raise ValueError("服务器不存在")
        _refresh(server)
        fresh = repo.get_server(server_id) or {}
        return {
            "traffic_used": fresh.get("traffic_used"),
            "instance_status": fresh.get("instance_status"),
            "stopped_mode": fresh.get("stopped_mode"),
            "public_ip": fresh.get("public_ip"),
        }

    # -- 日报 -------------------------------------------------------------
    def daily_report(self) -> bool:
        config = repo.get_settings()
        tz = _timezone(config.get("timezone") or "Asia/Shanghai")
        today = dt.datetime.now(tz).date().isoformat()
        flag = f"daily_report:{today}"
        if repo.get_flag(flag):
            return False

        servers = []
        for server in repo.list_servers(decrypt_secret=True):
            try:
                _refresh(server)
            except Exception:  # noqa: BLE001
                pass
            servers.append(repo.get_server(int(server["id"])) or server)
        if not servers:
            return False

        message = messages.daily_report(
            servers, dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        )
        results = notify.send_all(config, message)
        repo.set_flag(flag)
        event_id = repo.add_event("daily_report", messages.ev("每日报告已推送"), f"已推送 / {len(servers)} 台服务器")
        notify.note_notify(event_id, results)
        return True


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

_TZ_WARNED = False


def _timezone(name: str) -> dt.tzinfo:
    """时区名 → tzinfo。取不到就回落 +08:00，并大声警告一次。

    回落静默是最坑的：容器里没装 tzdata 时 ZoneInfo 会抛 ZoneInfoNotFoundError，
    看着一切正常，实际所有时刻都按北京时间算。
    """
    global _TZ_WARNED
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        if not _TZ_WARNED:
            _TZ_WARNED = True
            log.warning("时区 %s 不可用（缺 tzdata？），临时按 UTC+08:00 计算", name)
        return dt.timezone(dt.timedelta(hours=8))


_TZ_CACHE: dict[str, Any] = {"at": 0.0, "name": ""}


def timezone_name() -> str:
    """系统设置里的时区。duty_at 被界面 / DNS / 手动换班频繁调用，这里做 5 秒缓存。"""
    now = time.time()
    if _TZ_CACHE["name"] and now - float(_TZ_CACHE["at"]) < 5:
        return str(_TZ_CACHE["name"])
    try:
        name = str(repo.get_setting("timezone") or "").strip() or "Asia/Shanghai"
    except Exception:  # noqa: BLE001 - 读设置失败不该让轮换计算崩掉
        name = "Asia/Shanghai"
    _TZ_CACHE["at"] = now
    _TZ_CACHE["name"] = name
    return name


def _group_timezone(group: dict[str, Any]) -> str:
    """组上显式带的时区优先（引擎 reconcile 时塞的），否则读系统设置。

    以前只有引擎塞了 `_timezone`，界面当班高亮、DNS 指向判定、手动换班拿到的
    都是裸字典，一律回落到硬编码的 +08:00 —— 时区配成别的就和引擎错位。
    """
    return str(group.get("_timezone") or "").strip() or timezone_name()


def _encode_ids(servers: list[dict[str, Any]]) -> str:
    return json.dumps([int(s["id"]) for s in servers])


def _hours_of(group: dict[str, Any], server: dict[str, Any]) -> float:
    """这台机器在组里设的班次时长；读不到就按 12 小时，别让文案变成问号。"""
    for member in group.get("members") or []:
        if int(member.get("server_id") or 0) == int(server.get("id") or 0):
            return float(member.get("hours") or 12)
    return 12.0


def _member_of(group: dict[str, Any], server_id: int) -> dict[str, Any]:
    """按 server_id 找组成员；找不到返回空 dict。"""
    for member in group.get("members") or []:
        if int(member.get("server_id") or 0) == int(server_id):
            return member
    return {}


def _shift_span(
    group: dict[str, Any],
    duty: dict[str, Any],
    config: dict[str, str],
    now_ts: float | None = None,
) -> str:
    """当前班次文案：一月一轮写日期区间，一天一轮优先「班次名称（起–止）」。

    一月一轮按「今天是哪一段」写，不看当班是谁：手动换班 / 故障转移会把别的
    成员顶到这一段来，按当班成员去找他自己的排班段，10 月 5 号就会报出
    「10 月 16–31 日」，与今天的日期自相矛盾。当班是谁正文里已经写清楚了。
    """
    member = _member_of(group, int(duty.get("id") or 0))
    slot_name = member.get("slot_name") or ""
    if is_month_cycle(group):
        # 按月轮换没有小时边界，只报本段覆盖哪几天
        stamp = int(now_ts if now_ts is not None else time.time())
        now = dt.datetime.fromtimestamp(stamp, _timezone(_group_timezone(group)))
        for item, start_day, end_day in _month_layout(group, now.year, now.month):
            if start_day <= now.day <= end_day:
                # 段的名字属于「这一段」，不是当班那个人
                name = str(item.get("slot_name") or "").strip()
                span = f"{now.month} 月 {start_day}–{end_day} 日"
                return f"{name}（{span}）" if name else span
        return slot_name
    start = member.get("start_time") or ""
    end = member.get("end_time") or ""
    if slot_name or start or end:
        return messages.shift_period(slot_name, start, end)
    # 老 hours 累加模式：没有时刻表，退回「机名（N 小时）」式表达
    hours = float(member.get("hours") or _hours_of(group, duty))
    label = str(duty.get("label") or "")
    return f"{label}（{hours:g} 小时）"


def member_span_label(group: dict[str, Any], member: dict[str, Any], now_ts: float) -> str:
    """成员「这一班」在人眼里的写法，给界面用，不带班次名称。

    - 一月一轮 → `1–15 日`（本月这一段覆盖哪几天）
    - 固定时刻表 → `08:00–20:00`
    - 其余（老 hours 累加）→ 空串，界面自己退回「N 小时」

    界面三处（换班页成员行、轮换顺序、概览组卡片）都读这一个字段，
    免得前端各算一套，跟引擎排出来的段对不上。
    """
    if is_month_cycle(group):
        now = dt.datetime.fromtimestamp(now_ts, _timezone(_group_timezone(group)))
        target = int(member.get("server_id") or 0)
        for item, start_day, end_day in _month_layout(group, now.year, now.month):
            if int(item.get("server_id") or 0) == target:
                return f"{start_day}–{end_day} 日"
        return ""
    start = str(member.get("start_time") or "")
    end = str(member.get("end_time") or "")
    if start and end:
        # 复用通知那套写法：结束 00:00 显示成 24:00，不写「次日」
        return messages.shift_period("", start, end)
    return ""


def _over_threshold(server: dict[str, Any]) -> bool:
    used = server.get("traffic_used")
    if used is None:
        return False
    threshold = float(server.get("threshold_gb") or 0)
    return threshold > 0 and float(used) >= threshold


def _used_gb(server: dict[str, Any]) -> float:
    return float(server.get("traffic_used") or 0)


# 物理上已停机、可以不再下停机指令的 StoppedMode 取值。
# 其中 Not-applicable 表示「本实例不支持停机不收费」，停机后照常收费，
# 且无论怎么重发 StopCharging 都不可能变成节省停机 —— 必须认它「已停机」，
# 否则换班/熔断会陷入无限重发停机指令 + 永远不 ready 的死循环。
_STOPPED_MODES = {"StopCharging", "KeepCharging", "Stop", "Not-applicable"}


def _is_stopped_any_mode(server: dict[str, Any]) -> bool:
    """这台机器是否已经物理停机到位（无论是否节省停机）。

    用于换班/熔断/超限停机的「到位判断」：物理停机就不该再重发停机指令，
    避免对不支持节省停机的实例（StoppedMode=Not-applicable）陷入死循环。
    """
    if str(server.get("instance_status") or "") != "Stopped":
        return False
    return str(server.get("stopped_mode") or "") in _STOPPED_MODES


def stop_mode_of(server: dict[str, Any]) -> str:
    """实例设置里选的停机模式：StopCharging（节省停机）/ Stop（普通停机）。"""
    return repo.normalize_stop_mode(server.get("stop_mode"))


def stop_server_by_pref(server: dict[str, Any]) -> str:
    """按实例设置里的停机模式停机，返回**实际生效**的模式。

    全项目所有「关机」动作都走这里：保活、换班休班、熔断、超限停机、手动停机。
    以前每处都写死 "StopCharging"，用户在实例设置里改成普通停机也不生效。

    选节省停机时保留 fallback：本地盘 / 包年包月 / GPU 等规格不支持节省停机，
    硬发会直接报错把整条关机流程卡死，退化成普通停机至少能停下来（照常收规格费）。
    用户明确选普通停机就不 fallback —— Stop 永远可用，真失败该如实报错。
    """
    if stop_mode_of(server) == "Stop":
        return aliyun.stop_instance(server, "Stop", fallback=False)
    return aliyun.stop_instance(server, "StopCharging", fallback=True)


def _all_stopped_any_mode(servers: list[dict[str, Any]]) -> bool:
    return all(_is_stopped_any_mode(server) for server in servers)


# 计费月按阿里云账单口径算，固定 UTC+8（跟部署机器/界面时区设置无关）。
_BILL_TZ = dt.timezone(dt.timedelta(hours=8))


def _month_key(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, _BILL_TZ).strftime("%Y-%m")


def _resolve_empty_traffic(server: dict[str, Any]) -> float:
    """CDT 接口成功但没返回流量明细时的兜底：返回本轮该用的流量值。

    只有一种情况算「读数不可用」：本计费月里曾经拿到过读数，这次却空了。
    同月内累计流量只会涨，空读数只能是接口抖动/丢数据 —— 沿用库里旧值继续判定，
    绝不当成 0：否则一台已经烧到阈值的机器会被判成「没超限」，下一分钟就被开机。

    反过来，新的计费月第一天空明细是正常的（当月还没有流量），按 0 处理，
    照常启动，不会把机器卡在月初起不来。
    """
    server_id = int(server["id"])
    known = float(server.get("traffic_used") or 0)
    last_ts = repo.last_sample_ts(server_id)
    if known > 0 and last_ts and _month_key(last_ts) == _month_key(int(time.time())):
        flag = f"traffic_empty:{server_id}"
        if not repo.get_flag(flag):
            repo.set_flag(flag)
            repo.add_event(
                "warning",
                messages.ev("流量读数异常", messages.obj_server(str(server.get('label') or ''))),
                f"CDT 未返回流量明细，沿用上次读数 {known:.2f} GB 判定，不按 0 处理",
                level="warning",
                server_id=server_id,
            )
        return known
    return 0.0


# --------------------------------------------------------------------------
# 单机保活重试：一条事件从头记到尾（尝试次数 → 恢复）
# --------------------------------------------------------------------------


def _keepalive_retry_note(
    *,
    server_id: int,
    label: str,
    state_label: str,
    pending: dict[str, Any] | None,
    error: str = "",
    quiet: bool = False,
) -> int:
    """保活又试了一次：计数 +1，刷新同一条「进行中」事件的概览，返回事件 ID。

    开机指令下发成功和下发失败都走这里。库存不足 / 限流 / 欠费这类错误同样是
    一次「尝试」，不计数的话用户只看得到第一次的失败，之后每分钟在后台重试却
    完全静默，「一直试到起来」这条链路在事件上就断了。

    error 非空表示这次连开机指令都没发出去：概览补上原因，事件仍然是 running
    —— 下一轮还会继续试，不是终局。

    quiet=True 只留给「窗口恢复那一轮没开成、这一轮补拉」的情况：窗口到点本来是
    安静的，不能因为中间隔了一轮就变成「疑被抢占」。判定依据是巡检读到的
    keepalive_resume_pending 标记，不是「上一轮有没有看到它在跑」—— 后者会把
    「手动关机后重新开保活」误算成计划内，用户明明要求它在线却收不到消息。
    """
    tries_key = f"keepalive_tries:{server_id}"
    if pending:
        event_id = int(pending["id"])
        tries = repo.bump_flag(tries_key)
        detail = f"等待开机到位（已尝试 {tries} 次）"
        if error:
            detail += f"；启动指令失败：{error}"
        repo.update_event(event_id, detail=detail)
    else:
        # 新的一次中断：计数从 1 重来，别接着上次中断的次数往下数
        repo.clear_flag(tries_key)
        tries = repo.bump_flag(tries_key)
        detail = f"等待开机到位（已尝试 {tries} 次）"
        if error:
            detail += f"；启动指令失败：{error}"
        event_id = repo.add_event(
            "keepalive_quiet" if quiet else "start",
            messages.ev(
                "单机保活窗口恢复中" if quiet else "单机保活抢占恢复中",
                messages.obj_server(label),
            ),
            detail,
            level="warning",
            server_id=server_id,
            status="running",
            pending_status="Running",
            pending_kind="keepalive",
        )
        repo.add_event_progress(
            event_id,
            f"启动指令失败：{error}" if error else f"已下发启动指令（原状态 {state_label}）",
        )

    # 进度只留里程碑：一天 1440 轮，每分钟一行同义句会把事件点开糊成一片，
    # 也会把 PROGRESS_MAX_STEPS 撑爆、把开头那条挤掉。
    if tries in _KEEPALIVE_MILESTONES:
        repo.add_event_progress(
            event_id,
            f"{_KEEPALIVE_RETRY_MARK}{state_label}），已尝试拉起 {tries} 次",
        )
    return event_id


def _close_keepalive_event(server_id: int, label: str, note: str) -> None:
    """保活被暂停（流量超限停机）时，把那条还在转的「抢占恢复」收尾。

    不接这一手，事件会永远 running：确认器对保活**故意不设超时**（它要「一直试
    到起来」），于是会一直每 5 秒查一次实例状态；更糟的是等流量包重置、保活恢复
    之后，_keepalive_retry_note 会重新找到这条几个月前的事件继续往上写次数 ——
    一条事件横跨一个月，展开也读不懂。

    停机是主动的、预期内的动作，所以标题改写成「保活已暂停」、级别降回 info，
    不要留一个红色告警图标配「已完成」徽章。
    """
    event = repo.pending_event(server_id, "keepalive")
    # 这次中断到此为止：下次恢复（流量回落后重新拉起）从 1 重新数
    repo.clear_flag(f"keepalive_tries:{server_id}")
    if event is None:
        return
    eid = int(event["id"])
    repo.add_event_progress(eid, note)
    repo.update_event(eid, title=messages.ev("单机保活暂停", messages.obj_server(label)), level="info", kind="keepalive_pause")
    repo.finish_event(eid, "done", detail=note)


def _keepalive_one_shot(
    server_id: int,
    kind: str,
    title: str,
    summary: str,
    steps: list[str],
    *,
    level: str = "info",
) -> int:
    """落一条一次性事件：概览只放结果，展开看得到每一步过程。

    add_event(status="running") → 逐条写进度 → finish_event 写概览，这套写法与
    换班、手动开关机一致：收起时是一句结论，点开是「它做了什么」。
    直接 add_event(detail=...) 会把 detail 再拆成一条进度，摘要就在进度里重复一遍。
    """
    event_id = repo.add_event(kind, title, "", level=level, server_id=server_id, status="running")
    for text in steps:
        repo.add_event_progress(event_id, text)
    repo.finish_event(event_id, "done", detail=summary)
    return event_id


def _keepalive_pause_note(
    server_id: int,
    label: str,
    window_text: str,
    summary: str,
    steps: list[str],
    *,
    await_stop: bool = False,
) -> None:
    """离开运行窗口：落一条「单机保活窗口暂停」，把停机这一步也算进过程。

    有进行中的拉起事件就复用它，不另开一条：同一件事在列表里出现「一条还在转圈、
    一条已完成」是最容易让人误判的。标题统一改写成「单机保活窗口暂停」。

    await_stop=True 表示这一轮刚下发停机指令、实例还没停稳：事件保持 running，
    目标状态改成 Stopped，由确认器看到「已确认 Stopped」之后才收尾。就地写
    「已暂停」是假结论 —— 用户点开只能看到「已下发指令」，然后什么都没有。
    """
    repo.clear_flag(f"keepalive_tries:{server_id}")
    paused = repo.pending_event(server_id, "keepalive_stop")
    if paused is not None:
        # 确认器已经把这条转成「等停机」了：只补过程，收尾交给确认器
        for text in steps:
            repo.add_event_progress(int(paused["id"]), text)
        return
    event = repo.pending_event(server_id, "keepalive")
    head = f"已离开运行窗口{_window_bracket(window_text)}，窗口外不再自动拉起"
    if event is not None:
        eid = int(event["id"])
        repo.update_event(eid, title=messages.ev("单机保活窗口暂停", messages.obj_server(label)), level="info")
        if await_stop:
            repo.update_event(eid, kind="keepalive_pause", detail="等待停机到位")
        repo.add_event_progress(eid, head)
        for text in steps:
            repo.add_event_progress(eid, text)
        if await_stop:
            repo.retarget_event(eid, "Stopped", "keepalive_stop")
            return
        repo.finish_event(eid, "done", detail=summary)
        return
    title = messages.ev("单机保活窗口暂停", messages.obj_server(label))
    if await_stop:
        eid = repo.add_event(
            "keepalive_pause",
            title,
            "等待停机到位",
            server_id=server_id,
            status="running",
            pending_status="Stopped",
            pending_kind="keepalive_stop",
        )
        repo.add_event_progress(eid, head)
        for text in steps:
            repo.add_event_progress(eid, text)
        return
    _keepalive_one_shot(
        server_id,
        "keepalive_pause",
        title,
        summary,
        [head, *steps],
    )


def _keepalive_pause_progress(server_id: int, mode: str) -> None:
    """把巡检这轮的停机结果补进那条「单机保活窗口暂停」事件。

    确认器 5 秒一轮，可能比巡检先发现「已出窗口」：它只把事件转成「等停机」、
    不碰实例，真正下发停机的是巡检。不回写的话，事件详情永远停在「已离开运行窗口」，
    看不到「已下发停机指令（节省停机）」，和手动关机 / 超限停机的事件标准不一致。
    """
    event = repo.latest_server_event(server_id, ("keepalive_pause",), max_age=600)
    if event is None:
        return
    label = messages.mode_label(mode) or "停机"
    done_text = f"已暂停（{label}）"
    # 巡检每分钟都会走到这里（机器一直停在窗口外），detail 已经写对就别再刷新
    # finished_at —— 否则事件的完成时间会一直往后跳。
    if str(event.get("detail") or "") == done_text and str(event.get("status")) == "done":
        return
    repo.add_event_progress(int(event["id"]), f"已下发停机指令（{label}）")
    # 还转着就继续等确认器：停机到位才算这条事件的结果
    if str(event.get("status")) == "running":
        repo.update_event(int(event["id"]), detail=f"等待停机到位（{label}）")
        return
    repo.finish_event(int(event["id"]), "done", detail=done_text)


def _keepalive_rest_recovered(server_id: int, mode: str) -> None:
    """休止时段停机重试成功：给那条「单机保活停机失败」补上结论。

    原来只清标记、事件永远停在「停机指令失败」——点开看不到它到底停成没有，
    和「凡是要等外部状态的动作都要有结果」这条规则相悖。事件只收一次，
    同一模式重复进入也不重复写（巡检每分钟都会走到休止分支）。
    """
    event = repo.latest_server_event(server_id, ("keepalive_rest_error",), max_age=3600)
    if event is None:
        return
    label = messages.mode_label(mode) or "停机"
    done_text = f"重试成功，已停机（{label}）"
    if str(event.get("detail") or "") == done_text:
        return
    eid = int(event["id"])
    repo.add_event_progress(eid, done_text)
    repo.update_event(eid, detail=done_text)


def _keepalive_rest_fail_note(server_id: int, label: str, error: str) -> int:
    """休止时段里停机没成功：只记一条，下一轮继续重试，别每分钟刷一条。"""
    return repo.add_event(
        "keepalive_rest_error",
        messages.ev("单机保活停机失败", messages.obj_server(label)),
        f"停机指令失败：{error}",
        level="warning",
        server_id=server_id,
    )


def _keepalive_resume_note(server: dict[str, Any], window_text: str, status: str) -> int:
    """回到运行窗口：落一条「单机保活窗口恢复中」，并在这里把开机指令发出去。

    这条事件同时是后续确认的载体（pending_kind=keepalive），所以它自己就能显示
    「已下发开机指令 → 已确认运行中 → DDNS」的完整过程。
    kind 用 keepalive_quiet：确认器据此**不发「抢占恢复」通知** —— 到点开机不是
    被抢占，推一条「实例疑被抢占」会把人吓一跳。
    """
    server_id = int(server["id"])
    label = str(server.get("label") or f"#{server_id}")
    # 进行中叫「窗口恢复中」，收尾后由确认器改成「窗口恢复」：事件列表里同一个
    # 动作只有一个名字，和「单机保活窗口暂停」是同一套读法。
    title = messages.ev("单机保活窗口恢复中", messages.obj_server(label))
    done_title = messages.ev("单机保活窗口恢复", messages.obj_server(label))
    head = f"已进入运行窗口{_window_bracket(window_text)}，恢复自动拉起"
    # 这一轮开不了机（指令失败 / 还在 Stopping）：真正拉起要等下一轮巡检的
    # 「Stopped 就拉起」，那条路本来属于抢占恢复，会推通知。留一个标记把
    # 「这次是窗口到点」传下去，下一轮建事件时仍然走安静渠道。
    resume_flag = f"keepalive_resume_pending:{server_id}"
    if status == "Stopped":
        try:
            aliyun.start_instance(server)
        except Exception as exc:  # noqa: BLE001
            # 指令没发出去：不建 running 事件（没有可等的东西），下一轮巡检会按
            # 「Stopped 就拉起」的老路重试，那条重试事件会正常计数与升级。
            repo.set_flag(resume_flag)
            return _keepalive_one_shot(
                server_id,
                "keepalive_resume",
                messages.ev("单机保活窗口恢复失败", messages.obj_server(label)),
                "启动指令失败，下一轮重试",
                [head, f"启动指令失败：{_brief_exc(exc)}"],
                level="warning",
            )
        repo.clear_flag(resume_flag)
        event_id = repo.add_event(
            "keepalive_quiet",
            title,
            "等待开机到位",
            server_id=server_id,
            status="running",
            pending_status="Running",
            pending_kind="keepalive",
        )
        repo.add_event_progress(event_id, head)
        repo.add_event_progress(event_id, "已下发开机指令（原状态 已停机）")
        # 这是一次新的中断（休止时段），次数从 1 开始记
        repo.clear_flag(f"keepalive_tries:{server_id}")
        repo.bump_flag(f"keepalive_tries:{server_id}")
        return event_id
    if status == "Running":
        repo.clear_flag(resume_flag)
        return _keepalive_one_shot(
            server_id,
            "keepalive_resume",
            done_title,
            "运行中",
            [head, "实例已在运行，无需指令"],
        )
    # 还在 Stopping / Starting / Pending 等中间态：这一轮开不了机，也谈不上「恢复」，
    # 别急着落一条已完成的「窗口恢复 / 停止中」——等它停稳后真正拉起的那一轮，
    # 再建一条「窗口恢复中 → 窗口恢复」，同一件事只留一条完整记录。
    # 只留标记把「这次是窗口到点」传给下一轮，让它继续走安静渠道。
    repo.set_flag(resume_flag)
    return 0


# --------------------------------------------------------------------------
# 读取异常：一条事件记录完整过程（失败时刻 → 重试次数 → 恢复时刻）
# --------------------------------------------------------------------------

# 连续失败到这个次数才升格为 error。单次读失败多半是网络抖一下，立刻标红
# 会吓人；先给下一轮、下下一轮自愈的机会（默认间隔 60 秒，3 次≈3 分钟）。
_READ_ERROR_ESCALATE = 3
# 失败次数到这些里程碑时补一行进度，避免每分钟一行把进度刷爆。
_READ_ERROR_MILESTONES = (2, 5, 10, 30, 60)


def _hms(ts: float, tz_name: str = "Asia/Shanghai") -> str:
    return dt.datetime.fromtimestamp(
        ts, _timezone(tz_name or "Asia/Shanghai")
    ).strftime("%H:%M:%S")


def _retry_interval() -> int:
    """事件里写的「每 N 秒重试」必须是用户实际配的巡检间隔。

    以前直接用 settings.RECONCILE_INTERVAL —— 那是环境变量默认值（60），
    用户在界面上把间隔改成 300 之后，事件还写着「每 60 秒重试」，等于给了他
    一个错的节奏，等两分钟就以为程序卡了。
    """
    try:
        return max(
            30,
            int(
                repo.get_setting(
                    "reconcile_interval", str(settings.RECONCILE_INTERVAL)
                )
            ),
        )
    except Exception:  # noqa: BLE001
        # 脏值 / 库还没建好：退回环境默认，文案宁可不精确也不能让巡检中断
        return settings.RECONCILE_INTERVAL


def _brief_exc(exc: Exception | str) -> str:
    if isinstance(exc, Exception):
        text = aliyun.brief_error(exc)
    else:
        text = str(exc).strip()[:140]
    # 上层模板已经写了「读取失败：」，这里去掉异常自带的「CDT 流量查询失败：」
    # 之类前缀，避免一句话里连着两个「失败」。
    for sep in ("失败：", "失败:", "异常：", "异常:"):
        idx = text.find(sep)
        if 0 <= idx <= 12:
            return text[idx + len(sep):].strip() or text
    return text


def _read_error(
    *,
    server_id: int,
    title: str,
    exc: Exception | str,
    group_id: int = 0,
    tz_name: str = "Asia/Shanghai",
) -> None:
    """记一次状态读取失败：同一条进行中事件里写明失败时刻与重试节奏。

    不因为「这一次」就定性成错误：第一次只建一条**进行中**事件，详情写
    「HH:MM:SS 读取失败（原因），每 N 秒重试，恢复后自动更新」；后续只刷新
    概览里的最近时刻与次数，连续失败到阈值才升格为 error。恢复由
    _read_recovered 收尾并补上恢复时刻。
    """
    now = int(time.time())
    brief = _brief_exc(exc)
    count = repo.bump_flag(f"read_err:{server_id}")
    interval = _retry_interval()
    event = repo.pending_event(server_id, "read_error")
    if event is None:
        eid = repo.add_event(
            "warning",
            title,
            f"{_hms(now, tz_name)} 读取失败：{brief}；"
            f"每 {interval} 秒重试，恢复后自动更新",
            level="warning",
            group_id=group_id,
            server_id=server_id,
            status="running",
            pending_kind="read_error",
        )
        # 时间线首条用「第 1 次失败」而不是照抄概览（概览还带了重试节奏），
        # 这样展开后不会看到同一句话出现两遍，又能和第 2、3 次连成序列。
        repo.add_event_progress(eid, f"{_hms(now, tz_name)} 第 1 次失败：{brief}")
        return

    eid = int(event["id"])
    repo.update_event(
        eid,
        detail=(
            f"{_hms(now, tz_name)} 仍失败（连续 {count} 次）：{brief}；"
            f"每 {interval} 秒重试"
        ),
    )
    # 升格那一次（第 _READ_ERROR_ESCALATE 次）必须留痕：它是从「抖一下」变成
    # 「真有问题」的转折点，比前后任意一次都值得看，哪怕不在里程碑里也要记。
    if count in _READ_ERROR_MILESTONES or count == _READ_ERROR_ESCALATE:
        repo.add_event_progress(eid, f"{_hms(now, tz_name)} 第 {count} 次失败：{brief}")
    if count >= _READ_ERROR_ESCALATE:
        repo.update_event(eid, level="error")


def _read_recovered(
    *,
    server_id: int,
    title: str,
    tz_name: str = "Asia/Shanghai",
) -> None:
    """读取恢复正常：给那条进行中的事件补上恢复时刻并收尾。

    title 是收尾后的标题（如「Ali-CDT-02 状态读取已恢复」）；本来就没在
    失败中时直接返回，不产生任何事件。
    """
    key = f"read_err:{server_id}"
    age = repo.flag_age(key)
    if age < 0:
        return
    count = repo.flag_value(key)
    repo.clear_flag(key)
    event = repo.pending_event(server_id, "read_error")
    if event is None:
        return
    eid = int(event["id"])
    now = int(time.time())
    minutes = max(1, int(round(age / 60)))
    repo.add_event_progress(eid, f"{_hms(now, tz_name)} 已恢复")
    repo.update_event(
        eid,
        title=title,
        detail=(
            f"{_hms(now, tz_name)} 恢复正常"
            f"（此前连续失败 {count} 次，持续约 {minutes} 分钟）"
        ),
        # 收尾必须把级别降回来：图标颜色看 level、徽章看 status，只改 status
        # 会留下「红色错误图标 + 绿色完成徽章」的自相矛盾。
        level="info",
    )
    repo.finish_event(eid, "done")


def _close_orphan_read_errors(monitored: set[int]) -> None:
    """收掉「对象已不在监控里」的读取事件，避免它永远转圈。

    机器被删、移出换班组、关掉保活之后，不会再有恢复回调来收尾；
    每轮对账末尾统一处理一次。
    """
    for event in repo.running_pending_events("read_error"):
        server_id = int(event.get("server_id") or 0)
        if server_id and server_id in monitored:
            continue
        eid = int(event["id"])
        # 计数跟事件同生共死。只收事件不清标记的话，机器哪天重新被纳入监控，
        # 下一轮读取失败会从旧值接着往上数：一上来就「连续 12 次」、立刻升红，
        # 恢复时还会写出「连续失败 14 次，持续约 1 分钟」这种自相矛盾的话。
        if server_id:
            repo.clear_flag(f"read_err:{server_id}")
        # 同 _read_recovered：收尾成「完成」就不要再留红色错误图标。
        repo.update_event(eid, level="info")
        repo.finish_event(
            eid,
            "done",
            detail="对象已不在监控中，读取告警自动收尾",
        )


def _refresh(server: dict[str, Any]) -> None:
    real_traffic = aliyun.get_traffic_gb(server)
    empty_reading = real_traffic is None
    traffic = _resolve_empty_traffic(server) if empty_reading else float(real_traffic)
    info = aliyun.get_instance(server)
    repo.update_server_runtime(
        int(server["id"]),
        traffic=traffic,
        status=info.status,
        stopped_mode=info.stopped_mode,
        public_ip=info.public_ip,
        region_id=info.region_id,
    )
    # 空读数不落样本：曲线里插一个假 0 会让人以为流量掉了，实际只是没读上来。
    if not empty_reading:
        repo.add_sample(int(server["id"]), traffic, info.status)
    if repo.get_flag(f"traffic_empty:{int(server['id'])}") and not empty_reading:
        repo.clear_flag(f"traffic_empty:{int(server['id'])}")
    server["traffic_used"] = traffic
    server["instance_status"] = info.status
    server["stopped_mode"] = info.stopped_mode
    server["public_ip"] = info.public_ip
    if info.region_id:
        server["region_id"] = info.region_id


engine = Engine()
