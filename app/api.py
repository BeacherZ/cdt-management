"""HTTP API。"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import aliyun, ddns, notify, repo, security, settings
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
    PHASE_FAILOVER: "临时换班",
    PHASE_BREAKER: "熔断停机",
    PHASE_ERROR: "异常",
    "pending": "待执行",
}

# 配置变更事件里给人看的字段名，避免把 ddns_cf_token 这种内部键直接甩到界面上
SETTING_LABELS = {
    "timezone": "时区",
    "traffic_threshold_pct": "流量阈值",
    "reconcile_interval": "对账间隔",
    "daily_report_enabled": "每日汇总",
    "daily_report_time": "汇总时间",
    "notify_on_shift": "换班通知",
    "notify_on_failover": "故障转移通知",
    "notify_on_recovery": "抢占恢复通知",
    "notify_on_breaker": "熔断通知",
    "notify_on_limit_stop": "超限停机通知",
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
    "ddns_ttl": "TTL",
    "ddns_proxied": "CDN 代理",
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
    "admin_password": "登录密码",
}


def _setting_labels(keys: list[str]) -> str:
    names = [SETTING_LABELS.get(key, key) for key in sorted(keys)]
    if len(names) > 8:
        return "、".join(names[:8]) + f" 等 {len(names)} 项"
    return "、".join(names)

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

def server_summary(server: dict[str, Any]) -> dict[str, Any]:
    used = server.get("traffic_used")
    quota = float(server.get("quota_gb") or 0) or 1
    return {
        "id": server.get("id"),
        "label": server.get("label") or "",
        "access_key_masked": server.get("access_key_masked") or "",
        "region_id": server.get("region_id") or "",
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
    }


def _group_payload(group: dict[str, Any], config: dict[str, str], now_ts: float) -> dict[str, Any]:
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
                "enabled": bool(member["enabled"]),
                "is_duty": int(member["server_id"]) == duty_id,
                "server": server_summary(server) if server else None,
            }
        )

    return {
        "id": group["id"],
        "name": group.get("name") or "",
        "enabled": bool(group.get("enabled")),
        "failover_enabled": bool(group.get("failover_enabled")),
        "ddns_record": group.get("ddns_record") or "",
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

        return {
            "now": now.isoformat(),
            "now_ts": int(now.timestamp()),
            "timezone": config.get("timezone"),
            "version": settings.VERSION,
            "brand": settings.BRAND,
            "repo": settings.REPO,
            "servers": [server_summary(s) for s in repo.list_servers()],
            "keepalives": keepalives,
            "groups": [_group_payload(g, config, now.timestamp()) for g in repo.list_groups()],
            "ddns": {
                "enabled": config.get("ddns_enabled") == "1",
                "record": config.get("ddns_record") or "",
                "last_ip": config.get("ddns_last_ip") or "",
                "last_status": config.get("ddns_last_status") or "",
                "last_at": int(config.get("ddns_last_at") or 0),
            },
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
        return {"results": engine.reconcile_all()}

    # -- 服务器 ---------------------------------------------------------
    @app.get("/api/v1/servers")
    def servers(user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        return {"items": [server_summary(s) for s in repo.list_servers()]}

    @app.post("/api/v1/servers")
    def save_server(request: Request, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        return {"id": repo.upsert_server(payload)}

    @app.delete("/api/v1/servers/{server_id}")
    def remove_server(request: Request, server_id: int, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        repo.delete_server(server_id)
        return {"ok": True}

    @app.post("/api/v1/servers/{server_id}/refresh")
    def refresh_server(request: Request, server_id: int, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        return engine.refresh(server_id)

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
        return {"items": [_group_payload(g, config, now_ts) for g in repo.list_groups()]}

    @app.post("/api/v1/groups")
    def save_group(request: Request, payload: dict[str, Any], user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
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

    # -- 设置 -----------------------------------------------------------
    @app.get("/api/v1/settings")
    def get_settings(user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        config = repo.get_settings()
        return {
            "timezone": config.get("timezone"),
            "traffic_threshold_pct": config.get("traffic_threshold_pct"),
            "reconcile_interval": config.get("reconcile_interval"),
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
                "record": config.get("ddns_record") or "",
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
                raise ValueError("对账间隔应在 30–3600 秒之间")
            updates["reconcile_interval"] = str(interval)

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
        new_password = payload.get("new_password")
        if new_password:
            if len(str(new_password)) < 10:
                raise ValueError("新密码至少 10 个字符")
            updates["admin_password"] = security.hash_password(str(new_password))
            # 改密码就让所有旧票据失效，别让别的浏览器继续挂着
            bumped_epoch = int(time.time())
            updates["session_epoch"] = str(bumped_epoch)

        if updates:
            repo.set_settings(updates)
            repo.add_event("system", "配置已更新", _setting_labels(list(updates)))

        if bumped_epoch is not None:
            # 改密码的这个浏览器要留住，给它发一张新票据
            response = JSONResponse({"ok": True})
            _attach_session(response, user, bumped_epoch)
            return response
        return {"ok": True}

    # -- 测试 -----------------------------------------------------------
    @app.post("/api/v1/settings/test/{channel}")
    def test_channel(request: Request, channel: str, user: str = Depends(require_admin)):  # noqa: ANN202, ARG001
        require_csrf(request)
        config = repo.get_settings()
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
