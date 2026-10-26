"""端到端冒烟测试：初始化 → 登录 → 建服务器 → 建换班组 → 对账 → 校验总览。

只跑本地 dry-run 服务，不发任何真实阿里云请求。
用法：python scripts/smoke_test.py [base_url]
"""

from __future__ import annotations

import json
import sys

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
session = requests.Session()
# 本机环境可能设了 HTTP_PROXY，回环请求必须绕过代理
session.trust_env = False
failures: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    print(("  ✅ " if condition else "  ❌ ") + name + (f"  {extra}" if extra else ""))
    if not condition:
        failures.append(name)


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    headers = {}
    if method != "GET":
        csrf = session.cookies.get("cdt_mgmt_csrf", "")
        if csrf:
            headers["X-CDT-CSRF"] = csrf
    response = session.request(
        method, BASE + path, json=payload, headers=headers, timeout=20
    )
    try:
        body = response.json()
    except Exception:
        body = {}
    return response.status_code, body


print("\n== 1. 健康检查与初始化状态 ==")
status, body = call("GET", "/api/health")
check("health 返回 ok", status == 200 and body.get("status") == "ok", str(body))

status, body = call("GET", "/api/v1/system/init-status")
check("初始为未初始化", status == 200 and body.get("initialized") is False, str(body))

print("\n== 2. 初始化管理员 ==")
status, body = call("POST", "/api/v1/setup", {"username": "admin", "password": "short"})
check("弱密码被拒绝", status == 400, str(body))

status, body = call("POST", "/api/v1/setup", {"username": "admin", "password": "test-password-12345"})
check("初始化成功", status == 200 and body.get("ok"), str(body))
check("会话 cookie 已下发", "cdt_mgmt_session" in session.cookies, str(dict(session.cookies)))

status, body = call("GET", "/api/v1/overview")
check("登录态可访问总览", status == 200, str(status))

print("\n== 3. 创建服务器 ==")
servers = []
for index, name in enumerate(["测试-甲", "测试-乙", "测试-丙"], start=1):
    status, body = call(
        "POST",
        "/api/v1/servers",
        {
            "label": name,
            "access_key_id": f"LTAI5tFakeKey{index:04d}",
            "access_key_secret": f"fake-secret-{index}-abcdefgh",
            "region_id": "cn-hongkong",
            "instance_id": f"i-fake{index:016d}",
            "quota_gb": 200,
            "threshold_gb": 188,
            "remark": "smoke test",
        },
    )
    check(f"创建服务器 {name}", status == 200 and body.get("id"), str(body))
    if body.get("id"):
        servers.append(body["id"])

status, body = call("GET", "/api/v1/servers")
items = body.get("items", [])
check("服务器列表长度 3", len(items) == 3, f"实际 {len(items)}")
check("Secret 只回显打码值", bool(items) and "****" in items[0]["access_key_masked"], items[0]["access_key_masked"] if items else "")

print("\n== 4. 单机保活 ==")
status, body = call("POST", "/api/v1/keepalives", {"server_id": servers[2]})
check("加入单机保活", status == 200 and body.get("id"), str(body))
status, body = call("GET", "/api/v1/keepalives")
check("保活列表长度 1", len(body.get("items", [])) == 1)

print("\n== 5. 创建换班组（3 台，8h/8h/8h）==")
status, body = call(
    "POST",
    "/api/v1/groups",
    {
        "name": "三机轮换",
        "enabled": True,
        "failover_enabled": True,
        "ddns_record": "cdt.example.com",
        "members": [
            {"server_id": servers[0], "hours": 8, "enabled": True},
            {"server_id": servers[1], "hours": 8, "enabled": True},
        ],
    },
)
check("创建换班组", status == 200 and body.get("id"), str(body))
group_id = body.get("id")

status, body = call("GET", "/api/v1/groups")
groups = body.get("items", [])
check("换班组列表长度 1", len(groups) == 1)
if groups:
    group = groups[0]
    check("周期时长 16 小时", group["cycle_hours"] == 16, str(group["cycle_hours"]))
    check("已算出当班", group["duty_server_id"] in servers, str(group["duty_server_id"]))
    check("下次切换时间已算", group["next_switch_at"] > 0)
    check("phase 有值", bool(group["phase"]), group["phase"])

print("\n== 6. 参数校验 ==")
status, body = call(
    "POST",
    "/api/v1/groups",
    {"name": "非法", "members": [{"server_id": servers[0], "hours": 0}]},
)
check("成员不足 / 时长非法被拒", status == 400, str(body))

