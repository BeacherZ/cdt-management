"""HTTP API。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import aliyun, billing, ddns, dns, notify, repo, security, settings
from .shift import (
    PHASE_BREAKER,
    PHASE_ERROR,
    PHASE_FAILOVER,
    PHASE_SETTLED,
    PHASE_SWITCHING,
    _timezone,
    active_members,
    current_member_index,
    duty_at,
    engine,
    next_switch_at,
)

log = logging.getLogger("cdt-management.api")

PHASE_LABELS = {
    PHASE_SWITCHING: "换班中",
    PHASE_SETTLED: "已就绪",
    PHASE_FAILOVER: "故障转移",
    PHASE_BREAKER: "熔断停机",
    PHASE_ERROR: "异常",
    "pending": "待执行",
}

# 配置变更事件里给人看的字段名，避免把 ddns_cf_token 这种内部键直接甩到界面上
SETTING_LABELS = {
    "timezone": "时区",
    "traffic_threshold_pct": "流量阈值",
    "reconcile_interval": "同步间隔",
    "daily_report_enabled": "每日汇总",
    "daily_report_time": "汇总时间",
    "notify_on_shift": "换班通知",
    "notify_on_failover": "故障转移通知",
    "notify_on_recovery": "抢占恢复通知",
    "notify_on_breaker": "全局熔断通知（全部成员）",
    "notify_on_limit_stop": "全局熔断通知（单台）",
    "notify_on_error": "异常通知",
    "notify_on_keepalive": "保活通知",
    "ddns_enabled": "DDNS 开关",
    "ddns_provider": "DDNS 服务商",
    "ddns_cf_auth_mode": "DDNS 认证方式",
    "ddns_cf_email": "DDNS 账号",
    "ddns_cf_token": "DDNS Token",
    "ddns_cf_key": "DDNS Global Key",
    "ddns_zone_id": "Zone ID",
    "ddns_record": "默认域名",
    "ddns_records": "DDNS 记录名",
    "ddns_ttl": "TTL",
    "ddns_proxied": "CDN 代理",
    "billing_enabled": "费用查询",
    "billing_interval": "费用查询间隔",
    "notify_tg_enabled": "Telegram 开关",
    "notify_tg_token": "Telegram Token",
    "notify_tg_chat_id": "Telegram Chat",
    "notify_tg_proxy": "Telegram 代理",
    "notify_mail_enabled": "邮件开关",
    "notify_mail_host": "SMTP 主机",
    "notify_mail_port": "SMTP 端口",
    "notify_mail_security": "SMTP 加密",
    "notify_mail_username": "SMTP 账号",
    "notify_mail_password": "SMTP 密码",
    "notify_mail_to": "收件人",
    "notify_wh_enabled": "Webhook 开关",
    "notify_wh_url": "Webhook 地址",
    "notify_wh_method": "Webhook 方法",
    "notify_wh_body": "Webhook 请求体",
    "notify_wh_headers": "Webhook 请求头",
    "admin_username": "用户名",
    "admin_password": "登录密码",
}

# 内部状态键不算「本次改动」，别让它们出现在事件摘要里
SETTING_LABELS_SKIP = {"session_epoch", "initialized"}


def _setting_labels(keys: list[str]) -> str:
    keys = [key for key in keys if key not in SETTING_LABELS_SKIP]
    names = [SETTING_LABELS.get(key, key) for key in sorted(keys)]
    if len(names) > 8:
        return "、".join(names[:8]) + f" 等 {len(names)} 项"
    return "、".join(names)


def _config_from_draft(payload: dict[str, Any]) -> dict[str, str]:
    """把前端设置表单草稿转成 config dict，供「测试连接」使用。

    测试要用当前表单里填的值，而不是已保存的旧配置；但敏感字段（token / key /
    密码）在表单里留空表示「不修改」，这时回退到已保存的真实值，否则测试会误报未配置。
    """
    saved = repo.get_settings()
    cfg: dict[str, str] = dict(saved)

    def put(key: str, value: Any) -> None:
        if value is not None:
            cfg[key] = str(value)

    def put_bool(key: str, value: Any) -> None:
        if value is not None:
            cfg[key] = "1" if value else "0"

    put("timezone", payload.get("timezone"))
    put("traffic_threshold_pct", payload.get("traffic_threshold_pct"))
    put("reconcile_interval", payload.get("reconcile_interval"))
    put_bool("billing_enabled", payload.get("billing_enabled"))
    put_bool("daily_report_enabled", payload.get("daily_report_enabled"))
    put("daily_report_time", payload.get("daily_report_time"))
    for key in (
        "notify_on_shift",
        "notify_on_failover",
        "notify_on_recovery",
        "notify_on_breaker",
        "notify_on_limit_stop",
        "notify_on_error",
        "notify_on_keepalive",
    ):
        put_bool(key, payload.get(key))

    ddns = payload.get("ddns") or {}
    if ddns:
        put_bool("ddns_enabled", ddns.get("enabled"))
        put("ddns_cf_auth_mode", ddns.get("cf_auth_mode"))
        put("ddns_cf_email", ddns.get("cf_email"))
        put("ddns_zone_id", ddns.get("zone_id"))
        put("ddns_ttl", ddns.get("ttl"))
        put_bool("ddns_proxied", ddns.get("proxied"))
        # 敏感字段：草稿有值就用草稿，留空回退已保存值
        if ddns.get("cf_token"):
            cfg["ddns_cf_token"] = str(ddns["cf_token"])
        if ddns.get("cf_key"):
            cfg["ddns_cf_key"] = str(ddns["cf_key"])

    tg = payload.get("telegram") or {}
    if tg:
        put_bool("notify_tg_enabled", tg.get("enabled"))
        put("notify_tg_chat_id", tg.get("chat_id"))
        put("notify_tg_proxy", tg.get("proxy"))
        if tg.get("token"):
            cfg["notify_tg_token"] = str(tg["token"])

    mail = payload.get("email") or {}
    if mail:
        put_bool("notify_mail_enabled", mail.get("enabled"))
        put("notify_mail_host", mail.get("host"))
        put("notify_mail_port", mail.get("port"))
        put("notify_mail_security", mail.get("security"))
        put("notify_mail_username", mail.get("username"))
        put("notify_mail_to", mail.get("to"))
        if mail.get("password"):
            cfg["notify_mail_password"] = str(mail["password"])

    wh = payload.get("webhook") or {}
    if wh:
        put_bool("notify_wh_enabled", wh.get("enabled"))
        put("notify_wh_url", wh.get("url"))
        put("notify_wh_method", wh.get("method"))
        put("notify_wh_body", wh.get("body"))
        if wh.get("headers"):
            cfg["notify_wh_headers"] = str(wh["headers"])

    return cfg

_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()
LOGIN_WINDOW = 300
LOGIN_MAX_FAILURES = 8


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------

def _is_initialized() -> bool:
    return repo.get_setting("initialized") == "1"


def _session_epoch() -> int:
    try:
        return int(repo.get_setting("session_epoch") or 0)
    except Exception:
        return 0


def _current_user(request: Request) -> str | None:
    return security.read_session(
        request.cookies.get(settings.SESSION_COOKIE, ""), _session_epoch()
    )


def require_admin(request: Request) -> str:
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie = request.cookies.get(settings.CSRF_COOKIE, "")
    header = request.headers.get("x-cdt-csrf", "")
    if not cookie or cookie != header:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------

def _validate_group_timetable(payload: dict[str, Any]) -> None:
    """时刻表只有「全组都填」和「全组都不填」两种合法状态。

    只填一部分时引擎的 _is_timetable_group 会判定「没用时刻表」，静默退回
    按小时轮换，界面上却还显示着时刻 —— 宁可拒绝保存，也不让它悄悄降级。
    """
    members = [m for m in (payload.get("members") or []) if m.get("enabled", True)]
    filled = [
        m
        for m in members
        if str(m.get("start_time") or "").strip() and str(m.get("end_time") or "").strip()
    ]
    if filled and len(filled) != len(members):
        raise ValueError("要么每台成员都填起止时刻，要么全部留空（按班次时长轮换）")


def _managed_map() -> dict[int, dict[str, Any]]:
    """哪台服务器被哪个自动化流程接管。

    换班组优先于单机保活：同一台机器同时在两处时，单机保活本来就会让位，
    这里保持一致，免得界面给出两套互相矛盾的说明。
    """
    managed: dict[int, dict[str, Any]] = {}
    for group in repo.list_groups():
        for member in group.get("members") or []:
            if not member.get("enabled"):
                continue
            managed[int(member["server_id"])] = {
                "kind": "group",
                "group_id": int(group["id"]),
                "name": group.get("name") or "",
                "active": bool(group.get("enabled")),
            }
    for item in repo.list_keepalives():
        server_id = int(item["server_id"])
        if server_id in managed:
            continue
        managed[server_id] = {
            "kind": "keepalive",
            "keepalive_id": int(item["id"]),
            "name": item.get("label") or "",
            "active": bool(item.get("enabled")),
        }
    return managed


def server_summary(
    server: dict[str, Any],
    managed: dict[int, dict[str, Any]] | None = None,
    dns_map: dict[int, list[str]] | None = None,
) -> dict[str, Any]:
    used = server.get("traffic_used")
    quota = float(server.get("quota_gb") or 0) or 1
    owner = (managed or {}).get(int(server.get("id") or 0))
    return {
        "id": server.get("id"),
        "label": server.get("label") or "",
        "access_key_masked": server.get("access_key_masked") or "",
        "region_id": server.get("region_id") or "",
        "site": repo.normalize_site(server.get("site")),
        "instance_id": server.get("instance_id") or "",
        "quota_gb": server.get("quota_gb") or 0,
        "threshold_gb": server.get("threshold_gb") or 0,
        "remark": server.get("remark") or "",
        "traffic_used": used,
        "traffic_percent": round((used or 0) / quota * 100, 2) if used is not None else None,
        "instance_status": server.get("instance_status") or "Unknown",
        "stopped_mode": server.get("stopped_mode") or "",
        "public_ip": server.get("public_ip") or "",
        "traffic_checked_at": server.get("traffic_checked_at") or 0,
        "status_checked_at": server.get("status_checked_at") or 0,
        "secret_configured": server.get("secret_configured", False),
        # None 表示闲散机器：没编进任何自动化流程，可以随时手动开关机
        "managed": owner,
        # 本月账单 + 账户余额快照（慢数据，后台定时刷新，读库不出网）
        "billing": billing.summary_of(server),
        # 绑定的 DNS 域名（可能有多条，用顿号连起来）；空串表示这台机器没占任何记录
        "dns_record": "、".join((dns_map or {}).get(int(server.get("id") or 0), [])),
    }


def _group_payload(
    group: dict[str, Any],
    config: dict[str, str],
    now_ts: float,
    managed: dict[int, dict[str, Any]] | None = None,
    books: dict[str, dict[int, list[str]]] | None = None,
) -> dict[str, Any]:
    members = active_members(group)
    picked = duty_at(group, now_ts)
    duty_id = int(picked[0]["server_id"]) if picked else 0
    shift_start = picked[1] if picked else 0
    slot_key = str(shift_start)
    run = repo.get_run(int(group["id"]), slot_key) or {} if picked else {}
    # 本班次内发生过故障转移时，真正在跑的是替补，界面上的「当班」必须跟着它走，
    # 否则会高亮一台已经停机的机器
    if run.get("failover") and int(run.get("duty_server_id") or 0):
        duty_id = int(run["duty_server_id"])
    nxt = next_switch_at(group, now_ts)
    servers = {int(s["id"]): s for s in repo.list_servers()}

    member_payload = []
    for member in group.get("members") or []:
        server = servers.get(int(member["server_id"]))
        member_payload.append(
            {
                "id": member["id"],
                "server_id": member["server_id"],
                "position": member["position"],
                "hours": member["hours"],
                "slot_name": member.get("slot_name") or "",
                "start_time": member.get("start_time") or "",
                "end_time": member.get("end_time") or "",
                "enabled": bool(member["enabled"]),
                "is_duty": int(member["server_id"]) == duty_id,
                "server": server_summary(server, managed, (books or {}).get("servers"))
                if server
                else None,
            }
        )

    return {
        "id": group["id"],
        "name": group.get("name") or "",
        "enabled": bool(group.get("enabled")),
        "failover_enabled": bool(group.get("failover_enabled")),
        # 绑定关系在 DNS 页维护，这里只是读出来显示（可能有多条，顿号连接）
        "ddns_record": "、".join((books or {}).get("groups", {}).get(int(group["id"]), [])),
        "anchor_at": group.get("anchor_at") or 0,
        "cycle_hours": round(sum(float(m["hours"]) for m in members), 2),
        "members": member_payload,
        "duty_server_id": duty_id,
        "shift_started_at": shift_start,
        "next_switch_at": int(nxt) if nxt else 0,
        "phase": run.get("phase") or "pending",
        "phase_label": PHASE_LABELS.get(run.get("phase") or "pending", "待执行"),
        "note": run.get("note") or "",
        "updated_at": run.get("updated_at") or 0,
        "current_index": current_member_index(group, now_ts),
    }


# --------------------------------------------------------------------------
# 路由注册
# --------------------------------------------------------------------------

def register(app: FastAPI) -> None:
    @app.exception_handler(ValueError)
    def _value_error(_request: Request, exc: ValueError):  # noqa: ANN202
        return _error(400, "invalid_request", str(exc))

    @app.exception_handler(HTTPException)
    def _http_error(_request: Request, exc: HTTPException):  # noqa: ANN202
        code = "unauthorized" if exc.status_code == 401 else "request_failed"
        return _error(exc.status_code, code, str(exc.detail))

    # -- 系统 -----------------------------------------------------------
    @app.get("/api/health")
    def health():  # noqa: ANN202
        return {"status": "ok", "version": settings.VERSION}

    @app.get("/api/v1/system/init-status")
    def init_status():  # noqa: ANN202
        return {
            "initialized": _is_initialized(),
            "brand": settings.BRAND,
            "version": settings.VERSION,
            "repo": settings.REPO,
        }

    @app.post("/api/v1/setup")
    def setup(payload: dict[str, Any]):  # noqa: ANN202
        if _is_initialized():
            raise HTTPException(status_code=409, detail="系统已初始化")
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if len(username) < 3:
            raise ValueError("用户名至少 3 个字符")
        if len(password) < 10:
            raise ValueError("密码至少 10 个字符")
        repo.set_settings(
            {
                "admin_username": username,
                "admin_password": security.hash_password(password),
                "initialized": "1",
            }
        )
        repo.add_event("system", "系统初始化完成", f"管理员：{username}")
        response = JSONResponse({"ok": True})
        _attach_session(response, username)
        return response

    # -- 登录 -----------------------------------------------------------
    @app.post("/api/v1/auth/login")
    def login(request: Request, payload: dict[str, Any]):  # noqa: ANN202
        client = (request.client.host if request.client else "unknown") or "unknown"
        if _rate_limited(client):
            raise HTTPException(status_code=429, detail="尝试过于频繁，请 5 分钟后再试")

        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        stored_user = repo.get_setting("admin_username")
        stored_hash = repo.get_setting("admin_password")

        if username != stored_user or not security.verify_password(password, stored_hash):
            _record_failure(client)
            raise HTTPException(status_code=401, detail="用户名或密码不正确")

        _reset_failures(client)
        response = JSONResponse({"ok": True, "username": username})
        _attach_session(response, username)
        return response

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie(settings.SESSION_COOKIE, path="/")
        response.delete_cookie(settings.CSRF_COOKIE, path="/")
        return response

    @app.get("/api/v1/auth/me")
    def me(user: str = Depends(require_admin)):  # noqa: ANN202
        return {"username": user}

    # -- 总览 -----------------------------------------------------------
    @app.get("/api/v1/overview")
    def overview(user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        config = repo.get_settings()
        tz = _timezone(config.get("timezone") or "Asia/Shanghai")
        now = dt.datetime.now(tz)
        channels = notify.enabled_channels(config)
        keepalives = repo.list_keepalives()
        managed = _managed_map()
        books = dns.record_books()

        return {
            "now": now.isoformat(),
            "now_ts": int(now.timestamp()),
            "timezone": config.get("timezone"),
            "version": settings.VERSION,
            "brand": settings.BRAND,
            "repo": settings.REPO,
            "servers": [
                server_summary(s, managed, books.get("servers")) for s in repo.list_servers()
            ],
            "keepalives": [
                {
                    **item,
                    "dns_record": "、".join(books.get("servers", {}).get(int(item["server_id"]), [])),
                }
                for item in keepalives
            ],
            "groups": [
                _group_payload(g, config, now.timestamp(), managed, books)
                for g in repo.list_groups()
            ],
            # DNS 页的全部数据：记录名 + 绑定目标 + 上次同步结果
            "dns": dns.snapshot(config, now.timestamp()),
            "notifications": {
                "channels": channels,
                "telegram": "telegram" in channels,
                "email": "email" in channels,
                "webhook": "webhook" in channels,
            },
            "events": repo.list_events(limit=40),
        }

    @app.post("/api/v1/reconcile")
    def reconcile(request: Request, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        results = engine.reconcile_all()
        # 手动对账时顺手把 DNS 也过一遍，免得「点了立即对账但域名没跟上」
        return {"results": results, "dns": dns.reconcile(repo.get_settings())}

    # -- 服务器 ---------------------------------------------------------
    @app.get("/api/v1/servers")
    def servers(user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        managed = _managed_map()
        books = dns.record_books()
        return {
            "items": [
                server_summary(s, managed, books.get("servers")) for s in repo.list_servers()
            ]
        }

    @app.post("/api/v1/servers")
    def save_server(request: Request, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        return {"id": repo.upsert_server(payload)}

    @app.post("/api/v1/servers/reorder")
    def reorder_servers(request: Request, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        """手动排序：前端拖拽后把完整的有序 id 列表传回来持久化。"""
        require_csrf(request)
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            raise HTTPException(status_code=400, detail="ids 必须是非空的有序数组")
        try:
            ordered = [int(i) for i in ids]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="ids 里的元素必须是数字") from None
        repo.reorder_servers(ordered)
        return {"ok": True}

    @app.delete("/api/v1/servers/{server_id}")
    def remove_server(request: Request, server_id: int, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        repo.delete_server(server_id)
        return {"ok": True}

    @app.post("/api/v1/servers/{server_id}/refresh")
    def refresh_server(request: Request, server_id: int, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        return engine.refresh(server_id)

    @app.post("/api/v1/servers/{server_id}/billing")
    def refresh_billing(request: Request, server_id: int, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        snapshot = billing.refresh_server(server_id, force=True)
        if snapshot is None:
            raise HTTPException(status_code=400, detail="费用查询已关闭，或服务器不存在")
        if snapshot.get("error") and snapshot.get("monthly_cost") is None:
            # 两个接口都失败才算这次刷新失败，让前端把原因吐出来
            raise HTTPException(status_code=502, detail=snapshot["error"])
        return snapshot

    @app.post("/api/v1/servers/{server_id}/actions/{action}")
    def server_action(request: Request, server_id: int, action: str, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        return engine.manual_action(server_id, action)

    @app.get("/api/v1/servers/{server_id}/samples")
    def server_samples(server_id: int, hours: int = 72, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        return {"items": repo.list_samples(server_id, max(1, min(hours, 720)))}

    # -- 单机保活 -------------------------------------------------------
    @app.get("/api/v1/keepalives")
    def keepalives(user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        return {"items": repo.list_keepalives()}

    @app.post("/api/v1/keepalives")
    def add_keepalive(request: Request, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        server_id = int(payload.get("server_id") or 0)
        if not server_id:
            raise ValueError("请选择服务器")
        return {"id": repo.add_keepalive(server_id, bool(payload.get("enabled", True)))}

    @app.post("/api/v1/keepalives/{keepalive_id}/toggle")
    def toggle_keepalive(request: Request, keepalive_id: int, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        repo.set_keepalive_enabled(keepalive_id, bool(payload.get("enabled", True)))
        return {"ok": True}

    @app.delete("/api/v1/keepalives/{keepalive_id}")
    def remove_keepalive(request: Request, keepalive_id: int, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        repo.delete_keepalive(keepalive_id)
        return {"ok": True}

    # -- 换班组 ---------------------------------------------------------
    @app.get("/api/v1/groups")
    def groups(user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        config = repo.get_settings()
        now_ts = dt.datetime.now(_timezone(config.get("timezone") or "Asia/Shanghai")).timestamp()
        books = dns.record_books()
        return {
            "items": [_group_payload(g, config, now_ts, None, books) for g in repo.list_groups()]
        }

    @app.post("/api/v1/groups")
    def save_group(request: Request, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        _validate_group_timetable(payload)
        return {"id": repo.upsert_group(payload)}

    @app.delete("/api/v1/groups/{group_id}")
    def remove_group(request: Request, group_id: int, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        repo.delete_group(group_id)
        return {"ok": True}

    @app.post("/api/v1/groups/{group_id}/switch")
    def switch_group(request: Request, group_id: int, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        return {"result": engine.manual_switch(group_id)}

    # -- DNS -----------------------------------------------------------
    @app.get("/api/v1/dns")
    def get_dns(user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        return dns.snapshot(repo.get_settings())

    @app.post("/api/v1/dns/records")
    def add_dns_record(request: Request, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        name = dns.add_record(str(payload.get("record") or ""), repo.get_settings())
        repo.add_event("dns", "DNS 记录名已添加", name)
        return {"record": name}

    @app.delete("/api/v1/dns/records/{record}")
    def remove_dns_record(request: Request, record: str, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        row = repo.get_dns_record(record)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        dns.remove_record(record, repo.get_settings())
        repo.add_event(
            "dns", "DNS 记录名已删除", f"{record}（绑定已一并解除）" if row.get("target_kind") else record
        )
        return {"ok": True}

    @app.post("/api/v1/dns/records/{record}/bind")
    def bind_dns(request: Request, record: str, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        kind = str(payload.get("kind") or "")
        target_id = int(payload.get("id") or 0)
        if kind and not target_id:
            raise ValueError("请选择绑定目标")
        config = repo.get_settings()
        dns.sync_registry(config)
        return dns.bind(record, kind, target_id)

    @app.post("/api/v1/dns/records/{record}/sync")
    def sync_dns(request: Request, record: str, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        config = repo.get_settings()
        if config.get("ddns_enabled") != "1":
            raise HTTPException(status_code=400, detail="DDNS 未启用，请先在设置里打开")
        result = dns.sync_record(record, config, force=True)
        return {"record": record, "status": result.status, "detail": result.detail, "ip": result.ip}

    @app.post("/api/v1/dns/sync")
    def sync_all_dns(request: Request, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        config = repo.get_settings()
        if config.get("ddns_enabled") != "1":
            raise HTTPException(status_code=400, detail="DDNS 未启用，请先在设置里打开")
        return {"items": dns.reconcile(config, force=True)}

    # -- 设置 -----------------------------------------------------------
    @app.get("/api/v1/settings")
    def get_settings(user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        config = repo.get_settings()
        return {
            "admin_username": config.get("admin_username") or "",
            "timezone": config.get("timezone"),
            "traffic_threshold_pct": config.get("traffic_threshold_pct"),
            "reconcile_interval": config.get("reconcile_interval"),
            "billing_enabled": config.get("billing_enabled", "1") == "1",
            "billing_interval": config.get("billing_interval") or "600",
            "daily_report_enabled": config.get("daily_report_enabled") == "1",
            "daily_report_time": config.get("daily_report_time"),
            "notify_on_shift": config.get("notify_on_shift") == "1",
            "notify_on_failover": config.get("notify_on_failover") == "1",
            "notify_on_recovery": config.get("notify_on_recovery") == "1",
            "notify_on_breaker": config.get("notify_on_breaker") == "1",
            "notify_on_limit_stop": config.get("notify_on_limit_stop") == "1",
            "notify_on_error": config.get("notify_on_error") == "1",
            "notify_on_keepalive": config.get("notify_on_keepalive") == "1",
            "ddns": {
                "enabled": config.get("ddns_enabled") == "1",
                "provider": config.get("ddns_provider") or "cloudflare",
                "cf_auth_mode": config.get("ddns_cf_auth_mode") or "token",
                "cf_token_configured": bool(config.get("ddns_cf_token")),
                "cf_email": config.get("ddns_cf_email") or "",
                "cf_key_configured": bool(config.get("ddns_cf_key")),
                "zone_id": config.get("ddns_zone_id") or "",
                "record": ddns.default_record(config),
                "records": ddns.records(config),
                "ttl": config.get("ddns_ttl"),
                "proxied": config.get("ddns_proxied") == "1",
                "last_ip": config.get("ddns_last_ip") or "",
                "last_status": config.get("ddns_last_status") or "",
            },
            "telegram": {
                "enabled": config.get("notify_tg_enabled") == "1",
                "token_configured": bool(config.get("notify_tg_token")),
                "chat_id": config.get("notify_tg_chat_id") or "",
                "proxy": config.get("notify_tg_proxy") or "",
            },
            "email": {
                "enabled": config.get("notify_mail_enabled") == "1",
                "host": config.get("notify_mail_host") or "",
                "port": config.get("notify_mail_port"),
                "security": config.get("notify_mail_security") or "ssl",
                "username": config.get("notify_mail_username") or "",
                "password_configured": bool(config.get("notify_mail_password")),
                "to": config.get("notify_mail_to") or "",
            },
            "webhook": {
                "enabled": config.get("notify_wh_enabled") == "1",
                "url": config.get("notify_wh_url") or "",
                "method": config.get("notify_wh_method") or "POST",
                "headers_configured": bool(config.get("notify_wh_headers")),
                "body": config.get("notify_wh_body") or "",
            },
            "channels": notify.enabled_channels(config),
            "sdk_ready": aliyun.sdk_ready(),
            "dry_run": settings.DRY_RUN,
            "repo": settings.REPO,
            "version": settings.VERSION,
        }

    @app.post("/api/v1/settings")
    def save_settings(request: Request, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        updates: dict[str, str] = {}

        def put_str(key: str, value: Any) -> None:
            if value is not None:
                updates[key] = str(value)

        def put_bool(key: str, value: Any) -> None:
            if value is not None:
                updates[key] = "1" if value else "0"

        put_str("timezone", payload.get("timezone"))
        if payload.get("traffic_threshold_pct") is not None:
            percent = int(payload["traffic_threshold_pct"])
            if not 50 <= percent <= 100:
                raise ValueError("流量阈值百分比应在 50–100 之间")
            updates["traffic_threshold_pct"] = str(percent)
        if payload.get("reconcile_interval") is not None:
            interval = int(payload["reconcile_interval"])
            if not 30 <= interval <= 3600:
                raise ValueError("同步间隔应在 30–3600 秒之间")
            updates["reconcile_interval"] = str(interval)

        put_bool("billing_enabled", payload.get("billing_enabled"))
        if payload.get("billing_interval") is not None:
            interval = int(payload["billing_interval"])
            if not 60 <= interval <= 86400:
                raise ValueError("费用查询间隔应在 60–86400 秒之间")
            updates["billing_interval"] = str(interval)

        put_bool("daily_report_enabled", payload.get("daily_report_enabled"))
        put_str("daily_report_time", payload.get("daily_report_time"))
        for key in (
            "notify_on_shift",
            "notify_on_failover",
            "notify_on_recovery",
            "notify_on_breaker",
            "notify_on_limit_stop",
            "notify_on_error",
            "notify_on_keepalive",
        ):
            put_bool(key, payload.get(key))

        ddns_payload = payload.get("ddns") or {}
        if ddns_payload:
            put_bool("ddns_enabled", ddns_payload.get("enabled"))
            put_str("ddns_provider", ddns_payload.get("provider"))
            put_str("ddns_cf_auth_mode", ddns_payload.get("cf_auth_mode"))
            put_str("ddns_cf_email", ddns_payload.get("cf_email"))
            put_str("ddns_zone_id", ddns_payload.get("zone_id"))
            records_payload = ddns_payload.get("records")
            if records_payload is not None:
                if not isinstance(records_payload, list):
                    raise ValueError("DDNS 记录名需要是列表")
                cleaned: list[str] = []
                seen: set[str] = set()
                for item in records_payload:
                    name = str(item or "").strip()
                    if not name or name.lower() in seen:
                        continue
                    if any(ch.isspace() for ch in name):
                        raise ValueError(f"DDNS 记录名不能带空格：{name}")
                    seen.add(name.lower())
                    cleaned.append(name)
                updates["ddns_records"] = json.dumps(cleaned, ensure_ascii=False)
                # 兼容单记录时代的字段：默认域名永远等于第一条
                updates["ddns_record"] = cleaned[0] if cleaned else ""
            else:
                put_str("ddns_record", ddns_payload.get("record"))
            put_str("ddns_ttl", ddns_payload.get("ttl"))
            put_bool("ddns_proxied", ddns_payload.get("proxied"))
            if ddns_payload.get("cf_token"):
                updates["ddns_cf_token"] = str(ddns_payload["cf_token"])
            if ddns_payload.get("cf_key"):
                updates["ddns_cf_key"] = str(ddns_payload["cf_key"])

        tg = payload.get("telegram") or {}
        if tg:
            put_bool("notify_tg_enabled", tg.get("enabled"))
            put_str("notify_tg_chat_id", tg.get("chat_id"))
            put_str("notify_tg_proxy", tg.get("proxy"))
            if tg.get("token"):
                updates["notify_tg_token"] = str(tg["token"])

        mail = payload.get("email") or {}
        if mail:
            put_bool("notify_mail_enabled", mail.get("enabled"))
            put_str("notify_mail_host", mail.get("host"))
            put_str("notify_mail_port", mail.get("port"))
            put_str("notify_mail_security", mail.get("security"))
            put_str("notify_mail_username", mail.get("username"))
            put_str("notify_mail_to", mail.get("to"))
            if mail.get("password"):
                updates["notify_mail_password"] = str(mail["password"])

        wh = payload.get("webhook") or {}
        if wh:
            put_bool("notify_wh_enabled", wh.get("enabled"))
            put_str("notify_wh_url", wh.get("url"))
            put_str("notify_wh_method", wh.get("method"))
            put_str("notify_wh_body", wh.get("body"))
            if wh.get("headers"):
                updates["notify_wh_headers"] = str(wh["headers"])

        bumped_epoch: int | None = None
        new_session_user: str | None = None
        new_username = str(payload.get("new_username") or "").strip()
        new_password = payload.get("new_password")

        # 改用户名 / 改密码都会让所有旧票据失效，别让别的浏览器继续挂着；
        # 当前浏览器在最后重新签一张新票据。
        if new_username:
            if len(new_username) < 3:
                raise ValueError("用户名至少 3 个字符")
            if new_username != repo.get_setting("admin_username"):
                updates["admin_username"] = new_username
                bumped_epoch = int(time.time())
                updates["session_epoch"] = str(bumped_epoch)
                new_session_user = new_username

        if new_password:
            if len(str(new_password)) < 10:
                raise ValueError("新密码至少 10 个字符")
            updates["admin_password"] = security.hash_password(str(new_password))
            bumped_epoch = int(time.time())
            updates["session_epoch"] = str(bumped_epoch)

        if updates:
            # 前端每次保存都会把整张表单发上来，直接按 updates 记事件会写成「等 33 项」。
            # 只把值真的变了的字段记进去，事件里才是本次操作做了什么。
            before = repo.get_settings()
            changed = [
                key
                for key, value in updates.items()
                if str(before.get(key, "")) != str(value) and key not in SETTING_LABELS_SKIP
            ]
            repo.set_settings(updates)
            if "ddns_records" in updates:
                # 记录名列表变了，把表里的行同步一遍（增的补上、删的连绑定一起清掉）
                dns.sync_registry(repo.get_settings())
            if changed:
                repo.add_event("system", "配置已更新", _setting_labels(changed))

        if bumped_epoch is not None:
            # 改用户名 / 改密码的这个浏览器要留住，给它发一张新票据；
            # 改了用户名就用新名字签，否则沿用票据里的 user
            response = JSONResponse({"ok": True, "username": new_session_user or user})
            _attach_session(response, new_session_user or user, bumped_epoch)
            return response
        return {"ok": True}

    # -- 测试 -----------------------------------------------------------
    @app.post("/api/v1/settings/test/{channel}")
    def test_channel(request: Request, channel: str, payload: dict[str, Any] | None = None, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        # 用前端此刻表单里的草稿来测，而不是已保存的旧配置：
        # 这样改完 token / 主机地址不用先保存就能直接点「测试」。
        config = _config_from_draft(payload or {})
        if channel == "ddns":
            result = ddns.test(config)
            return {"ok": result.status.startswith("✅"), "status": result.status, "detail": result.detail}

        if channel not in ("telegram", "email", "webhook"):
            raise ValueError("未知渠道")
        message = notify.Message(
            kind="test",
            title="通知通道测试",
            html=f"✅ <b>{settings.BRAND} 通知测试</b>\n\n配置工作正常。\n⏰ {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            text="通知通道测试：配置工作正常。",
        )
        try:
            notify.send(config, channel, message)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": "已发送"}

    # -- 事件 -----------------------------------------------------------
    @app.get("/api/v1/events")
    def events(limit: int = 100, kind: str = "", level: str = "", user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        return {"items": repo.list_events(limit=max(1, min(limit, 500)), kind=kind, level=level)}


# --------------------------------------------------------------------------
# 会话 cookie
# --------------------------------------------------------------------------

def _attach_session(response: JSONResponse, username: str, epoch: int | None = None) -> None:
    token = security.issue_session(username, _session_epoch() if epoch is None else epoch)
    response.set_cookie(
        settings.SESSION_COOKIE,
        token,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        settings.CSRF_COOKIE,
        security.new_csrf_token(),
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=False,
        samesite="lax",
        path="/",
    )


# --------------------------------------------------------------------------
# 登录限速
# --------------------------------------------------------------------------

def _rate_limited(client: str) -> bool:
    now = time.time()
    with _login_lock:
        attempts = [t for t in _login_failures.get(client, []) if now - t < LOGIN_WINDOW]
        _login_failures[client] = attempts
        return len(attempts) >= LOGIN_MAX_FAILURES


def _record_failure(client: str) -> None:
    with _login_lock:
        _login_failures.setdefault(client, []).append(time.time())


def _reset_failures(client: str) -> None:
    with _login_lock:
        _login_failures.pop(client, None)
