"""数据访问层：settings / servers / keepalives / shift groups / runs / events。"""

from __future__ import annotations

import json
import time
from typing import Any

from . import db, security

SENSITIVE_SETTINGS = {
    "admin_password",
    "ddns_cf_token",
    "ddns_cf_key",
    "notify_tg_token",
    "notify_mail_password",
    "notify_wh_headers",
}


def _is_hhmm(value: str) -> bool:
    """校验 HH:MM 时刻，支持 00:00–23:59。"""
    parts = value.split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def get_settings() -> dict[str, str]:
    rows = db.query("SELECT key, value FROM settings")
    data = {row["key"]: row["value"] for row in rows}
    for key in SENSITIVE_SETTINGS:
        if data.get(key):
            try:
                data[key] = security.decrypt(data[key])
            except Exception:
                data[key] = ""
    return data


def get_setting(key: str, fallback: str = "") -> str:
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return fallback
    value = row["value"]
    if key in SENSITIVE_SETTINGS and value:
        try:
            return security.decrypt(value)
        except Exception:
            return fallback
    return value


def set_settings(values: dict[str, str]) -> None:
    conn = db.connect()
    conn.execute("BEGIN")
    try:
        for key, value in values.items():
            stored = security.encrypt(value) if key in SENSITIVE_SETTINGS else value
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, stored),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_flag(key: str) -> bool:
    return db.query_one("SELECT 1 FROM flags WHERE key=?", (key,)) is not None


def set_flag(key: str) -> None:
    db.execute(
        "INSERT INTO flags(key, created_at) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
        (key, int(time.time())),
    )


def clear_flag(key: str) -> None:
    db.execute("DELETE FROM flags WHERE key=?", (key,))


def flag_age(key: str) -> float:
    """这个标记是多久以前设的（秒）；不存在返回 -1，表示「可以立刻执行」。"""
    row = db.query_one("SELECT created_at FROM flags WHERE key=?", (key,))
    if row is None:
        return -1.0
    return time.time() - float(row["created_at"])


def clear_flags_like(prefix: str) -> None:
    """按前缀清一批标记（如 `dns_fail:a.example.com:` 的所有失败类型）。

    用 substr 比较而不是 LIKE：记录名里允许多种字符，`_` 在 LIKE 里是通配符，
    会把不该清的标记一起删掉。
    """
    db.execute(
        "DELETE FROM flags WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
    )


# --------------------------------------------------------------------------
# servers
# --------------------------------------------------------------------------

SERVER_COLUMNS = (
    "id, label, access_key_id, access_key_secret, region_id, site, instance_id, "
    "quota_gb, threshold_gb, remark, traffic_used, instance_status, stopped_mode, "
    "public_ip, traffic_checked_at, status_checked_at, monthly_cost, balance_amount, "
    "billing_currency, billing_cycle, billing_updated_at, billing_error, created_at, sort_order"
)

# 站点取值只有两个；其余一律归一到中国站，避免脏数据流到 endpoint 解析里
SITE_TYPES = ("china", "international")


def normalize_site(value: Any) -> str:
    site = str(value or "").strip().lower()
    return site if site in SITE_TYPES else "china"


def _server_row(row: Any, decrypt_secret: bool = False) -> dict[str, Any]:
    item = dict(row)
    item["site"] = normalize_site(item.get("site"))
    secret = item.pop("access_key_secret", "") or ""
    item["secret_configured"] = bool(secret)
    if decrypt_secret:
        # 调用阿里云时必须拿明文，否则签出来的是密文
        if not secret:
            item["access_key_secret"] = ""
        else:
            try:
                item["access_key_secret"] = security.decrypt(secret)
            except Exception as exc:
                raise ValueError(
                    f"{item.get('label') or '该服务器'} 的 AccessKey Secret 解密失败，"
                    "通常是 master.key 被替换或丢失，请重新填写一遍 Secret"
                ) from exc
    else:
        # master.key 丢了 / 换了，解密会抛错：这里不能把整个列表接口带崩
        try:
            item["access_key_masked"] = security.mask(security.decrypt(secret))
        except Exception:
            item["access_key_masked"] = "⚠️ 解密失败"
            item["secret_configured"] = False
    return item


