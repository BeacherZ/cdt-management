"""后台调度：定期对账 + 每日报告 + 事件清理。"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from . import billing, dns, repo, settings
from .shift import engine, _timezone

log = logging.getLogger("cdt-management.scheduler")

_stop = threading.Event()
_thread: threading.Thread | None = None
_last_report_day = ""


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="cdt-scheduler", daemon=True)
    _thread.start()
    log.info("调度线程已启动，对账间隔 %s 秒", settings.RECONCILE_INTERVAL)


def stop() -> None:
    _stop.set()


def _loop() -> None:
    last_prune = 0.0
    while not _stop.is_set():
        try:
            engine.reconcile_all()
        except Exception:  # noqa: BLE001
            log.exception("对账轮次异常")

        try:
            _maybe_daily_report()
        except Exception:  # noqa: BLE001
            log.exception("日报检查异常")

        # DNS：只在「目标在跑但解析对不上」时才真发请求，稳定状态零开销
        try:
            dns.reconcile(repo.get_settings())
        except Exception:  # noqa: BLE001
            log.exception("DNS 巡检异常")

        # 费用是慢数据，refresh_due 内部按 30 分钟 TTL 自行筛，命中时才真打接口
        try:
            billing.refresh_due()
        except Exception:  # noqa: BLE001
            log.exception("费用快照刷新异常")

        now = time.time()
        if now - last_prune > 3600:
            last_prune = now
            try:
                repo.prune_events(settings.EVENT_RETAIN_DAYS)
            except Exception:  # noqa: BLE001
                log.exception("清理历史事件失败")

        interval = _interval()
        _stop.wait(interval)


def _interval() -> int:
    try:
        return max(30, int(repo.get_setting("reconcile_interval", str(settings.RECONCILE_INTERVAL))))
    except Exception:
        return settings.RECONCILE_INTERVAL


def _maybe_daily_report() -> None:
    global _last_report_day
    config = repo.get_settings()
    if config.get("daily_report_enabled") != "1":
        return
    if repo.get_setting("initialized") != "1":
        return

    tz = _timezone(config.get("timezone") or "Asia/Shanghai")
    now = dt.datetime.now(tz)
    target = config.get("daily_report_time") or "23:58"
    try:
        hour, minute = (int(part) for part in target.split(":"))
    except Exception:
        hour, minute = 23, 58

    today = now.date().isoformat()
    if _last_report_day == today:
        return
    # 用「到点了没有」判断，而不是卡死在某一分钟：容器在 23:59 重启也能补发
    target_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target_at:
        return
    if engine.daily_report():
        _last_report_day = today
