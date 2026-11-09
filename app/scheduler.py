"""后台调度：定期对账 + 每日报告 + 事件清理 + 事件状态确认。"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from . import aliyun, billing, dns, messages, notify, repo, settings
from .shift import engine, keepalive_window, _timezone

log = logging.getLogger("cdt-management.scheduler")

_stop = threading.Event()
_thread: threading.Thread | None = None
_last_report_day = ""

# 事件状态确认器：独立线程，比「对账」频率高得多（5 秒），
# 专门盯着「进行中、需要等实例状态到位」的事件（如手动开关机）。
_confirm_thread: threading.Thread | None = None

# 事件轮询间隔（秒）
EVENT_POLL_INTERVAL = 5
# 事件等待状态到位的超时（秒），超过则标记失败。
# 给到 10 分钟：阿里云 Windows 实例开机 2–3 分钟很常见，早期按 120 秒算，
# 事件被标成「超时未确认」而实例其实正常起来了，看着像失败。
EVENT_CONFIRM_TIMEOUT = 600

# 加密对账窗口：新建换班组后要尽快把组员摆到排班表该有的位置（开机 + 停待机
# 要跨好几轮），这段时间把对账间隔临时压到 10 秒，最多持续 90 秒。
# 场景结束就自动恢复成用户配置的间隔，不是长期提速。
_FAST_INTERVAL = 10
_FAST_WINDOW = 90
_fast_until = 0.0
# 「立刻跑一轮」的唤醒信号：让 _wait 不必睡满 interval
_wake = threading.Event()


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
    if settings.GRACE_ORDER_BROKEN:
        # 只在有人把 CDT_SHIFT_TIMEOUT_MINUTES 调到 3 分钟及以下时出现：
        # 起不来判定阈值已经追平/超过超时，先转移后报警的顺序维护不了。
        log.warning(
            "换班超时 %s 分钟 ≤ 起不来判定 %s 秒：故障转移不会早于「换班超时」触发",
            settings.SHIFT_TIMEOUT_MINUTES,
            settings.DUTY_STALL_SECONDS,
        )


def stop() -> None:
    _stop.set()
    _wake.set()


def request_fast_reconcile(seconds: float = _FAST_WINDOW) -> None:
    """申请一段加密对账窗口，并立刻唤醒调度线程跑一轮。

    新建换班组保存后调这个：用户期望「点完保存就看到组员被摆到位」，
    等满一个对账间隔（默认 60 秒）才推进下一步太久了。
    """
    global _fast_until
    _fast_until = max(_fast_until, time.time() + seconds)
    _wake.set()


def _next_tick(now: float, interval: int, offset: int) -> float:
    """下一轮该在什么时候开始：对齐到 interval 网格上的第 offset 秒。

    原来调度是「跑完一轮再睡 interval 秒」，执行耗时全被算进周期里 —— 一轮跑
    3 秒，实际周期就是 63 秒，跑一天触发时刻越飘越远，也没法说清「这一轮到底
    在每分钟第几秒打接口」。改成按绝对时间对齐后：interval=60、offset=18 就是
    每分钟第 18 秒，前面那轮跑了多久都不影响。

    返回的刻度一定大于 now：正好踩在刻度上时顺延一格，避免同一秒里跑两轮。
    """
    span = max(1, int(interval))
    # 间隔比 60 秒还小（新建换班组的 10 秒加密窗口）按取模处理：
    # 18 % 10 = 8 → 落在 :08/:18/:28，仍然包含用户指定的那一秒。
    mark = int(offset) % span
    slot = int(now // span) * span + mark
    return float(slot + span if slot <= now else slot)


def _offset() -> int:
    """巡检对齐到网格的哪一秒（0–59）。读不到或越界都当 0，保持老行为。"""
    try:
        return max(0, min(59, int(repo.get_setting("reconcile_offset", "0"))))
    except Exception:  # noqa: BLE001
        return 0


def _wait(deadline: float) -> None:
    """等到指定时刻；期间有「立刻对账」请求就提前返回。

    用 1 秒切片等，既能被 _wake 及时叫醒，也不至于空转。
    """
    while not _stop.is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        if _wake.wait(min(remaining, 1.0)):
            _wake.clear()
            return


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
        if time.time() < _fast_until:
            interval = min(interval, _FAST_INTERVAL)
        # 对齐到固定的秒位，而不是「跑完再睡 interval 秒」：后者会把执行耗时
        # 计入周期，触发时刻一路漂移，也做不到「每分钟第 18 秒」这种错峰。
        _wait(_next_tick(time.time(), interval, _offset()))


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
    keepalive_ids: set[int] | None = None
    keepalive_schedules: dict[int, dict] | None = None
    local_now = dt.datetime.fromtimestamp(now, _timezone(config.get("timezone") or "Asia/Shanghai"))
    for event in repo.list_pending_events():
        event_id = int(event["id"])
        server_id = int(event.get("server_id") or 0)
        pending = str(event.get("pending_status") or "").strip()
        pending_kind = str(event.get("pending_kind") or "").strip()
        if not server_id or not pending:
            # 数据不完整，直接结束掉，别让它永远转圈
            repo.finish_event(event_id, "error", "缺少待确认目标，已终止")
            continue

        # 单机保活是「一直试到起来为止」，不设超时：一台机器躺一整天也只有这一条
        # 记录，概要里写着已经试了多少次，点开能看到里程碑。只有它被移出保活或
        # 停用保活时才收尾，否则会留一条永远不会结束的转圈。
        if pending_kind == "keepalive":
            # 保活名单整轮只查一次：这里 5 秒一轮，每个保活事件都单独 SELECT 一遍
            # 是白花的开销（保活机器多的时候尤其明显）。
            if keepalive_ids is None:
                keepalive_ids = repo.keepalive_server_ids()
            if server_id not in keepalive_ids:
                repo.finish_event(event_id, "error", "已移出单机保活，停止拉起")
                repo.clear_flag(f"keepalive_tries:{server_id}")
                continue
            # 运行窗口外：立刻收尾，别再等状态 —— 一是窗口外本来就不该拉起，
            # 二是万一它这会儿正好到 Running，确认器会推一条「抢占恢复」，而这次
            # 停机/不开机是计划的，推「疑被抢占」是误导。收尾时顺手置上休止标记，
            # 巡检那一轮就不会再记一条同名的「单机保活窗口暂停」。
            if keepalive_schedules is None:
                keepalive_schedules = repo.keepalive_schedules()
            schedule = keepalive_schedules.get(server_id)
            if schedule and schedule.get("enabled"):
                in_window, window_text = keepalive_window(schedule, local_now)
                if not in_window:
                    label = str((repo.get_server(server_id) or {}).get("label") or f"#{server_id}")
                    window_note = f"（{window_text}）" if window_text else ""
                    repo.add_event_progress(
                        event_id, f"已离开运行窗口{window_note}，窗口外不再自动拉起"
                    )
                    repo.update_event(
                        event_id, title=messages.ev("单机保活窗口暂停", messages.obj_server(label)), level="info", kind="keepalive_pause"
                    )
                    # 不能就地写成「已暂停」：这时停机指令还没下发，写下去就是假结论。
                    # 改成等 Stopped，巡检下发停机、确认器看到停稳后才收尾，
                    # 事件里才有「窗口外 → 下发停机 → 已确认 Stopped」的完整过程。
                    repo.retarget_event(event_id, "Stopped", "keepalive_stop")
                    repo.update_event(event_id, detail="等待停机到位")
                    repo.set_flag(f"keepalive_rest:{server_id}")
                    repo.clear_flag(f"keepalive_tries:{server_id}")
                    continue

        # 超时兜底：别让一个卡住的事件永远占着转圈。保活除外 —— 它本来就该
        # 一直等到实例起来（见上面的注释），超时会把一条长长的中断切成一堆。
        # 计时从「开始等当前目标」算（pending_ts），不是事件创建时间：保活事件被
        # retarget 成等停机后，ts 还是几小时前的，按 ts 算会当场判超时。
        base = int(event.get("pending_ts") or event.get("ts") or now)
        elapsed = now - base
        if elapsed > EVENT_CONFIRM_TIMEOUT and pending_kind != "keepalive":
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
            # 查询失败不算到位，下一轮再试。文案必须脱敏：这里 5 秒一轮，
            # 原始异常带着整条签名 URL 和对象地址，既会把 AK/Signature 摊在事件里，
            # 又因为每次文本都不同而躲过 add_event_progress 的相邻去重 —— 一条跨天的
            # 保活事件能被刷到条数上限，把开头的步骤挤掉。
            repo.add_event_progress(
                event_id, f"状态查询失败：{aliyun.brief_error(exc)}"
            )
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
            suffix = messages.status_suffix(info.status, info.stopped_mode)

            # server 是轮询开始前取的快照：实例刚停机时它的 public_ip 还是空的。
            # 通知里的「IP / 地域 / 状态」行要用刚查到的实时值，否则报告会带着空 IP 发出去。
            server = {
                **server,
                "public_ip": info.public_ip or server.get("public_ip") or "",
                "instance_status": info.status,
                "stopped_mode": info.stopped_mode,
                "region_id": info.region_id or server.get("region_id") or "",
            }

            # 概览收起时只显示 detail 这一行，所以摘要只放「结果」：
            # 「原状态 Stopped」「已下发开机指令」都属于过程，展开后的步骤列表里还在，
            # 这里再写一遍就是啰嗦。steps 里的每一段都是可以直接读的结果短语，
            # 用「 / 」连起来，与换班结算（当班 X / 待机 N 台 / ✅ 已同步）同一套写法。
            steps: list[str] = []

            def record(text: str, summary: str | None = None) -> None:
                """text 写进展开的步骤列表，summary 进概览摘要（不给就复用 text）。"""
                repo.add_event_progress(event_id, text)
                steps.append(text if summary is None else summary)

            # 先落「已确认」：事件进度里状态确认一定在最前面，之后的 DDNS、
            # 解析、通知都是确认到位后的收尾动作，通知永远排在最后一步。
            record(
                f"已确认 {info.status}{suffix}",
                messages.result_label(info.status, info.stopped_mode),
            )

            # 到位后的收尾动作按事件类型区分
            if pending_kind == "keepalive":
                # 收尾时补一句总次数：概览原本会被「运行中」整条覆盖，用户想看
                # 「这次被抢之后一共试了多少次才起来」就只剩进度里那几行里程碑。
                tries = repo.flag_value(f"keepalive_tries:{server_id}")
                if tries > 1:
                    record(f"共尝试拉起 {tries} 次", f"共尝试 {tries} 次")
                # 保活拉起（抢占恢复）：同步这台机器绑的所有域名后补发通知。
                # IP 必须用刚查到的实时值：server 是轮询前取的快照，实例停机期间
                # 库里存的是空 IP，拿快照去同步只会得到「⚠️ IP 无效跳过」。
                # verify=True：这是用户明确要求的「强制查一次」时间点，读完真实值
                # 再决定改不改；通知里仍用 ddns_status，保持一句「✅ 已同步」。
                ddns_report = None
                try:
                    ddns_report = engine._sync_ddns(
                        server,
                        config,
                        dns.TARGET_SERVER,
                        server_id,
                        ip=info.public_ip,
                        verify=True,
                    )
                    ddns_status = ddns_report.status
                except Exception as exc:  # noqa: BLE001
                    ddns_status = f"❌ DDNS 异常：{exc}"
                # 只在「开了 DDNS 且这台确实挂了域名」时记这一步：换班那边是同一套
                # 判断，没绑域名时两边都不该多一行「DDNS：⏸ 未绑定」的噪音。
                # 通知里照旧带 ddns_status，让用户知道域名没跟着走。
                bound = bool(repo.dns_records_of(dns.TARGET_SERVER, server_id))
                if config.get("ddns_enabled") == "1" and bound:
                    # 进度写带前后 IP 的详情，概览只要「已更新 / IP 未变化」；
                    # 异常只留短结论，堆栈太占地方。
                    if ddns_report is None:
                        record(f"DDNS：{ddns_status}", "❌ DDNS 异常")
                    else:
                        record(f"DDNS：{ddns_report.detail}", ddns_report.brief)
                # 收尾标题跟着来源走，别让两种拉起在列表里长得一样：
                # 窗口到点开的（kind=keepalive_quiet）写「单机保活窗口恢复」，
                # 其余（掉线被抢占、手动关机后重新开保活、刚加进保活）写
                # 「单机保活抢占恢复」。进行时叫「…恢复中」，这里只换时态。
                quiet = str(event.get("kind") or "") == "keepalive_quiet"
                done_title = "单机保活窗口恢复" if quiet else "单机保活抢占恢复"
                repo.update_event(
                    event_id,
                    title=messages.ev(done_title, messages.obj_server(str(server.get('label') or ''))),
                )
                # 这次中断结束了，重试计数归零，下一次从 1 重新数
                repo.clear_flag(f"keepalive_tries:{server_id}")
                # 只有窗口到点那条不发通知：那是计划内的自动开关，推「疑被抢占」
                # 会让人以为机器被阿里云回收了。其余拉起一律走抢占恢复渠道。
                if config.get("notify_on_keepalive") == "1" and not quiet:
                    try:
                        results = notify.send_all(
                            config,
                            messages.recovery(
                                server,
                                ddns_status,
                                headline="实例疑被抢占，已自动恢复运行。",
                                kind="keepalive",
                            ),
                        )
                        # 通知是流程的最后一步，成败都如实写进事件（send_all 吞异常只回结果）
                        notify.note_notify(event_id, results)
                    except Exception as exc:  # noqa: BLE001
                        record(f"通知发送失败：{exc}", "❌ 通知发送失败")
            elif pending == "Running" and info.public_ip and config.get("ddns_enabled") == "1":
                # 手动开机到位后，把这台机器绑的域名全指过来（一台可以挂好几条）。
                # DDNS 关着的时候一步都别走：sync_record 会返回「⏸ DDNS 未启用」，
                # 那句会被当成这条解析的结果写进进度，还会把 dns_records.last_status
                # 覆盖掉 —— 原本「✅ 已同步」的记录变成「待同步」黄点，界面开始报假异常。
                try:
                    # 手动开机到位也是「强制查一次」的时间点：不同来源（控制台/API/
                    # 别处）改过解析时，本地缓存是过期的，这里以 CF 的真实值为准。
                    names, results = dns.sync_target_full(
                        dns.TARGET_SERVER,
                        server_id,
                        config,
                        ip=info.public_ip,
                        verify=True,
                    )
                    if names:
                        ddns_report = dns.report(names, results)
                        # 详情写前后 IP，概览写「已更新 / IP 未变化」，与保活、换班同一套
                        record(f"DDNS：{ddns_report.detail}", ddns_report.brief)
                except Exception as exc:  # noqa: BLE001
                    record(f"解析同步失败：{exc}", "❌ 解析同步失败")

            # 收起时这一行就是事件的全部可见信息：只讲结果，段与段之间统一用「 / 」
            summary = " / ".join(steps)
            if pending_kind == "keepalive_stop":
                # 这条事件的语义是「保活暂停」，结论就该写「已暂停」，
                # 与手动停机的「已停止」区分开
                summary = f"已暂停（{messages.mode_label(info.stopped_mode) or '停机'}）"
            elif pending_kind == "limit_stop":
                # 超限停机事件原本只有原因（已用多少 / 阈值多少），
                # 收尾时补上真实结果，而不是只说「下发过指令」。
                # 进行中的概览是「等待停机到位 / 原因」，这里要把等待那一段摘掉，
                # 否则会写成「已停止（节省停机） / 等待停机到位 / 已用 …」。
                reason = str(event.get("detail") or "").strip()
                if reason.startswith("等待停机到位"):
                    reason = reason.split(" / ", 1)[1].strip() if " / " in reason else ""
                result = messages.result_label(info.status, info.stopped_mode)
                summary = f"{result} / {reason}" if reason else result
            repo.finish_event(event_id, "done", summary)
        else:
            # 单机保活等的是「一直试到起来」，中间态不往进度里刷状态：
            # 概要行由巡检每分钟更新成「等待开机到位（已尝试 N 次）」，这里再插一句就重复了。
            if pending_kind == "keepalive":
                continue
            # 还没到位：记录一次当前状态，方便用户在进度里看到中间态
            suffix = messages.status_suffix(info.status, info.stopped_mode)
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
