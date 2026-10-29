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
# 人工点了「立即换班」并已就绪：内部当班是 run.duty_server_id 覆盖出来的，
# 和流量超限的 failover 共用一个覆盖字段，但对外必须说「手动换班」。
PHASE_MANUAL = "manual"
PHASE_BREAKER = "breaker"
PHASE_ERROR = "error"

# shift_runs.origin：本班次是怎么来的。
# 新建换班组后的第一次对齐单独标出来 —— 那次事件要一直叫「组名：新建换班组」，
# 不能被结算逻辑改名成「换班完成」，否则用户点保存后看到的事件对不上自己刚做的事。
ORIGIN_NEW_GROUP = "new_group"

TRANSIENT_STATUS = {"Starting", "Pending", "Stopping"}

# 同一台机器在同一个班次里重复下发停机指令的最小间隔（秒）。
# 只有在「状态读不到、库里看着还在跑」时才会重复触发，给它一个节流。
_BREAKER_STOP_COOLDOWN = 120


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


def dns_transitions(snapshot_raw: str, names: list[str]) -> list[str]:
    """对比换班前的快照和记录当前值，返回「旧 → 新」描述（没变的不列）。"""
    try:
        before = json.loads(snapshot_raw or "{}")
    except Exception:  # noqa: BLE001 - 脏数据当没快照处理
        before = {}
    if not isinstance(before, dict):
        before = {}
    changes: list[str] = []
    for name in names:
        row = repo.get_dns_record(name) or {}
        new = str(row.get("last_ip") or "")
        old = str(before.get(name) or "")
        if new and old and old != new:
            changes.append(f"{old} → {new}" if len(names) == 1 else f"{name} {old} → {new}")
        elif new and not old:
            changes.append(f"首次指向 {new}" if len(names) == 1 else f"{name} 首次指向 {new}")
    return changes


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
    def reconcile_all(self) -> list[dict[str, Any]]:
        if not self._lock.acquire(blocking=False):
            log.debug("上一轮还没结束，跳过本次")
            return []
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
                repo.add_event("error", "单机保活同步异常", str(exc), level="error")

            try:
                results.extend(
                    self.reconcile_idle_limits(config, now, grouped, keepalive_ids)
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("闲散机器超限对账失败")
                repo.add_event("error", "闲散机器超限检查异常", str(exc), level="error")

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
        """被换班组接管的机器。

        只算**启用的**组：组停用后成员就该回到闲散状态，单机保活要能接手。
        以前漏了这个判断，停用组 + 组员挂保活 → 保活认为「已被换班组接管」直接
        让位，结果两边都不管，机器就躺着没人开机。
        """
        ids: set[int] = set()
        for group in repo.list_groups():
            if not group.get("enabled"):
                continue
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

            # 流量超限 → 节省停机并暂停保活，不再无脑拉起。
            # 这是 1.1.15 补上的功能：之前单机保活只判断「停了就拉」，从不看阈值，
            # 机器超了阈值还在烧流量。停一次打一个 limit_stop 标记，标记在就不再
            # 碰它（否则下轮又被拉起）；流量回落到阈值以下（包重置 / 调高阈值）后
            # 自动清标记、恢复保活。
            if _over_threshold(server):
                if self._limit_stop_server(server_id, server, config, messages.SCOPE_KEEPALIVE):
                    results.append({"keepalive": server_id, "over_limit": True})
                # 超限停机的机器不再走「停了就拉」：清掉等待标记，别让残留状态
                # 干扰下一轮判断（流量回落前它就该保持停机）。
                if repo.get_flag(f"keepalive_wait:{server_id}"):
                    repo.clear_flag(f"keepalive_wait:{server_id}")
                continue
            # 流量回落到阈值以下（流量包重置 / 调高阈值）：清掉超限标记，
            # 恢复正常的「停了就拉」。否则标记会永久钉住这台机器。
            if repo.get_flag(f"limit_stop:{messages.SCOPE_KEEPALIVE}:{server_id}"):
                repo.clear_flag(f"limit_stop:{messages.SCOPE_KEEPALIVE}:{server_id}")

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
        if not _is_stopped_any_mode(server):
            try:
                aliyun.stop_instance(server, "StopCharging", fallback=True)
            except Exception as exc:  # noqa: BLE001
                err = f"limit_stop_err:{scope}:{server_id}"
                if not repo.get_flag(err):
                    repo.set_flag(err)
                    repo.add_event(
                        "error",
                        f"{server.get('label')}：超限停机失败",
                        str(exc),
                        level="error",
                        server_id=server_id,
                    )
                return False
            repo.clear_flag(f"limit_stop_err:{scope}:{server_id}")

        repo.set_flag(flag)
        event_id = repo.add_event(
            "limit_stop",
            f"{server.get('label')}：流量超限，已节省停机",
            f"已用 {_used_gb(server):.2f} GB，阈值 {float(server.get('threshold_gb') or 0):g} GB",
            level="warning",
            server_id=server_id,
        )
        if config.get("notify_on_limit_stop") == "1":
            results = notify.send_all(config, messages.over_limit(server, scope))
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
        for server in repo.list_servers(decrypt_secret=True):
            server_id = int(server["id"])
            if server_id in grouped or server_id in keepalive_ids:
                continue
            try:
                _refresh(server)
            except Exception:  # noqa: BLE001
                # 读不到状态就当没这回事，别让一台机器把整轮拖垮
                continue
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
            return {"group_id": group_id, "skipped": "disabled"}

        # 时刻表模式需要知道时区
        group["_timezone"] = config.get("timezone") or "Asia/Shanghai"

        picked = actual_duty(group, now_ts)
        if picked is None:
            return {"group_id": group_id, "skipped": "成员不足两台"}
        duty_member, shift_start = picked
        slot_key = str(shift_start)
        run = repo.get_run(group_id, slot_key) or {}

        # manual 和 failover 是两个独立的含义：manual 是「人切的」，failover 是
        # 「流量超限自动转的」。当班覆盖已经由 actual_duty 统一处理，这里只读标志
        # 决定后续怎么描述这次换班（事件标题 / 报告标题 / 阶段）。
        manual_switch = bool(run.get("manual"))
        failed_over = bool(run.get("failover"))

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
        for server in others:
            try:
                _refresh(server)
                healthy_others.append(server)
                if repo.get_flag(f"member_err:{server['id']}"):
                    repo.clear_flag(f"member_err:{server['id']}")
            except Exception as exc:  # noqa: BLE001
                unreadable.add(int(server["id"]))
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

        # 当班状态读不到：本轮不许凭库里的旧快照发指令。
        # 旧快照说「停着」而机器其实在跑，就会给运行中的实例发开机，阿里云回
        # IncorrectInstanceStatus，事件里冒出一条「当班启动失败」，看着像系统坏了；
        # 反过来旧快照说「跑着」而机器已经停了，就漏掉一次该有的拉起。
        # 读不到就等下一轮 —— 拿不到状态时「什么都不做」比「猜着做」安全。
        if duty_error:
            flag = f"duty_err:{group_id}:{slot_key}"
            if not repo.get_flag(flag):
                repo.set_flag(flag)
                repo.add_event(
                    "warning",
                    f"{name}：当班状态读取失败",
                    f"{duty.get('label')}：{duty_error}",
                    level="warning",
                    group_id=group_id,
                    server_id=int(duty["id"]),
                )
            return {"group_id": group_id, "state_unreadable": duty_error}
        repo.clear_flag(f"duty_err:{group_id}:{slot_key}")

        # 2) 当班超限 → 找一台没超的接班；找不到就停机，绝不让超限的当班继续跑
        failover = False
        breaker = False
        breaker_reason = ""
        if _over_threshold(duty) and group.get("failover_enabled") and not duty_error:
            candidates = [s for s in healthy_others if not _over_threshold(s)]
            if candidates:
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

        # 故障转移标志要在「启动当班」之前就确定并持久化，否则转移后下一轮会
        # 因为 run.failover 没写进去而重新判定超限、重复记「故障转移」事件。
        failover_flag = 1 if (failover or failed_over) else 0

        # 3) 保证当班运行 —— 只做「启动当班」，不碰非当班。
        #    整个换班过程（启动当班 → 停非当班 → DDNS）都记在同一条事件里：
        #    以前每一步都单开一条事件，界面上一轮换班会冒出三条互不相干的记录。
        previously_settled = run.get("phase") in (
            PHASE_SETTLED,
            PHASE_FAILOVER,
            PHASE_MANUAL,
        )
        duty_status = str(duty.get("instance_status") or "")
        duty_not_running = duty_status not in ("Running",) and duty_status not in TRANSIENT_STATUS
        # 抢占恢复跨多轮，用 flag 记住「这次是恢复而不是换班」（本地变量活不过一轮）
        recovery_flag = f"recovery:{group_id}:{slot_key}"
        event_id = int(run.get("event_id") or 0)

        if duty_not_running:
            if previously_settled:
                # 已结算的班次里当班又不跑了（被抢占 / 被手动停掉）→ 单独记一条恢复事件。
                # 这不是「换班」，不计换班超时：started_at 从这次拉起重新计时。
                repo.set_flag(recovery_flag)
                event_id = repo.add_event(
                    "recovery",
                    f"{name}：当班未运行，已自动拉起",
                    f"{duty.get('label')} 原状态 {duty_status or '未知'}",
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
                # 事件里挂着的转圈要打上失败标志，否则「最近事件」里永远在转
                repo.add_event_progress(event_id, f"当班启动失败：{exc}")
                repo.finish_event(event_id, "error")
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
                # 已在停机过程中，等它落地
                all_stopped = False
                continue
            try:
                aliyun.stop_instance(server, "StopCharging")
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

        if all_stopped and not previously_settled:
            repo.add_event_progress(event_id, "非当班均已节省停机", once=True)

        # 6) 全部实例状态确认到位之后才动 DDNS。
        #    以前是「当班一开机就同步」，非当班还在跑的时候域名已经指过去了；而且
        #    停机那一轮还会再同步一次，进度里冒出重复的「DDNS：✅ 已同步」这种没意义的一行。
        ddns_text = ""
        ddns_ok = True
        if all_stopped:
            ddns_text = str(self._sync_ddns(duty, config, dns.TARGET_GROUP, group_id))
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
                names = list(repo.dns_records_of(dns.TARGET_GROUP, group_id))
                for name in repo.dns_records_of(dns.TARGET_SERVER, int(duty["id"])):
                    if name not in names:
                        names.append(name)
                before = str((repo.get_run(group_id, slot_key) or {}).get("dns_before") or "")
                text = f"DDNS：{ddns_text}"
                changes = dns_transitions(before, names)
                if changes:
                    text = f"{text} {'、'.join(changes)}"
                # 跨轮可能先失败后成功，这里不用 once：文案变了就该补一条，
                # 完全相同的相邻文案 add_event_progress 自己会吞掉。
                repo.add_event_progress(event_id, text)

        ready = duty_status == "Running" and all_stopped and (has_ip or not ddns_enabled) and ddns_ok

        first_settle = ready and not previously_settled
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
                label, kind = "故障转移", "shift_failover"
            elif manual_switch:
                label, kind = "手动换班完成", "manual"
            elif str(run.get("origin") or "") == ORIGIN_NEW_GROUP:
                label, kind = "新建换班组", "group_new"
            else:
                label, kind = "换班完成", "shift_done"
            repo.update_event(event_id, title=f"{name}：{label}", kind=kind)
            repo.finish_event(event_id, "done")

        # 抢占恢复：当班被拉起并重新就绪，收掉那条恢复事件。
        # flag 跨轮持久化，所以「这轮才拉起、下一轮才就绪」也能收尾。

        recovered = bool(repo.get_flag(recovery_flag)) and not first_settle
        if recovered:
            repo.clear_flag(recovery_flag)
            if event_id:
                repo.add_event_progress(event_id, "当班已恢复运行", once=True)
                repo.update_event(
                    event_id,
                    title=f"{name}：当班已恢复运行",
                    detail=f"当班 {duty.get('label')} 已恢复运行",
                    kind="recovery",
                )
                repo.finish_event(event_id, "done")

        if not notify_enabled:
            return summary

        # 全部确认（当班 Running + 非当班节省停机 + DDNS 已同步）后才发通知。
        # 手动换班同样走这条：报告标题带「(手动换班)」后缀，和自动换班、故障转移分开。
        if first_settle or failover:
            self._notify_settled(
                group, config, duty, others, ddns_text, failover_flag, slot_key,
                manual=manual_switch, shift_event_id=event_id,
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
        if int(run.get("event_id") or 0):
            repo.add_event_progress(int(run["event_id"]), f"{why}，触发全局熔断")
            repo.finish_event(int(run["event_id"]), "error")

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
                aliyun.stop_instance(server, "StopCharging")
                repo.clear_flag(f"breaker_stop_err:{group_id}:{slot_key}:{server_id}")
            except Exception as exc:  # noqa: BLE001
                flag = f"breaker_stop_err:{group_id}:{slot_key}:{server_id}"
                if not repo.get_flag(flag):
                    repo.set_flag(flag)
                    repo.add_event(
                        "error",
                        f"{name}：熔断停机失败",
                        f"{server.get('label')}：{exc}",
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
                    f"{name}：全局熔断",
                    f"{why}，{messages.stop_tail(everyone)}",
                    level="error",
                    group_id=group_id,
                )
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
        if int(run.get("event_id") or 0):
            repo.add_event_progress(int(run["event_id"]), "当班流量超限，触发超限停机")
            repo.finish_event(int(run["event_id"]), "error")

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
                        aliyun.stop_instance(duty, "StopCharging")
                        repo.clear_flag(f"limit_stop_err:{group_id}:{slot_key}")
                        event_flag = f"limit_stop_evt:{group_id}:{slot_key}"
                        if not repo.get_flag(event_flag):
                            repo.set_flag(event_flag)
                            repo.add_event(
                                "limit_stop",
                                f"{name}：当班流量超限，已强制节省停机",
                                f"{duty.get('label')} 原状态 {duty_status or '未知'}",
                                level="warning",
                                group_id=group_id,
                                server_id=int(duty["id"]),
                            )
                    except Exception as exc:  # noqa: BLE001
                        err_flag = f"limit_stop_err:{group_id}:{slot_key}"
                        if not repo.get_flag(err_flag):
                            repo.set_flag(err_flag)
                            repo.add_event(
                                "error",
                                f"{name}：超限停机失败",
                                f"{duty.get('label')}：{exc}",
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
            f"{group.get('name')}：换班超时",
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
    ) -> str:
        """把「这台机器正在跑」写进它该占的记录，返回一句汇总状态。

        同步的不止一条：
          - 目标自己绑的（换班组 → 该组绑的域名；单机 → 这台机器绑的域名）
          - 换班组当班时，还要把**这台机器自己**绑的域名一起指过来 ——
            一台机器可能既在换班组里、又单独挂了别的域名，开机了就该都指向它
        没绑任何记录时返回「⏸ 未绑定」。

        ip 由调用方从刚查到的实时状态传进来：`server` 往往是轮询开始前的数据库
        快照，实例停机时快照里的 public_ip 是空的，拿它去同步只会得到「IP 无效跳过」。
        """
        ip = str(ip or server.get("public_ip") or "")
        names, statuses = dns.sync_target(kind, target_id, config, ip=ip)
        if kind == dns.TARGET_GROUP:
            own_names, own_statuses = dns.sync_target(
                dns.TARGET_SERVER, int(server.get("id") or 0), config, ip=ip
            )
            names = names + own_names
            statuses = statuses + own_statuses
        if not names:
            return dns.STATUS_UNBOUND
        return dns.combine_status(statuses)

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
            return event_id
        event_id = repo.add_event(
            "shift_done",
            f"{name}：换班进行中",
            f"正在启动当班 {duty_label}",
            level="info",
            group_id=group_id,
            status="running",
        )
        # 开工前先记下各域名此刻指向哪，结算那一步才能写出「旧 IP → 新 IP」
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
    ) -> None:
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
        else:
            flag = f"notify:{group_id}:{slot_key}:{scope}"
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
            # 本班次如果先发过「换班异常」，成功时按脚本换标题；
            # 手动切班的报告标题带「(手动换班)」，与自动换班区分
            delayed = bool(repo.get_flag(f"timeout:{group_id}:{slot_key}"))
            period = _shift_span(group, duty, config)
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
        # 换班过程的那条 running 事件已经在 reconcile 里改题、finish 过了；
        # 这里只补一份结算摘要，不再另开一条 0 秒完成的 done 事件。
        run = repo.get_run(group_id, slot_key) or {}
        run_event_id = int(run.get("event_id") or 0)
        if run_event_id:
            repo.update_event(
                run_event_id,
                detail=f"当班 {duty.get('label')} / 待机 {len(others)} 台 / {ddns_status}",
            )
            # 报告是整条换班流程的最后一步：写进事件详情，事后能确认发没发出去
            notify.note_notify(run_event_id, results)
        else:
            # 兜底：理论上不会走到（_shift_event 一定先建了 running 事件），
            # 万一 run 里丢了 event_id，仍补一条结果事件，别让通知发了却无迹可寻
            event_id = repo.add_event(
                kind,
                f"{name}：{'故障转移' if is_failover else ('手动换班完成' if is_manual else '换班完成')}",
                f"当班 {duty.get('label')} / 待机 {len(others)} 台 / {ddns_status}",
                group_id=group_id,
                server_id=int(duty["id"]),
            )
            notify.note_notify(event_id, results)

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
                    f"{name}：新建换班组",
                    "换班组未启用，没有对组员下发任何启停指令",
                    group_id=group_id,
                )
                return {"group_id": group_id, "skipped": "disabled"}

            picked = actual_duty(group, now_ts)
            if picked is None:
                repo.add_event(
                    "group_new",
                    f"{name}：新建换班组",
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
                    f"{name}：新建换班组",
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
                f"{name}：新建换班组",
                f"排班表：{when}由 {duty.get('label')} 当班，其余 {len(others)} 台应节省停机",
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
                        event_id, f"{label}：状态读取失败（{exc}），交给下一轮同步重试"
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
                f"{name}：解散换班组",
                f"组内 {len(member_ids)} 台服务器保持当前状态，未下发任何启停指令",
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
                        event_id, f"{label}：状态读取失败（{exc}），保持不动"
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

        老 hours 累加模式：把轮换起点 anchor_at 平移到下一班的开头。
        固定时刻表模式：当班由真实时钟决定，anchor 不起作用，改走
        shift_runs 的 manual/duty_server_id 覆盖 —— reconcile_group 会沿用
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

            if _is_timetable_group(group):
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
        # 上一次手动换班还没走完就又被点了一次：那条事件马上会被新事件顶掉
        # （run.event_id 被覆盖），界面上它就永远停在「进行中」转圈。
        # 先把它收掉并写明原因，别留一条转不完的假进度。
        for stale in repo.running_events(group_id, ("manual", "shift_done")):
            repo.add_event_progress(
                int(stale["id"]), f"已被新的手动换班取代（改为 {to_label}）"
            )
            repo.update_event(int(stale["id"]), title=f"{name}：手动换班已取消")
            repo.finish_event(int(stale["id"]), "done")
        event_id = repo.add_event(
            "manual",
            f"{name}：手动换班进行中",
            f"{from_label} → {to_label}",
            level="info",
            group_id=group_id,
            status="running",
        )
        repo.add_event_progress(event_id, f"手动换班：{from_label} → {to_label}")
        # 切之前先把各域名的当前指向记下来，DDNS 那一步要写「旧 IP → 新 IP」
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
        """固定时刻表模式下的手动换班：把「下一台」写进本班次 run 的 duty_server_id，
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
            detail += f" / 由{note}托管，下一轮同步会按流程纠正"

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
        results = notify.send_all(config, message)
        repo.set_flag(flag)
        event_id = repo.add_event("daily_report", "每日报告已推送", f"{len(servers)} 台服务器")
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