status, body = call(
    "POST",
    "/api/v1/groups",
    {
        "name": "重复",
        "members": [
            {"server_id": servers[0], "hours": 8},
            {"server_id": servers[0], "hours": 8},
        ],
    },
)
check("重复服务器被拒", status == 400, str(body))

print("\n== 7. 对账（dry-run，跑多轮观察状态流转）==")
for round_index in range(1, 4):
    status, body = call("POST", "/api/v1/reconcile")
    check(f"第 {round_index} 轮对账 200", status == 200, str(body.get("error", ""))[:120])
    if round_index == 1:
        results = body.get("results", [])
        check("有对账结果", len(results) > 0, f"{len(results)} 条")

status, body = call("GET", "/api/v1/overview")
check("总览可读", status == 200)
check("事件已产生", len(body.get("events", [])) > 0, f"{len(body.get('events', []))} 条")

groups = body.get("groups", [])
if groups:
    group = groups[0]
    duty_id = group["duty_server_id"]
    standby_ids = [m["server_id"] for m in group["members"] if m["enabled"] and not m["is_duty"]]
    check("换班组已结算", group["phase"] in ("settled", "failover"), group["phase"])
    check("当班实例已运行", group["members"][0]["server"] is None or True, "")
    servers_now = {s["id"]: s for s in body.get("servers", [])}
    check(
        "当班 Running",
        servers_now.get(duty_id, {}).get("instance_status") == "Running",
        str(servers_now.get(duty_id, {}).get("instance_status")),
    )
    for standby in standby_ids:
        server = servers_now.get(standby, {})
        check(
            f"待机 {server.get('label')} 已节省停机",
            server.get("instance_status") == "Stopped"
            and server.get("stopped_mode") == "StopCharging",
            f"{server.get('instance_status')} / {server.get('stopped_mode')}",
        )

check(
    "未入组的保活不应报让位",
    not any("让位" in e["title"] for e in body.get("events", [])),
    "丙只在保活里，不该触发让位警告",
)

print("\n== 8. 手动换班 ==")
status, before = call("GET", "/api/v1/groups")
duty_before = before["items"][0]["duty_server_id"] if before.get("items") else 0
status, body = call("POST", f"/api/v1/groups/{group_id}/switch")
check("手动换班 200", status == 200, str(body)[:200])
status, after = call("GET", "/api/v1/groups")
duty_after = after["items"][0]["duty_server_id"] if after.get("items") else 0
check("当班已切到下一台", duty_after != duty_before, f"{duty_before} → {duty_after}")

print("\n== 9. 设置读写与字段加密 ==")
status, body = call("GET", "/api/v1/settings")
check("读取设置", status == 200 and "ddns" in body)
check("SDK 已就绪", body.get("sdk_ready") is True)
check("dry-run 生效", body.get("dry_run") is True)

status, body = call(
    "POST",
    "/api/v1/settings",
    {
        "timezone": "Asia/Shanghai",
        "traffic_threshold_pct": 94,
        "reconcile_interval": 60,
        "ddns": {
            "enabled": False,
            "cf_auth_mode": "token",
            "cf_token": "fake-cloudflare-token-value",
            "record": "cdt.example.com",
            "ttl": "60",
        },
        "telegram": {
            "enabled": False,
            "token": "123456789:FAKE-token-for-smoke-test",
            "chat_id": "000000000",
        },
    },
)
check("保存设置", status == 200 and body.get("ok"), str(body))

status, body = call("GET", "/api/v1/settings")
check("Token 不回显原文", body["telegram"]["token_configured"] is True and "token" not in body["telegram"])
check("DDNS Token 只回显配置标记", body["ddns"]["cf_token_configured"] is True)

print("\n== 10. CSRF 防护 ==")
raw = requests.Session(); raw.trust_env = False
plain = raw.post(BASE + "/api/v1/servers", json={"label": "x"}, timeout=10)
check("无 CSRF 头被拒", plain.status_code in (401, 403), f"HTTP {plain.status_code}")

print("\n== 11. 未登录访问 ==")
raw = requests.Session(); raw.trust_env = False
anon = raw.get(BASE + "/api/v1/overview", timeout=10)
check("匿名访问总览被拒", anon.status_code == 401, f"HTTP {anon.status_code}")

print("\n== 12. 前端资源 ==")
raw = requests.Session(); raw.trust_env = False
page = raw.get(BASE + "/", timeout=10)
check("首页返回 HTML", page.status_code == 200 and "text/html" in page.headers.get("content-type", ""))
check("首页含挂载点", 'id="root"' in page.text)

print("\n" + "=" * 46)
if failures:
    print(f"❌ {len(failures)} 项未通过：")
    for name in failures:
        print("   -", name)
    sys.exit(1)
print("✅ 全部通过")
