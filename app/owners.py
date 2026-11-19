"""实例归属：一台机器只能归一个自动化流程管。

单机保活 / 联合换班 / 流量均衡三者互斥。两个流程同时接管同一台机器，
每轮对账会互相启停，卡片状态来回跳，事件和通知也会重复发一遍。
校验放在这一处，三个模块入组时都走它，避免各写一套、漏掉其中一条。
"""

from __future__ import annotations

from typing import Any

from . import repo

KEEPALIVE = "keepalive"
# 换班组的 kind 走线上一贯口径 "group"（和 DNS 目标类型同名），不是 "shift"。
# 前端 owners.ts / ServerPicker / ServersView 都按 "group" 判断，改这里会让
# 概览和实例列表的归属集体退回「单机保活」。
SHIFT = "group"
BALANCE = "balance"

LABELS = {
    KEEPALIVE: "单机保活",
    SHIFT: "联合换班",
    BALANCE: "流量均衡",
}


def managed_map() -> dict[int, dict[str, Any]]:
    """server_id -> {kind, id, name, active}。

    顺序决定优先级：换班 > 均衡 > 保活。正常库里不会有两头都占的机器
    （入组时已经拦了），这里只是给界面一个确定答案，不制造第二种说法。
    """
    managed: dict[int, dict[str, Any]] = {}
    for group in repo.list_balance_groups():
        for member in group.get("members") or []:
            if not member.get("enabled"):
                continue
            managed[int(member["server_id"])] = {
                "kind": BALANCE,
                "group_id": int(group["id"]),
                "name": group.get("name") or "",
                "active": bool(group.get("enabled")),
            }
    for group in repo.list_groups():
        for member in group.get("members") or []:
            if not member.get("enabled"):
                continue
            managed[int(member["server_id"])] = {
                "kind": SHIFT,
                "group_id": int(group["id"]),
                "name": group.get("name") or "",
                "active": bool(group.get("enabled")),
            }
    for item in repo.list_keepalives():
        server_id = int(item["server_id"])
        if server_id in managed:
            continue
        managed[server_id] = {
            "kind": KEEPALIVE,
            "keepalive_id": int(item["id"]),
            "name": item.get("label") or "",
            "active": bool(item.get("enabled")),
        }
    return managed


def conflict_of(
    server_id: int,
    kind: str,
    target_id: int = 0,
    managed: dict[int, dict[str, Any]] | None = None,
) -> str:
    """这台机器是否已被**别的**流程占用。空串表示可用。

    target_id 是正在编辑的组：它自己的成员不算冲突，否则编辑已有组会一直报错。
    """
    owner = (managed or managed_map()).get(int(server_id))
    if owner is None:
        return ""
    if owner["kind"] == kind and int(owner.get("group_id") or 0) == int(target_id or 0):
        return ""
    return LABELS.get(owner["kind"], owner["kind"])


def ensure_free(
    server_ids: list[int],
    kind: str,
    target_id: int = 0,
    labels: dict[int, str] | None = None,
) -> None:
    """入组前校验。有冲突就抛 ValueError，文案直接给用户看。"""
    managed = managed_map()
    for server_id in server_ids:
        taken = conflict_of(int(server_id), kind, target_id, managed)
        if not taken:
            continue
        name = (labels or {}).get(int(server_id)) or f"#{server_id}"
        raise ValueError(f"{name} 已加入{taken}，一台实例只能归一个流程管，先移出后再添加")
