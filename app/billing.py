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
import time
from typing import Any

from . import aliyun, repo, settings

log = logging.getLogger("cdt-management.billing")

# 本月账单刷新间隔的默认值（秒）。实际间隔从设置 billing_interval 读，
# 用户可在「设置-常规」里调；默认 10 分钟。
DEFAULT_BILL_TTL = 10 * 60


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
        balance = float(wallet.get("amount") or 0)
        currency = str(wallet.get("currency") or "")
        detected_site = detected_site or str(wallet.get("site") or "")
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
            f"{server.get('label')}：站点已自动纠正",
            f"{configured_site} → {detected_site}（阿里云返回的站点与配置不一致，费用接口按实际站点走）",
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
    _mark_transition(server, error_text)
    return {
        "monthly_cost": cost,
        "balance": balance,
        "currency": currency,
        "cycle": cycle,
        "error": error_text,
    }


def refresh_due(now_ts: float | None = None) -> int:
    """给调度线程调用：只刷新过期的那几台，返回实际刷新台数。

    不会抛异常 —— 后台循环里因为账单查询挂掉而停掉整个调度是最糟的失败模式。
    """
    config = repo.get_settings()
    if not enabled(config):
        return 0

    now = now_ts or time.time()
    refreshed = 0
    bill_ttl = _bill_ttl(config)
    for server in repo.list_servers():
        if not server.get("secret_configured"):
            continue
        last = float(server.get("billing_updated_at") or 0)
        cycle = str(server.get("billing_cycle") or "")
        # 跨月立即刷新：上个月的账单数字留着会误导
        if last and now - last < bill_ttl and cycle == current_cycle(config):
            continue
        try:
            if refresh_server(int(server["id"])) is not None:
                refreshed += 1
        except Exception:  # noqa: BLE001
            log.exception("刷新费用快照失败 server=%s", server.get("id"))
    return refreshed


def _mark_transition(server: dict[str, Any], error_text: str) -> None:
    """只在「从成功变失败」或「从失败变成功」时记事件，避免每轮刷同一条。

    权限没配好的时候，卡片上已经有提示，事件流里再刷一屏就没法看了。
    """
    was_error = bool(str(server.get("billing_error") or "").strip())
    now_error = bool(error_text)
    if was_error == now_error:
        return
    if now_error:
        repo.add_event(
            "error",
            f"{server.get('label')}：费用查询失败",
            f"{error_text}（缺 BSS 权限时可在设置里关闭费用查询）",
            level="warning",
            server_id=int(server["id"]),
        )
    else:
        repo.add_event(
            "billing",
            f"{server.get('label')}：费用已恢复",
            "账单与余额查询恢复正常",
            server_id=int(server["id"]),
        )


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