def list_servers(decrypt_secret: bool = False) -> list[dict[str, Any]]:
    # 手动排序优先：sort_order 非 0 的按 sort_order 升序；其余（含 0 默认）按 id。
    # 用一个统一键排序：sort_order 为 0 时退回一个基于 id 的稳定值。
    rows = db.query(
        f"SELECT {SERVER_COLUMNS} FROM servers ORDER BY "
        "CASE WHEN sort_order=0 THEN id + 1000000 ELSE sort_order END, id"
    )
    return [_server_row(row, decrypt_secret) for row in rows]


def get_server(server_id: int, decrypt_secret: bool = False) -> dict[str, Any] | None:
    row = db.query_one(f"SELECT {SERVER_COLUMNS} FROM servers WHERE id=?", (server_id,))
    return _server_row(row, decrypt_secret) if row else None


def upsert_server(payload: dict[str, Any]) -> int:
    server_id = int(payload.get("id") or 0)
    label = str(payload.get("label") or "").strip()
    ak = str(payload.get("access_key_id") or "").strip()
    sk = str(payload.get("access_key_secret") or "").strip()
    region = str(payload.get("region_id") or "").strip()
    site = normalize_site(payload.get("site"))
    instance = str(payload.get("instance_id") or "").strip()
    quota = float(payload.get("quota_gb") or 200)
    threshold = float(payload.get("threshold_gb") or 188)
    remark = str(payload.get("remark") or "").strip()

    if not region:
        raise ValueError("地域不能为空")
    if threshold <= 0 or threshold > quota:
        raise ValueError("阈值必须大于 0 且不超过总配额")

    if server_id:
        existing = db.query_one(
            "SELECT access_key_id, access_key_secret, site, instance_id FROM servers WHERE id=?",
            (server_id,),
        )
        if existing is None:
            raise ValueError("服务器不存在")
        if not ak:
            ak = existing["access_key_id"]
        if payload.get("site") is None:
            # 老前端不传这个字段时保持原值，别把已有配置默默重置成中国站
            site = normalize_site(existing["site"])
        encrypted = security.encrypt(sk) if sk else existing["access_key_secret"]
        # 换了账号 / 实例 / 站点，旧账单就不是这台机器的了，必须清掉重查
        billing_changed = (
            ak != existing["access_key_id"]
            or instance != (existing["instance_id"] or "")
            or site != normalize_site(existing["site"])
        )
        reset_billing = (
            ", monthly_cost=NULL, balance_amount=NULL, billing_currency='', "
            "billing_cycle='', billing_updated_at=0, billing_error=''"
            if billing_changed
            else ""
        )
        db.execute(
            "UPDATE servers SET label=?, access_key_id=?, access_key_secret=?, region_id=?, "
            "site=?, instance_id=?, quota_gb=?, threshold_gb=?, remark=?"
            + reset_billing
            + " WHERE id=?",
            (label or ak[-6:], ak, encrypted, region, site, instance, quota, threshold, remark, server_id),
        )
        return server_id

    if not ak:
        raise ValueError("AccessKey ID 不能为空")
    if not sk:
        raise ValueError("新建服务器必须填写 AccessKey Secret")
    return db.insert(
        "INSERT INTO servers(label, access_key_id, access_key_secret, region_id, site, instance_id, "
        "quota_gb, threshold_gb, remark, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            label or ak[-6:],
            ak,
            security.encrypt(sk),
            region,
            site,
            instance,
            quota,
            threshold,
            remark,
            int(time.time()),
        ),
    )


