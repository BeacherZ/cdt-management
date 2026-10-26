"""后台调度：定期对账 + 每日报告 + 事件清理 + 事件状态确认。"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from . import aliyun, billing, dns, messages, notify, repo, settings
from .shift import engine, _timezone

log = logging.getLogger("cdt-management.scheduler")

_stop = threading.Event()
_thread: threading.Thread | None = None
_last_report_day = ""

# 事件状态确认器：独立线程，比「对账」频率高得多（5 秒），
# 专门盯着「进行中、需要等实例状态到位」的事件（如手动开关机）。
_confirm_thread: threading.Thread | None = None

# 事件轮询间隔（秒）
EVENT_POLL_INTERVAL = 5
# 事件等待状态到位的超时（秒），超过则标记失败
EVENT_CONFIRM_TIMEOUT = 120


def start() -> None:
    global _thread, _confirm_thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="cdt-scheduler", daemon=True)
    _thread.start()
    if _confirm_thread is None or not _confirm_thread.is_alive():
        _confirm_thread = threading.Thread(
            target=_confirm_loop, name="cdt-event-confirm", daemon=True
        )
        _confirm_thread.start()
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


# --------------------------------------------------------------------------
# 事件状态确认
# --------------------------------------------------------------------------

def _confirm_loop() -> None:
    """每 5 秒扫一遍「进行中、待确认目标状态」的事件，查实例状态、打完成标志。"""
    while not _stop.is_set():
        try:
            _confirm_pending_events()
        except Exception:  # noqa: BLE001
            log.exception("事件状态确认轮次异常")
        _stop.wait(EVENT_POLL_INTERVAL)


def _confirm_pending_events() -> None:
    now = int(time.time())
    config = repo.get_settings()
    for event in repo.list_pending_events():
        event_id = int(event["id"])
        server_id = int(event.get("server_id") or 0)
        pending = str(event.get("pending_status") or "").strip()
        pending_kind = str(event.get("pending_kind") or "").strip()
        if not server_id or not pending:
            # 数据不完整，直接结束掉，别让它永远转圈
            repo.finish_event(event_id, "error", "缺少待确认目标，已终止")
            continue

        # 超时兜底：别让一个卡住的事件永远占着转圈
        elapsed = now - int(event.get("ts") or now)
        if elapsed > EVENT_CONFIRM_TIMEOUT:
            repo.add_event_progress(event_id, "等待状态超时，未确认到位")
            repo.finish_event(event_id, "error", "超时未确认")
            continue

        try:
            server = repo.get_server(server_id, decrypt_secret=True)
            if server is None:
                repo.finish_event(event_id, "error", "服务器已被删除")
                continue
            info = aliyun.get_instance(server)
        except Exception as exc:  # noqa: BLE001
            # 查询失败不算到位，下一轮再试；但不刷屏
            repo.add_event_progress(event_id, f"状态查询失败：{exc}")
            continue

        # 用真实状态刷新服务器运行时快照，让卡片同步
        try:
            repo.update_server_runtime(
                server_id,
                status=info.status,
                stopped_mode=info.stopped_mode,
                public_ip=info.public_ip,
                region_id=info.region_id,
            )
        except Exception:  # noqa: BLE001
            pass

        if _status_reached(info, pending):
            mode_text = messages.mode_label(info.stopped_mode) if info.stopped_mode else ""
            suffix = f"（{mode_text}）" if mode_text else ""

            # 到位后的收尾动作按事件类型区分
            if pending_kind == "keepalive":
                # 保活拉起（抢占恢复）：同步 DDNS 后补发「抢占恢复」通知
                try:
                    ddns_result = engine._sync_ddns(server, config, dns.TARGET_SERVER, server_id)
                    ddns_status = str(ddns_result)
                except Exception as exc:  # noqa: BLE001
                    ddns_status = f"❌ DDNS 异常：{exc}"
                repo.add_event_progress(event_id, f"DDNS：{ddns_status}")
                if config.get("notify_on_keepalive") == "1":
                    try:
                        notify.send_all(
                            config,
                            messages.recovery(
                                server,
                                ddns_status,
                                headline="实例疑被抢占，已自动恢复运行。",
                                kind="keepalive",
                            ),
                        )
                        repo.add_event_progress(event_id, "已推送抢占恢复通知")
                    except Exception as exc:  # noqa: BLE001
                        repo.add_event_progress(event_id, f"通知发送失败：{exc}")
            elif pending == "Running" and info.public_ip:
                # 手动开机到位后，若这台闲散机器绑了域名，立刻把解析指过来
                try:
                    record = repo.dns_record_of(dns.TARGET_SERVER, server_id)
                    if record:
                        result = dns.sync_record(record, config, ip=info.public_ip)
                        repo.add_event_progress(event_id, f"解析 {record} {result.status}")
                except Exception as exc:  # noqa: BLE001
                    repo.add_event_progress(event_id, f"解析同步失败：{exc}")

            repo.add_event_progress(event_id, f"已确认 {info.status}{suffix}")
            repo.finish_event(event_id, "done")
        else:
            # 还没到位：记录一次当前状态，方便用户在进度里看到中间态
            mode_text = messages.mode_label(info.stopped_mode) if info.stopped_mode else ""
            suffix = f"（{mode_text}）" if mode_text else ""
            repo.add_event_progress(
                event_id,
                f"当前 {info.status}{suffix}，等待到位",
            )


def _status_reached(info, pending: str) -> bool:
    """判断实例当前状态是否已达到目标。

    - 目标 Running：status == Running 即可
    - 目标 Stopped：status == Stopped 即可（节省停机与否由真实 StoppedMode 决定，
      这里不强求 StopCharging，避免不支持节省停机的规格永远确认不了）
    """
    if pending == "Running":
        return info.status == "Running"
    if pending == "Stopped":
        return info.status == "Stopped"
    # 未知目标：按 status 相等判断
    return info.status == pending


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
