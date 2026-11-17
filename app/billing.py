"""费用快照：本月账单（DescribeInstanceBill）+ 账户余额（QueryAccountBalance）。

为什么单独一个模块而不是塞进 shift.py：

- 费用是**慢数据**，跟保活/换班的对账节奏不在一个量级，混在一起会让引擎难读；
- 账单接口失败绝不能影响启停流程（BSS 权限经常没给够），单独一层好做降级；
- 手动刷新按钮和后台定时刷新共用同一段逻辑。

缓存策略三层：
1. `aliyun.get_account_balance` 进程内 6 小时（余额）；
2. 这里落 SQLite（`servers.monthly_cost` 等），容器重启不用立刻重查；
3. 账期字段 `billing_cycle` 记住数字属于哪个月，跨月后界面不再当本月费用显示。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import Any

from . import aliyun, messages, repo, settings

log = logging.getLogger("cdt-management.billing")

# 本月账单刷新间隔的默认值（秒）。实际间隔从设置 billing_interval 读，
# 用户可在「设置-常规」里调；默认 10 分钟。
DEFAULT_BILL_TTL = 10 * 60

# 费用查询失败时那条「进行中」事件的类型标记。和状态巡检（read_error）一样，
# 失败与恢复共用**一条**事件：首次失败建 running 事件，后续失败往里追加进度，
# 恢复时找到它收尾。否则一次连续故障会在时间线上裂成「失败」「已恢复」两条，
# 中间还夹着别的机器，看不出是一段时间里同一次故障。
_BILLING_ERR_KIND = "billing_error"
# 到这些次数才补一行进度。查询本身按 billing_interval 节流（默认 10 分钟一轮），
# 连续失败一整天也有上百次，里程碑足够说明「一直没好」而不刷屏。
_BILLING_ERR_MILESTONES = (2, 5, 10, 30, 60)
# 只在首次失败时提一次：账号没给 BSS 权限是最常见的失败原因，但每轮都复述
# 就显得啰嗦。放**过程条目**而不是概览 —— 概览每轮被最新一次失败覆盖，
# 放那儿第二轮就没了；进度里挂 `once=True` 整条事件只出现一次。
_BILLING_ERR_TIP = "缺 BSS 权限时可在设置里关闭费用查询"


def _bill_ttl(config: dict[str, str]) -> int:
    try:
        return max(60, int(config.get("billing_interval") or DEFAULT_BILL_TTL))
    except (TypeError, ValueError):
        return DEFAULT_BILL_TTL


def _timezone(name: str) -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return dt.timezone(dt.timedelta(hours=8))


def _tz_name() -> str:
    """系统设置里的时区（同步状态巡检事件用的那套），读失败按 +08:00 兜底。"""
    try:
        return str(repo.get_setting("timezone") or "").strip() or "Asia/Shanghai"
    except Exception:  # noqa: BLE001 - 时区读不出来不该让费用记账挂掉
        return "Asia/Shanghai"


def _hms(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, _timezone(_tz_name())).strftime("%H:%M:%S")


def _retry_text(ttl: int) -> str:
    """把刷新间隔写成「每 10 分钟重试」，不足整分钟就按秒写。"""
    if ttl >= 60 and ttl % 60 == 0:
        return f"每 {ttl // 60} 分钟重试"
    return f"每 {ttl} 秒重试"


# 错误文本自带的抬头（`实例账单查询失败：` / `账户余额查询失败：`）。
# 概览模板里已经有「查询失败：」，留着会变成「查询失败：实例账单查询失败：…」。
_ERR_HEAD = re.compile(r"^[^；]{0,14}?(?:查询)?(?:失败|异常)[：:]")


def _brief_error(text: str) -> str:
    """剥掉每段错误自带的抬头，只留原因，与巡检的 `_brief_exc` 对齐。

    两段错误用「；」拼接（账单失败 + 余额失败），逐段处理；没有抬头就原样保留。
    """
    parts: list[str] = []
    for chunk in str(text).split("；"):
        chunk = chunk.strip()
        if not chunk:
            continue
        head = _ERR_HEAD.match(chunk)
        if head:
            chunk = chunk[head.end():].strip() or chunk
        parts.append(chunk)
    return "；".join(parts) or str(text).strip()


def current_cycle(config: dict[str, str] | None = None) -> str:
    """当前账期（YYYY-MM），按用户配置的时区算，避免 23:30 就跨月。"""
    tz_name = (config or {}).get("timezone") or "Asia/Shanghai"
    return dt.datetime.now(_timezone(tz_name)).strftime("%Y-%m")


def enabled(config: dict[str, str] | None = None) -> bool:
    return (config or repo.get_settings()).get("billing_enabled", "1") == "1"


def refresh_server(server_id: int, *, force: bool = False) -> dict[str, Any] | None:
    """抓一台机器的账单与余额并落库。

    返回快照 dict；关闭费用查询或机器不存在时返回 None。
    两个接口**各自独立容错**：余额没权限不该连带账单数字一起丢掉。
    """
    config = repo.get_settings()
    if not enabled(config):
        return None

    server = repo.get_server(server_id, decrypt_secret=True)
    if server is None:
        return None

    cycle = current_cycle(config)
    cost: float | None = None
    balance: float | None = None
    currency = ""
    detected_site = ""
    errors: list[str] = []

    try:
        bill = aliyun.get_instance_bill(server, cycle)
        cost = float(bill.get("total") or 0)
        detected_site = str(bill.get("site") or "")
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
        log.warning("实例账单查询失败 server=%s: %s", server_id, exc)

    try:
        wallet = aliyun.get_account_balance(server, force=force)
        currency = str(wallet.get("currency") or "")
        detected_site = detected_site or str(wallet.get("site") or "")
        # 余额统一取 AvailableAmount（可用额度）：现金余额、信控额度、未结清都算在内，
        # 既对应「现金余额」也对应「可用额度」，不必按站点区分口径。
        # 后付费账号恒为 0（显示 $0.00），代理商注册的预付费账号有真实余额。
        balance = float(wallet.get("amount") or 0)
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
        log.warning("账户余额查询失败 server=%s: %s", server_id, exc)

    # BSS 会告诉我们账号究竟属于哪个站点；和配置不一致就直接纠正，别让用户对着
    # AuthSiteFail 猜。站点只影响费用接口与地域候选，保活/流量统计两站完全一样。
    configured_site = repo.normalize_site(server.get("site"))
    if detected_site and detected_site != configured_site:
        repo.update_server_site(server_id, detected_site)
        repo.add_event(
            "billing",
            messages.ev("站点已自动纠正", messages.obj_server(str(server.get('label') or ''))),
            f"已纠正 / {configured_site} → {detected_site}"
            "（阿里云返回的站点与配置不一致，费用接口按实际站点走）",
            server_id=server_id,
        )
        log.info("站点自动纠正 server=%s: %s -> %s", server_id, configured_site, detected_site)

    error_text = "；".join(errors)
    repo.update_server_billing(
        server_id,
        monthly_cost=cost,
        balance=balance,
        currency=currency,
        cycle=cycle if cost is not None else "",
        error=error_text,
    )
    _mark_transition(server, error_text, _bill_ttl(config))
    return {
        "monthly_cost": cost,
        "balance": balance,
        "currency": currency,
        "cycle": cycle,
        "error": error_text,
    }


def close_orphan_billing_errors(monitored: set[int]) -> None:
    """收掉「对象已不在监控里」的费用失败事件，避免它永远转圈。

    机器被删、或整个费用查询被关掉之后，不会再有成功回调来收尾；
    每轮调度末尾统一处理一次，传空集合表示「一台都不再监控」。
    """
    for event in repo.running_pending_events(_BILLING_ERR_KIND):
        sid = int(event.get("server_id") or 0)
        if sid and sid in monitored:
            continue
        eid = int(event["id"])
        # 计数跟事件同生共死：只收事件不清标记的话，机器哪天重新纳入监控，
        # 下一轮失败会从旧值接着往上数，恢复时写出「连续失败 14 次，持续约 1 分钟」。
        if sid:
            repo.clear_flag(f"billing_err:{sid}")
        # 同 _billing_recovered：收成「完成」就别再留红色错误图标。
        repo.update_event(eid, level="info")
        repo.finish_event(
            eid,
            "done",
            detail="对象已不在监控中，费用告警自动收尾",
        )


def refresh_due(now_ts: float | None = None) -> int:
    """给调度线程调用：只刷新过期的那几台，返回实际刷新台数。

    不会抛异常 —— 后台循环里因为账单查询挂掉而停掉整个调度是最糟的失败模式。
    """
    config = repo.get_settings()
    servers = repo.list_servers()
    if not enabled(config):
        # 关掉费用查询后再没有成功回调，进行中的失败事件得在这里收掉。
        close_orphan_billing_errors(set())
        return 0

    now = now_ts or time.time()
    refreshed = 0
    bill_ttl = _bill_ttl(config)
    current_cycle_name = current_cycle(config)
    for server in servers:
        if not server.get("secret_configured"):
            continue
        last = float(server.get("billing_updated_at") or 0)
        cycle = str(server.get("billing_cycle") or "")
        # 跨月立即刷新：上个月的账单数字留着会误导。
        # cycle 为空表示「最近一次没查成」（refresh_server 失败时不写账期），
        # 这种情况同样要按 TTL 节流 —— 否则缺 BSS 权限的账号每 60 秒就被重打
        # 一次账单 + 余额，TTL 形同不存在。
        if last and now - last < bill_ttl and cycle in ("", current_cycle_name):
            continue
        try:
            if refresh_server(int(server["id"])) is not None:
                refreshed += 1
        except Exception:  # noqa: BLE001
            log.exception("刷新费用快照失败 server=%s", server.get("id"))
    close_orphan_billing_errors({int(s["id"]) for s in servers})
    return refreshed


def _billing_error(server: dict[str, Any], error_text: str, ttl: int) -> None:
    """记一次费用查询失败：同一条进行中事件里写明时刻、次数与重试节奏。

    和状态巡检告警同一套打法：第一次只建 running 事件，后续把最新一次的错误
    覆盖到概览、只在里程碑（第 2/5/10… 次）补一行进度；恢复由
    `_billing_recovered` 收尾。这样「一直查不到」是一个事件，而不是每次失败
    都新开一条把时间线刷满。
    """
    sid = int(server["id"])
    now = int(time.time())
    stamp = _hms(now)
    retry = _retry_text(ttl)
    label = str(server.get("label") or "")
    key = f"billing_err:{sid}"
    # 与巡检同构：`查询失败：网络不可达（…）`，原因里不再重复「实例账单查询失败：」
    brief = _brief_error(error_text)
    event = repo.pending_event(sid, _BILLING_ERR_KIND)
    if event is None:
        # 新的一段故障：计数与起始时刻一起从零开始。flag 若有残留（事件被清理、
        # 库被手工改过），接着旧值往上数会让「第 1 次失败」和恢复时的
        # 「连续失败 N 次」互相矛盾。
        repo.clear_flag(key)
    # bump_flag 只在首次写入 created_at，后续只加 value —— 起始时刻因此保持不动，
    # 恢复时才算得出「持续约 M 分钟」。
    count = repo.bump_flag(key)
    if event is None:
        eid = repo.add_event(
            # kind 用 warning 与状态巡检一致：前端图标按 kind 取形状（三角）、
            # 颜色看 level。查询失败属「能继续跑，只是看不到数字」，恒为 warning。
            "warning",
            messages.ev("费用查询异常", messages.obj_server(label)),
            f"{stamp} 查询失败：{brief}；{retry}，恢复后自动更新",
            level="warning",
            server_id=sid,
            status="running",
            pending_kind=_BILLING_ERR_KIND,
        )
        # 过程条目不带时刻：前端左侧已经显示每条的 ts，正文再写一遍就是两个时间。
        repo.add_event_progress(eid, f"第 1 次失败：{brief}")
        # 提示只挂一次，整条事件里不再复述（概览会被下一轮覆盖，所以放过程里）。
        repo.add_event_progress(eid, f"提示：{_BILLING_ERR_TIP}", once=True)
        return

    eid = int(event["id"])
    # 概览只留最新一次的错误。
    repo.update_event(
        eid,
        detail=f"{stamp} 仍失败（连续 {count} 次）：{brief}；{retry}",
    )
    if count in _BILLING_ERR_MILESTONES:
        repo.add_event_progress(eid, f"第 {count} 次失败：{brief}")


def _billing_recovered(server: dict[str, Any]) -> None:
    """费用查询恢复正常：给那条进行中的事件补恢复时刻并收尾。

    没有失败标记（进程重启后第一次就查成功）时不补事件，避免凭空多一条
    「已恢复」——它前面没有对应的失败，读起来会莫名其妙。
    """
    sid = int(server["id"])
    key = f"billing_err:{sid}"
    age = repo.flag_age(key)
    if age < 0:
        return
    count = repo.flag_value(key)
    repo.clear_flag(key)
    event = repo.pending_event(sid, _BILLING_ERR_KIND)
    if event is None:
        return
    eid = int(event["id"])
    stamp = _hms(time.time())
    minutes = max(1, int(round(age / 60)))
    repo.add_event_progress(eid, "已恢复")
    repo.update_event(
        eid,
        title=messages.ev(
            "费用查询已恢复", messages.obj_server(str(server.get("label") or ""))
        ),
        detail=f"{stamp} 已恢复（此前连续失败 {count} 次，持续约 {minutes} 分钟）",
        # 收尾要把级别降回来，否则会留下「红色错误图标 + 绿色完成徽章」。
        level="info",
    )
    repo.finish_event(eid, "done")


def _mark_transition(server: dict[str, Any], error_text: str, ttl: int) -> None:
    """每轮刷新后同步那条「进行中」事件：失败就续写，成功就收尾。

    早期版本拿「上一轮有没有错误」做短路，只在状态翻转时动事件。改成单事件
    聚合后这个短路是错的 —— 连续失败从第二轮起 `was_error == now_error`，
    概览停在第一轮的文案、计数永远涨不上去，恢复时还会写出「连续失败 1 次」
    这种明显对不上的数字。现在判断全部下沉到两个函数内部：
    失败由 `_billing_error` 看有没有进行中事件决定「新建 / 续写」，
    成功由 `_billing_recovered` 看有没有失败标记决定「收尾 / 无事可做」。
    每轮至多一条进度，不会刷屏。
    """
    if error_text:
        _billing_error(server, error_text, ttl)
    else:
        _billing_recovered(server)


def summary_of(server: dict[str, Any], config: dict[str, str] | None = None) -> dict[str, Any]:
    """把库里的快照转成前端友好的结构。

    `stale=True` 表示这份数字是上个月的，界面应该显示「待更新」而不是当本月费用。
    """
    cost = server.get("monthly_cost")
    balance = server.get("balance_amount")
    cycle = str(server.get("billing_cycle") or "")
    now_cycle = current_cycle(config)
    return {
        "monthly_cost": float(cost) if cost is not None else None,
        "balance": float(balance) if balance is not None else None,
        "currency": str(server.get("billing_currency") or "") or "CNY",
        "cycle": cycle,
        "stale": bool(cycle) and cycle != now_cycle,
        "updated_at": int(server.get("billing_updated_at") or 0),
        "error": str(server.get("billing_error") or ""),
    }


def sdk_ok() -> bool:
    return aliyun.sdk_ready() or settings.DRY_RUN