def reorder_servers(ordered_ids: list[int]) -> None:
    """按给定顺序持久化服务器排序：sort_order 从 1 开始递增。

    传进来的 ordered_ids 必须是「完整的有序 id 列表」，未列出的服务器
    sort_order 会被清零、回退到按 id 的自然顺序排在后面。
    """
    if not ordered_ids:
        return
    conn = db.connect()
    conn.execute("BEGIN")
    try:
        # 先把全部清零，避免「拖到前面后，原位置残留旧序号」
        conn.execute("UPDATE servers SET sort_order=0")
        for index, server_id in enumerate(ordered_ids, start=1):
            conn.execute(
                "UPDATE servers SET sort_order=? WHERE id=?",
                (index, int(server_id)),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def delete_server(server_id: int) -> None:
    used = db.query_one("SELECT 1 FROM shift_members WHERE server_id=?", (server_id,))
    if used is not None:
        raise ValueError("该服务器仍在某个换班组里，请先移出")
    db.execute("DELETE FROM servers WHERE id=?", (server_id,))
    db.execute("DELETE FROM keepalives WHERE server_id=?", (server_id,))
    db.execute("DELETE FROM traffic_samples WHERE server_id=?", (server_id,))
    # 目标没了，DNS 绑定必须一并松掉，否则记录还挂在已经不存在的机器上
    db.execute(
        "UPDATE dns_records SET target_kind='', target_id=0, last_ip='', "
        "last_status='', last_at=0, updated_at=? "
        "WHERE target_kind='server' AND target_id=?",
        (int(time.time()), server_id),
    )


def update_server_site(server_id: int, site: str) -> None:
    """只改站点。费用接口自己探出来后调用，不动其它字段。"""
    db.execute("UPDATE servers SET site=? WHERE id=?", (normalize_site(site), server_id))


def update_server_billing(    server_id: int,
    *,
    monthly_cost: float | None = None,
    balance: float | None = None,
    currency: str = "",
    cycle: str = "",
    error: str = "",
    touch: bool = True,
) -> None:
    """写入费用快照。

    `monthly_cost` / `balance` 传 None 表示「这次没查到，保留旧值」，
    区别于「查到了但为 0」—— 后者要正常落 0，不能当成没有。
    """
    sets: list[str] = []
    params: list[Any] = []
    if monthly_cost is not None:
        sets.append("monthly_cost=?")
        params.append(float(monthly_cost))
    if balance is not None:
        sets.append("balance_amount=?")
        params.append(float(balance))
    if currency:
        sets.append("billing_currency=?")
        params.append(str(currency))
    if cycle:
        sets.append("billing_cycle=?")
        params.append(str(cycle))
    sets.append("billing_error=?")
    params.append(str(error or ""))
    if touch:
        sets.append("billing_updated_at=?")
        params.append(int(time.time()))
    params.append(server_id)
    db.execute(f"UPDATE servers SET {', '.join(sets)} WHERE id=?", params)


def update_server_runtime(
    server_id: int,
    *,
    traffic: float | None = None,
    status: str | None = None,
    stopped_mode: str | None = None,
    public_ip: str | None = None,
    region_id: str | None = None,
) -> None:
    now = int(time.time())
    if traffic is not None:
        db.execute(
            "UPDATE servers SET traffic_used=?, traffic_checked_at=? WHERE id=?",
            (traffic, now, server_id),
        )
    if status is not None:
        if public_ip:
            db.execute(
                "UPDATE servers SET instance_status=?, stopped_mode=?, public_ip=?, "
                "status_checked_at=? WHERE id=?",
                (status, stopped_mode or "", public_ip, now, server_id),
            )
        elif status.startswith("Stop") or "Releas" in status:
            # 停机/释放后公网 IP 确实没了，清掉才对
            db.execute(
                "UPDATE servers SET instance_status=?, stopped_mode=?, public_ip='', "
                "status_checked_at=? WHERE id=?",
                (status, stopped_mode or "", now, server_id),
            )
        else:
            # 运行中/启动中却拿不到公网 IP：多半是 DescribeInstances 瞬时缺字段。
            # 这时候把库里已经存着的 IP 抹成空，会让 DNS 目标判定、通知里的 IP 行
            # 集体变成空值，宁可先留着上一轮的读数。
            db.execute(
                "UPDATE servers SET instance_status=?, stopped_mode=?, "
                "status_checked_at=? WHERE id=?",
                (status, stopped_mode or "", now, server_id),
            )
    if region_id:
        # 从 ECS DescribeInstances 返回值动态回填真实地域，避免手工填错/漏填
        db.execute(
            "UPDATE servers SET region_id=? WHERE id=?",
            (region_id, server_id),
        )


def add_sample(server_id: int, used: float, status: str) -> None:
    now = int(time.time())
    last = db.query_one(
        "SELECT used, ts FROM traffic_samples WHERE server_id=? ORDER BY ts DESC LIMIT 1",
        (server_id,),
    )
    if last is not None and now - last["ts"] < 600 and abs(last["used"] - used) < 0.001:
        return
    db.insert(
        "INSERT INTO traffic_samples(server_id, ts, used, status) VALUES(?,?,?,?)",
        (server_id, now, used, status),
    )


def list_samples(server_id: int, hours: int = 72) -> list[dict[str, Any]]:
    since = int(time.time()) - hours * 3600
    rows = db.query(
        "SELECT ts, used, status FROM traffic_samples WHERE server_id=? AND ts>=? ORDER BY ts",
        (server_id, since),
    )
    return [dict(row) for row in rows]


def latest_samples(server_id: int) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT used, ts FROM traffic_samples WHERE server_id=? ORDER BY ts DESC LIMIT 1",
        (server_id,),
    )
    return dict(row) if row else None


