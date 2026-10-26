"""核心引擎：联合换班（N 台按小时轮换）+ 单机保活。

对账（reconcile）幂等：同一班次内反复执行不会重复操作，也不会重复推送。
所有「已做过」的标记都落 SQLite，容器重启不丢。
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import logging
import re
import threading
import time
from typing import Any

from . import aliyun, db as dbmod, ddns, notify, repo, settings

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

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str) -> str:
    return _TAG_RE.sub("", value)


def _gb(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f} GB"


def _usage_line(server: dict[str, Any]) -> str:
    used = server.get("traffic_used")
    quota = float(server.get("quota_gb") or 0) or 1
    percent = (used or 0) / quota * 100
    tone = "🟢 正常" if percent < 60 else ("🟡 注意" if percent < 90 else "🔴 危险")
    return f"┃ 📈 已用：{_gb(used)}（{percent:.2f}%）{tone}"


def server_block(
    icon: str,
    heading: str,
    server: dict[str, Any],
    status_text: str,
    ddns_status: str | None = None,
) -> str:
    lines = [
        f"{icon} <b>{heading}</b>",
        f"┃ 🏷 别称：{html_lib.escape(str(server.get('label') or ''))}",
        f"┃ 📦 实例：<code>{html_lib.escape(str(server.get('instance_id') or ''))}</code>",
        f"┃ 🌐 ＩＰ：{server.get('public_ip') or '—'}",
    ]
    if ddns_status:
        # 「⏸ DDNS 未启用」这类状态自带前缀，不要再叠一层 "DDNS："
        label = ddns_status if "DDNS" in ddns_status else f"DDNS：{ddns_status}"
        lines.append(f"┃ ☁️ {label}")
    lines.append(_usage_line(server))
    lines.append(f"┃ 🚧 状态：{status_text}")
    return "\n".join(lines)


def _message(kind: str, title: str, body: str) -> notify.Message:
    raw = f"{title}\n{body}"
    return notify.Message(kind=kind, title=strip_html(title), html=raw, text=strip_html(raw))


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
                repo.add_event("error", "单机保活对账异常", str(exc), level="error")

            for group in repo.list_groups():
                try:
                    results.append(self.reconcile_group(group, config, now, now_ts))
                except Exception as exc:  # noqa: BLE001
                    log.exception("换班组 %s 对账失败", group.get("id"))
                    repo.add_event(
                        "error",
                        f"换班组 {group.get('name')} 对账异常",
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
                    repo.add_event(
                        "start",
                        f"{server.get('label')}：保活拉起",
                        f"原状态 {status}",
                        level="warning",
                        server_id=server_id,
                    )
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
                repo.clear_flag(f"keepalive_wait:{server_id}")
                if config.get("notify_on_keepalive") == "1":
                    ddns_status = self._sync_ddns(server, config, None)
                    body = (
                        "\n\n实例此前处于停机状态，已自动拉起。\n\n"
                        + server_block(
                            "🖥", server.get("label") or "", server, "🟢 Running", str(ddns_status)
                        )
                        + "\n━━━━━━━━━━━━━━━"
                    )
                    notify.send_all(config, _message("keepalive", "🟢 <b>CDT 单机保活报告</b>", body))

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

        picked = duty_at(group, now_ts)
        if picked is None:
            return {"group_id": group_id, "skipped": "成员不足两台"}
        duty_member, shift_start = picked
        slot_key = str(shift_start)
        run = repo.get_run(group_id, slot_key) or {}

        # 本班次内已经故障转移过：沿用当时选中的替补，不再按时间重新推导当班。
        # 否则每一轮都会重新判定「原当班超限」，事件刷屏、通知重复，且替补每分钟换一次。
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
                # 同一台连续读失败只记一条，恢复后解除
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

        # 3) 保证当班运行
        duty_status = str(duty.get("instance_status") or "")
        if duty_status not in ("Running",) and duty_status not in TRANSIENT_STATUS:
            if run.get("phase") in (PHASE_SETTLED, PHASE_FAILOVER):
                repo.set_flag(f"recovery:{group_id}:{slot_key}")
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
                duty_status = "Starting"
            except Exception as exc:  # noqa: BLE001
                # 库存不足这类错误会每轮复现，只记一次，换班次后自然重来
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
                repo.upsert_run(
                    group_id,
                    slot_key,
                    phase=PHASE_ERROR,
                    duty_server_id=int(duty["id"]),
                    standby_server_ids=_encode_ids(others),
                    note=str(exc)[:200],
                )
                return {"group_id": group_id, "error": str(exc)}

        # 4) 当班跑起来了 → 其余全部节省停机
        all_stopped = True
        if duty_status == "Running":
            for server in others:
                if server.get("instance_status") == "Stopped":
                    continue
                if server.get("instance_status") in TRANSIENT_STATUS:
                    # 已经在停机过程中，等它落地即可；重复下发只会刷事件
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
        else:
            all_stopped = False

        ddns_enabled = config.get("ddns_enabled") == "1"
        has_ip = bool(str(duty.get("public_ip") or "").strip())
        ready = duty_status == "Running" and all_stopped and (has_ip or not ddns_enabled)

        previously_settled = run.get("phase") in (PHASE_SETTLED, PHASE_FAILOVER)
        first_settle = ready and not previously_settled
        # 故障转移在本班次内保持一致（sticky），所以「已转移」标记不能因为本轮回落到
        # normal 分支就被清零，否则下一轮又会重新判一遍换班
        failover_flag = 1 if (failover or failed_over) else 0
        phase = (PHASE_FAILOVER if failover_flag else PHASE_SETTLED) if ready else PHASE_SWITCHING

        repo.upsert_run(
            group_id,
            slot_key,
            phase=phase,
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
        }

        if not ready:
            self._maybe_report_timeout(
                group, config, now_ts, slot_key, shift_start, duty, others, notify_enabled
            )
            return summary

        # 5) DDNS 先行，再通知（保证通知里的解析状态真实）
        ddns_status = self._sync_ddns(duty, config, group.get("ddns_record") or None)
        summary["ddns"] = str(ddns_status)

        if not notify_enabled:
            return summary

        # 正常结算只在班次第一次就绪时推；临时换班是本轮真的发生了切换才推
        if first_settle or failover:
            self._notify_settled(group, config, duty, others, ddns_status, failover, slot_key)
        elif (
            repo.get_flag(f"recovery:{group_id}:{slot_key}")
            and config.get("notify_on_recovery") == "1"
        ):
            repo.clear_flag(f"recovery:{group_id}:{slot_key}")
            body = (
                "\n\n当班实例疑被抢占，已自动恢复运行。\n\n"
                + server_block("🖥", duty.get("label") or "", duty, "🟢 Running", str(ddns_status))
                + "\n━━━━━━━━━━━━━━━"
            )
            notify.send_all(config, _message("recovery", "🟢 <b>CDT 抢占恢复报告</b>", body))
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

        for server in everyone:
            if server.get("instance_status") != "Stopped":
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
            duty_server_id=int(duty["id"]),
            standby_server_ids=_encode_ids(others),
            note="全部成员流量均超限",
        )

        flag = f"breaker:{group_id}:{slot_key}"
        if notify_enabled and config.get("notify_on_breaker") == "1" and not repo.get_flag(flag):
            fresh = [repo.get_server(int(s["id"])) or s for s in everyone]
            if all(s.get("instance_status") == "Stopped" for s in fresh):
                blocks = "\n━━━━━━━━━━━━━━━\n".join(
                    server_block("🖥", s.get("label") or "", s, "🔴 Stopped (StopCharging)")
                    for s in fresh
                )
                body = (
                    "\n\n⚠ <b>警告：所有实例流量均已到达阈值，「全局熔断」，均已节省停机。</b>\n\n"
                    + blocks
                    + "\n━━━━━━━━━━━━━━━"
                )
                notify.send_all(config, _message("breaker", "🚨 <b>CDT 全局熔断报告</b>", body))
                repo.set_flag(flag)
                repo.add_event(
                    "breaker",
                    f"{name}：全局熔断",
                    f"{len(fresh)} 台成员全部超限，均已节省停机",
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

        if duty_status != "Stopped":
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
            fresh = repo.get_server(int(duty["id"])) or duty
            flag = f"limit_stop:{group_id}:{slot_key}"
            if (
                notify_enabled
                and config.get("notify_on_limit_stop") == "1"
                and fresh.get("stopped_mode") == "StopCharging"
                and not repo.get_flag(flag)
            ):
                body = (
                    "\n\n当班实例流量超限且无法换班，已强制节省停机。\n\n"
                    + server_block(
                        "🖥", fresh.get("label") or "", fresh, "🔴 Stopped (StopCharging)"
                    )
                    + "\n━━━━━━━━━━━━━━━"
                )
                notify.send_all(config, _message("limit_stop", "🔴 <b>CDT 超限停机报告</b>", body))
                repo.set_flag(flag)

        repo.upsert_run(
            group_id,
            slot_key,
            phase=PHASE_BREAKER,
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
        elapsed = (now_ts - shift_start) / 60
        if elapsed < settings.SHIFT_TIMEOUT_MINUTES:
            return
        flag = f"timeout:{group['id']}:{slot_key}"
        if repo.get_flag(flag):
            return

        blocks = [
            server_block(
                "🚩",
                "应当运行账号",
                duty,
                f"{duty.get('instance_status')} ({duty.get('stopped_mode') or '-'})",
            )
        ]
        for server in others:
            blocks.append(
                server_block(
                    "🚩",
                    "应当停止账号",
                    server,
                    f"{server.get('instance_status')} ({server.get('stopped_mode') or '-'})",
                )
            )
        body = (
            f"\n\n⏳ <b>换班超时：已执行 {settings.SHIFT_TIMEOUT_MINUTES} 分钟，但状态未就绪。</b>\n\n"
            + "\n━━━━━━━━━━━━━━━\n".join(blocks)
            + "\n━━━━━━━━━━━━━━━"
        )
        notify.send_all(config, _message("shift_error", "⚠️ <b>CDT 换班异常报告</b>", body))
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
        self, server: dict[str, Any], config: dict[str, str], record_override: str | None
    ) -> ddns.DDNSResult:
        ip = str(server.get("public_ip") or "")
        if record_override:
            config = {**config, "ddns_record": record_override}
        record = (config.get("ddns_record") or "").strip()
        cache_key = f"ddns_last_ip:{record}" if record else "ddns_last_ip"
        cached = repo.get_setting(cache_key) or ("" if record_override else config.get("ddns_last_ip") or "")
        result = ddns.sync(ip, {**config, "ddns_last_ip": cached})

        update: dict[str, str] = {
            "ddns_last_status": result.status,
            "ddns_last_at": str(int(time.time())),
        }
        if result.status in (ddns.STATUS_SYNCED, ddns.STATUS_UNCHANGED):
            update[cache_key] = ip
            if not record_override:
                update["ddns_last_ip"] = ip
        repo.set_settings(update)
        return result

    # -- 结算通知 ---------------------------------------------------------
    def _notify_settled(
        self,
        group: dict[str, Any],
        config: dict[str, str],
        duty: dict[str, Any],
        others: list[dict[str, Any]],
        ddns_status: ddns.DDNSResult,
        failover: bool,
        slot_key: str,
    ) -> None:
        group_id = int(group["id"])
        name = group.get("name") or ""
        kind = "shift_failover" if failover else "shift_done"
        # 换班完成与临时换班共用同一个 slot，但要去重键必须分开，
        # 否则「已结算过又临时换班」时这条报告会被当成重复直接吞掉
        flag = f"notify:{group_id}:{slot_key}:{'failover' if failover else 'settled'}"
        if repo.get_flag(flag):
            return

        enabled_key = "notify_on_failover" if failover else "notify_on_shift"
        if config.get(enabled_key) != "1":
            repo.set_flag(flag)
            return

        if failover:
            title = "⚠️ <b>CDT 临时换班报告</b>"
            head = "\n\n📢 <b>故障转移</b>：当班流量超限，已启用备用实例。"
        else:
            title = "✅ <b>CDT 换班完成报告</b>"
            head = f"\n\n🕒 <b>当前班次</b>：{duty.get('label')}（{_hours_of(group, duty)} 小时）"

        blocks = [
            server_block(
                "🟢", "接替账号" if failover else "运行账号", duty, "🟢 Running", str(ddns_status)
            )
        ]
        for server in others:
            blocks.append(
                server_block(
                    "⚪", "超限账号" if failover else "停止账号", server, "⚪ Stopped (StopCharging)"
                )
            )
        body = head + "\n━━━━━━━━━━━━━━━\n" + "\n━━━━━━━━━━━━━━━\n".join(blocks) + "\n━━━━━━━━━━━━━━━"

        notify.send_all(config, _message(kind, title, body))
        repo.set_flag(flag)
        repo.add_event(
            kind,
            f"{name}：{'临时换班' if failover else '换班完成'}",
            f"当班 {duty.get('label')} / 待机 {len(others)} 台 / {ddns_status}",
            group_id=group_id,
            server_id=int(duty["id"]),
        )

    # -- 手动操作 ---------------------------------------------------------
    def manual_switch(self, group_id: int) -> dict[str, Any]:
        """立即跳到下一台：把轮换起点平移到当前班的结束时刻。"""
        # 和后台调度抢同一把锁：否则两边会同时给同一批实例下指令
        if not self._lock.acquire(timeout=30):
            raise ValueError("后台正在对账，请稍后再试")
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

    def manual_action(self, server_id: int, action: str) -> dict[str, Any]:
        server = repo.get_server(server_id, decrypt_secret=True)
        if server is None:
            raise ValueError("服务器不存在")
        if action == "start":
            aliyun.start_instance(server)
        elif action == "stop":
            aliyun.stop_instance(server, "StopCharging")
        else:
            raise ValueError("只支持 start / stop")
        info = aliyun.get_instance(server)
        repo.update_server_runtime(
            server_id,
            status=info.status,
            stopped_mode=info.stopped_mode,
            public_ip=info.public_ip,
        )
        label = "开机" if action == "start" else "节省停机"
        repo.add_event(
            "manual", f"{server.get('label')}：手动{label}", f"当前 {info.status}", server_id=server_id
        )
        return info.to_dict()

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

        blocks = [
            server_block(
                "🖥",
                server.get("label") or "",
                server,
                f"{server.get('instance_status')} ({server.get('stopped_mode') or '-'})",
            )
            for server in servers
        ]
        body = "\n\n" + "\n━━━━━━━━━━━━━━━\n".join(blocks) + "\n━━━━━━━━━━━━━━━"
        body += f"\n⏰ 更新时间：{dt.datetime.now(tz).strftime('%Y-%m-%d %H:%M')}"
        notify.send_all(config, _message("daily_report", "📊 <b>CDT 每日流量报告</b>", body))
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


def _hours_of(group: dict[str, Any], server: dict[str, Any]) -> str:
    for member in group.get("members") or []:
        if int(member.get("server_id") or 0) == int(server.get("id") or 0):
            return f"{float(member.get('hours') or 0):g}"
    return "?"


def _over_threshold(server: dict[str, Any]) -> bool:
    used = server.get("traffic_used")
    if used is None:
        return False
    threshold = float(server.get("threshold_gb") or 0)
    return threshold > 0 and float(used) >= threshold


def _refresh(server: dict[str, Any]) -> None:
    traffic = aliyun.get_traffic_gb(server)
    info = aliyun.get_instance(server)
    repo.update_server_runtime(
        int(server["id"]),
        traffic=traffic,
        status=info.status,
        stopped_mode=info.stopped_mode,
        public_ip=info.public_ip,
    )
    repo.add_sample(int(server["id"]), traffic, info.status)
    server["traffic_used"] = traffic
    server["instance_status"] = info.status
    server["stopped_mode"] = info.stopped_mode
    server["public_ip"] = info.public_ip


engine = Engine()
