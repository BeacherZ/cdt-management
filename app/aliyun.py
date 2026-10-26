"""阿里云 ECS / CDT 客户端封装。

用官方 SDK 保证签名正确；SDK 缺失时（纯前端调试）自动降级为 dry-run 桩。
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import threading
import time
from typing import Any

from . import settings

log = logging.getLogger("cdt-management.aliyun")

try:  # pragma: no cover - 依赖是否装好由运行环境决定
    import requests.packages.urllib3.util.connection as urllib3_conn
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest
    from aliyunsdkecs.request.v20140526 import (
        DescribeInstancesRequest,
        StartInstancesRequest,
        StopInstancesRequest,
    )

    _SDK_READY = True
except Exception:  # pragma: no cover
    _SDK_READY = False
    urllib3_conn = None
    AcsClient = None  # type: ignore
    CommonRequest = None  # type: ignore
    DescribeInstancesRequest = None  # type: ignore
    StartInstancesRequest = None  # type: ignore
    StopInstancesRequest = None  # type: ignore

# 强制 IPv4，避免容器内 IPv6 路由不通导致 API 调用悬挂超时
if urllib3_conn is not None:
    try:
        urllib3_conn.allowed_gai_family = lambda: socket.AF_INET
    except Exception:
        pass


class AliyunError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# endpoint 解析
#
# 站点只影响 BSS（费用/余额）：
#   中国站   business.aliyuncs.com             / cn-hangzhou
#   国际站   business.ap-southeast-1.aliyuncs.com / ap-southeast-1
# ECS 与 CDT 两个站用的是同一套域名（ecs.{region}.aliyuncs.com / cdt.aliyuncs.com），
# 所以换站点不会影响保活与流量统计，只在接账单类接口时才需要区分。
# 取值参考上游 wang4386/CDT-Monitor 的 bssEndpoint()。
# --------------------------------------------------------------------------

CDT_DOMAIN = "cdt.aliyuncs.com"
# CDT 是中心化接口，与实例所在地域无关，固定用香港（上游同样如此）
CDT_REGION = "cn-hongkong"

BSS_ENDPOINTS: dict[str, tuple[str, str]] = {
    "china": ("cn-hangzhou", "business.aliyuncs.com"),
    "international": ("ap-southeast-1", "business.ap-southeast-1.aliyuncs.com"),
}


def ecs_domain(region: str) -> str:
    return f"ecs.{region}.aliyuncs.com"


def bss_endpoint(site: str) -> tuple[str, str]:
    """返回 (region, domain)：余额与账单接口按站点走不同域名。"""
    return BSS_ENDPOINTS.get(site, BSS_ENDPOINTS["china"])


# --------------------------------------------------------------------------
# 费用 / 余额（BSS OpenAPI 2017-12-14）
#
# 两个接口都是「按账号」维度的慢数据，做两层缓存：
#   aliyun 层：进程内 TTL（余额 6 小时），避免多个入口重复打接口
#   billing 层：落 SQLite，容器重启后不用立刻再查一遍
#
# 需要的 RAM 权限：AliyunBSSReadOnlyAccess（或 bss:QueryAccountBalance /
# bss:DescribeInstanceBill）。缺权限时会抛 AliyunError，由上层降级显示。
# --------------------------------------------------------------------------

BSS_VERSION = "2017-12-14"
BALANCE_TTL = 6 * 3600

# key: sha256(ak:site) 前 20 位 —— 不把明文 AccessKey 留在内存字典的键里
_balance_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_balance_lock = threading.Lock()


def _cache_key(account: dict) -> str:
    raw = f"{account.get('access_key_id') or ''}:{account.get('site') or 'china'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _bss_request(account: dict, action: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """BSS 接口统一入口：按站点选 region/domain，返回解析后的 JSON。"""
    region, domain = bss_endpoint(str(account.get("site") or "china"))
    client = _client(account, region)
    request = CommonRequest()
    request.set_domain(domain)
    request.set_version(BSS_VERSION)
    request.set_action_name(action)
    request.set_method("POST")
    for key, value in (params or {}).items():
        request.add_query_param(key, value)
    raw = client.do_action_with_exception(request)
    return json.loads(raw.decode("utf-8"))


# 站点选错时阿里云的回法：AuthSiteFail / NotApplicable + "site matches"
_SITE_ERROR_HINTS = ("AuthSiteFail", "NotApplicable", "site matches", "AuthPrincipal")


def _looks_like_site_error(exc: Exception) -> bool:
    text = str(exc)
    return any(hint in text for hint in _SITE_ERROR_HINTS)


def _bss_with_site_fallback(
    account: dict, action: str, params: dict[str, str] | None = None
) -> tuple[dict[str, Any], str]:
    """调 BSS；站点选错时换另一个站点重试一次。

    阿里云的 RAM 子账号是绑站点的：中国站账号打 business.aliyuncs.com 之外的域名
    会直接 AuthSiteFail。与其让用户对着错误码猜，不如自己试一遍 —— 成功就用哪个。
    返回 (数据, 实际生效的站点)。
    """
    site = str(account.get("site") or "china")
    try:
        return _bss_request(account, action, params), site
    except Exception as exc:  # noqa: BLE001
        if not _looks_like_site_error(exc):
            raise
        other = "international" if site == "china" else "china"
        try:
            data = _bss_request({**account, "site": other}, action, params)
        except Exception:  # noqa: BLE001
            # 换站也失败：原始错误更能说明问题（通常是没权限）
            raise exc from None
        log.info("BSS 站点自动纠正：%s -> %s（action=%s）", site, other, action)
        return data, other


def get_account_balance(account: dict, force: bool = False) -> dict[str, Any]:
    """账户可用额度。返回 {"amount": float, "currency": "CNY", "site": str}。

    同一 AK + 站点 6 小时内复用缓存：余额变动慢，没必要每次对账都问一遍。
    """
    if settings.DRY_RUN:
        state = _mock(account)
        return {
            "amount": float(state["balance"]),
            "currency": state["currency"],
            "site": str(account.get("site") or "china"),
        }

    key = _cache_key(account)
    now = time.time()
    with _balance_lock:
        cached = _balance_cache.get(key)
        if cached and not force and now - cached[0] < BALANCE_TTL:
            return dict(cached[1])

    try:
        data, site = _bss_with_site_fallback(account, "QueryAccountBalance")
    except Exception as exc:  # noqa: BLE001
        raise AliyunError(f"账户余额查询失败: {exc}") from exc

    payload = data.get("Data") or {}
    try:
        amount = float(payload.get("AvailableAmount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    currency = str(payload.get("Currency") or "").strip() or "CNY"
    value = {"amount": amount, "currency": currency, "site": site}

    with _balance_lock:
        _balance_cache[key] = (now, value)
    return dict(value)


def get_instance_bill(account: dict, cycle: str) -> dict[str, Any]:
    """某台实例在指定账期（YYYY-MM）的税前消费合计。

    DescribeInstanceBill 是一行一行明细（实例、云盘、带宽分开），所以这里做累加。
    返回 {"total": float, "site": str}，site 是实际生效的站点（可能被自动纠正过）。
    """
    if settings.DRY_RUN:
        return {
            "total": round(float(_mock(account)["cost"]), 2),
            "site": str(account.get("site") or "china"),
        }

    params = {
        "BillingCycle": cycle,
        "InstanceID": str(account.get("instance_id") or ""),
        "Granularity": "MONTHLY",
        "MaxResults": "300",
    }
    try:
        data, site = _bss_with_site_fallback(account, "DescribeInstanceBill", params)
    except Exception as exc:  # noqa: BLE001
        raise AliyunError(f"实例账单查询失败: {exc}") from exc

    items = (data.get("Data") or {}).get("Items") or []
    # 有的返回把数组包一层 {"Item": [...]}
    if isinstance(items, dict):
        items = items.get("Item") or []

    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            total += float(item.get("PretaxAmount") or 0)
        except (TypeError, ValueError):
            continue
    return {"total": round(total, 2), "site": site}


def _client(account: dict, region: str | None = None) -> "AcsClient":
    if not _SDK_READY:
        raise AliyunError("阿里云 SDK 未安装，无法调用真实接口")
    return AcsClient(
        account["access_key_id"],
        account["access_key_secret"],
        region or account["region_id"],
        timeout=int(settings.HTTP_TIMEOUT),
    )


class InstanceInfo:
    __slots__ = ("status", "stopped_mode", "public_ip", "region_id")

    def __init__(self, status: str, stopped_mode: str, public_ip: str, region_id: str = "") -> None:
        self.status = status
        self.stopped_mode = stopped_mode
        self.public_ip = public_ip
        self.region_id = region_id

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "stopped_mode": self.stopped_mode,
            "public_ip": self.public_ip,
            "region_id": self.region_id,
        }


def sdk_ready() -> bool:
    return _SDK_READY


# --------------------------------------------------------------------------
# dry-run 桩：内存里模拟实例状态流转，方便本地把换班流程跑完
# --------------------------------------------------------------------------

_MOCK: dict[str, dict[str, Any]] = {}
# 可重入：_mock_set 持锁时会再进 _mock
_MOCK_LOCK = threading.RLock()

# dry-run 下模拟云操作异步到位的延迟（秒）：start/stop 后进入 Starting/Stopping
# 中间态，等这个时长后才在 get_instance 里推进到最终态，便于验证「转圈确认」流程。
# 由 settings.MOCK_OP_DELAY 控制，测试可设 0 关闭延迟。


def _mock_op_delay() -> float:
    return settings.MOCK_OP_DELAY


def _mock_key(account: dict) -> str:
    return str(account.get("instance_id") or account.get("access_key_id") or "mock")


def _mock(account: dict) -> dict[str, Any]:
    key = _mock_key(account)
    with _MOCK_LOCK:
        state = _MOCK.get(key)
        if state is None:
            index = len(_MOCK) + 10
            # 用实例 ID 散列出一个稳定的偏移，演示数据里每台费用不同但不会每轮跳变
            seed = sum(ord(char) for char in key)
            quota = float(account.get("quota_gb") or 200)
            state = {
                "status": "Stopped",
                "stopped_mode": "StopCharging",
                # 47.243.x 是阿里云香港的公网段，看着像真的；
                # 这里只是内存桩，dry-run 下不会真的往 Cloudflare 写解析
                "public_ip": f"47.243.{index}.{20 + index}",
                "traffic": f"{quota * 0.42:.2f}",
                "cost": f"{quota * 0.072 + 9 + (seed % 420) / 100:.2f}",
                "balance": f"{800 + seed % 1600}.{(seed * 7) % 100:02d}",
                "currency": "CNY",
                # 异步流转辅助字段：目标状态 + 到位时刻（0 表示无进行中的操作）
                "pending_until": 0.0,
                "target_status": "",
            }
            _MOCK[key] = state
        return state


def _mock_set(account: dict, **fields: Any) -> None:
    with _MOCK_LOCK:
        state = _mock(account)
        state.update(fields)


def _mock_advance(state: dict[str, Any]) -> None:
    """dry-run 桩内部：若存在进行中的操作且已到延迟点，把状态推进到目标态。"""
    pending_until = float(state.get("pending_until") or 0)
    target = str(state.get("target_status") or "")
    if pending_until and target and time.time() >= pending_until:
        state["pending_until"] = 0.0
        state["target_status"] = ""
        state["status"] = target
        if target == "Stopped":
            # 停机目标态默认按节省停机
            state.setdefault("stopped_mode", "StopCharging")
        else:
            state["stopped_mode"] = ""


def get_traffic_gb(account: dict) -> float:
    """CDT 当月出方向流量（GB）。与用户脚本一致：累加所有 TrafficDetails。"""
    if settings.DRY_RUN:
        return float(_mock(account)["traffic"])
    client = _client(account, CDT_REGION)
    request = CommonRequest()
    request.set_domain(CDT_DOMAIN)
    request.set_version("2021-08-13")
    request.set_action_name("ListCdtInternetTraffic")
    request.set_method("POST")
    try:
        raw = client.do_action_with_exception(request)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise AliyunError(f"CDT 流量查询失败: {exc}") from exc
    total_bytes = sum(
        float(item.get("Traffic", 0) or 0) for item in data.get("TrafficDetails", []) or []
    )
    return total_bytes / (1024 ** 3)


def get_instance(account: dict) -> InstanceInfo:
    if settings.DRY_RUN:
        with _MOCK_LOCK:
            state = _mock(account)
            _mock_advance(state)
            return InstanceInfo(
                state["status"],
                state["stopped_mode"],
                state["public_ip"],
                str(account.get("region_id") or ""),
            )
    client = _client(account)
    request = DescribeInstancesRequest.DescribeInstancesRequest()
    request.set_InstanceIds(json.dumps([account["instance_id"]]))
    try:
        raw = client.do_action_with_exception(request)
        data = json.loads(raw.decode("utf-8"))
        instance = data["Instances"]["Instance"][0]
    except Exception as exc:
        raise AliyunError(f"实例状态查询失败: {exc}") from exc

    status = str(instance.get("Status") or "")
    mode = str(instance.get("StoppedMode") or "")
    region = str(instance.get("RegionId") or instance.get("ZoneId") or "").strip()
    eip = (instance.get("EipAddress") or {}).get("IpAddress")
    if eip:
        public_ip = str(eip)
    else:
        addresses = (instance.get("PublicIpAddress") or {}).get("IpAddress") or []
        public_ip = str(addresses[0]) if addresses else ""
    return InstanceInfo(status, mode, public_ip, region)


def start_instance(account: dict) -> None:
    if settings.DRY_RUN:
        log.info("[dry-run] start %s", account.get("instance_id"))
        with _MOCK_LOCK:
            state = _mock(account)
            delay = _mock_op_delay()
            if delay <= 0:
                state["status"] = "Running"
                state["stopped_mode"] = ""
                state["pending_until"] = 0.0
                state["target_status"] = ""
            else:
                state["status"] = "Starting"
                state["stopped_mode"] = ""
                state["target_status"] = "Running"
                state["pending_until"] = time.time() + delay
        return
    client = _client(account)
    request = StartInstancesRequest.StartInstancesRequest()
    request.set_InstanceIds([account["instance_id"]])
    try:
        client.do_action_with_exception(request)
    except Exception as exc:
        message = str(exc)
        if "InsufficientInventory" in message:
            raise AliyunError("库存不足（InsufficientInventory），该地域暂无可用抢占式库存") from exc
        raise AliyunError(f"启动实例失败: {message}") from exc


def stop_instance(account: dict, mode: str = "StopCharging", fallback: bool = False) -> None:
    """mode=StopCharging 为「节省停机」，不计实例规格费用。

    fallback=True 时，如果实例不支持节省停机（部分规格 / 包年包月按固定价），
    自动退化成普通停机，避免手动关机直接报错没法用。
    """
    if settings.DRY_RUN:
        log.info("[dry-run] stop %s mode=%s", account.get("instance_id"), mode)
        with _MOCK_LOCK:
            state = _mock(account)
            delay = _mock_op_delay()
            if delay <= 0:
                state["status"] = "Stopped"
                state["stopped_mode"] = mode
                state["pending_until"] = 0.0
                state["target_status"] = ""
            else:
                state["status"] = "Stopping"
                state["stopped_mode"] = mode
                state["target_status"] = "Stopped"
                state["pending_until"] = time.time() + delay
        return
    client = _client(account)
    request = StopInstancesRequest.StopInstancesRequest()
    request.set_InstanceIds([account["instance_id"]])
    request.set_StoppedMode(mode)
    try:
        client.do_action_with_exception(request)
    except Exception as exc:
        if fallback and mode != "Stop":
            log.warning("节省停机失败，退化为普通停机: %s", exc)
            request.set_StoppedMode("Stop")
            try:
                client.do_action_with_exception(request)
                return
            except Exception as retry_exc:  # noqa: BLE001
                raise AliyunError(f"停止实例失败: {retry_exc}") from retry_exc
        raise AliyunError(f"停止实例失败: {exc}") from exc