# --------------------------------------------------------------------------
# keepalives（单机保活）
# --------------------------------------------------------------------------

def list_keepalives() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT k.id, k.server_id, k.enabled, k.created_at, s.label, s.instance_status, "
        "s.stopped_mode, s.public_ip, s.traffic_used, s.quota_gb, s.threshold_gb, "
        "s.instance_id, s.region_id, s.site, s.status_checked_at "
        "FROM keepalives k JOIN servers s ON s.id=k.server_id ORDER BY k.id"
    )
    return [dict(row) for row in rows]


def keepalive_server_ids() -> set[int]:
    return {int(row["server_id"]) for row in db.query("SELECT server_id FROM keepalives WHERE enabled=1")}


def add_keepalive(server_id: int, enabled: bool = True) -> int:
    if db.query_one("SELECT 1 FROM servers WHERE id=?", (server_id,)) is None:
        raise ValueError("服务器不存在")
    existing = db.query_one("SELECT id FROM keepalives WHERE server_id=?", (server_id,))
    if existing is not None:
        db.execute("UPDATE keepalives SET enabled=? WHERE id=?", (1 if enabled else 0, existing["id"]))
        return int(existing["id"])
    return db.insert(
        "INSERT INTO keepalives(server_id, enabled, created_at) VALUES(?,?,?)",
        (server_id, 1 if enabled else 0, int(time.time())),
    )


def set_keepalive_enabled(keepalive_id: int, enabled: bool) -> None:
    db.execute("UPDATE keepalives SET enabled=? WHERE id=?", (1 if enabled else 0, keepalive_id))


def delete_keepalive(keepalive_id: int) -> None:
    # 只删保活任务本身。DNS 绑定是用户在 DNS 页显式建立的，不跟着一起清 ——
    # 一台机器可能同时挂着别的用途的域名，删个保活就把人家的解析抹了太粗暴。
    db.execute("DELETE FROM keepalives WHERE id=?", (keepalive_id,))


# --------------------------------------------------------------------------
# DNS 记录 + 绑定
# --------------------------------------------------------------------------

