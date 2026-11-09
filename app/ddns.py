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
    # 更新前记录里的值：事件详情要写「旧 → 新」，通知里也能看出改了什么。
    # verify=True 时取自 Cloudflare 的实时内容；其余情况取本地缓存的上次 IP。
    previous: str = ""

    def __str__(self) -> str:  # 直接塞进通知文案
        return self.status


# --------------------------------------------------------------------------
# 记录名
# --------------------------------------------------------------------------

def is_valid_record(name: str) -> bool:
    """能不能当域名用：至少一层点、不带空白。

    加这一层是因为 records() 的结果会直接拿去删绑定表 —— 把库里写坏的值
    （比如 "not json at all"）当成记录名，会先把真记录删掉，再建一条垃圾行。
    """
    name = (name or "").strip()
    return bool(name) and "." in name and not any(ch.isspace() for ch in name)


def parse_records(config: dict[str, str]) -> list[str] | None:
    """解析 ddns_records 里的记录名。

    返回 None 表示「值在、但读不懂」（不是列表，或列表里混了不是域名的东西）。
    调用方必须把 None 和空列表分开：空列表是用户清空，None 是配置坏了 ——
    后者不许拿来删绑定表，否则手改一次库就把所有域名解绑了。
    """
    raw = (config.get("ddns_records") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # 手改过配置文件、写成一行一条的也认
        parsed = raw.splitlines()
    if not isinstance(parsed, list):
        return None
    items = [str(item).strip() for item in parsed]
    items = [item for item in items if item]
    if any(not is_valid_record(item) for item in items):
        return None
    return items


def records(config: dict[str, str]) -> list[str]:
    """全部 DDNS 记录名。第一条是默认记录：单机保活和没单独指定域名的换班组都用它。

    ddns_records 存 JSON 数组；老库里只有单条 ddns_record 时自动当成一条，
    所以升级不需要迁移脚本。
    """
    items = parse_records(config)
    if items is None:
        # 读不懂就当没有：展示层退回「未配置」，不把垃圾值当域名去解析
        items = []
    if not items:
        single = (config.get("ddns_record") or "").strip()
        if is_valid_record(single):
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


def _find_record(
    headers: dict[str, str], zone_id: str, record: str
) -> tuple[str, str] | None:
    """查询记录，返回 (记录 ID, 当前指向的 IP)。

    顺手把 content 取回来：关键时刻（开机/换班）要先比对 Cloudflare 上的真实值
    再决定要不要 PUT，光有 ID 做不到「IP 没变就不动记录」。
    """
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
        item = payload["result"][0]
        return str(item["id"]), str(item.get("content") or "")
    return None


def sync(
    ip: str,
    config: dict[str, str],
    force: bool = False,
    record: str | None = None,
    verify: bool = False,
) -> DDNSResult:
    """把 ip 同步到 Cloudflare A 记录。

    record 为空时用默认记录（第一条）；换班组可以指定自己那一条。

    verify=True 用于「实例刚开机 / 刚换班」这类关键时刻：不信本地缓存，先查一次
    Cloudflare 上记录的真实值，一致就不 PUT、直接算「未变化」。巡检对账不走这条
    （verify 默认关），否则每分钟一次读请求白花。
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
        previous = str(config.get("ddns_last_ip") or "")
        log.info("DRY RUN：假装已把 %s 同步到 %s", name, ip)
        same = verify and not force and previous == ip
        return DDNSResult(
            STATUS_UNCHANGED if same else STATUS_SYNCED,
            "IP 未变化" if same else f"{name} → {ip}",
            changed=not same,
            ip=ip,
            previous=previous,
        )

    # 记录名与 IP 都没变就免掉一次 API 调用。verify 时不走这条：用户要的就是
    # 「去 Cloudflare 看一眼真实值」，本地缓存算出来的结论不作数。
    if (
        not force
        and not verify
        and config.get("ddns_last_ip") == ip
        and (config.get("ddns_last_status") or "").startswith("✅")
    ):
        return DDNSResult(
            STATUS_UNCHANGED, "IP 未变化", ip=ip, previous=str(config.get("ddns_last_ip") or "")
        )

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
    # 最多试 3 次、每次间隔 1 秒（最后一次不再等）。以前每次都 sleep(2)、
    # 异常路径还多睡一次，坏记录一条就能拖 6~8 秒，巡检一轮 60 秒根本不够用；
    # Cloudflare 的瞬时抖动 1 秒足够过去，配置类错误重试再多也没用。
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            found = _find_record(headers, zone_id, name)
            if not found:
                last_error = "记录不存在或查询失败"
                if attempt < attempts:
                    time.sleep(1)
                continue
            record_id, current = found

            # 关键时刻已核对过真实值：CF 上就是这台机器的 IP，再 PUT 一遍只会
            # 白白刷新记录的修改时间，事件里还得写「已更新」，属于谎报。
            if verify and not force and current == ip:
                log.info("DDNS 已是最新 %s -> %s，跳过更新", name, ip)
                return DDNSResult(
                    STATUS_UNCHANGED, "IP 未变化", ip=ip, previous=current
                )

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
                return DDNSResult(
                    STATUS_SYNCED,
                    f"{name} → {ip}",
                    changed=True,
                    ip=ip,
                    previous=current,
                )
            last_error = f"HTTP {response.status_code}"
            log.warning("DDNS 第 %s 次更新失败: %s", attempt, last_error)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.warning("DDNS 第 %s 次更新异常: %s", attempt, exc)
        if attempt < attempts:
            time.sleep(1)

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
        # 每条记录各自解析 Zone：多条域名可能落在不同 Zone，以前统一用第一条的
        # zone_id，第二条必然报「未找到 A 记录」，而实际 sync() 是逐条解析的。
        # 网络异常也在 try 内 —— 以前只包了 _resolve_zone_id，_find_record 抛出
        # 去会变成接口 500，前端拿不到任何可读提示。
        zones: list[str] = []
        missing: list[str] = []
        for name in names:
            zone_id = _resolve_zone_id(headers, config, name)
            zones.append(zone_id)
            if not _find_record(headers, zone_id, name):
                missing.append(name)
    except Exception as exc:  # noqa: BLE001
        return DDNSResult(STATUS_ERROR, str(exc))

    if missing:
        return DDNSResult(STATUS_NO_RECORD_ID, f"{'、'.join(missing)} 未找到 A 记录")
    detail = (
        f"Zone {zones[0][:8]}… / {names[0]} 已就绪"
        if len(names) == 1
        else f"{len(names)} 条记录均就绪"
    )
    return DDNSResult("✅ 连接正常", detail)
