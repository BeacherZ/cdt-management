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


# --------------------------------------------------------------------------
# servers
# --------------------------------------------------------------------------

SERVER_COLUMNS = (
    "id, label, access_key_id, access_key_secret, region_id, instance_id, "
    "quota_gb, threshold_gb, remark, traffic_used, instance_status, stopped_mode, "
    "public_ip, traffic_checked_at, status_checked_at, created_at"
)


def _server_row(row: Any, decrypt_secret: bool = False) -> dict[str, Any]:
    item = dict(row)
    secret = item.pop("access_key_secret", "") or ""
    item["secret_configured"] = bool(secret)
    if decrypt_secret:
        item["access_key_secret"] = secret
    else:
        item["access_key_masked"] = security.mask(security.decrypt(secret))
    return item


def list_servers(decrypt_secret: bool = False) -> list[dict[str, Any]]:
    rows = db.query(f"SELECT {SERVER_COLUMNS} FROM servers ORDER BY id")
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
            "SELECT access_key_id, access_key_secret FROM servers WHERE id=?", (server_id,)
        )
        if existing is None:
            raise ValueError("服务器不存在")
        if not ak:
            ak = existing["access_key_id"]
        encrypted = security.encrypt(sk) if sk else existing["access_key_secret"]
        db.execute(
            "UPDATE servers SET label=?, access_key_id=?, access_key_secret=?, region_id=?, "
            "instance_id=?, quota_gb=?, threshold_gb=?, remark=? WHERE id=?",
            (label or ak[-6:], ak, encrypted, region, instance, quota, threshold, remark, server_id),
        )
        return server_id

    if not ak:
        raise ValueError("AccessKey ID 不能为空")
    if not sk:
        raise ValueError("新建服务器必须填写 AccessKey Secret")
    return db.insert(
        "INSERT INTO servers(label, access_key_id, access_key_secret, region_id, instance_id, "
        "quota_gb, threshold_gb, remark, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            label or ak[-6:],
            ak,
            security.encrypt(sk),
            region,
            instance,
            quota,
            threshold,
            remark,
            int(time.time()),
        ),
    )


def delete_server(server_id: int) -> None:
    used = db.query_one("SELECT 1 FROM shift_members WHERE server_id=?", (server_id,))
    if used is not None:
        raise ValueError("该服务器仍在某个换班组里，请先移出")
    db.execute("DELETE FROM servers WHERE id=?", (server_id,))
    db.execute("DELETE FROM keepalives WHERE server_id=?", (server_id,))
    db.execute("DELETE FROM traffic_samples WHERE server_id=?", (server_id,))


def update_server_runtime(
    server_id: int,
    *,
    traffic: float | None = None,
    status: str | None = None,
    stopped_mode: str | None = None,
    public_ip: str | None = None,
) -> None:
    now = int(time.time())
    if traffic is not None:
        db.execute(
            "UPDATE servers SET traffic_used=?, traffic_checked_at=? WHERE id=?",
            (traffic, now, server_id),
        )
    if status is not None:
        db.execute(
            "UPDATE servers SET instance_status=?, stopped_mode=?, public_ip=?, "
            "status_checked_at=? WHERE id=?",
            (status, stopped_mode or "", public_ip or "", now, server_id),
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
        "s.instance_id, s.region_id, s.status_checked_at "
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
    db.execute("DELETE FROM keepalives WHERE id=?", (keepalive_id,))


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
        "s.label, s.instance_id, s.region_id, s.instance_status, s.stopped_mode, "
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
    ddns_record = str(payload.get("ddns_record") or "").strip()

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
        members.append(
            {
                "server_id": server_id,
                "position": index,
                "hours": hours,
                "enabled": 1 if item.get("enabled", True) else 0,
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
                "SELECT server_id, hours FROM shift_members WHERE group_id=? ORDER BY position",
                (group_id,),
            ).fetchall()
            old_signature = [(row["server_id"], row["hours"]) for row in old]
            new_signature = [(m["server_id"], m["hours"]) for m in members]
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
                "INSERT INTO shift_members(group_id, server_id, position, hours, enabled) "
                "VALUES(?,?,?,?,?)",
                (group_id, member["server_id"], member["position"], member["hours"], member["enabled"]),
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


def upsert_run(group_id: int, slot: str, **fields: Any) -> dict[str, Any]:
    now = int(time.time())
    existing = get_run(group_id, slot)
    if existing is None:
        db.execute(
            "INSERT INTO shift_runs(group_id, slot, started_at, updated_at) VALUES(?,?,?,?)",
            (group_id, slot, now, now),
        )
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
) -> None:
    db.insert(
        "INSERT INTO events(ts, level, kind, group_id, server_id, title, detail) "
        "VALUES(?,?,?,?,?,?,?)",
        (int(time.time()), level, kind, group_id, server_id, title, detail),
    )


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
    return [dict(row) for row in db.query(sql, params)]


def prune_events(days: int) -> None:
    cutoff = int(time.time()) - days * 86400
    db.execute("DELETE FROM events WHERE ts<?", (cutoff,))
    db.execute("DELETE FROM flags WHERE created_at<?", (cutoff,))
    db.execute("DELETE FROM shift_runs WHERE updated_at<?", (int(time.time()) - 3 * 86400,))
    db.execute("DELETE FROM traffic_samples WHERE ts<?", (int(time.time()) - 7 * 86400,))