def list_dns_records() -> list[dict[str, Any]]:
    return [dict(row) for row in db.query("SELECT * FROM dns_records ORDER BY rowid")]


def get_dns_record(record: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM dns_records WHERE record=?", (record,))
    return dict(row) if row else None


def ensure_dns_record(record: str) -> None:
    """登记一条记录名。已存在时什么都不改，绑定与状态都保留。"""
    now = int(time.time())
    db.execute(
        "INSERT INTO dns_records(record, updated_at) VALUES(?, ?) "
        "ON CONFLICT(record) DO NOTHING",
        (record, now),
    )


def delete_dns_record(record: str) -> None:
    db.execute("DELETE FROM dns_records WHERE record=?", (record,))


def bind_dns_record(record: str, kind: str, target: int) -> None:
    """把记录绑到某个目标上。

    一条记录同时只能属于一个目标；反过来一个目标可以挂多条记录 ——
    一台机器用几个域名（www、api…）就绑几条，彼此互不影响。
    """
    db.execute(
        "UPDATE dns_records SET target_kind=?, target_id=?, updated_at=? WHERE record=?",
        (kind, target if kind else 0, int(time.time()), record),
    )


def dns_records_of(kind: str, target: int) -> list[str]:
    """这个目标当前占着哪些记录（按登记顺序）；没绑返回空列表。"""
    if not kind or not target:
        return []
    rows = db.query(
        "SELECT record FROM dns_records WHERE target_kind=? AND target_id=? ORDER BY rowid",
        (kind, target),
    )
    return [str(row["record"]) for row in rows]


def dns_record_of(kind: str, target: int) -> str:
    """这个目标当前占着的第一条记录；没绑返回空串。

    只用于「有没有绑」这种是非判断和界面展示，同步走 dns_records_of() 拿全量。
    """
    names = dns_records_of(kind, target)
    return names[0] if names else ""


def set_dns_state(record: str, ip: str, status: str, at: int | None = None) -> None:
    db.execute(
        "UPDATE dns_records SET last_ip=?, last_status=?, last_at=? WHERE record=?",
        (ip, status, int(at if at is not None else time.time()), record),
    )


# --------------------------------------------------------------------------
# 换班组 + 成员
# --------------------------------------------------------------------------

def list_groups() -> list[dict[str, Any]]:
    groups = [dict(row) for row in db.query("SELECT * FROM shift_groups ORDER BY id")]
    for group in groups:
        group["members"] = list_members(int(group["id"]))
    return groups


def get_group(group_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM shift_groups WHERE id=?", (group_id,))
    if row is None:
        return None
    group = dict(row)
    group["members"] = list_members(group_id)
    return group


def list_members(group_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT m.id, m.group_id, m.server_id, m.position, m.hours, m.enabled, "
        "m.slot_name, m.start_time, m.end_time, "
        "s.label, s.instance_id, s.region_id, s.site, s.instance_status, s.stopped_mode, "
        "s.public_ip, s.traffic_used, s.quota_gb, s.threshold_gb, s.status_checked_at "
        "FROM shift_members m LEFT JOIN servers s ON s.id=m.server_id "
        "WHERE m.group_id=? ORDER BY m.position",
        (group_id,),
    )
    return [dict(row) for row in rows]


def upsert_group(payload: dict[str, Any]) -> int:
    group_id = int(payload.get("id") or 0)
    name = str(payload.get("name") or "").strip() or "换班组"
    enabled = 1 if payload.get("enabled", True) else 0
    failover = 1 if payload.get("failover_enabled", True) else 0
    # 域名绑定已经挪到 DNS 页，这里只管老字段：没传就保持原值，别被整表提交清空
    if "ddns_record" in payload:
        ddns_record = str(payload.get("ddns_record") or "").strip()
    else:
        row = db.query_one("SELECT ddns_record FROM shift_groups WHERE id=?", (group_id,))
        ddns_record = str(row["ddns_record"] or "") if row else ""

    raw_members = payload.get("members") or []
    members: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, item in enumerate(raw_members):
        server_id = int(item.get("server_id") or 0)
        if not server_id:
            continue
        if server_id in seen:
            raise ValueError("同一台服务器不能在组里出现两次")
        seen.add(server_id)
        hours = float(item.get("hours") or 0)
        if hours <= 0 or hours > 168:
            raise ValueError("每台的运行时长应在 0–168 小时之间")
        slot_name = str(item.get("slot_name") or "").strip()
        start_time = str(item.get("start_time") or "").strip()
        end_time = str(item.get("end_time") or "").strip()
        # 时刻要么都空（走老 hours 累加），要么都填且格式合法
        if (start_time and not end_time) or (end_time and not start_time):
            raise ValueError("班次起止时刻要成对填写")
        if start_time and not _is_hhmm(start_time):
            raise ValueError(f"起始时刻「{start_time}」格式应为 HH:MM")
        if end_time and not _is_hhmm(end_time):
            raise ValueError(f"结束时刻「{end_time}」格式应为 HH:MM")
        members.append(
            {
                "server_id": server_id,
                "position": index,
                "hours": hours,
                "enabled": 1 if item.get("enabled", True) else 0,
                "slot_name": slot_name,
                "start_time": start_time,
                "end_time": end_time,
            }
        )

    if len(members) < 2:
        raise ValueError("换班组至少需要两台服务器")
    if sum(m["hours"] for m in members if m["enabled"]) <= 0:
        raise ValueError("至少需要一台启用的成员，且时长大于 0")

    conn = db.connect()
    conn.execute("BEGIN")
    try:
        if group_id:
            if conn.execute("SELECT 1 FROM shift_groups WHERE id=?", (group_id,)).fetchone() is None:
                raise ValueError("换班组不存在")
            # 成员构成变了就重置轮换起点，避免时序错乱
            old = conn.execute(
                "SELECT server_id, hours, slot_name, start_time, end_time "
                "FROM shift_members WHERE group_id=? ORDER BY position",
                (group_id,),
            ).fetchall()
            old_signature = [
                (row["server_id"], row["hours"], row["slot_name"], row["start_time"], row["end_time"])
                for row in old
            ]
            new_signature = [
                (m["server_id"], m["hours"], m["slot_name"], m["start_time"], m["end_time"])
                for m in members
            ]
            conn.execute(
                "UPDATE shift_groups SET name=?, enabled=?, failover_enabled=?, ddns_record=? WHERE id=?",
                (name, enabled, failover, ddns_record, group_id),
            )
            if old_signature != new_signature:
                conn.execute(
                    "UPDATE shift_groups SET anchor_at=? WHERE id=?", (int(time.time()), group_id)
                )
            conn.execute("DELETE FROM shift_members WHERE group_id=?", (group_id,))
        else:
            cur = conn.execute(
                "INSERT INTO shift_groups(name, enabled, failover_enabled, ddns_record, anchor_at, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (name, enabled, failover, ddns_record, int(time.time()), int(time.time())),
            )
            group_id = int(cur.lastrowid or 0)

        for member in members:
            conn.execute(
                "INSERT INTO shift_members(group_id, server_id, position, hours, enabled, "
                "slot_name, start_time, end_time) VALUES(?,?,?,?,?,?,?,?)",
                (
                    group_id,
                    member["server_id"],
                    member["position"],
                    member["hours"],
                    member["enabled"],
                    member["slot_name"],
                    member["start_time"],
                    member["end_time"],
                ),
            )
        conn.execute("COMMIT")
        return group_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def delete_group(group_id: int) -> None:
    conn = db.connect()
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM shift_members WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM shift_runs WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM shift_groups WHERE id=?", (group_id,))
        conn.execute(
            "UPDATE dns_records SET target_kind='', target_id=0, last_ip='', "
            "last_status='', last_at=0, updated_at=? "
            "WHERE target_kind='group' AND target_id=?",
            (int(time.time()), group_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reset_group_anchor(group_id: int) -> None:
    db.execute("UPDATE shift_groups SET anchor_at=? WHERE id=?", (int(time.time()), group_id))


# --------------------------------------------------------------------------
# shift runs
# --------------------------------------------------------------------------

def get_run(group_id: int, slot: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM shift_runs WHERE group_id=? AND slot=?", (group_id, slot))
    return dict(row) if row else None


# 换班「正常」阶段：note 是异常原因（熔断 / 超限停机），回到这些阶段就该清掉，
# 否则界面上会一直挂着「全部成员流量超限」，而同一张卡片写着「已就绪、当班运行中」。
_HEALTHY_PHASES = ("pending", "switching", "settled", "failover", "manual")


def upsert_run(group_id: int, slot: str, **fields: Any) -> dict[str, Any]:
    now = int(time.time())
    existing = get_run(group_id, slot)
    if existing is None:
        db.execute(
            "INSERT INTO shift_runs(group_id, slot, started_at, updated_at) VALUES(?,?,?,?)",
            (group_id, slot, now, now),
        )
    if str(fields.get("phase") or "") in _HEALTHY_PHASES and "note" not in fields:
        fields["note"] = ""
    if fields:
        assignments = ", ".join(f"{key}=?" for key in fields)
        db.execute(
            f"UPDATE shift_runs SET {assignments}, updated_at=? WHERE group_id=? AND slot=?",
            (*fields.values(), now, group_id, slot),
        )
    return get_run(group_id, slot) or {}


def recent_runs(limit: int = 30) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT r.*, g.name AS group_name FROM shift_runs r "
        "LEFT JOIN shift_groups g ON g.id=r.group_id "
        "ORDER BY r.updated_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in rows]


def standby_ids(run: dict[str, Any]) -> list[int]:
    try:
        return [int(value) for value in json.loads(run.get("standby_server_ids") or "[]")]
    except Exception:
        return []


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def add_event(
    kind: str,
    title: str,
    detail: str = "",
    level: str = "info",
    group_id: int = 0,
    server_id: int = 0,
    status: str = "done",
    pending_status: str = "",
    pending_kind: str = "",
) -> int:
    """写入一条事件。返回事件 ID，供后续 add_event_progress 追加过程。

    status 取值：done（一次成型，历史事件）/ running（进行中）/ error（失败）。
    pending_status：异步事件待确认的目标状态（如 Stopped/Running），空串表示无需轮询。
    pending_kind：异步事件类型（manual/keepalive），确认器据此决定到位后是否补发通知。

    一次性事件（done / error）没有后续轮次往里追加进度，这里把 detail 按行拆成
    过程条目补上，保证**任何**事件点开都能看到「它做了什么」，不只是标题加一句摘要。
    """
    now = int(time.time())
    event_id = db.insert(
        "INSERT INTO events(ts, level, kind, group_id, server_id, title, detail, "
        "status, finished_at, pending_status, pending_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            now,
            level,
            kind,
            group_id,
            server_id,
            title,
            detail,
            status,
            now if status != "running" else 0,
            pending_status if status == "running" else "",
            pending_kind if status == "running" else "",
        ),
    )
    if status != "running" and detail:
        for line in detail.splitlines():
            text = line.strip().lstrip("·-").strip()
            if text:
                add_event_progress(event_id, text)
    return event_id


def list_pending_events() -> list[dict[str, Any]]:
    """所有「进行中且需要轮询确认」的事件：确认器线程据此查状态、打完成标志。"""
    rows = db.query(
        "SELECT * FROM events WHERE status='running' AND pending_status<>'' "
        "ORDER BY id"
    )
    events = [dict(row) for row in rows]
    for event in events:
        event["progress"] = _parse_progress(event.get("progress"))
    return events


# 单个事件最多保留的进度条数，超出丢弃最旧的，防止长期卡住的事件把进度撑爆
PROGRESS_MAX_STEPS = 50


def add_event_progress(event_id: int, text: str, *, once: bool = False) -> None:
    """往事件的过程日志追加一条，并刷新 updated 时间戳（用 ts 近似当 updated_at）。

    相邻两条 text 相同就跳过，避免换班每轮 reconcile 反复追加同一条进度刷屏。
    once=True 时整条事件内出现过就不再追加（换班跨多轮，中间插了别的步骤后
    同一条进度还会再冒出来一次，用这个开关彻底去重）。
    单事件进度条数封顶 PROGRESS_MAX_STEPS，超出丢最旧的。
    """
    if not event_id:
        return
    now = int(time.time())
    row = db.query_one("SELECT progress FROM events WHERE id=?", (event_id,))
    if row is None:
        return
    try:
        progress = json.loads(row["progress"] or "[]")
    except Exception:
        progress = []
    if progress and progress[-1].get("text") == text:
        return
    if once and any(step.get("text") == text for step in progress):
        return
    progress.append({"ts": now, "text": text})
    if len(progress) > PROGRESS_MAX_STEPS:
        progress = progress[-PROGRESS_MAX_STEPS:]
    db.execute(
        "UPDATE events SET progress=? WHERE id=?",
        (json.dumps(progress, ensure_ascii=False), event_id),
    )


def finish_event(event_id: int, status: str = "done", detail: str = "") -> None:
    """标记事件结束。status：done / error。"""
    if not event_id:
        return
    sets = ["status=?", "finished_at=?"]
    params: list[Any] = [status, int(time.time())]
    if detail:
        sets.append("detail=?")
        params.append(detail)
    params.append(event_id)
    db.execute(f"UPDATE events SET {', '.join(sets)} WHERE id=?", params)


def update_event(
    event_id: int, *, title: str = "", detail: str = "", kind: str = ""
) -> None:
    """改写一条事件的标题 / 摘要 / 类型（如「换班进行中」→「换班完成」），保留原 ts 与进度。"""
    if not event_id:
        return
    sets: list[str] = []
    params: list[Any] = []
    if title:
        sets.append("title=?")
        params.append(title)
    if detail:
        sets.append("detail=?")
        params.append(detail)
    if kind:
        sets.append("kind=?")
        params.append(kind)
    if not sets:
        return
    params.append(event_id)
    db.execute(f"UPDATE events SET {', '.join(sets)} WHERE id=?", params)


def list_events(
    limit: int = 200, kind: str = "", level: str = "", group_id: int = 0
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events WHERE 1=1"
    params: list[Any] = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if level:
        sql += " AND level=?"
        params.append(level)
    if group_id:
        sql += " AND group_id=?"
        params.append(group_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    events = [dict(row) for row in db.query(sql, params)]
    for event in events:
        event["progress"] = _parse_progress(event.get("progress"))
    return events


def _parse_progress(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def prune_events(days: int) -> None:
    now = int(time.time())
    cutoff = now - days * 86400
    db.execute("DELETE FROM events WHERE ts<?", (cutoff,))
    # flags 里既有 per-slot 的一次性去重键，也有「已让位」「已熔断」这类长期状态，
    # 统一按 90 天兜底清理，别跟着事件保留期一起删
    db.execute("DELETE FROM flags WHERE created_at<?", (now - max(days, 90) * 86400,))
    # shift_runs 必须留得比「最长班次」久：抢占恢复要靠上一轮 phase 判断，
    # 提前删掉会让重启后的当班被当成新班次，重复推一条换班完成
    db.execute("DELETE FROM shift_runs WHERE updated_at<?", (now - 30 * 86400,))
    db.execute("DELETE FROM traffic_samples WHERE ts<?", (now - 7 * 86400,))
