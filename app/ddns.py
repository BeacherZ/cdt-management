"""Cloudflare DDNS。

行为对齐用户脚本：先取 Record ID，再 PUT 更新 A 记录，失败重试 3 次，
并把「已同步 / IP 无效跳过 / 获取 ID 失败 / 更新失败」这几种结果回传给通知文案。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass

import requests

from . import settings

log = logging.getLogger("cdt-management.ddns")

API = "https://api.cloudflare.com/client/v4"

STATUS_SYNCED = "✅ 已同步"
# IP 没变、不需要动记录：对外文案和「已同步」统一，通知和事件进度里出现「无变化」
# 反而让用户以为没查。内部靠 DDNSResult.changed 区分，不靠文案。
STATUS_UNCHANGED = "✅ 已同步"
STATUS_INVALID_IP = "⚠️ IP 无效跳过"
STATUS_NO_RECORD_ID = "❌ 获取 ID 失败"
STATUS_UPDATE_FAILED = "❌ 更新失败"
STATUS_DISABLED = "⏸ DDNS 未启用"
STATUS_ERROR = "❌ DDNS 异常"


@dataclass
class DDNSResult:
    status: str
    detail: str = ""
    changed: bool = False
    ip: str = ""

    def __str__(self) -> str:  # 直接塞进通知文案
        return self.status


# --------------------------------------------------------------------------
# 记录名
# --------------------------------------------------------------------------

def records(config: dict[str, str]) -> list[str]:
    """全部 DDNS 记录名。第一条是默认记录：单机保活和没单独指定域名的换班组都用它。

    ddns_records 存 JSON 数组；老库里只有单条 ddns_record 时自动当成一条，
    所以升级不需要迁移脚本。
    """
    raw = (config.get("ddns_records") or "").strip()
    items: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # 手改过配置文件、写成一行一条的也认
            parsed = raw.splitlines()
        if isinstance(parsed, list):
            items = [str(item).strip() for item in parsed]
    items = [item for item in items if item]
    if not items:
        single = (config.get("ddns_record") or "").strip()
        if single:
            items = [single]
    # 去重但保留顺序
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            unique.append(item)
    return unique


def default_record(config: dict[str, str]) -> str:
    items = records(config)
    return items[0] if items else ""


def _record_of(config: dict[str, str], record: str | None = None) -> str:
    return (record or "").strip() or default_record(config)


def _headers(config: dict[str, str]) -> dict[str, str]:
    mode = (config.get("ddns_cf_auth_mode") or "token").lower()
    if mode == "global_key":
        email = config.get("ddns_cf_email", "")
        key = config.get("ddns_cf_key", "")
        if not email or not key:
            raise ValueError("Cloudflare Global API Key 模式需要同时填写邮箱和 Key")
        return {"X-Auth-Email": email, "X-Auth-Key": key, "Content-Type": "application/json"}
    token = config.get("ddns_cf_token", "")
    if not token:
        raise ValueError("Cloudflare API Token 未配置")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _force_ipv4() -> None:
    try:
        import requests.packages.urllib3.util.connection as conn

        conn.allowed_gai_family = lambda: socket.AF_INET
    except Exception:
        pass


def is_public_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return False
    return address.version == 4 and not (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    )


def _root_domain(record: str) -> str:
    parts = record.strip().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else record


def _resolve_zone_id(headers: dict[str, str], config: dict[str, str], record: str) -> str:
    zone_id = (config.get("ddns_zone_id") or "").strip()
    if zone_id:
        return zone_id
    if not record:
        raise ValueError("DDNS 记录名未配置")
    root = _root_domain(record)
    response = requests.get(
        f"{API}/zones",
        headers=headers,
        params={"name": root, "status": "active"},
        timeout=settings.HTTP_TIMEOUT,
    )
    payload = response.json() if response.content else {}
    zones = payload.get("result") or []
    if response.status_code != 200 or not zones:
        raise ValueError(f"无法自动识别 {root} 的 Zone ID，请手动填写")
    return str(zones[0]["id"])


def _find_record_id(headers: dict[str, str], zone_id: str, record: str) -> str | None:
    response = requests.get(
        f"{API}/zones/{zone_id}/dns_records",
        headers=headers,
        params={"name": record, "type": "A"},
        timeout=settings.HTTP_TIMEOUT,
    )
    if response.status_code != 200:
        log.warning("查询 DNS 记录失败 HTTP %s", response.status_code)
        return None
    payload = response.json()
    if payload.get("success") and payload.get("result"):
        return str(payload["result"][0]["id"])
    return None


def sync(
    ip: str, config: dict[str, str], force: bool = False, record: str | None = None
) -> DDNSResult:
    """把 ip 同步到 Cloudflare A 记录。

    record 为空时用默认记录（第一条）；换班组可以指定自己那一条。
    """
    if config.get("ddns_enabled") != "1":
        return DDNSResult(STATUS_DISABLED, ip=ip)

    name = _record_of(config, record)
    if not name:
        return DDNSResult(STATUS_ERROR, "记录名未配置", ip=ip)
    if not is_public_ipv4(ip):
        log.warning("DDNS 跳过：IP 不可用 (%s)", ip)
        return DDNSResult(STATUS_INVALID_IP, f"IP={ip or '空'}", ip=ip)

    if settings.DRY_RUN:
        # 演示/截图模式：照常走完上面的校验，但不碰真实的 Cloudflare
        log.info("DRY RUN：假装已把 %s 同步到 %s", name, ip)
        return DDNSResult(STATUS_SYNCED, f"{name} → {ip}", changed=True, ip=ip)

    # 记录名与 IP 都没变就免掉一次 API 调用
    if (
        not force
        and config.get("ddns_last_ip") == ip
        and (config.get("ddns_last_status") or "").startswith("✅")
    ):
        return DDNSResult(STATUS_UNCHANGED, "IP 未变化", ip=ip)

    _force_ipv4()
    # 认证/Zone 这类配置问题重试也没用，先一次性判掉，别白等 6 秒
    try:
        headers = _headers(config)
        zone_id = _resolve_zone_id(headers, config, name)
    except ValueError as exc:
        return DDNSResult(STATUS_ERROR, str(exc), ip=ip)
    except Exception as exc:  # noqa: BLE001 - 网络异常
        log.warning("DDNS 初始化失败: %s", exc)
        return DDNSResult(STATUS_ERROR, f"无法连接 Cloudflare：{exc}", ip=ip)

    last_error = ""
    for attempt in range(1, 4):
        try:
            record_id = _find_record_id(headers, zone_id, name)
            if not record_id:
                last_error = "记录不存在或查询失败"
                time.sleep(2)
                continue

            response = requests.put(
                f"{API}/zones/{zone_id}/dns_records/{record_id}",
                headers=headers,
                json={
                    "type": "A",
                    "name": name,
                    "content": ip,
                    "ttl": int(config.get("ddns_ttl") or 60),
                    "proxied": config.get("ddns_proxied") == "1",
                },
                timeout=settings.HTTP_TIMEOUT,
            )
            payload = response.json() if response.content else {}
            if response.status_code == 200 and payload.get("success"):
                log.info("DDNS 更新成功 %s -> %s", name, ip)
                return DDNSResult(STATUS_SYNCED, f"{name} → {ip}", changed=True, ip=ip)
            last_error = f"HTTP {response.status_code}"
            log.warning("DDNS 第 %s 次更新失败: %s", attempt, last_error)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.warning("DDNS 第 %s 次更新异常: %s", attempt, exc)
        time.sleep(2)

    if "Zone ID" in last_error:
        return DDNSResult(STATUS_ERROR, last_error, ip=ip)
    if "记录" in last_error:
        return DDNSResult(STATUS_NO_RECORD_ID, last_error, ip=ip)
    return DDNSResult(STATUS_UPDATE_FAILED, last_error, ip=ip)


def test(config: dict[str, str]) -> DDNSResult:
    """连通性自检：只读，不改记录。多条记录逐条查一遍。"""
    names = records(config)
    if not names:
        return DDNSResult(STATUS_ERROR, "记录名未配置")
    if settings.DRY_RUN:
        return DDNSResult("✅ 连接正常", f"{len(names)} 条记录（DRY RUN，未真的访问 Cloudflare）")
    try:
        headers = _headers(config)
        zone_id = _resolve_zone_id(headers, config, names[0])
    except Exception as exc:  # noqa: BLE001
        return DDNSResult(STATUS_ERROR, str(exc))

    missing = [name for name in names if not _find_record_id(headers, zone_id, name)]
    if missing:
        return DDNSResult(STATUS_NO_RECORD_ID, f"{'、'.join(missing)} 未找到 A 记录")
    detail = f"Zone {zone_id[:8]}… / {names[0]} 已就绪" if len(names) == 1 else f"{len(names)} 条记录均就绪"
    return DDNSResult("✅ 连接正常", detail)
