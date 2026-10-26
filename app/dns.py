"""DNS 记录与绑定。

这里是「域名 → 机器」的唯一出入口。规则只有一条：**绑定必须是显式的**。

以前是「没指定就用第一条记录」，结果两条保活任务、两个换班组会抢着往同一个域名上写，
谁最后跑完谁说了算 —— 这就是瞎绑。现在没绑的记录一律不动，界面上也明说「未绑定」。

三层分工：
  ddns.py   只跟 Cloudflare 说话（查 Zone、找 Record ID、PUT A 记录）
  dns.py    管绑定关系、谁该指向哪个 IP、什么时候需要更新（本文件）
  shift.py  换班/保活跑到该同步的时机时调 sync_record()，拿回状态塞进通知
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import ddns, repo

log = logging.getLogger("cdt-management.dns")

TARGET_SERVER = "server"
TARGET_GROUP = "group"
TARGET_NONE = ""

STATUS_UNBOUND = "⏸ 未绑定"
STATUS_IDLE = "⏸ 目标未运行"
STATUS_NO_IP = "⚠️ 目标没有公网 IP"

STATE_UNBOUND = "unbound"
STATE_IDLE = "idle"
STATE_PENDING = "pending"
STATE_SYNCED = "synced"
STATE_ERROR = "error"


def normalize(name: str | None) -> str:
    return (name or "").strip()


# --------------------------------------------------------------------------
# 登记：设置里的记录名 ⇄ dns_records 表
# --------------------------------------------------------------------------

def sync_registry(config: dict[str, str]) -> None:
    """把设置里的记录名同步进表里，设置里删掉的就从表里删掉。"""
    names = ddns.records(config)
    keep = set(names)
    for name in names:
        repo.ensure_dns_record(name)
    for row in repo.list_dns_records():
        if row["record"] not in keep:
            repo.delete_dns_record(row["record"])


def adopt_legacy(config: dict[str, str]) -> None:
    """老版本把域名直接存在换班组上，没有绑定表。启动时接过来，只接没绑过的。"""
    names = {name.lower() for name in ddns.records(config)}
    for group in repo.list_groups():
        legacy = normalize(group.get("ddns_record"))
        if not legacy:
            continue
        if legacy.lower() not in names:
            # 设置里已经没有这个名字了，说明是升级前删掉的残留
            continue
        row = repo.get_dns_record(legacy)
        if row is not None and row.get("target_kind"):
            continue
        if repo.dns_record_of(TARGET_GROUP, int(group["id"])):
            continue
        repo.bind_dns_record(legacy, TARGET_GROUP, int(group["id"]))
        log.info("接手换班组「%s」的域名 %s", group.get("name"), legacy)


def add_record(name: str, config: dict[str, str]) -> str:
    name = normalize(name)
    if not name:
        raise ValueError("域名不能为空")
    if any(ch.isspace() for ch in name):
        raise ValueError("域名不能带空格")
    if "." not in name:
        raise ValueError("域名至少要有一层，形如 cdt.example.com")

    names = ddns.records(config)
    if name.lower() not in {item.lower() for item in names}:
        names.append(name)
    repo.set_settings(
        {
            "ddns_records": json.dumps(names, ensure_ascii=False),
            # 老字段留个镜像，仅为了降级回旧版本时还能读
            "ddns_record": names[0] if names else "",
        }
    )
    repo.ensure_dns_record(name)
    return name


def remove_record(name: str, config: dict[str, str]) -> None:
    name = normalize(name)
    names = [item for item in ddns.records(config) if item.lower() != name.lower()]
    repo.set_settings(
        {
            "ddns_records": json.dumps(names, ensure_ascii=False),
            "ddns_record": names[0] if names else "",
        }
    )
    repo.delete_dns_record(name)


# --------------------------------------------------------------------------
# 目标解析
# --------------------------------------------------------------------------

def _servers() -> dict[int, dict[str, Any]]:
    return {int(server["id"]): server for server in repo.list_servers()}


def _group_runner(group_id: int, now_ts: float, servers: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """换班组此刻真正在跑哪台。故障转移过的班次以运行记录为准，跟界面高亮保持一致。"""
    from . import shift  # 延迟导入：shift 反过来要用本模块，顶层导入会成环

    group = repo.get_group(group_id)
    if group is None:
        return None
    picked = shift.duty_at(group, now_ts)
    if not picked:
        return None
    server_id = int(picked[0]["server_id"])
    run = repo.get_run(group_id, str(picked[1])) or {}
    if run.get("failover") and int(run.get("duty_server_id") or 0):
        server_id = int(run["duty_server_id"])
    return servers.get(server_id)


def target_info(
    kind: str,
    target_id: int,
    servers: dict[int, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """绑定的目标现在是什么样：叫什么、在不在跑、当前 IP 是多少。"""
    if not kind or not target_id:
        return None
    servers = servers if servers is not None else _servers()
    now_ts = time.time() if now_ts is None else now_ts

    if kind == TARGET_SERVER:
        server = servers.get(int(target_id))
        if server is None:
            return None
        return {
            "kind": TARGET_SERVER,
            "id": int(target_id),
            "label": server.get("label") or f"#{target_id}",
            "status": server.get("instance_status") or "Unknown",
            "running": (server.get("instance_status") == "Running"),
            "ip": (server.get("public_ip") or "").strip(),
        }

    if kind == TARGET_GROUP:
        group = repo.get_group(int(target_id))
        if group is None:
            return None
        runner = _group_runner(int(target_id), now_ts, servers)
        return {
            "kind": TARGET_GROUP,
            "id": int(target_id),
            "label": group.get("name") or f"#{target_id}",
            "status": (runner or {}).get("instance_status") or "Unknown",
            "running": bool(runner) and runner.get("instance_status") == "Running",
            "ip": ((runner or {}).get("public_ip") or "").strip(),
            "duty": (runner or {}).get("label") or "",
        }

    return None


def desired_ip(target: dict[str, Any] | None) -> str:
    """这个目标现在该被解析到哪个 IP。没在跑就不该动解析，返回空串。"""
    if not target or not target.get("running"):
        return ""
    ip = str(target.get("ip") or "").strip()
    return ip if ddns.is_public_ipv4(ip) else ""


def state_of(row: dict[str, Any], target: dict[str, Any] | None) -> str:
    if target is None:
        return STATE_UNBOUND
    live = desired_ip(target)
    if not live:
        return STATE_IDLE
    last = str(row.get("last_ip") or "")
    status = str(row.get("last_status") or "")
    if last == live and status.startswith("✅"):
        return STATE_SYNCED
    if status and not status.startswith("✅") and last == live:
        return STATE_ERROR
    return STATE_PENDING


# --------------------------------------------------------------------------
# 同步
# --------------------------------------------------------------------------

def sync_record(
    record: str,
    config: dict[str, str],
    ip: str | None = None,
    force: bool = False,
) -> ddns.DDNSResult:
    """把某条记录同步到它该指向的 IP。

    ip 留空时自己算（目标没在跑就不同步）。换班/保活那边已经知道 IP，直接传进来。
    """
    record = normalize(record)
    if not record:
        return ddns.DDNSResult(STATUS_UNBOUND)
    row = repo.get_dns_record(record)
    if row is None:
        return ddns.DDNSResult(STATUS_UNBOUND, f"{record} 不在记录名列表里")

    if ip is None:
        target = target_info(row.get("target_kind") or "", int(row.get("target_id") or 0))
        if target is None:
            repo.set_dns_state(record, row.get("last_ip") or "", STATUS_UNBOUND)
            return ddns.DDNSResult(STATUS_UNBOUND, f"{record} 还没绑定", ip="")
        ip = desired_ip(target)
        if not ip:
            status = STATUS_NO_IP if target.get("running") else STATUS_IDLE
            repo.set_dns_state(record, row.get("last_ip") or "", status)
            return ddns.DDNSResult(status, f"{target.get('label')} 没有可用的公网 IP", ip="")

    # 每条记录自己的「上次同步到哪」，不能借用全局那一个，否则会互相覆盖
    scoped = {
        **config,
        "ddns_last_ip": row.get("last_ip") or "",
        "ddns_last_status": row.get("last_status") or "",
    }
    result = ddns.sync(ip, scoped, force=force, record=record)
    if result.status in (ddns.STATUS_SYNCED, ddns.STATUS_UNCHANGED):
        repo.set_dns_state(record, ip, result.status)
    else:
        # 失败时保留上次成功的 IP：界面上「现在解析到哪」要能看出来是旧的
        repo.set_dns_state(record, row.get("last_ip") or "", result.status)
    return result


def reconcile(config: dict[str, str], force: bool = False) -> list[dict[str, Any]]:
    """巡检所有绑定：目标在跑、但 IP 跟上次不一样时才真打接口。

    稳定的机器这里一次网络请求都不会发，所以放在每分钟的对账里也不心疼。
    """
    if config.get("ddns_enabled") != "1":
        return []

    sync_registry(config)
    servers = _servers()
    now_ts = time.time()
    results: list[dict[str, Any]] = []

    for row in repo.list_dns_records():
        kind = row.get("target_kind") or ""
        if not kind:
            continue
        record = str(row["record"])
        target = target_info(kind, int(row.get("target_id") or 0), servers, now_ts)
        if target is None:
            continue
        live = desired_ip(target)
        if not live:
            continue
        if not force and live == (row.get("last_ip") or "") and (row.get("last_status") or "").startswith("✅"):
            continue

        previous = row.get("last_ip") or ""
        result = sync_record(record, config, ip=live, force=force)
        results.append(
            {"record": record, "target": target.get("label"), "ip": live, "status": result.status}
        )
        if result.status in (ddns.STATUS_SYNCED, ddns.STATUS_UNCHANGED) and previous != live:
            repo.add_event(
                "dns",
                f"{record}：解析已更新",
                f"{previous or '—'} → {live}（{target.get('label')}）",
            )
        elif result.status not in (ddns.STATUS_SYNCED, ddns.STATUS_UNCHANGED):
            # 同一类失败不要每分钟刷一条
            flag = f"dns_fail:{record}:{result.status}"
            if not repo.get_flag(flag):
                repo.set_flag(flag)
                repo.add_event(
                    "error",
                    f"{record}：{result.status}",
                    result.detail or target.get("label") or "",
                    level="warning",
                )
        else:
            repo.clear_flag(f"dns_fail:{record}:{result.status}")
    return results


# --------------------------------------------------------------------------
# 绑定
# --------------------------------------------------------------------------

def bind(record: str, kind: str, target_id: int) -> dict[str, Any]:
    record = normalize(record)
    if record == "":
        raise ValueError("域名不能为空")
    if repo.get_dns_record(record) is None:
        raise ValueError(f"{record} 不在记录名列表里，请先在设置里添加")
    if kind in ("", None):
        repo.bind_dns_record(record, TARGET_NONE, 0)
        # 解绑后清掉残留的解析状态，界面别再把旧 IP 显示成「当前解析」
        repo.set_dns_state(record, "", "")
        return {"record": record, "target": None}

    if kind not in (TARGET_SERVER, TARGET_GROUP):
        raise ValueError("只能绑定到服务器或换班组")
    if kind == TARGET_SERVER and repo.get_server(int(target_id)) is None:
        raise ValueError("服务器不存在")
    if kind == TARGET_GROUP and repo.get_group(int(target_id)) is None:
        raise ValueError("换班组不存在")

    # 这个目标原来占的那条会被松掉，避免一个目标挂两个域名
    previous = repo.dns_record_of(kind, int(target_id))
    repo.bind_dns_record(record, kind, int(target_id))
    target = target_info(kind, int(target_id))
    label = (target or {}).get("label") or f"#{target_id}"
    detail = f"{record} → {label}"
    if previous and previous != record:
        detail += f"（原 {previous} 已解绑）"
    repo.add_event("dns", "DNS 绑定已更新", detail)
    return {"record": record, "target": target, "released": previous if previous != record else ""}


# --------------------------------------------------------------------------
# 界面快照
# --------------------------------------------------------------------------

def snapshot(config: dict[str, str], now_ts: float | None = None) -> dict[str, Any]:
    sync_registry(config)
    servers = _servers()
    now_ts = time.time() if now_ts is None else now_ts

    records = []
    for row in repo.list_dns_records():
        record = str(row["record"])
        target = target_info(row.get("target_kind") or "", int(row.get("target_id") or 0), servers, now_ts)
        records.append(
            {
                "record": record,
                "target": target,
                "last_ip": row.get("last_ip") or "",
                "last_status": row.get("last_status") or "",
                "last_at": int(row.get("last_at") or 0),
                "desired_ip": desired_ip(target),
                "state": state_of(row, target),
            }
        )

    return {
        "enabled": config.get("ddns_enabled") == "1",
        "provider": config.get("ddns_provider") or "cloudflare",
        "configured": bool(config.get("ddns_cf_token"))
        or bool(config.get("ddns_cf_email") and config.get("ddns_cf_key")),
        "ttl": config.get("ddns_ttl") or "60",
        "proxied": config.get("ddns_proxied") == "1",
        "records": records,
    }


def record_books() -> dict[str, dict[str, Any]]:
    """每台服务器 / 每个换班组当前占着哪条记录，界面用来显示「已绑定 xxx」。"""
    server_map: dict[int, str] = {}
    group_map: dict[int, str] = {}
    for row in repo.list_dns_records():
        kind = row.get("target_kind") or ""
        if not kind:
            continue
        if kind == TARGET_SERVER:
            server_map[int(row["target_id"])] = str(row["record"])
        elif kind == TARGET_GROUP:
            group_map[int(row["target_id"])] = str(row["record"])
    return {"servers": server_map, "groups": group_map}
