"""生成 UI 截图用的演示数据。

只针对本地 dry-run 实例（CDT_DRY_RUN=1），全部字段都是假值：
AK/SK、实例 ID、域名、Token 一律用 example.com / 假号，不含任何真实凭据。

用法：python scripts/seed_demo.py [base_url]
"""

from __future__ import annotations

import json
import sys

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
USERNAME = "admin"
PASSWORD = "test-password-12345"

session = requests.Session()
session.trust_env = False  # 绕过系统代理，回环请求直连


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    headers = {}
    if method != "GET":
        csrf = session.cookies.get("cdt_mgmt_csrf", "")
        if csrf:
            headers["X-CDT-CSRF"] = csrf
    response = session.request(method, BASE + path, json=payload, headers=headers, timeout=30)
    try:
        body = response.json()
    except Exception:
        body = {}
    if response.status_code >= 400:
        print(f"  ! {method} {path} -> {response.status_code} {json.dumps(body, ensure_ascii=False)}")
    return response.status_code, body


# -- 1. 初始化或登录 --------------------------------------------------------
_, status = call("GET", "/api/v1/system/init-status")
if not status.get("initialized"):
    code, body = call("POST", "/api/v1/setup", {"username": USERNAME, "password": PASSWORD})
    print(f"初始化管理员 -> {code}")
else:
    code, body = call("POST", "/api/v1/auth/login", {"username": USERNAME, "password": PASSWORD})
    print(f"登录 -> {code}")
if code != 200:
    sys.exit(f"无法进入已登录状态：{body}")

# -- 2. 服务器 --------------------------------------------------------------
SERVER_SPECS = [
    {
        "label": "香港-主力",
        "region_id": "cn-hongkong",
        "site": "china",
        "instance_id": "i-demo00000000000001",
        "quota_gb": 200,
        "threshold_gb": 188,
        "remark": "白天班，带宽优先",
    },
    {
        "label": "香港-备用",
        "region_id": "cn-hongkong",
        "instance_id": "i-demo00000000000002",
        "quota_gb": 200,
        "threshold_gb": 188,
        "remark": "夜间班",
    },
    {
        "label": "新加坡-二线",
        "region_id": "ap-southeast-1",
        "site": "international",
        "instance_id": "i-demo00000000000003",
        "quota_gb": 200,
        "threshold_gb": 180,
        "remark": "联合换班第三顺位",
    },
    {
        "label": "日本-临时",
        "region_id": "ap-northeast-1",
        "site": "international",
        "instance_id": "i-demo00000000000004",
        "quota_gb": 100,
        "threshold_gb": 92,
        "remark": "单机保活，不进换班组",
    },
    {
        "label": "泰国-灾备",
        "region_id": "ap-southeast-7",
        "instance_id": "i-demo00000000000005",
        "quota_gb": 100,
        "threshold_gb": 90,
        "remark": "单机保活，长期待命",
    },
    {
        "label": "德国-闲置",
        "region_id": "eu-central-1",
        "site": "international",
        "instance_id": "i-demo00000000000006",
        "quota_gb": 200,
        "threshold_gb": 188,
        "remark": "没编进任何流程，手动开关机",
    },
]

server_ids: dict[str, int] = {}
for index, spec in enumerate(SERVER_SPECS, start=1):
    payload = dict(spec)
    payload["access_key_id"] = f"LTAI5tDemoKey{index:04d}"
    payload["access_key_secret"] = f"demo-secret-{index}-not-a-real-key"
    code, body = call("POST", "/api/v1/servers", payload)
    if body.get("id"):
        server_ids[spec["label"]] = body["id"]
print(f"服务器 -> {len(server_ids)} 台")

# -- 3. 单机保活 ------------------------------------------------------------
existing = call("GET", "/api/v1/keepalives")[1].get("items", [])
known = {item["server_id"] for item in existing}
for label in ("日本-临时", "泰国-灾备"):
    server_id = server_ids.get(label)
    if server_id and server_id not in known:
        call("POST", "/api/v1/keepalives", {"server_id": server_id})
print(f"单机保活 -> {len(call('GET', '/api/v1/keepalives')[1].get('items', []))} 条")

# -- 4. 换班组（3 台：10h / 8h / 6h，一个周期 24 小时）---------------------
members = []
for label, hours in (("香港-主力", 10), ("香港-备用", 8), ("新加坡-二线", 6)):
    if label in server_ids:
        members.append({"server_id": server_ids[label], "hours": hours, "enabled": True})

if len(members) >= 2:
    code, body = call(
        "POST",
        "/api/v1/groups",
        {
            "name": "港新联合保活",
            "enabled": True,
            "failover_enabled": True,
            "ddns_record": "cdt-demo.example.com",
            "members": members,
        },
    )
    print(f"换班组 -> {code} id={body.get('id')}")

# -- 5. 配置项（凭据全部假值，渠道保持关闭以免真的外发）--------------------
code, _ = call(
    "POST",
    "/api/v1/settings",
    {
        "timezone": "Asia/Shanghai",
        "traffic_threshold_pct": 92,
        "reconcile_interval": 60,
        "daily_report_enabled": True,
        "daily_report_time": "23:58",
        "notify_on_shift": True,
        "notify_on_failover": True,
        "notify_on_recovery": True,
        "notify_on_breaker": True,
        "notify_on_error": True,
        "notify_on_keepalive": True,
        "ddns": {
            "enabled": False,
            "provider": "cloudflare",
            "cf_auth_mode": "token",
            "cf_email": "noreply@example.com",
            "zone_id": "0123456789abcdef0123456789abcdef",
            "records": ["cdt-demo.example.com", "cdt-demo-2.example.com"],
            "ttl": "120",
            "proxied": False,
            "cf_token": "demo-cloudflare-token-not-real",
        },
        "telegram": {
            "enabled": False,
            "chat_id": "100000001",
            "proxy": "",
            "token": "1234567890:DEMOtokenDEMOtokenDEMOtokenDEMOtoken",
        },
        "email": {
            "enabled": False,
            "host": "smtp.example.com",
            "port": "465",
            "security": "ssl",
            "username": "noreply@example.com",
            "to": "noreply@example.com",
            "password": "demo-password",
        },
    },
)
print(f"配置项 -> {code}")

# -- 6. 跑两轮对账：第一轮拉起实例，第二轮实例就绪后结算，截图里才是「已就绪」
for round_index in (1, 2):
    code, body = call("POST", "/api/v1/reconcile")
    print(f"对账第 {round_index} 轮 -> {code}")

# -- 7. 抓一次费用：dry-run 下由桩返回固定数字，截图里卡片才有「本月 / 余额」
_, servers = call("GET", "/api/v1/servers")
for server in servers.get("items", []):
    code, body = call("POST", f"/api/v1/servers/{server['id']}/billing")
    print(f"费用刷新 {server.get('label')} -> {code} {body.get('monthly_cost')}")

_, overview = call("GET", "/api/v1/overview")
print(
    "总览 -> 服务器 %s 台 / 保活 %s 条 / 换班组 %s 个 / 事件 %s 条"
    % (
        len(overview.get("servers", [])),
        len(overview.get("keepalives", [])),
        len(overview.get("groups", [])),
        len(overview.get("events", [])),
    )
)
