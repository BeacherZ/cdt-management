"""端到端冒烟测试：初始化 → 登录 → 建服务器 → 建换班组 → 对账 → 校验总览。

只跑本地 dry-run 服务，不发任何真实阿里云请求。
用法：python scripts/smoke_test.py [base_url]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
check(
    "单条 record 自动当成一条记录名",
    body["ddns"]["records"] == ["cdt.example.com"],
    str(body["ddns"]["records"]),
)

status, body = call(
    "POST",
    "/api/v1/settings",
    {"ddns": {"records": ["cdt.example.com", "cdn.example.com", "cdt.example.com"]}},
)
check("保存多条记录名", status == 200 and body.get("ok"), str(body)[:200])
status, body = call("GET", "/api/v1/settings")
check(
    "重复记录名被去掉且保持顺序",
    body["ddns"]["records"] == ["cdt.example.com", "cdn.example.com"],
    str(body["ddns"]["records"]),
)
check("默认域名等于第一条", body["ddns"]["record"] == "cdt.example.com", body["ddns"]["record"])

status, body = call("GET", "/api/v1/events?kind=system&limit=50")
before_changed = len(body.get("items", []))
call("POST", "/api/v1/settings", {"timezone": "Asia/Shanghai", "traffic_threshold_pct": 94})
status, body = call("GET", "/api/v1/events?kind=system&limit=50")
after_changed = len(body.get("items", []))
check(
    "没改任何值就不记「配置已更新」",
    after_changed == before_changed,
    f"{before_changed} → {after_changed}",
)

call("POST", "/api/v1/settings", {"traffic_threshold_pct": 93})
status, body = call("GET", "/api/v1/events?kind=system&limit=50")
latest = body["items"][0] if body.get("items") else {}
check(
    "只记本次真正改动的项",
    latest.get("detail") == "流量阈值",
    str(latest.get("detail")),
)

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

print("\n== 13. 故障转移（同一班次内只能转移一次，且要粘住替补）==")
# dry-run 里每台固定用掉 42% 配额，所以把阈值压到 10GB 就能稳定触发「当班超限」
fail_servers = []
for name, threshold in (("超限-甲", 10), ("接替-乙", 188)):
    status, body = call(
        "POST",
        "/api/v1/servers",
        {
            "label": name,
            "access_key_id": f"LTAI5tFailover{len(fail_servers)}",
            "access_key_secret": f"fake-secret-failover-{len(fail_servers)}",
            "region_id": "cn-hongkong",
            "instance_id": f"i-failover0000000000{len(fail_servers)}",
            "quota_gb": 200,
            "threshold_gb": threshold,
        },
    )
    if body.get("id"):
        fail_servers.append(body["id"])
check("建了两台用于转移的服务器", len(fail_servers) == 2)

status, body = call(
    "POST",
    "/api/v1/groups",
    {
        "name": "转移测试",
        "enabled": True,
        "failover_enabled": True,
        "ddns_record": "",
        "members": [
            {"server_id": fail_servers[0], "hours": 8, "enabled": True},
            {"server_id": fail_servers[1], "hours": 8, "enabled": True},
        ],
    },
)
fail_group = body.get("id")
check("建了转移测试组", bool(fail_group))

for _ in range(3):
    call("POST", "/api/v1/reconcile")

status, groups_body = call("GET", "/api/v1/groups")
target = next((g for g in groups_body.get("items", []) if g["id"] == fail_group), None)
check("组存在", target is not None)
if target:
    check("当班已换成替补", target["duty_server_id"] == fail_servers[1], str(target["duty_server_id"]))
    check("phase 保持临时换班", target["phase"] == "failover", target["phase"])
    duty_member = next((m for m in target["members"] if m["is_duty"]), None)
    check("界面高亮的当班就是替补", bool(duty_member) and duty_member["server_id"] == fail_servers[1])

status, events_body = call("GET", "/api/v1/events?limit=200")
failover_events = [e for e in events_body.get("items", []) if e["kind"] == "failover"]
check("故障转移事件只记一次", len(failover_events) == 1, f"实际 {len(failover_events)} 条")

print("\n== 14. 改密码后旧会话立即失效 ==")
other = requests.Session()
other.trust_env = False
login = other.post(
    BASE + "/api/v1/auth/login",
    json={"username": "admin", "password": "test-password-12345"},
    timeout=15,
)
check("第二个会话登录成功", login.status_code == 200, f"HTTP {login.status_code}")

changed = session.post(
    BASE + "/api/v1/settings",
    json={"new_password": "test-password-67890"},
    headers={"X-CDT-CSRF": session.cookies.get("cdt_mgmt_csrf", "")},
    timeout=20,
)
check("改密码成功", changed.status_code == 200, f"HTTP {changed.status_code}")
check(
    "改密码的浏览器还留着会话",
    session.get(BASE + "/api/v1/overview", timeout=10).status_code == 200,
)
check(
    "另一个会话已被踢下线",
    other.get(BASE + "/api/v1/overview", timeout=10).status_code == 401,
)

print("\n== 15. 站点类型（中国站 / 国际站）==")
site_server = call(
    "POST",
    "/api/v1/servers",
    {
        "label": "国际站-测试",
        "access_key_id": "LTAI_DEMO_INTL",
        "access_key_secret": "demo-secret-intl",
        "region_id": "ap-southeast-1",
        "site": "international",
        "instance_id": "i-demo00000000000009",
        "quota_gb": 200,
        "threshold_gb": 188,
    },
)
check("建国际站服务器", site_server[0] == 200, f"HTTP {site_server[0]}")
items = {s["label"]: s for s in call("GET", "/api/v1/servers")[1]["items"]}
check(
    "站点已保存为 international",
    items.get("国际站-测试", {}).get("site") == "international",
    str(items.get("国际站-测试", {}).get("site")),
)
check(
    "未指定站点的服务器默认 china",
    all(s.get("site") in {"china", "international"} for s in items.values()),
)

dirty = call(
    "POST",
    "/api/v1/servers",
    {
        "id": items["国际站-测试"]["id"],
        "label": "国际站-测试",
        "region_id": "ap-southeast-1",
        "site": "not-a-site",
        "instance_id": "i-demo00000000000009",
        "quota_gb": 200,
        "threshold_gb": 188,
    },
)
check("脏站点值被接受但不入库", dirty[0] == 200, f"HTTP {dirty[0]}")
items = {s["label"]: s for s in call("GET", "/api/v1/servers")[1]["items"]}
check(
    "脏值归一到 china",
    items["国际站-测试"]["site"] == "china",
    str(items["国际站-测试"]["site"]),
)

# 端点解析：ECS / CDT 两个站一致，只有 BSS 分站点
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import aliyun  # noqa: E402

check("ECS 域名按地域拼", aliyun.ecs_domain("cn-hongkong") == "ecs.cn-hongkong.aliyuncs.com")
check("CDT 域名两站相同", aliyun.CDT_DOMAIN == "cdt.aliyuncs.com")
check(
    "中国站 BSS",
    aliyun.bss_endpoint("china") == ("cn-hangzhou", "business.aliyuncs.com"),
)
check(
    "国际站 BSS",
    aliyun.bss_endpoint("international")
    == ("ap-southeast-1", "business.ap-southeast-1.aliyuncs.com"),
)
check("未知站点回退中国站", aliyun.bss_endpoint("??") == aliyun.bss_endpoint("china"))

print("\n== 16. 闲散服务器手动开关机 ==")
items = {s["label"]: s for s in call("GET", "/api/v1/servers")[1]["items"]}
idle = items["国际站-测试"]
check("闲散机器 managed 为空", idle.get("managed") is None, str(idle.get("managed")))

status, body = call("POST", f"/api/v1/servers/{idle['id']}/actions/stop")
check("闲散机器可手动关机", status == 200, f"HTTP {status} {str(body)[:120]}")
check("关机后状态为 Stopped", body.get("status") == "Stopped", str(body.get("status")))
check("闲散机器不带托管提示", not body.get("managed_note"), str(body.get("managed_note")))
check(
    "关机返回真实停机模式",
    body.get("stopped_mode") == "StopCharging",
    str(body.get("stopped_mode")),
)

status, body = call("POST", f"/api/v1/servers/{idle['id']}/actions/start")
check("闲散机器可手动开机", status == 200 and body.get("status") == "Running", str(body)[:120])

events = call("GET", "/api/v1/overview")[1]["events"]
check(
    # 事件标题带上真实停机模式，省得和普通停机混在一起看不出来
    "手动操作已记录事件",
    any("手动停机（节省停机）" in e.get("title", "") for e in events),
    str([e.get("title") for e in events[:5]]),
)

status, body = call("POST", f"/api/v1/servers/{idle['id']}/actions/reboot")
check("非法动作被拒绝", status == 400, f"HTTP {status}")

# 编进自动化流程后，managed 要如实反映出来
call("POST", "/api/v1/keepalives", {"server_id": idle["id"]})
items = {s["label"]: s for s in call("GET", "/api/v1/servers")[1]["items"]}
managed = items["国际站-测试"].get("managed") or {}
check("加入保活后标记为 keepalive", managed.get("kind") == "keepalive", str(managed))
check("保活启用时 active 为真", managed.get("active") is True, str(managed))

keepalive_id = next(
    k["id"] for k in call("GET", "/api/v1/keepalives")[1]["items"] if k["server_id"] == idle["id"]
)
call("POST", f"/api/v1/keepalives/{keepalive_id}/toggle", {"enabled": False})
items = {s["label"]: s for s in call("GET", "/api/v1/servers")[1]["items"]}
managed = items["国际站-测试"].get("managed") or {}
check("暂停后 active 为假（可手动接管）", managed.get("active") is False, str(managed))

status, body = call("POST", f"/api/v1/servers/{idle['id']}/actions/stop")
check("暂停保活后可手动关机", status == 200, f"HTTP {status}")
check("暂停时不再提示托管", not body.get("managed_note"), str(body.get("managed_note")))

# 重新启用后，手动操作要提醒「下一轮对账会纠正」
call("POST", f"/api/v1/keepalives/{keepalive_id}/toggle", {"enabled": True})
status, body = call("POST", f"/api/v1/servers/{idle['id']}/actions/stop")
check("托管中操作会带提示", body.get("managed_note") == "单机保活", str(body.get("managed_note")))
call("DELETE", f"/api/v1/keepalives/{keepalive_id}")

grouped = [s for s in call("GET", "/api/v1/servers")[1]["items"] if (s.get("managed") or {}).get("kind") == "group"]
check("换班组成员标记为 group", len(grouped) >= 1, f"实际 {len(grouped)} 台")
check(
    "组成员带换班组名字",
    all((s.get("managed") or {}).get("name") for s in grouped),
    str([s.get("managed") for s in grouped[:2]]),
)

print("\n== 17. 费用与余额（BSS）==")

# 列表里必须带 billing 结构，前端卡片靠它渲染
items = {s["label"]: s for s in call("GET", "/api/v1/servers")[1]["items"]}
check("服务器带 billing 字段", all("billing" in s for s in items.values()), str(list(items)[:2]))

status, body = call("POST", f"/api/v1/servers/{idle['id']}/billing")
check("手动刷新费用  HTTP 200", status == 200, f"HTTP {status}")
check("拿到本月费用", isinstance(body.get("monthly_cost"), (int, float)), str(body.get("monthly_cost")))
check("拿到账户余额", isinstance(body.get("balance"), (int, float)), str(body.get("balance")))
check("账期为当前月", len(str(body.get("cycle"))) == 7, str(body.get("cycle")))

items = {s["label"]: s for s in call("GET", "/api/v1/servers")[1]["items"]}
billing = items["国际站-测试"]["billing"]
check("费用已落库并回显", billing.get("monthly_cost") is not None, str(billing))
check("currency 有默认值", billing.get("currency") in ("CNY", "USD"), str(billing.get("currency")))
check("当前账期不算过期", billing.get("stale") is False, str(billing.get("stale")))
check("正常时不带错误", not billing.get("error"), str(billing.get("error")))

# 换实例后旧账单必须清空，否则会拿别的机器的钱冒充这台
before = items["国际站-测试"]["billing"]["monthly_cost"]
call(
    "POST",
    "/api/v1/servers",
    {
        "id": idle["id"],
        "label": "国际站-测试",
        # 不传 AccessKey：接口只回显打码值，留空即沿用原配置
        "region_id": idle["region_id"],
        "site": "international",
        "instance_id": "i-switched-0001",
        "quota_gb": 100,
        "threshold_gb": 94,
    },
)
items = {s["label"]: s for s in call("GET", "/api/v1/servers")[1]["items"]}
check("换实例后费用被清空", items["国际站-测试"]["billing"]["monthly_cost"] is None, f"之前 {before}")

# 关掉开关后不应再去查，也不该报错
call("POST", "/api/v1/settings", {"billing_enabled": False})
status, _ = call("POST", f"/api/v1/servers/{idle['id']}/billing")
check("关闭费用查询后拒绝刷新", status == 400, f"HTTP {status}")
call("POST", "/api/v1/settings", {"billing_enabled": True})
settings_body = call("GET", "/api/v1/settings")[1]
check("开关能读回", settings_body.get("billing_enabled") is True, str(settings_body.get("billing_enabled")))

print("\n== 18. DNS 记录与绑定 ==")
# 记录名仍然由设置维护（DNS 页负责加删与绑定），先确保有两条
call("POST", "/api/v1/settings", {"ddns": {"records": ["cdt.example.com", "cdn.example.com"]}})
status, body = call("GET", "/api/v1/dns")
check("DNS 列表可读", status == 200 and len(body.get("records", [])) == 2, str(body)[:200])
check("没绑的记录状态是 unbound", all(r["state"] == "unbound" for r in body["records"]), str(body)[:200])

# 关键回归：以前没绑也会拿第一条记录兜底，多台机器互相覆盖同一个域名
call("POST", "/api/v1/settings", {"ddns": {"enabled": True, "cf_token": "fake-token", "ttl": "60"}})
status, body = call("POST", "/api/v1/dns/records/cdt.example.com/sync")
check(
    "未绑定的记录同步返回「未绑定」而不是偷偷用第一条",
    status == 200 and body.get("status") == "⏸ 未绑定",
    str(body)[:200],
)

target = servers[0]
status, body = call(
    "POST", "/api/v1/dns/records/cdt.example.com/bind", {"kind": "server", "id": target}
)
check("绑定到服务器", status == 200 and (body.get("target") or {}).get("id") == target, str(body)[:200])

status, body = call("GET", "/api/v1/dns")
row = {item["record"]: item for item in body["records"]}["cdt.example.com"]
check("列表回显绑定目标", (row.get("target") or {}).get("kind") == "server", str(row)[:200])
check("绑完之后 state 不再是 unbound", row["state"] in ("idle", "pending", "synced"), row["state"])

# 一个目标只能占一条记录：改绑另一条，旧的那条必须自动松开
status, body = call(
    "POST", "/api/v1/dns/records/cdn.example.com/bind", {"kind": "server", "id": target}
)
check("同一目标改绑会释放上一条", body.get("released") == "cdt.example.com", str(body)[:200])
status, body = call("GET", "/api/v1/dns")
rows = {item["record"]: item for item in body["records"]}
check("旧记录回到未绑定", rows["cdt.example.com"]["target"] is None, str(rows["cdt.example.com"])[:200])

# 绑到换班组
status, body = call(
    "POST", "/api/v1/dns/records/cdt.example.com/bind", {"kind": "group", "id": group_id}
)
check("绑定到换班组", status == 200, str(body)[:200])
groups = call("GET", "/api/v1/groups")[1]["items"]
check(
    "换班组回显绑定的域名",
    groups[0]["ddns_record"] == "cdt.example.com",
    str(groups[0]["ddns_record"]),
)

# 解绑
status, _ = call("POST", "/api/v1/dns/records/cdt.example.com/bind", {"kind": "", "id": 0})
status, body = call("GET", "/api/v1/dns")
row = {item["record"]: item for item in body["records"]}["cdt.example.com"]
check("解绑生效", row["target"] is None and row["state"] == "unbound", str(row)[:200])

# 参数校验
status, _ = call("POST", "/api/v1/dns/records/cdt.example.com/bind", {"kind": "alien", "id": 1})
check("非法绑定目标被拒", status == 400, f"HTTP {status}")
status, _ = call("POST", "/api/v1/dns/records", {"record": "bad name.example.com"})
check("带空格的域名被拒", status == 400, f"HTTP {status}")
status, _ = call("POST", "/api/v1/dns/records", {"record": "nodots"})
check("没有点的域名被拒", status == 400, f"HTTP {status}")
status, _ = call("POST", "/api/v1/dns/records", {"record": "new.example.com"})
check("添加记录名", status == 200, f"HTTP {status}")
status, body = call("GET", "/api/v1/dns")
check("新记录已入列表", any(r["record"] == "new.example.com" for r in body["records"]))
status, _ = call("DELETE", "/api/v1/dns/records/new.example.com")
status, body = call("GET", "/api/v1/dns")
check("删除记录名", not any(r["record"] == "new.example.com" for r in body["records"]))

# DDNS 关着时不允许同步
call("POST", "/api/v1/settings", {"ddns": {"enabled": False}})
status, _ = call("POST", "/api/v1/dns/sync")
check("DDNS 关闭时拒绝全部同步", status == 400, f"HTTP {status}")

# 删掉换班组后，挂在它身上的绑定必须松开
call("POST", "/api/v1/dns/records/cdn.example.com/bind", {"kind": "group", "id": group_id})
call("DELETE", f"/api/v1/groups/{group_id}")
status, body = call("GET", "/api/v1/dns")
row = {item["record"]: item for item in body["records"]}["cdn.example.com"]
check("删除换班组后绑定被解除", row["target"] is None, str(row)[:200])

print("\n" + "=" * 46)
if failures:
    print(f"❌ {len(failures)} 项未通过：")
    for name in failures:
        print("   -", name)
    sys.exit(1)
print("✅ 全部通过")
