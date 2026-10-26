"""阿里云 ECS / CDT 客户端封装。

用官方 SDK 保证签名正确；SDK 缺失时（纯前端调试）自动降级为 dry-run 桩。
"""

from __future__ import annotations

import json
import logging
import socket
import threading

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
    """返回 (region, domain)。目前没接费用接口，留给后续账单/余额功能直接用。"""
    return BSS_ENDPOINTS.get(site, BSS_ENDPOINTS["china"])


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
    __slots__ = ("status", "stopped_mode", "public_ip")

    def __init__(self, status: str, stopped_mode: str, public_ip: str) -> None:
        self.status = status
        self.stopped_mode = stopped_mode
        self.public_ip = public_ip

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status,
            "stopped_mode": self.stopped_mode,
            "public_ip": self.public_ip,
        }


def sdk_ready() -> bool:
    return _SDK_READY


# --------------------------------------------------------------------------
# dry-run 桩：内存里模拟实例状态流转，方便本地把换班流程跑完
# --------------------------------------------------------------------------

_MOCK: dict[str, dict[str, str]] = {}
# 可重入：_mock_set 持锁时会再进 _mock
_MOCK_LOCK = threading.RLock()


def _mock_key(account: dict) -> str:
    return str(account.get("instance_id") or account.get("access_key_id") or "mock")


def _mock(account: dict) -> dict[str, str]:
    key = _mock_key(account)
    with _MOCK_LOCK:
        state = _MOCK.get(key)
        if state is None:
            index = len(_MOCK) + 10
            state = {
                "status": "Stopped",
                "stopped_mode": "StopCharging",
                # 203.0.113.0/24 是文档保留网段，不会指向任何真实主机
                "public_ip": f"203.0.113.{index}",
                "traffic": f"{float(account.get('quota_gb') or 200) * 0.42:.2f}",
            }
            _MOCK[key] = state
        return state


def _mock_set(account: dict, **fields: str) -> None:
    with _MOCK_LOCK:
        state = _mock(account)
        state.update(fields)


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
        state = _mock(account)
        return InstanceInfo(state["status"], state["stopped_mode"], state["public_ip"])
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
    eip = (instance.get("EipAddress") or {}).get("IpAddress")
    if eip:
        public_ip = str(eip)
    else:
        addresses = (instance.get("PublicIpAddress") or {}).get("IpAddress") or []
        public_ip = str(addresses[0]) if addresses else ""
    return InstanceInfo(status, mode, public_ip)


def start_instance(account: dict) -> None:
    if settings.DRY_RUN:
        log.info("[dry-run] start %s", account.get("instance_id"))
        _mock_set(account, status="Running", stopped_mode="")
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
        _mock_set(account, status="Stopped", stopped_mode=mode)
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
