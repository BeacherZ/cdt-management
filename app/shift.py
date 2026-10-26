"""核心引擎：联合换班（N 台按小时轮换）+ 单机保活。

对账（reconcile）幂等：同一班次内反复执行不会重复操作，也不会重复推送。
所有「已做过」的标记都落 SQLite，容器重启不丢。
"""

from __future__ import annotations

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
PHASE_BREAKER = "breaker"
PHASE_ERROR = "error"

TRANSIENT_STATUS = {"Starting", "Pending", "Stopping"}


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

    每个成员占据一天内的 [start, end) 分钟区间；end <= start 表示跨天
    （如 18:00–次日 06:00），区间要折成 [start, 1440) ∪ [0, end)。

    返回 (当班成员, 该班次开始时间戳)。开始时间戳用于 slot_key 去重与通知时间段显示。
    """
    members = active_members(group)
    tz_name = str(group.get("_timezone") or "Asia/Shanghai")
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
    """今天 start_minutes 时刻的时间戳；跨天班次（start 已过且是昨天开始）要回退一天。

    比如现在 02:00 属于「18:00–06:00」夜班，这个班次是从昨天 18:00 开始的。
    """
    start_minutes = max(0, start_minutes)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = today_start + dt.timedelta(minutes=start_minutes)
    if candidate > now:
        candidate -= dt.timedelta(days=1)
    return int(candidate.timestamp())


def active_members(group: dict[str, Any]) -> list[dict[str, Any]]:
    members = [
        member
        for member in group.get("members") or []
        if member.get("enabled") and float(member.get("hours") or 0) > 0
    ]
    return sorted(members, key=lambda item: int(item.get("position") or 0))


def duty_at(group: dict[str, Any], now_ts: float) -> tuple[dict[str, Any], int] | None:
    """返回 (当班成员, 该班次开始时间戳)。按 position 顺序循环轮换。"""
    members = active_members(group)
    if len(members) < 2:
        return None

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


def next_switch_at(group: dict[str, Any], now_ts: float) -> float | None:
    members = active_members(group)
    if len(members) < 2:
        return None

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


def _next_switch_timetable(group: dict[str, Any], now_ts: float) -> float:
    """固定时刻表模式：下一班开始时刻 = 当前班次的结束时刻。"""
    members = active_members(group)
    tz_name = str(group.get("_timezone") or "Asia/Shanghai")
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
    picked = duty_at(group, now_ts)
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
    def reconcile_all(self) -> list[dict[str, Any]]:
        if not self._lock.acquire(blocking=False):
            log.debug("上一轮还没结束，跳过本次")
            return []
        try:
            config = repo.get_settings()
            now = dt.datetime.now(_timezone(config.get("timezone") or "Asia/Shanghai"))
            now_ts = now.timestamp()

            grouped = self._grouped_server_ids()
            results: list[dict[str, Any]] = []

            try:
                results.extend(self.reconcile_keepalives(config, now, grouped))
            except Exception as exc:  # noqa: BLE001
                log.exception("单机保活对账失败")
                repo.add_event("error", "单机保活同步异常", str(exc), level="error")

            for group in repo.list_groups():
                try:
                    results.append(self.reconcile_group(group, config, now, now_ts))
                except Exception as exc:  # noqa: BLE001
                    log.exception("换班组 %s 对账失败", group.get("id"))
                    repo.add_event(
                        "error",
                        f"换班组 {group.get('name')} 同步异常",
                        str(exc),
                        level="error",
                        group_id=int(group["id"]),
                    )
                    results.append({"group_id": group["id"], "error": str(exc)})
            return results
        finally:
            self._lock.release()

    def _grouped_server_ids(self) -> set[int]:
        ids: set[int] = set()
        for group in repo.list_groups():
            for member in group.get("members") or []:
                if member.get("enabled"):
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
                # 换班组会接管这台机器，单机保活让位，避免两边互相拉扯
                flag = f"keepalive_conflict:{server_id}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "warning",
                        f"{item.get('label')} 同时在换班组里，单机保活已让位",
                        "同一台服务器不要同时挂在两个模块下",
                        level="warning",
                        server_id=server_id,
                    )
                continue

            server = repo.get_server(server_id, decrypt_secret=True)
            if server is None:
                continue
            try:
                _refresh(server)
            except Exception as exc:  # noqa: BLE001
                # 同一台机器连续读取失败只记一次，恢复后自动解除，避免每分钟刷一条错误
                flag = f"keepalive_err:{server_id}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "error",
                        f"{server.get('label')} 保活状态读取失败",
                        str(exc),
                        level="error",
                        server_id=server_id,
                    )
                results.append({"keepalive": server_id, "error": str(exc)})
                continue
            if repo.get_flag(f"keepalive_err:{server_id}"):
                repo.clear_flag(f"keepalive_err:{server_id}")

            status = str(server.get("instance_status") or "")
            if status == "Stopped":
                try:
                    aliyun.start_instance(server)
                    # 保活拉起是异步的：建 running 事件，交给确认器轮询到 Running 后
                    # 补发「抢占恢复」通知并打完成标志，不在这里同步等状态。
                    event_id = repo.add_event(
                        "start",
                        f"{server.get('label')}：保活拉起",
                        f"原状态 {status}",
                        level="warning",
                        server_id=server_id,
                        status="running",
                        pending_status="Running",
                        pending_kind="keepalive",
                    )
                    repo.add_event_progress(event_id, f"已下发启动指令（原状态 {status}）")
                    repo.set_flag(f"keepalive_wait:{server_id}")
                    if repo.get_flag(f"keepalive_fail:{server_id}"):
                        repo.clear_flag(f"keepalive_fail:{server_id}")
                except Exception as exc:  # noqa: BLE001
                    # 同样只报一次：库存不足这类错误会连续出现，不必每分钟刷屏
                    flag = f"keepalive_fail:{server_id}"
                    if not repo.get_flag(flag):
                        repo.set_flag(flag)
                        repo.add_event(
                            "error",
                            f"{server.get('label')}：保活启动失败",
                            str(exc),
                            level="error",
                            server_id=server_id,
                        )
            elif status == "Running" and repo.get_flag(f"keepalive_wait:{server_id}"):
                # 拉起成功、状态已 Running：清理等待标记。抢占恢复通知由确认器
                # 在「Running 到位」时统一发出（见 scheduler._confirm_pending_events）。
                repo.clear_flag(f"keepalive_wait:{server_id}")

            results.append(
                {
                    "keepalive": server_id,
                    "label": server.get("label"),
                    "status": server.get("instance_status"),
                    "traffic": server.get("traffic_used"),
                }
            )
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
            return {"group_id": group_id, "skipped": "disabled"}

        # 时刻表模式需要知道时区
        group["_timezone"] = config.get("timezone") or "Asia/Shanghai"

        picked = duty_at(group, now_ts)
        if picked is None:
            return {"group_id": group_id, "skipped": "成员不足两台"}
        duty_member, shift_start = picked
        slot_key = str(shift_start)
        run = repo.get_run(group_id, slot_key) or {}

        # 手动换班：本班次被用户手动切过，当班沿用 run.duty_server_id，
        # 且不再发「流量超限故障转移」这类误导通知（界面 toast 已反馈）。
        manual_switch = bool(run.get("manual"))

        # 本班次内已经故障转移过：沿用当时选中的替补，不再按时间重新推导当班。
        failed_over = bool(run.get("failover"))
        if failed_over and int(run.get("duty_server_id") or 0):
            for member in active_members(group):
                if int(member["server_id"]) == int(run["duty_server_id"]):
                    duty_member = member
                    break

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
        for server in others:
            try:
                _refresh(server)
                healthy_others.append(server)
                if repo.get_flag(f"member_err:{server['id']}"):
                    repo.clear_flag(f"member_err:{server['id']}")
            except Exception as exc:  # noqa: BLE001
                flag = f"member_err:{server['id']}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "warning",
                        f"{server.get('label')} 状态读取失败",
                        str(exc),
                        level="warning",
                        group_id=group_id,
                        server_id=int(server["id"]),
                    )

        # 2) 当班超限 → 找一台没超的接班
        failover = False
        breaker = False
        if _over_threshold(duty) and group.get("failover_enabled") and not duty_error:
            candidates = [s for s in healthy_others if not _over_threshold(s)]
            if healthy_others and not candidates:
                breaker = True
            elif candidates:
                replacement = min(candidates, key=lambda s: float(s.get("traffic_used") or 0))
                repo.add_event(
                    "failover",
                    f"{name}：当班流量超限，故障转移",
                    f"{duty.get('label')} → {replacement.get('label')}",
                    level="warning",
                    group_id=group_id,
                    server_id=int(replacement["id"]),
                )
                others = [s for s in others if int(s["id"]) != int(replacement["id"])]
                others.append(duty)
                duty = replacement
                failover = True
            else:
                flag = f"conservative:{group_id}:{slot_key}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "warning",
                        f"{name}：其他成员状态读取失败，保守维持当班",
                        "本轮不做故障转移",
                        level="warning",
                        group_id=group_id,
                    )

        if breaker:
            return self._handle_breaker(group, config, now, slot_key, duty, others, notify_enabled)

        if _over_threshold(duty) and not failover:
            return self._handle_limit_stop(group, config, now, slot_key, duty, others, notify_enabled)

        # 故障转移标志要在「启动当班」之前就确定并持久化，否则转移后下一轮会
        # 因为 run.failover 没写进去而重新判定超限、重复记「故障转移」事件。
        failover_flag = 1 if (failover or failed_over) else 0

        # 3) 保证当班运行 —— 只做「启动当班」，不碰非当班
        duty_status = str(duty.get("instance_status") or "")
        duty_not_running = duty_status not in ("Running",) and duty_status not in TRANSIENT_STATUS
        if duty_not_running:
            # 当班还没跑起来：下发启动，记进度，本轮到此为止（等下一轮确认 Running）
            if run.get("phase") in (PHASE_SETTLED, PHASE_FAILOVER):
                repo.set_flag(f"recovery:{group_id}:{slot_key}")
            # 上一轮可能已经为这个班次开了 running 事件，启动失败时要用它打 error 标志
            prior_event_id = int(run.get("event_id") or 0)
            try:
                aliyun.start_instance(duty)
                repo.add_event(
                    "start",
                    f"{name}：当班未运行，已下发启动",
                    f"{duty.get('label')} 原状态 {duty_status or '未知'}",
                    level="warning",
                    group_id=group_id,
                    server_id=int(duty["id"]),
                )
                repo.clear_flag(f"start_err:{group_id}:{slot_key}")
                event_id = self._shift_event(group_id, slot_key, name, duty.get("label") or "")
                repo.add_event_progress(event_id, f"已下发启动当班 {duty.get('label')}")
                duty_status = "Starting"
            except Exception as exc:  # noqa: BLE001
                flag = f"start_err:{group_id}:{slot_key}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "error",
                        f"{name}：当班启动失败",
                        str(exc),
                        level="error",
                        group_id=group_id,
                        server_id=int(duty["id"]),
                    )
                # 本班次若已有进行中的换班事件，启动失败要把它标记为 error，
                # 否则前端「最近事件」里永远挂着一个转圈。
                if prior_event_id:
                    repo.add_event_progress(prior_event_id, f"当班启动失败：{exc}")
                    repo.finish_event(prior_event_id, "error")
                repo.upsert_run(
                    group_id,
                    slot_key,
                    phase=PHASE_ERROR,
                    stage="",
                    event_id=0,
                    duty_server_id=int(duty["id"]),
                    standby_server_ids=_encode_ids(others),
                    failover=failover_flag,
                    note=str(exc)[:200],
                )
                return {"group_id": group_id, "error": str(exc)}

        # 当班还在启动中（Starting/Pending），本轮不做任何停非当班的动作，等它 Running
        if duty_status in TRANSIENT_STATUS:
            repo.upsert_run(
                group_id,
                slot_key,
                phase=PHASE_SWITCHING,
                stage="starting_duty",
                duty_server_id=int(duty["id"]),
                standby_server_ids=_encode_ids(others),
                failover=failover_flag,
                attempts=int(run.get("attempts") or 0) + 1,
            )
            self._maybe_report_timeout(
                group, config, now_ts, slot_key, shift_start, duty, others, notify_enabled
            )
            return {"group_id": group_id, "slot": slot_key, "ready": False, "stage": "starting_duty"}

        # 4) 当班已 Running：先确认公网 IP 就绪（DDNS 启用时），再同步 DDNS、再停非当班。
        #    ECS 开机后 EIP 绑定有几秒延迟，IP 没就绪就等下一轮，避免把空 IP 写进解析。
        ddns_enabled = config.get("ddns_enabled") == "1"
        has_ip = bool(str(duty.get("public_ip") or "").strip())
        if ddns_enabled and not has_ip:
            repo.upsert_run(
                group_id,
                slot_key,
                phase=PHASE_SWITCHING,
                stage="starting_duty",
                duty_server_id=int(duty["id"]),
                standby_server_ids=_encode_ids(others),
                failover=failover_flag,
                attempts=int(run.get("attempts") or 0) + 1,
            )
            self._maybe_report_timeout(
                group, config, now_ts, slot_key, shift_start, duty, others, notify_enabled
            )
            return {"group_id": group_id, "slot": slot_key, "ready": False, "stage": "waiting_ip"}

        # 当班 Running 且 IP 就绪 → 同步 DDNS（此时域名指向新当班机）
        ddns_status = self._sync_ddns(duty, config, dns.TARGET_GROUP, group_id)

        # 本班次是否已经结算过：结算过就不重复记进度、不重复停非当班、不重复 finish。
        previously_settled = run.get("phase") in (PHASE_SETTLED, PHASE_FAILOVER)

        # 换班事件进度：当班已开机 + DDNS 结果（只在首次进行时记，避免每轮 reconcile 刷屏）
        event_id = self._shift_event(group_id, slot_key, name, duty.get("label") or "")
        if not previously_settled:
            repo.add_event_progress(event_id, f"当班 {duty.get('label')} 已开机")
            repo.add_event_progress(event_id, f"DDNS：{ddns_status}")

        # DDNS 只有「已设置且已绑定」时才要求同步成功，否则不卡流程：
        #   - 未启用 / 未绑定（⏸ 开头）→ 跳过确认，不阻塞
        #   - 已绑定但同步失败（❌ / ⚠️ 开头）→ 本轮不算就绪，等下一轮重试
        ddns_text = str(ddns_status)
        ddns_bound = bool(repo.dns_record_of(dns.TARGET_GROUP, group_id))
        ddns_ok = (not ddns_enabled) or (not ddns_bound) or ddns_text.startswith("✅")

        # 5) 停非当班（此时才停）。物理停机（StopCharging / KeepCharging / Stop /
        #    Not-applicable）就算停好，不再重发；普通停机照常收费但已到位，无需死循环。
        all_stopped = True
        for server in others:
            if _is_stopped_any_mode(server):
                continue
            if server.get("instance_status") in TRANSIENT_STATUS:
                # 已在停机过程中，等它落地
                all_stopped = False
                continue
            try:
                aliyun.stop_instance(server, "StopCharging")
                repo.add_event(
                    "stop",
                    f"{name}：已为非当班实例下发节省停机",
                    f"{server.get('label')} 原状态 {server.get('instance_status') or '未知'}",
                    group_id=group_id,
                    server_id=int(server["id"]),
                )
                repo.clear_flag(f"stop_err:{server['id']}")
                repo.add_event_progress(event_id, f"已下发停机 {server.get('label')}")
            except Exception as exc:  # noqa: BLE001
                flag = f"stop_err:{server['id']}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "error",
                        f"{name}：非当班停机失败",
                        f"{server.get('label')}：{exc}",
                        level="error",
                        group_id=group_id,
                        server_id=int(server["id"]),
                    )
            all_stopped = False

        ready = duty_status == "Running" and all_stopped and (has_ip or not ddns_enabled) and ddns_ok

        first_settle = ready and not previously_settled
        phase = (PHASE_FAILOVER if failover_flag else PHASE_SETTLED) if ready else PHASE_SWITCHING
        stage = "" if ready else "stopping_standby"

        repo.upsert_run(
            group_id,
            slot_key,
            phase=phase,
            stage=stage,
            duty_server_id=int(duty["id"]),
            standby_server_ids=_encode_ids(others),
            failover=failover_flag,
            attempts=int(run.get("attempts") or 0) + 1,
        )

        summary: dict[str, Any] = {
            "group_id": group_id,
            "slot": slot_key,
            "duty": duty.get("label"),
            "mode": "failover" if failover_flag else "normal",
            "ready": ready,
            "ddns": str(ddns_status),
        }

        if not ready:
            self._maybe_report_timeout(
                group, config, now_ts, slot_key, shift_start, duty, others, notify_enabled
            )
            return summary

        # 换班完成：确认全部到位，把进行中的事件改写为最终标题并打上完成标志
        # （只在首次 settle 时做，之后每轮 reconcile 维持 settled 状态，不再重复追加、重复 finish）。
        # 复用「换班进行中」这条 running 事件而不是另开一条，这样事件全程耗时才是真实的换班过程。
        if first_settle and event_id:
            repo.add_event_progress(event_id, "非当班均已节省停机")
            repo.add_event_progress(event_id, "全部状态确认完毕")
            repo.update_event(
                event_id,
                title=f"{name}：{'临时换班' if failover_flag else '换班完成'}",
                kind="shift_failover" if failover_flag else "shift_done",
            )
            repo.finish_event(event_id, "done")

        if not notify_enabled:
            return summary

        # 手动换班：换班动作已执行、事件已打完完成标志，但不再推通知 ——
        # 用户是自己点按钮切的，界面上已经看到结果，再发一条「流量超限故障转移」
        # 或「换班完成」都是误导。
        if manual_switch:
            return summary

        # 6) 全部确认（当班 Running + 非当班节省停机 + DDNS 已同步）后才发通知
        if first_settle or failover:
            self._notify_settled(
                group, config, duty, others, ddns_status, failover_flag, slot_key
            )
        elif (
            repo.get_flag(f"recovery:{group_id}:{slot_key}")
            and config.get("notify_on_recovery") == "1"
        ):
            repo.clear_flag(f"recovery:{group_id}:{slot_key}")
            notify.send_all(config, messages.recovery(duty, str(ddns_status), group_name=name))
            repo.add_event(
                "recovery",
                f"{name}：抢占恢复",
                f"{duty.get('label')} 已恢复运行",
                group_id=group_id,
                server_id=int(duty["id"]),
            )

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
    ) -> dict[str, Any]:
        group_id = int(group["id"])
        name = group.get("name")
        everyone = [duty, *others]

        # 本班次若已有进行中的换班事件，熔断要把它关掉，否则前端永远转圈
        run = repo.get_run(group_id, slot_key) or {}
        if int(run.get("event_id") or 0):
            repo.add_event_progress(int(run["event_id"]), "全部成员流量超限，触发全局熔断")
            repo.finish_event(int(run["event_id"]), "error")

        for server in everyone:
            # 已经物理停机就不再下指令（含 Not-applicable，避免死循环）
            if not _is_stopped_any_mode(server):
                try:
                    aliyun.stop_instance(server, "StopCharging")
                except Exception as exc:  # noqa: BLE001
                    repo.add_event(
                        "error",
                        f"{name}：熔断停机失败",
                        f"{server.get('label')}：{exc}",
                        level="error",
                        group_id=group_id,
                        server_id=int(server["id"]),
                    )

        repo.upsert_run(
            group_id,
            slot_key,
            phase=PHASE_BREAKER,
            event_id=0,
            duty_server_id=int(duty["id"]),
            standby_server_ids=_encode_ids(others),
            note="全部成员流量均超限",
        )

        flag = f"breaker:{group_id}:{slot_key}"
        if notify_enabled and config.get("notify_on_breaker") == "1" and not repo.get_flag(flag):
            # 全部成员物理停机到位才发通知。everyone 是 reconcile 开头 _refresh
            # 实时拉过的状态：本轮刚下发停机、实例可能还在 Stopping 中间态，
            # _all_stopped_any_mode 判定为 False，本轮不发，等下一轮 _refresh
            # 经 API 确认全部 Stopped 后再发，避免「指令下发≠状态到位」就误报。
            if _all_stopped_any_mode(everyone):
                notify.send_all(config, messages.breaker(everyone, group_name=name))
                repo.set_flag(flag)
                repo.add_event(
                    "breaker",
                    f"{name}：全局熔断",
                    f"{len(everyone)} 台成员全部超限，{messages.stop_tail(everyone)}",
                    level="error",
                    group_id=group_id,
                )

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
        if int(run.get("event_id") or 0):
            repo.add_event_progress(int(run["event_id"]), "当班流量超限，触发超限停机")
            repo.finish_event(int(run["event_id"]), "error")

        # 只有物理停机（Stopped 且 mode 合法）才算到位；否则（运行中/中间态）下指令
        if not _is_stopped_any_mode(duty):
            if duty_status not in TRANSIENT_STATUS:
                try:
                    aliyun.stop_instance(duty, "StopCharging")
                    repo.add_event(
                        "limit_stop",
                        f"{name}：当班流量超限，已强制节省停机",
                        f"{duty.get('label')} 原状态 {duty_status or '未知'}",
                        level="warning",
                        group_id=group_id,
                        server_id=int(duty["id"]),
                    )
                except Exception as exc:  # noqa: BLE001
                    repo.add_event(
                        "error",
                        f"{name}：超限停机失败",
                        str(exc),
                        level="error",
                        group_id=group_id,
                        server_id=int(duty["id"]),
                    )
        else:
            flag = f"limit_stop:{group_id}:{slot_key}"
            if (
                notify_enabled
                and config.get("notify_on_limit_stop") == "1"
                and _is_stopped_any_mode(duty)
                and not repo.get_flag(flag)
            ):
                # duty 是 reconcile 开头 _refresh 实时拉过的状态，物理停机到位才发；
                # 不再用 repo.get_server 的旧快照覆盖，确保「API 确认到位」后才报告。
                notify.send_all(config, messages.limit_stop(duty, group_name=name))
                repo.set_flag(flag)

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

        notify.send_all(
            config,
            messages.shift_error(
                duty, others, settings.SHIFT_TIMEOUT_MINUTES, group_name=group.get("name") or ""
            ),
        )
        repo.set_flag(flag)
        repo.add_event(
            "error",
            f"{group.get('name')}：换班超时",
            f"已等待 {elapsed:.0f} 分钟仍未就绪",
            level="error",
            group_id=int(group["id"]),
        )

    # -- DDNS -------------------------------------------------------------
    def _sync_ddns(
        self, server: dict[str, Any], config: dict[str, str], kind: str, target_id: int
    ) -> ddns.DDNSResult:
        """把「这台机器正在跑」写进它绑定的那条记录。

        没绑就没有记录可写，返回「未绑定」而不是偷偷用第一条 —— 以前就是那样，
        两台机器抢同一个域名，谁最后跑完谁说了算。
        """
        record = repo.dns_record_of(kind, target_id)
        if not record:
            return ddns.DDNSResult(dns.STATUS_UNBOUND, "未在 DNS 页绑定记录")
        return dns.sync_record(record, config, ip=str(server.get("public_ip") or ""))

    # -- 换班进行中的事件（动态化）----------------------------------------
    def _shift_event(self, group_id: int, slot_key: str, name: str, duty_label: str) -> int:
        """取本班次「进行中」的换班事件 ID；没有就新建一条 running 事件。

        换班跨多轮 reconcile，这里用 shift_runs.event_id 记住事件，后续轮次
        追加进度（add_event_progress），最终 finish_event 打完成标志。
        """
        run = repo.get_run(group_id, slot_key) or {}
        event_id = int(run.get("event_id") or 0)
        if event_id:
            return event_id
        event_id = repo.add_event(
            "shift_done",
            f"{name}：换班进行中",
            f"正在启动当班 {duty_label}",
            level="info",
            group_id=group_id,
            status="running",
        )
        repo.upsert_run(group_id, slot_key, event_id=event_id)
        repo.add_event_progress(event_id, f"换班开始：当班 {duty_label}")
        return event_id

    # -- 结算通知 ---------------------------------------------------------
    def _notify_settled(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        duty: dict[str, Any],
        others: list[dict[str, Any]],
        ddns_status: ddns.DDNSResult,
        failover: int,
        slot_key: str,
    ) -> None:
        group_id = int(group["id"])
        name = group.get("name") or ""
        is_failover = bool(failover)
        kind = "shift_failover" if is_failover else "shift_done"
        # 换班完成与临时换班共用同一个 slot，但要去重键必须分开，
        # 否则「已结算过又临时换班」时这条报告会被当成重复直接吞掉
        flag = f"notify:{group_id}:{slot_key}:{'failover' if is_failover else 'settled'}"
        if repo.get_flag(flag):
            return

        enabled_key = "notify_on_failover" if is_failover else "notify_on_shift"
        if config.get(enabled_key) != "1":
            repo.set_flag(flag)
            return

        if is_failover:
            # 引擎里被接管的那台一定在 others 里；流量读数缺失时退化成「第一台」，
            # 免得报告只剩「接替账号」一块
            over = [server for server in others if _over_threshold(server)]
            if not over and others:
                over = [others[0]]
            taken = {id(server) for server in over}
            standby = [server for server in others if id(server) not in taken]
            message = messages.shift_failover(
                duty, over, standby, str(ddns_status), group_name=name
            )
        else:
            # 本班次如果先发过「换班异常」，成功时按脚本换标题
            delayed = bool(repo.get_flag(f"timeout:{group_id}:{slot_key}"))
            period = _shift_span(group, duty, config)
            message = messages.shift_done(
                duty, others, period, str(ddns_status), group_name=name, delayed=delayed
            )
            if delayed:
                repo.clear_flag(f"timeout:{group_id}:{slot_key}")

        notify.send_all(config, message)
        repo.set_flag(flag)
        # 换班过程的那条 running 事件已经在 reconcile 里改题、finish 过了；
        # 这里只补一份结算摘要，不再另开一条 0 秒完成的 done 事件。
        run = repo.get_run(group_id, slot_key) or {}
        event_id = int(run.get("event_id") or 0)
        if event_id:
            repo.update_event(
                event_id,
                detail=f"当班 {duty.get('label')} / 待机 {len(others)} 台 / {ddns_status}",
            )
        else:
            # 兜底：理论上不会走到（_shift_event 一定先建了 running 事件），
            # 万一 run 里丢了 event_id，仍补一条结果事件，别让通知发了却无迹可寻
            repo.add_event(
                kind,
                f"{name}：{'临时换班' if is_failover else '换班完成'}",
                f"当班 {duty.get('label')} / 待机 {len(others)} 台 / {ddns_status}",
                group_id=group_id,
                server_id=int(duty["id"]),
            )

    # -- 手动操作 ---------------------------------------------------------
    def manual_switch(self, group_id: int) -> dict[str, Any]:
        """立即跳到下一台当班。

        老 hours 累加模式：把轮换起点 anchor_at 平移到下一班的开头。
        固定时刻表模式：当班由真实时钟决定，anchor 不起作用，改走
        shift_runs 的 failover/duty_server_id 覆盖 —— reconcile_group 里
        failed_over 分支会沿用这个手动指定，把下一台拉起来、停掉原当班。
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

            if _is_timetable_group(group):
                return self._manual_switch_timetable(group, config, now, now_ts)

            total = sum(float(m["hours"]) for m in members) * 3600
            anchor = float(group.get("anchor_at") or group.get("created_at") or now_ts)
            elapsed = max(0.0, now_ts - anchor)
            cycle_index = int(elapsed // total)
            offset = elapsed - cycle_index * total

            # 找到当前所在成员，再取「下一个成员」在一个周期里的累计起点。
            # 把 anchor 挪到 now - 该起点，此刻就正好落在下一班的开头。
            accumulated = 0.0
            cum_before_next = 0.0
            size = len(members)
            for index, member in enumerate(members):
                span = float(member["hours"]) * 3600
                if offset < accumulated + span:
                    cum_before_next = accumulated + span if index + 1 < size else 0.0
                    break
                accumulated += span
            else:
                cum_before_next = 0.0

            new_anchor = int(now_ts - cum_before_next)
            dbmod.execute(
                "UPDATE shift_groups SET anchor_at=? WHERE id=?", (new_anchor, group_id)
            )
            repo.add_event("manual", f"{group.get('name')}：手动换班", "已跳到下一台", group_id=group_id)
            return self.reconcile_group(repo.get_group(group_id) or group, config, now, now_ts)
        finally:
            self._lock.release()

    def _manual_switch_timetable(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        now: dt.datetime,
        now_ts: float,
    ) -> dict[str, Any]:
        """固定时刻表模式下的手动换班：把「下一台」写进本班次 run 的 duty_server_id，
        借 reconcile_group 的 failed_over 分支完成切换（开下一台 → 停原当班 → 通知）。"""
        group_id = int(group["id"])
        members = active_members(group)
        picked = duty_at(group, now_ts)
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

        repo.upsert_run(
            group_id,
            slot_key,
            phase=PHASE_SWITCHING,
            stage="",
            duty_server_id=int(next_member["server_id"]),
            failover=1,
            manual=1,
            event_id=0,
        )
        repo.add_event(
            "manual",
            f"{group.get('name')}：手动换班",
            f"{current_member.get('label') or current_member['server_id']} → "
            f"{next_member.get('label') or next_member['server_id']}",
            group_id=group_id,
        )
        # reconcile_group 里 failed_over 会沿用 run.duty_server_id，把下一台当班；
        # manual 标记让它在换班动作完成后跳过通知（手动换班界面已反馈）。
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

        # 1) 下发指令。停机用节省停机，不支持则退化普通停机（不报错）。
        if action == "start":
            aliyun.start_instance(server)
        else:
            aliyun.stop_instance(server, "StopCharging", fallback=True)

        # 2) 建 running 事件，交给后台确认器轮询确认，不在这里同步等状态。
        detail = f"已下发{label}指令"
        if action == "stop":
            detail += "（节省停机）"
        if note:
            detail += f" · 由{note}托管，下一轮同步会按流程纠正"

        event_id = repo.add_event(
            "manual",
            f"{server.get('label')}：手动{label}",
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
        notify.send_all(config, message)
        repo.set_flag(flag)
        repo.add_event("daily_report", "每日流量报告已推送", f"{len(servers)} 台服务器")
        return True


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _timezone(name: str) -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return dt.timezone(dt.timedelta(hours=8))


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


def _shift_span(group: dict[str, Any], duty: dict[str, Any], config: dict[str, str]) -> str:
    """当前班次文案：优先用「班次名称（起–止）」，时刻表没配就退回小时数。"""
    member = _member_of(group, int(duty.get("id") or 0))
    slot_name = member.get("slot_name") or ""
    start = member.get("start_time") or ""
    end = member.get("end_time") or ""
    if slot_name or start or end:
        return messages.shift_period(slot_name, start, end)
    # 老 hours 累加模式：没有时刻表，退回「机名（N 小时）」式表达
    hours = float(member.get("hours") or _hours_of(group, duty))
    label = str(duty.get("label") or "")
    return f"{label}（{hours:g} 小时）"


def _over_threshold(server: dict[str, Any]) -> bool:
    used = server.get("traffic_used")
    if used is None:
        return False
    threshold = float(server.get("threshold_gb") or 0)
    return threshold > 0 and float(used) >= threshold


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


def _all_stopped_any_mode(servers: list[dict[str, Any]]) -> bool:
    return all(_is_stopped_any_mode(server) for server in servers)


def _refresh(server: dict[str, Any]) -> None:
    traffic = aliyun.get_traffic_gb(server)
    info = aliyun.get_instance(server)
    repo.update_server_runtime(
        int(server["id"]),
        traffic=traffic,
        status=info.status,
        stopped_mode=info.stopped_mode,
        public_ip=info.public_ip,
        region_id=info.region_id,
    )
    repo.add_sample(int(server["id"]), traffic, info.status)
    server["traffic_used"] = traffic
    server["instance_status"] = info.status
    server["stopped_mode"] = info.stopped_mode
    server["public_ip"] = info.public_ip
    if info.region_id:
        server["region_id"] = info.region_id


engine = Engine()
