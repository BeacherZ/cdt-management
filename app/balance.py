"""流量均衡：不看时钟，看累计流量。

组内成员通常各属不同阿里云账号（一个账号一台），域名同一时刻只指向一台。
每到一个评估点，把「当班累计 − 最少成员累计」算一遍，差值够了就切过去。

与联合换班的区别只有两点：
  - 选人规则是「谁用得最少」，不是按排班表轮；
  - 成员可以一直在线（包年包月停机不省钱），切换只动 DNS，不下发停机。

共同点：先确认接替的那台在跑，再改域名，最后才按「下班动作」处理上一台。
顺序错了会出现「域名指向一台没起来的机器」或者「停完机才想起来域名还挂着」。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Any

from . import aliyun, dns, messages, notify, repo, settings, shift

log = logging.getLogger(__name__)

# 接替的那台起不来，最多试这么多次就换人（与换班一致）
START_TRIES = 3
# 两次下发开机指令的最小间隔：加速通道 10 秒一轮，没有这道闸，「试 3 次」
# 会被压成 30 秒，一次状态同步稍慢就把慢启动判成起不来
START_RETRY_SECONDS = settings.DUTY_START_RETRY_SECONDS
# 判死需要的墙钟下限：次数够了还要真的等过这么久，和换班的加速轮一个口径
STALL_SECONDS = settings.DUTY_STALL_SECONDS
# 单次切换等就绪的墙钟上限：成员可能被抢占后排队开机，给足 10 分钟
START_TIMEOUT = 600
# 全员到阈值后，多久再看一眼（等月初重置或用户调高阈值）
EXHAUSTED_RECHECK = 3600
# 同一组的异常提醒最小间隔，避免每轮对账都推一条
ERROR_NOTIFY_INTERVAL = 3600

# 切换进行中的两种阶段。加速通道只认这两个 —— 其余阶段没有指令在飞，
# 10 秒一读纯属白烧阿里云接口。
AWAITING_PHASES = ("starting", "switching")

# 「当班超限、但组里关了故障转移」这条熔断的原因标记。
# 它和「全员到阈值」共用 phase=exhausted，但解除条件完全不同，靠这句话分辨。
STOP_DUTY_NOTE = "当班流量超限且未启用故障转移"

_lock = threading.RLock()


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _month_key(now: float, tz: dt.tzinfo) -> str:
    """账期键。阿里云免费额度按自然月重置，跨月后所有累计值归零。"""
    return dt.datetime.fromtimestamp(now, tz).strftime("%Y-%m")


def _find(rows: list[dict[str, Any]], server_id: int) -> dict[str, Any] | None:
    for row in rows:
        if int(row.get("id") or 0) == int(server_id or 0):
            return row
    return None


def _is_running(server: dict[str, Any]) -> bool:
    return str(server.get("instance_status") or "") == "Running"


def _used(server: dict[str, Any]) -> float | None:
    value = server.get("traffic_used")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _threshold(server: dict[str, Any]) -> float:
    try:
        return float(server.get("threshold_gb") or 0)
    except (TypeError, ValueError):
        return 0.0


def over_limit(server: dict[str, Any]) -> bool:
    """这台是否已经吃到阈值。读不到流量不算超限，避免把「没读上来」当成「用完了」。"""
    used = _used(server)
    threshold = _threshold(server)
    return used is not None and threshold > 0 and used >= threshold


def _least(
    rows: list[dict[str, Any]], exclude: set[int] = frozenset()  # type: ignore[assignment]
) -> dict[str, Any] | None:
    """累计用得最少的那台。流量相同时按组内顺序，保证结果稳定。

    已到阈值的、正在被排除的、流量还没读到的，都不参与 —— 读不到就选，
    可能把域名指给一台其实已经超额停机的机器。
    """
    pool = [
        row
        for row in rows
        if int(row["id"]) not in exclude and not over_limit(row) and _used(row) is not None
    ]
    if not pool:
        return None
    pool.sort(key=lambda row: (_used(row) or 0.0, int(row.get("position") or 0)))
    return pool[0]


def _candidate_status(rows: list[dict[str, Any]], exclude: set[int]) -> str:
    """排除掉指定成员后，还有没有人能接班。三种答案要分开：

      ok      有明确没到阈值、且读得到流量的成员；
      over    能读到的成员都到阈值了 —— 该熔断，不是故障；
      unknown 一台流量都没读到 —— 这是「没数据」，不能当成「用完了」。
              以前二者混在一起，CDT 抖一下就会把整组停机（还推一条「全员到阈值」）。
    """
    pool = [row for row in rows if int(row["id"]) not in exclude]
    if not pool:
        return "over"
    known = False
    for row in pool:
        if _used(row) is None:
            continue
        known = True
        if not over_limit(row):
            return "ok"
    return "over" if known else "unknown"


def _stall_key(group_id: int, server_id: int) -> str:
    """「这台是什么时候开始没在跑的」：判死要按墙钟，不能只数次数。"""
    return f"balance_stall:{int(group_id)}:{int(server_id)}"


def _cmd_key(group_id: int, server_id: int) -> str:
    """「上一次下发开机是什么时候」：加速轮 10 秒一次，靠它节流成 60 秒一发。"""
    return f"balance_cmd:{int(group_id)}:{int(server_id)}"


def _clear_wait(group_id: int, server_id: int) -> None:
    repo.clear_flag(_stall_key(group_id, server_id))
    repo.clear_flag(_cmd_key(group_id, server_id))


def _down_for(group_id: int, server_id: int) -> float:
    """这台已经「该跑但没跑」多久（秒）。第一次问就先把起点记下来。"""
    key = _stall_key(group_id, server_id)
    age = repo.flag_age(key)
    if age < 0:
        repo.set_flag(key)
        return 0.0
    return age


def _start(group_id: int, server: dict[str, Any]) -> bool:
    """下发开机，成功返回 True。60 秒内不重复下发，避免每轮砸一次写接口。

    与换班一致：`aliyun.start_instance` 失败只记日志，调用方下一轮再来 ——
    开机接口本身有幂等保护，重复失败也不会把状态搞乱。
    """
    server_id = int(server["id"])
    age = repo.flag_age(_cmd_key(group_id, server_id))
    if 0 <= age < START_RETRY_SECONDS:
        return False
    try:
        aliyun.start_instance(server)
    except Exception as exc:  # noqa: BLE001 - 下一轮（节流后）再试
        log.warning(
            "流量均衡下发开机失败 group=%s server=%s: %s", group_id, server_id, exc
        )
        return False
    repo.clear_flag(_cmd_key(group_id, server_id))
    repo.set_flag(_cmd_key(group_id, server_id))
    return True


def _reason_text(reason: str) -> str:
    return {
        "first": "首次指派",
        "new": "首次指派",
        "traffic": "当班流量到阈值",
        "stall": "当班多次拉起未成功",
        "manual": "手动均衡",
        "flow": "当班用量领先",
        "restart": "当班掉线后重新拉起",
    }.get(reason, reason)


def _settle_note(
    group: dict[str, Any],
    target: dict[str, Any],
    prev: dict[str, Any] | None,
    reason: str,
) -> str:
    """结算时写进事件的一条「为什么切」。带上具体数字，事后能复核。

    和换班的区别只有这一条：换班按时刻表走，不需要解释原因；
    流量均衡每一轮都可能切，不给数字的话，用户无法判断这次切换合不合理。
    """
    used_target = _used(target) or 0.0
    label = str(target.get("label") or "—")
    if reason in ("first", "new"):
        # 「最少」是在**能接班的成员**里比出来的：超限的、读不到流量的都不参与，
        # 所以不能写成「全组最少」—— 那种场景下被选中的可能就是剩下那台用得最多的。
        return f"首次指派：交给可用成员里累计用量最少的 {label}（{used_target:.2f} GB）"
    if reason == "manual":
        return f"手动触发：跳过差值与冷却，直接交给 {label}（{used_target:.2f} GB）"
    if reason == "traffic":
        return f"当班已到阈值，改由 {label} 接班（{used_target:.2f} GB）"
    if reason == "stall":
        return f"当班多次拉起未成功，改由 {label} 接班（{used_target:.2f} GB）"
    if reason == "restart":
        return f"当班掉线，已重新拉起 {label}（{used_target:.2f} GB）"
    used_prev = _used(prev) if prev is not None else 0.0
    diff = float(group.get("diff_gb") or 10)
    return (
        f"当班累计 {used_prev:.2f} GB，比 {label} 多出 {used_prev - used_target:.2f} GB"
        f"（阈值 {diff:g} GB）"
    )


# --------------------------------------------------------------------------
# 对外：一轮对账
# --------------------------------------------------------------------------

def reconcile_all(config: dict[str, str] | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """调度线程每轮调用。只有启用且成员够数的组才动。"""
    config = config or repo.get_settings()
    now = float(now if now is not None else time.time())
    out: list[dict[str, Any]] = []
    for group in repo.list_balance_groups():
        if not group.get("enabled"):
            continue
        try:
            with _lock:
                result = _reconcile(group, config, now)
            if result:
                out.append(result)
        except Exception as exc:  # noqa: BLE001 - 一组出错不能拖垮其他组和巡检
            log.exception("流量均衡组对账失败 group=%s", group.get("id"))
            repo.upsert_balance_state(
                int(group["id"]), phase="error", note=str(exc)[:200]
            )
    return out


def reconcile_awaiting(config: dict[str, str] | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """加速通道：只推进「开机 / 切换进行中」的那几组，每 10 秒一次。

    和换班的那条通道同构：走的是同一段 _reconcile，判断逻辑一行没改，
    只是把这组的闹钟从「巡检间隔」换成 10 秒，并且只读状态、不查流量。
    日常巡检的间隔仍然只来自用户设置，这里不碰。

    没有这个通道时，均衡下发开机后要等下一轮巡检（默认 60 秒、最长可到几十分钟）
    才结算，用户看到的是「域名半天不切」。成员数越多、开机越慢，这段空窗越明显。
    """
    config = config or repo.get_settings()
    now = float(now if now is not None else time.time())
    out: list[dict[str, Any]] = []
    for group_id in repo.balance_awaiting_ids():
        group = repo.get_balance_group(group_id)
        if group is None or not group.get("enabled"):
            continue
        try:
            with _lock:
                # 抢到锁之后再确认一次：日常巡检可能刚把这一组推进完了
                state = repo.get_balance_state(group_id) or {}
                if str(state.get("phase") or "") not in AWAITING_PHASES:
                    continue
                result = _reconcile(group, config, now, fast=True)
            if result:
                out.append(result)
        except Exception:  # noqa: BLE001 - 一组出错不能拖垮其他组
            log.exception("流量均衡加速对账失败 group=%s", group_id)
    return out


def _next_slot(now: float, interval: int, tz: dt.tzinfo) -> int:
    """下一个整点评估时刻（严格大于 now）。

    评估周期都是整小时的倍数（1/3/6/12/24），所以按本地时区的整点网格排：
    1 小时=每个整点，3 小时=00/03/06/09…，24 小时=每天 0 点。
    以前是 now+interval，从建组 / 上次切换那一刻起算，13:47 建好的组以后
    永远在 47 分评估，日志里一堆零碎时间，也没法和整点的巡检对表。
    """
    local = dt.datetime.fromtimestamp(now, tz)
    # 以本地 0 点为网格原点：所有周期都能整除 24 小时，所以网格稳定
    base = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    step = float(max(60, int(interval)))
    slot = int((now - base) // step) + 1
    return int(base + slot * step)


def _on_grid(ts: float, interval: int, tz: dt.tzinfo) -> bool:
    """这个时间戳是否正好落在评估网格上。"""
    local = dt.datetime.fromtimestamp(ts, tz)
    base = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    step = float(max(60, int(interval)))
    return abs((ts - base) % step) < 1


def _reconcile(
    group: dict[str, Any],
    config: dict[str, str],
    now: float,
    fast: bool = False,
) -> dict[str, Any] | None:
    # fast=True 只有加速通道会用：读状态但不查流量（沿用库里上一轮读数）。
    # 流量是 GB 级慢数据，差十几秒不影响超限判断；CDT 才是要省着打的接口。
    group_id = int(group["id"])
    members = [m for m in group.get("members") or [] if m.get("enabled")]
    if len(members) < 2:
        return {"group": group_id, "state": "idle"}

    servers = {int(s["id"]): s for s in repo.list_servers(decrypt_secret=True)}
    rows: list[dict[str, Any]] = []
    for member in members:
        server = servers.get(int(member["server_id"]))
        if server is None:
            continue
        row = dict(server)
        row["off_action"] = str(member.get("off_action") or "keep")
        row["position"] = int(member.get("position") or 0)
        rows.append(row)
    if len(rows) < 2:
        return {"group": group_id, "state": "idle"}

    interval = max(60, int(group.get("interval_minutes") or 60)) * 60
    tz = shift._timezone(config.get("timezone") or "Asia/Shanghai")
    cycle = _month_key(now, tz)
    state = repo.get_balance_state(group_id) or {}

    # 跨月：额度归零，档位起点跟着清。当前当班先不动 —— 月初全员都是 0，
    # 这时候按规则评会立刻乱切一轮，等下一个评估点再看。
    if str(state.get("cycle_key") or "") != cycle:
        state = repo.upsert_balance_state(
            group_id,
            cycle_key=cycle,
            slot_base_gb=0.0,
            next_eval_at=_next_slot(now, interval, tz),
        )

    phase = str(state.get("phase") or "idle")
    if phase in AWAITING_PHASES:
        return _advance(group, state, rows, config, now, fast)
    if phase == "exhausted":
        if str(state.get("note") or "") == STOP_DUTY_NOTE:
            # 关着故障转移时只停了当班：必须等**这台自己**流量回落（月初重置 /
            # 调高阈值）才恢复。不能因为「别的成员没超」就换人 —— 那等于绕过了
            # 用户的开关，绕了一圈还是自动换了一台。
            held = _find(rows, int(state.get("duty_server_id") or 0))
            if held is None:
                # 被停的那台已经不在组里（用户改了成员）：没有「等它回落」可言，
                # 直接解除，让下一轮按新成员重新指派，否则整组永久卡在熔断上
                ready = True
            else:
                ready = _used(held) is not None and not over_limit(held)
        else:
            # 熔断（能读到的都到阈值）：任一台流量回落就解除，下一轮重新指派
            ready = any(_used(row) is not None and not over_limit(row) for row in rows)
        if ready:
            state = repo.upsert_balance_state(
                group_id, phase="idle", duty_server_id=0, next_eval_at=0
            )
        else:
            return {"group": group_id, "state": "exhausted"}

    # 旧版本按「上次时间 + 周期」排的评估点会落在零碎分钟（13:47、14:47…），
    # 就地纠正到整点网格，不用等下一次切换才归位。只改还没到点的：
    # 已经过期的点照常立刻评估，评估完自然会排到下一个整点。
    next_eval = int(state.get("next_eval_at") or 0)
    if next_eval > now and not _on_grid(next_eval, interval, tz):
        state = repo.upsert_balance_state(group_id, next_eval_at=_next_slot(now, interval, tz))

    duty = _find(rows, int(state.get("duty_server_id") or 0))

    # 还没有当班：直接指派用得最少的一台
    if duty is None:
        pick = _least(rows)
        if pick is None:
            return _no_candidate(group, rows, config, now, set())
        return _begin(group, state, rows, config, now, pick, "first", verify=not fast)

    shift._refresh(duty, with_traffic=not fast)

    # ① 流量保护第一：当班到阈值，不等评估点
    if over_limit(duty):
        if not group.get("failover_enabled"):
            # 组里关了故障转移，但「不让机器烧穿流量」是全局约定，比组的开关更靠前：
            # 只停当班这一台，其余成员不动，等流量回落或人工处理。
            return _stop_duty(group, state, rows, config, now, duty)
        pick = _least(rows, exclude={int(duty["id"])})
        if pick is None:
            return _no_candidate(
                group, rows, config, now, {int(duty["id"])}, note="其余成员也已到阈值"
            )
        return _begin(group, state, rows, config, now, pick, "traffic", verify=not fast)

    # 当班跑着且没超限：把残留的判死计时清掉（它已经好了）
    if _is_running(duty):
        _clear_wait(group_id, int(duty["id"]))
    else:
        # ② 当班掉线（被抢占 / 被停 / 开机失败）：先拉起，拉不起来才换人。
        #    以前的实现只在下一次评估点才动，评估周期是 24 小时的组等于整天空着。
        return _begin(group, state, rows, config, now, duty, "restart")

    # ③ 到评估点：读所有成员，比较差值
    if now >= float(state.get("next_eval_at") or 0):
        for row in rows:
            if int(row["id"]) != int(duty["id"]):
                shift._refresh(row, with_traffic=not fast)
        # 休息中的成员到阈值照样停机：它们不接域名，但流量是实打实在烧
        for row in rows:
            if int(row["id"]) != int(duty["id"]) and over_limit(row) and _is_running(row):
                _stop_member(group, row, config)
        pick = _least(rows, exclude={int(duty["id"])})
        if pick is None:
            status = _candidate_status(rows, {int(duty["id"])})
            if status == "unknown":
                # 流量读不到 ≠ 都用完了：不动当班，把这一轮记到下一个评估点
                repo.upsert_balance_state(group_id, next_eval_at=_next_slot(now, interval, tz))
                return {"group": group_id, "state": "idle", "duty": int(duty["id"])}
            return _breaker(group, rows, config, now)
        used_duty = _used(duty) or 0.0
        used_pick = _used(pick) or 0.0
        diff = float(group.get("diff_gb") or 10)
        min_duty = int(group.get("min_duty_minutes") or 30) * 60
        cooled = now - float(state.get("last_switch_at") or 0) >= min_duty
        if cooled and used_duty - used_pick >= diff:
            return _begin(group, state, rows, config, now, pick, "flow", verify=not fast)
        repo.upsert_balance_state(group_id, next_eval_at=_next_slot(now, interval, tz))

    return {"group": group_id, "state": "idle", "duty": int(duty["id"])}


def _begin(
    group: dict[str, Any],
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    config: dict[str, str],
    now: float,
    pick: dict[str, Any],
    reason: str,
    verify: bool = True,
) -> dict[str, Any]:
    """决定把域名交给 pick。已在跑的直接结算，没跑的先下发开机等下一轮。

    reason="restart" 是「当班掉线，原位拉起」：pick 就是当前当班，prev 也指向它，
    结算时不会把它当成交班对象停掉。

    verify=True 时，只在「这台已经在跑、这一轮就要结算」的场合多读一次它的真实
    流量：选人靠的是库里的缓存读数（可能是上一轮甚至上一小时的），真读回来发现
    它已经到阈值就不能把域名指过去 —— 那等于让它接着烧，而这一轮本来就是按
    「谁用得少」做的决定。一次切换一次 CDT 调用，代价可接受；跑不起来的那台
    走的是开机分支，本来就要等，不在这里多查。
    """
    group_id = int(group["id"])
    old_id = int(state.get("duty_server_id") or 0)
    pick_id = int(pick["id"])
    if verify and _is_running(pick):
        shift._refresh(pick, with_traffic=True)
        if over_limit(pick):
            log.info(
                "流量均衡暂不结算：接替成员已到阈值 group=%s server=%s", group_id, pick_id
            )
            return {
                "group": group_id,
                "state": str(state.get("phase") or "idle"),
                "note": "接替成员流量已到阈值，下一轮重挑",
            }

    event_id = _open_event(group, pick, reason)
    # 新建组不只是「指一次域名」：组员此刻的真实状态往往和「下班后」设置对不上，
    # 逐台核对并按设置摆正，用户点开事件就能看到每台为什么动/没动。
    if reason == "new":
        _audit_members(group, rows, event_id, pick_id)
    # 换人 / 重启都从零开始计次：新目标要享有完整的「试 3 次」额度，
    # 不清的话它一上来就继承上一台的次数，下一轮立刻又被判死。
    _clear_wait(group_id, pick_id)

    started = False
    if not _is_running(pick):
        started = _start(group_id, pick)
        if not started:
            repo.add_event_progress(event_id, "下发开机失败或仍在节流窗口内，下一轮重试")

    state = repo.upsert_balance_state(
        group_id,
        duty_server_id=pick_id,
        prev_duty_id=old_id,
        phase="starting",
        stage="start",
        attempts=1 if started else 0,
        last_switch_at=int(now),
        event_id=event_id,
        note=reason,
    )
    if _is_running(pick):
        return _settle(group, state, rows, config, now, reason)
    return {"group": group_id, "state": "starting"}


def _advance(
    group: dict[str, Any],
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    config: dict[str, str],
    now: float,
    fast: bool = False,
) -> dict[str, Any]:
    """切换进行中：等接替的那台 Running，起不来就换下一个。

    判死要「次数 + 墙钟」两个条件。加速通道 10 秒一轮，只看次数的话
    3 次就是 30 秒，一次状态同步稍慢就把慢启动判成起不来，触发连环换人。
    """
    group_id = int(group["id"])
    target = _find(rows, int(state.get("duty_server_id") or 0))
    if target is None:
        _abort_event(state, "接替成员已不在组内，切换中止")
        repo.upsert_balance_state(
            group_id, phase="idle", duty_server_id=0, prev_duty_id=0, event_id=0, note=""
        )
        return {"group": group_id, "state": "idle"}

    target_id = int(target["id"])
    shift._refresh(target, with_traffic=not fast)

    # 接替的那台自己已经到阈值了：别把域名指过去，换下一个。
    # 放在「是否 Running」之前判断：跑着但超限同样不能接，否则结算完下一轮又要切，
    # 这中间域名还白白指了一趟烧穿流量的机器。
    if over_limit(target):
        exclude = {target_id, int(state.get("prev_duty_id") or 0)}
        pick = _least(rows, exclude=exclude)
        if pick is None:
            result = _no_candidate(
                group, rows, config, now, exclude, note="接替成员也已到阈值"
            )
            if result.get("state") == "exhausted":
                return result
            # 读不到流量、又没人能接：别把这一轮挂在 starting 上无限等
            return _give_up_switch(group_id, state, "接替成员超限且没有其他可用成员，切换中止")
        repo.add_event_progress(
            int(state.get("event_id") or 0),
            f"{target.get('label') or '接替成员'} 流量已到阈值，改由 {pick.get('label') or '其他成员'} 接替",
        )
        _clear_wait(group_id, target_id)
        repo.upsert_balance_state(group_id, phase="idle")
        fresh = repo.get_balance_state(group_id) or {}
        return _begin(group, fresh, rows, config, now, pick, "traffic", verify=not fast)

    if _is_running(target):
        # 起来了：把「掉线多久 / 试了几次」清掉，下次它再掉线从头算
        _clear_wait(group_id, target_id)
        return _settle(group, state, rows, config, now, str(state.get("note") or ""))

    attempts = int(state.get("attempts") or 0)
    waited = now - float(state.get("last_switch_at") or 0)
    down_for = _down_for(group_id, target_id)

    if waited > START_TIMEOUT:
        _notify_error(
            group,
            target,
            config,
            now,
            int(waited // 60),
            f"{target.get('label') or '接替成员'} 一直未就绪",
        )

    if (attempts >= START_TRIES and down_for >= STALL_SECONDS) or waited > START_TIMEOUT:
        exclude = {target_id, int(state.get("prev_duty_id") or 0)}
        pick = _least(rows, exclude=exclude)
        if pick is None:
            result = _no_candidate(
                group, rows, config, now, exclude, note="接替成员多次拉起未成功"
            )
            if result.get("state") == "exhausted":
                return result
            return _give_up_switch(
                group_id, state, "接替成员未就绪且没有其他可用成员，切换中止"
            )
        repo.add_event_progress(
            int(state.get("event_id") or 0),
            f"{target.get('label') or '接替成员'} 未就绪，改由 {pick.get('label') or '其他成员'} 接替",
        )
        # 只清 phase，保留 duty_server_id：_begin 会把它写进 prev_duty_id，
        # 结算通知里「超限/离线账号」才指得准。以前这里连 duty 一起清零，
        # 换人结算后 prev 变成 0，故障转移通知只剩「接替账号」一段。
        _clear_wait(group_id, target_id)
        repo.upsert_balance_state(group_id, phase="idle")
        fresh = repo.get_balance_state(group_id) or {}
        return _begin(group, fresh, rows, config, now, pick, "stall", verify=not fast)

    if _start(group_id, target):
        repo.upsert_balance_state(group_id, attempts=attempts + 1)
    return {"group": group_id, "state": "starting"}


def _settle(
    group: dict[str, Any],
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    config: dict[str, str],
    now: float,
    reason: str,
) -> dict[str, Any]:
    """接替的那台已经 Running：先改域名，再按下班动作处理上一台。"""
    group_id = int(group["id"])
    target = _find(rows, int(state.get("duty_server_id") or 0))
    if target is None:
        _abort_event(state, "当班成员已不在组内，本轮切换中止")
        repo.upsert_balance_state(
            group_id, phase="idle", duty_server_id=0, prev_duty_id=0, event_id=0, note=""
        )
        return {"group": group_id, "state": "idle"}
    prev = _find(rows, int(state.get("prev_duty_id") or 0))
    event_id = int(state.get("event_id") or 0)

    report = _sync_dns(group, target, config)
    repo.add_event_progress(event_id, f"DDNS：{report.detail}")
    # 换班有时刻表，用户知道为什么换；均衡没有，所以「为什么这一轮切了」
    # 必须留在事件里，否则事后只剩一句「均衡完成」，看不出是差值到了还是被抢占。
    repo.add_event_progress(event_id, _settle_note(group, target, prev, reason))

    # 交班那台写进报告时要用**停机之后**的状态：不下班（off_action=keep）就还是
    # Running，按设置停掉了就是 Stopped。拿停机前的 rows 副本会写成 Running，
    # 和同一份报告里「已停机」的进度对不上。
    prev_report = prev
    if prev is not None and int(prev["id"]) != int(target["id"]):
        if str(prev.get("off_action") or "keep") == "stop" and _is_running(prev):
            try:
                prev_copy = dict(prev)
                shift.stop_server_by_pref(prev_copy)
                prev_copy["instance_status"] = "Stopped"
                prev_report = prev_copy
                repo.add_event_progress(event_id, f"{prev.get('label') or '上一台'} 已按实例设置停机")
            except Exception as exc:  # noqa: BLE001 - 停不掉不影响域名已经切走
                log.warning("流量均衡停旧当班失败 group=%s server=%s: %s", group_id, prev.get("id"), exc)
                repo.add_event_progress(event_id, f"{prev.get('label') or '上一台'} 停机失败：{exc}")

    interval = max(60, int(group.get("interval_minutes") or 60)) * 60
    tz = shift._timezone(config.get("timezone") or "Asia/Shanghai")
    # 结算完这一轮等待就结束了：把「掉线多久 / 试了几次」清掉，别带到下一轮
    _clear_wait(group_id, int(target["id"]))
    if prev is not None:
        _clear_wait(group_id, int(prev["id"]))
    repo.upsert_balance_state(
        group_id,
        phase="idle",
        stage="",
        attempts=0,
        prev_duty_id=0,
        event_id=0,
        slot_base_gb=float(_used(target) or 0.0),
        last_switch_at=int(now),
        next_eval_at=_next_slot(now, interval, tz),
        note="",
    )

    # 三种结论各自一条事件，和换班的「换班完成 / 手动换班完成 / 故障转移」同构。
    # 以前只有一种「均衡完成」，故障转移看起来像正常轮换，事后翻事件分不出区别。
    failover = reason in ("traffic", "stall")
    manual = reason == "manual"
    recover = reason == "restart"
    new_group = reason == "new"
    if failover:
        title = messages.ev("均衡转移", messages.obj_balance(str(group.get("name") or "")))
        kind = "balance_failover"
    elif manual:
        title = messages.ev("手动均衡完成", messages.obj_balance(str(group.get("name") or "")))
        kind = "balance_manual"
    elif recover:
        # 当班掉线后被原位拉起：这是恢复，不是一次新的均衡，别写成「均衡完成」
        title = messages.ev("均衡恢复", messages.obj_balance(str(group.get("name") or "")))
        kind = "balance_recover"
    elif new_group:
        # 与换班的「新建换班组」一致：事件名不随结算改口，全程一个说法
        title = messages.ev("新建均衡组", messages.obj_balance(str(group.get("name") or "")))
        kind = "balance_new"
    else:
        title = messages.ev("均衡完成", messages.obj_balance(str(group.get("name") or "")))
        kind = "balance_done"
    # 概览和换班结算同格式：当班 + 待机台数 + DDNS。原因写在上面的进度里，
    # 不要塞进这一行 —— 概览是要「一眼看完」，细节点开再看。
    standby = max(0, len(rows) - 1)
    detail = f"当班 {target.get('label') or '—'}，待机 {standby} 台，{report.brief}"
    repo.update_event(event_id, title=title, detail=detail, kind=kind)
    repo.finish_event(event_id, "done", detail)

    # 故障转移沿用换班的开关口径（notify_on_failover），正常轮换用流量均衡自己的开关。
    # 两条都默认开，用户关掉某一个时语义清楚：关「流量均衡完成」不该把故障转移也静音。
    # 掉线恢复、新建组都不发轮换报告：恢复不是一次新的均衡，新建组是用户刚点完保存。
    # 但恢复本身要提醒 —— 设置页「实例恢复运行」写的是「实例被停掉后自动拉起成功时
    # 提醒」，没限定归属，换班组和单机保活都发，均衡组漏发等于文案没兑现。
    if recover or new_group:
        if recover and config.get("notify_on_recovery") == "1":
            results = notify.send_all(
                config,
                messages.recovery(
                    target,
                    report.status,
                    group_name=str(group.get("name") or ""),
                    scope=messages.SCOPE_BALANCE,
                ),
            )
            notify.note_notify(event_id, results)
        return {"group": group_id, "state": "idle", "duty": int(target["id"])}

    enabled_key = "notify_on_failover" if failover else "notify_on_balance"
    if config.get(enabled_key) == "1":
        if failover:
            over = [prev_report] if prev_report is not None else []
            message = messages.balance_failover(
                target,
                over,
                rows,
                report.status,
                group_name=str(group.get("name") or ""),
                reason=reason,
            )
        else:
            message = messages.balance_switch(
                target,
                rows,
                report.status,
                group_name=str(group.get("name") or ""),
                prev=prev_report,
                manual=manual,
                reason=reason,
            )
        results = notify.send_all(config, message)
        # 和换班一样写成「通知已发送 / 通知发送失败：x」，
        # 没配渠道时什么都不写（不是失败，只是没发）。
        notify.note_notify(event_id, results)

    return {"group": group_id, "state": "idle", "duty": int(target["id"])}


def _breaker(
    group: dict[str, Any],
    rows: list[dict[str, Any]],
    config: dict[str, str],
    now: float,
    note: str = "",
) -> dict[str, Any]:
    """没人能接班：全部停掉，避免整组继续烧流量。

    只在「能读到的成员都到阈值」时调用。流量读不到会走 _no_candidate 的另一条
    分支，不在这里 —— 把「没数据」当成「用完了」会把整组无辜停机。
    """
    group_id = int(group["id"])
    state = repo.get_balance_state(group_id) or {}
    _abort_event(state, "全员流量到阈值，切换中止")
    stopped: list[dict[str, Any]] = []
    for row in rows:
        if not _is_running(row):
            continue
        try:
            copy = dict(row)
            shift.stop_server_by_pref(copy)
            copy["instance_status"] = "Stopped"
            stopped.append(copy)
        except Exception as exc:  # noqa: BLE001
            log.warning("流量均衡熔断停机失败 group=%s server=%s: %s", group_id, row.get("id"), exc)

    repo.upsert_balance_state(
        group_id,
        phase="exhausted",
        duty_server_id=0,
        prev_duty_id=0,
        stage="",
        attempts=0,
        event_id=0,
        next_eval_at=int(now + EXHAUSTED_RECHECK),
        note=note or "全员流量到阈值",
    )
    title = messages.ev("全局熔断", messages.obj_balance(str(group.get("name") or "")))
    detail = f"均衡组没有可接班的成员（{note or '全部到阈值'}），已停机 {len(stopped)} 台"
    repo.add_event("balance_breaker", title, detail, level="warn", group_id=group_id)
    if config.get("notify_on_breaker") == "1":
        notify.send_all(
            config, messages.balance_exhausted(stopped or rows, str(group.get("name") or ""), note)
        )
    return {"group": group_id, "state": "exhausted"}


def _no_candidate(
    group: dict[str, Any],
    rows: list[dict[str, Any]],
    config: dict[str, str],
    now: float,
    exclude: set[int],
    note: str = "",
) -> dict[str, Any]:
    """没有人能接班时的分流：真超限才熔断，读不到就下一轮再看。"""
    if _candidate_status(rows, exclude) == "unknown":
        log.info("流量均衡暂不动作：成员流量未读到 group=%s", group.get("id"))
        return {"group": int(group["id"]), "state": "idle", "note": "成员流量未读到"}
    return _breaker(group, rows, config, now, note=note)


def _stop_duty(
    group: dict[str, Any],
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    config: dict[str, str],
    now: float,
    duty: dict[str, Any],
) -> dict[str, Any]:
    """当班到阈值、但组里关了故障转移：只停这一台，别让整组陪葬。

    换班那边同样的场景也这么处理（`_handle_limit_stop`）：流量保护是全局约定，
    比组上的「允许故障转移」更靠前 —— 开关只决定「找不找替班」，不决定
    「要不要眼睁睁看它烧穿」。

    停完进 exhausted，**不会因为别的成员没超就自动换人**：那等于绕开开关换了一台。
    等这台自己流量回落（月初重置 / 调高阈值）再恢复，见 `_reconcile` 的解除分支。
    """
    group_id = int(group["id"])
    _abort_event(state, "当班流量超限且未启用故障转移，切换中止")
    mode = ""
    if not shift._is_stopped_any_mode(duty):
        try:
            mode = shift.stop_server_by_pref(dict(duty))
        except Exception as exc:  # noqa: BLE001 - 停不掉也要记，下一轮还会再来
            log.warning("流量均衡超限停机失败 group=%s server=%s: %s", group_id, duty.get("id"), exc)
            mode = str(duty.get("stopped_mode") or "")
    stopped = dict(duty)
    stopped["instance_status"] = "Stopped"
    stopped["stopped_mode"] = mode or stopped.get("stopped_mode") or ""

    event_id = repo.add_event(
        "balance_limit",
        messages.ev(
            f"当班流量超限，{messages.stop_phrase(mode)}",
            messages.obj_balance(str(group.get("name") or "")),
        ),
        f"等待停机到位 / {shift._limit_usage(duty)}",  # noqa: SLF001 - 与换班同一句
        level="warning",
        group_id=group_id,
        server_id=int(duty["id"]),
        status="running",
        pending_status="Stopped",
        pending_kind="limit_stop",
    )
    repo.add_event_progress(event_id, shift._limit_usage(duty))  # noqa: SLF001
    if config.get("notify_on_limit_stop") == "1":
        results = notify.send_all(
            config, messages.over_limit(duty, messages.SCOPE_BALANCE, mode=mode or None)
        )
        notify.note_notify(event_id, results)

    repo.upsert_balance_state(
        group_id,
        phase="exhausted",
        # 当班位留着：域名还指着这台、界面也要能把「谁被停了」指出来。
        # 解除时的判断依据就是这台自己的流量，清掉就没法判了。
        prev_duty_id=0,
        stage="",
        attempts=0,
        event_id=0,
        next_eval_at=int(now + EXHAUSTED_RECHECK),
        note=STOP_DUTY_NOTE,
    )
    return {"group": group_id, "state": "exhausted"}


def _stop_member(group: dict[str, Any], row: dict[str, Any], config: dict[str, str]) -> None:
    """休息中的成员也到阈值了：它不接域名，但流量是实打实在烧，照样停。

    只在评估点做（那一轮本来就会读全员流量），不额外增加 API 调用。
    """
    group_id = int(group["id"])
    mode = ""
    try:
        mode = shift.stop_server_by_pref(dict(row))
    except Exception as exc:  # noqa: BLE001
        log.warning("流量均衡停超限成员失败 group=%s server=%s: %s", group_id, row.get("id"), exc)
        return
    stopped = dict(row)
    stopped["instance_status"] = "Stopped"
    stopped["stopped_mode"] = mode or stopped.get("stopped_mode") or ""
    event_id = repo.add_event(
        "balance_limit",
        messages.ev(
            f"流量超限，{messages.stop_phrase(mode)}",
            messages.obj_balance(str(group.get("name") or "")),
        ),
        f"等待停机到位 / {shift._limit_usage(row)}",  # noqa: SLF001
        level="warning",
        group_id=group_id,
        server_id=int(row["id"]),
        status="running",
        pending_status="Stopped",
        pending_kind="limit_stop",
    )
    repo.add_event_progress(event_id, shift._limit_usage(row))  # noqa: SLF001
    if config.get("notify_on_limit_stop") == "1":
        results = notify.send_all(
            config, messages.over_limit(row, messages.SCOPE_BALANCE, mode=mode or None)
        )
        notify.note_notify(event_id, results)


def _abort_event(state: dict[str, Any], note: str) -> None:
    """把还在转的切换事件收尾，别让它永远转圈（成员被删 / 熔断 / 组停用）。

    只碰「进行中」的那条：结算过的组 state 里可能还留着上一条事件的 id，
    拿它再写一笔「切换中止」，就会挂到一条已经完成的「均衡完成」上，
    事后看事件像自己反悔了。
    """
    event_id = int(state.get("event_id") or 0)
    if not event_id:
        return
    event = repo.get_event(event_id)
    if event is None or str(event.get("status") or "") != "running":
        return
    repo.add_event_progress(event_id, note)
    repo.finish_event(event_id, "error", note)


def _give_up_switch(group_id: int, state: dict[str, Any], note: str) -> dict[str, Any]:
    """这一轮切换推不下去了（没有可接替的成员 / 流量读不到）：收尾事件并退回原当班。

    两件事都不能省：
      - phase 必须拨回 idle。留在 starting 的话，加速通道每 10 秒进来一次、
        永远转不完，界面上也一直挂着「切换中」；
      - 当班必须退回 prev。这时候域名还指着原来那台（DNS 只在 _settle 里改），
        状态不能跑到现实前面，否则下一轮会按「当班是那台没起来的机器」去判断。
    """
    _abort_event(state, note)
    repo.upsert_balance_state(
        group_id,
        phase="idle",
        duty_server_id=int(state.get("prev_duty_id") or 0),
        prev_duty_id=0,
        stage="",
        attempts=0,
        event_id=0,
        note="",
    )
    return {"group": group_id, "state": "idle", "note": note}


def _sync_dns(
    group: dict[str, Any], target: dict[str, Any], config: dict[str, str]
) -> dns.DDNSReport:
    """组绑的记录 + 这台机器自己绑的记录一起指过来，与换班结算时一致。"""
    if config.get("ddns_enabled") != "1":
        return dns.no_sync_report(config)
    ip = str(target.get("public_ip") or "")
    names, results = dns.sync_target_full(
        dns.TARGET_BALANCE, int(group["id"]), config, ip=ip, verify=True
    )
    own_names, own_results = dns.sync_target_full(
        dns.TARGET_SERVER, int(target["id"]), config, ip=ip, verify=True
    )
    return dns.report(names + own_names, results + own_results)


def _notify_error(
    group: dict[str, Any],
    duty: dict[str, Any] | None,
    config: dict[str, str],
    now: float,
    minutes: int,
    reason: str = "",
) -> None:
    """切换卡住时的异常提醒。同一组一小时内只发一次 —— 每轮对账都发等于刷屏。"""
    group_id = int(group["id"])
    flag = f"balance_err_sent:{group_id}"
    if repo.get_flag(flag) and repo.flag_age(flag) < ERROR_NOTIFY_INTERVAL:
        return
    repo.set_flag(flag)
    title = messages.ev("均衡异常", messages.obj_balance(str(group.get("name") or "")))
    detail = f"切换 {minutes} 分钟未就绪" + (f"（{reason}）" if reason else "")
    repo.add_event("balance_error", title, detail, level="warn", group_id=group_id)
    if config.get("notify_on_balance_error") == "1":
        notify.send_all(
            config,
            messages.balance_error(duty, minutes, str(group.get("name") or ""), reason),
        )


def _open_event(group: dict[str, Any], pick: dict[str, Any], reason: str) -> int:
    """开一条「进行中」事件，和换班的 _shift_event 同构：

    标题是结论 + 对象（换班进行中 → 均衡进行中），概览只说「在等谁到位」，
    「为什么这一轮动」放进第一条进度。换班也是这么分的：概览一眼看完，细节点开再看。
    """
    group_id = int(group["id"])
    label = pick.get("label") or "—"
    recover = reason == "restart"
    new_group = reason == "new"
    if new_group:
        # 和换班一样：新建组那条事件从开到尾都叫「新建均衡组」，
        # 不写「均衡进行中」—— 用户刚点完保存，看到「进行中」会以为系统背着他动了什么。
        title = messages.ev("新建均衡组", messages.obj_balance(str(group.get("name") or "")))
    else:
        title = messages.ev(
            "均衡恢复中" if recover else "均衡进行中",
            messages.obj_balance(str(group.get("name") or "")),
        )
    detail = f"等待当班 {label} {'重新' if recover else ''}到位"
    event_id = repo.add_event(
        "balance_switch",
        title,
        detail,
        status="running",
        group_id=group_id,
        server_id=int(pick["id"]),
    )
    head = "新建均衡组" if new_group else ("均衡恢复" if recover else "均衡开始")
    repo.add_event_progress(
        event_id,
        f"{head}：当班 {label}；原因：{_reason_text(reason)}",
    )
    return event_id


def _audit_members(
    group: dict[str, Any], rows: list[dict[str, Any]], event_id: int, duty_id: int
) -> None:
    """新建组时逐台核对并摆正成员状态，口径与交班新建组一致。

    当班的开机会由 `_begin` 自己下发，这里只报状态，不重复发指令；待机成员按
    各自的「下班后」设置处理：设了停机的在跑就停掉，设了保持运行的停着就拉起。
    以前这一步完全没做，事件里只有一句「待机 2 台」，设了停机却还在跑的成员
    不会被发现，也永远不会被摆正。
    """
    group_id = int(group["id"])
    repo.add_event_progress(event_id, "按组员设置核对状态：当班运行，待机按「下班后」摆正")
    for row in sorted(rows, key=lambda item: int(item.get("position") or 0)):
        label = row.get("label") or f"#{row['id']}"
        is_duty = int(row["id"]) == duty_id
        should_run = is_duty or str(row.get("off_action") or "keep") == "keep"
        try:
            # 只读状态：这一轮不查流量（流量在 _begin 里只对当班查一次）
            shift._refresh(row, with_traffic=False)
        except Exception as exc:  # noqa: BLE001 - 单台读不到不能挡住其他成员
            repo.add_event_progress(
                event_id, f"{label}：状态读取失败（{shift._brief_exc(exc)}），交给下一轮同步"
            )
            continue
        status = str(row.get("instance_status") or "")
        suffix = messages.status_suffix(status, row.get("stopped_mode"))
        running = _is_running(row)
        if running and should_run:
            repo.add_event_progress(event_id, f"{label}：{status}{suffix}，符合设置")
        elif running and not should_run:
            try:
                shift.stop_server_by_pref(row)
                repo.add_event_progress(
                    event_id, f"{label}：{status}{suffix}，已按设置停机"
                )
            except Exception as exc:  # noqa: BLE001 - 停不掉下一轮再试，别卡住建组
                repo.add_event_progress(
                    event_id, f"{label}：停机失败（{shift._brief_exc(exc)}），下一轮重试"
                )
        elif not should_run:
            repo.add_event_progress(event_id, f"{label}：{status}{suffix}，符合设置")
        elif is_duty:
            repo.add_event_progress(event_id, f"{label}：{status}{suffix}，需要开机")
        elif _start(group_id, row):
            repo.add_event_progress(event_id, f"{label}：{status}{suffix}，已按设置开机")
        else:
            repo.add_event_progress(
                event_id, f"{label}：{status}{suffix}，开机已下发或仍在节流窗口内，下一轮确认"
            )


# --------------------------------------------------------------------------
# 手动 / 立即均衡
# --------------------------------------------------------------------------

def manual_switch(
    group_id: int, config: dict[str, str] | None = None, reason: str = "manual"
) -> dict[str, Any]:
    """用户点「立即均衡」：不管差值、不管冷却，直接交给用得最少的那台。

    前提是「确实有更少的成员」：当班自己就是全组最少时什么也不做，返回
    state="best"，否则 2 台组会为了「点了要有反应」把域名切给更满的那台。

    reason 只区分「谁触发的」：用户点按钮是 manual，建组后的首次指派是 first。
    两者走同一条流程，但事件标题和通知文案要分开 —— 刚保存完组就收到
    「手动均衡完成」会让人以为自己点错了什么。
    """
    config = config or repo.get_settings()
    group = repo.get_balance_group(group_id)
    if group is None:
        raise ValueError("均衡组不存在")
    now = time.time()
    with _lock:
        state = repo.get_balance_state(group_id) or {}
        if str(state.get("phase") or "") in ("starting", "switching"):
            raise ValueError("正在均衡中，请稍后再试")
        members = [m for m in group.get("members") or [] if m.get("enabled")]
        servers = {int(s["id"]): s for s in repo.list_servers(decrypt_secret=True)}
        rows = []
        for member in members:
            server = servers.get(int(member["server_id"]))
            if server is None:
                continue
            row = dict(server)
            row["off_action"] = str(member.get("off_action") or "keep")
            row["position"] = int(member.get("position") or 0)
            rows.append(row)
        if len(rows) < 2:
            raise ValueError("均衡组至少要有 2 台可用实例")
        duty_id = int(state.get("duty_server_id") or 0)
        # 全组一起比，不把当班排除在外：以前只在「非当班」里挑最少，2 台组就只剩
        # 唯一一台，哪怕它比当班更满也会被切过去 —— 手动均衡反而把流量搬到了更满
        # 的机器上。当班自己就是全组最少时不做无意义的切换，直接把结论告诉用户。
        pick = _least(rows)
        if pick is None:
            raise ValueError("没有可接班的成员（全部到阈值或流量未读到）")
        if duty_id and int(pick["id"]) == duty_id:
            return {
                "group": group_id,
                "state": "best",
                "note": "当班已是用量最少的成员，无需切换",
            }
        # 冷却时间置零，让 _begin 立刻生效
        repo.upsert_balance_state(group_id, last_switch_at=0)
        state = repo.get_balance_state(group_id) or {}
        return _begin(group, state, rows, config, now, pick, reason)


def bootstrap_group(group_id: int, config: dict[str, str] | None = None) -> dict[str, Any]:
    """新建组后立刻指一次域名：选流量最少的，在跑就直接切，没跑就等它开机。

    口径和换班的 bootstrap_group 完全一致：**本班次不发结算通知**（用户刚点完
    保存，不需要再收一份自己刚做过的事的报告），事件名固定为「新建均衡组」，
    点进去是「首次指派 → DDNS 同步」的完整过程。

    走不下去的时候（组没启用、成员不足、全员到阈值）也要留一条事件：以前直接
    返回 idle，用户只看到域名没动、事件列表里也没有任何记录，无从判断是没执行
    还是执行失败。
    """
    group = repo.get_balance_group(group_id)
    if group is None:
        raise ValueError("均衡组不存在")
    name = str(group.get("name") or "")
    title = messages.ev("新建均衡组", messages.obj_balance(name))
    if not group.get("enabled"):
        repo.add_event("balance_new", title, "均衡组未启用，没有指派当班，也未改动域名", group_id=group_id)
        return {"state": "idle", "skipped": "disabled"}
    enabled = [m for m in group.get("members") or [] if m.get("enabled")]
    if len(enabled) < 2:
        repo.add_event(
            "balance_new",
            title,
            f"启用成员只有 {len(enabled)} 台，不足以轮流，未指派当班",
            group_id=group_id,
        )
        return {"state": "idle", "skipped": "成员不足两台"}
    try:
        return manual_switch(group_id, config, reason="new")
    except ValueError as exc:
        # 组已经存好了，首次指派失败不能把配置回滚；只补一条事件说明为什么没动
        repo.add_event(
            "balance_new", title, f"未指派当班：{exc}", level="warn", group_id=group_id
        )
        return {"state": "idle", "skipped": str(exc)}


def dissolve_group(group_id: int) -> dict[str, Any]:
    """解散只删配置：成员保持当前状态回到闲散手动，不下发任何指令。"""
    group = repo.get_balance_group(group_id)
    if group is None:
        raise ValueError("均衡组不存在")
    repo.delete_balance_group(group_id)
    title = messages.ev("已解散", messages.obj_balance(str(group.get("name") or "")))
    repo.add_event("balance_dissolve", title, "组员恢复为闲散手动，未下发启停指令", group_id=group_id)
    return {"ok": True}


def groups_of_server(server_id: int) -> list[int]:
    """这台机器在哪些启用中的均衡组里。DNS 巡检要据此判断「此刻别动它的解析」。"""
    found: list[int] = []
    for group in repo.list_balance_groups():
        if not group.get("enabled"):
            continue
        for member in group.get("members") or []:
            if int(member["server_id"]) == int(server_id) and member.get("enabled"):
                found.append(int(group["id"]))
                break
    return found


def snapshot() -> list[dict[str, Any]]:
    """给界面用的组列表：状态、当班、下次评估时间、成员用量。"""
    now = time.time()
    servers = {int(s["id"]): s for s in repo.list_servers()}
    out: list[dict[str, Any]] = []
    for group in repo.list_balance_groups():
        group_id = int(group["id"])
        state = repo.get_balance_state(group_id) or {}
        duty_id = int(state.get("duty_server_id") or 0)
        members = []
        for member in group.get("members") or []:
            server = servers.get(int(member["server_id"])) or {}
            members.append(
                {
                    "id": member["id"],
                    "server_id": member["server_id"],
                    "position": member["position"],
                    "off_action": member.get("off_action") or "keep",
                    "enabled": bool(member.get("enabled")),
                    "is_duty": int(member["server_id"]) == duty_id,
                    "label": server.get("label") or "",
                    "instance_id": server.get("instance_id") or "",
                    "region_id": server.get("region_id") or "",
                    "site": server.get("site") or "",
                    "instance_status": server.get("instance_status") or "",
                    "public_ip": server.get("public_ip") or "",
                    "traffic_used": _used(server),
                    "quota_gb": float(server.get("quota_gb") or 0),
                    "threshold_gb": _threshold(server),
                    "over_limit": over_limit(server),
                }
            )
        out.append(
            {
                "id": group_id,
                "name": group.get("name") or "",
                "enabled": bool(group.get("enabled")),
                "failover_enabled": bool(group.get("failover_enabled")),
                "interval_minutes": int(group.get("interval_minutes") or 60),
                "diff_gb": float(group.get("diff_gb") or 10),
                "min_duty_minutes": int(group.get("min_duty_minutes") or 30),
                "ddns_record": "、".join(repo.dns_records_of(dns.TARGET_BALANCE, group_id)),
                "members": members,
                "duty_server_id": duty_id,
                "phase": str(state.get("phase") or "idle"),
                "note": _reason_text(str(state.get("note") or "")),
                "last_switch_at": int(state.get("last_switch_at") or 0),
                "next_eval_at": int(state.get("next_eval_at") or 0),
                "updated_at": int(state.get("updated_at") or 0) or int(group.get("created_at") or 0),
            }
        )
    return out
