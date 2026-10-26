"""SQLite 存储层：建表、迁移、每线程独立连接（WAL）。"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable

from . import settings

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 服务器：一个阿里云账号 + 一个 ECS 实例
CREATE TABLE IF NOT EXISTS servers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    label               TEXT    NOT NULL,
    access_key_id       TEXT    NOT NULL,
    access_key_secret   TEXT    NOT NULL,
    region_id           TEXT    NOT NULL,
    -- 站点：china（中国站 aliyun.com）/ international（国际站 alibabacloud.com）。
    -- ECS 与 CDT 的 endpoint 两个站完全相同，只有 BSS（费用/余额）不同，
    -- 具体解析在 aliyun.py 的 _endpoints()。先存着，将来接账单接口不用再迁库。
    site                TEXT    NOT NULL DEFAULT 'china',
    instance_id         TEXT    NOT NULL,
    quota_gb            REAL    NOT NULL DEFAULT 200,
    threshold_gb        REAL    NOT NULL DEFAULT 188,
    remark              TEXT    NOT NULL DEFAULT '',
    traffic_used        REAL,
    instance_status     TEXT    NOT NULL DEFAULT '',
    stopped_mode        TEXT    NOT NULL DEFAULT '',
    public_ip           TEXT    NOT NULL DEFAULT '',
    traffic_checked_at  INTEGER NOT NULL DEFAULT 0,
    status_checked_at   INTEGER NOT NULL DEFAULT 0,
    -- 费用快照（BSS 2017-12-14）：本月账单 / 账户余额。
    -- 都是慢数据，抓一次存下来，界面直接读库，不在请求路径上打阿里云。
    monthly_cost        REAL,
    balance_amount      REAL,
    billing_currency    TEXT    NOT NULL DEFAULT '',
    -- 账单所属账期（YYYY-MM）：跨月后旧数字要能识别出来，不能当本月费用显示
    billing_cycle       TEXT    NOT NULL DEFAULT '',
    billing_updated_at  INTEGER NOT NULL DEFAULT 0,
    billing_error       TEXT    NOT NULL DEFAULT '',
    created_at          INTEGER NOT NULL DEFAULT 0
);

-- 单机保活：只负责「停了就拉起来」，不做换班
CREATE TABLE IF NOT EXISTS keepalives (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id  INTEGER NOT NULL UNIQUE,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT 0
);

-- 联合换班组
CREATE TABLE IF NOT EXISTS shift_groups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    failover_enabled  INTEGER NOT NULL DEFAULT 1,
    ddns_record       TEXT    NOT NULL DEFAULT '',
    anchor_at         INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL DEFAULT 0
);

-- 组成员：按 position 顺序轮换，各自跑 hours 小时
CREATE TABLE IF NOT EXISTS shift_members (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL,
    server_id  INTEGER NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    hours      REAL    NOT NULL DEFAULT 12,
    enabled    INTEGER NOT NULL DEFAULT 1,
    UNIQUE(group_id, position)
);
CREATE INDEX IF NOT EXISTS idx_members_group ON shift_members(group_id, position);

-- 每一次「某人当班」的运行状态
CREATE TABLE IF NOT EXISTS shift_runs (
    group_id            INTEGER NOT NULL,
    slot                TEXT    NOT NULL,
    phase               TEXT    NOT NULL DEFAULT 'pending',
    duty_server_id      INTEGER NOT NULL DEFAULT 0,
    standby_server_ids  TEXT    NOT NULL DEFAULT '',
    failover            INTEGER NOT NULL DEFAULT 0,
    attempts            INTEGER NOT NULL DEFAULT 0,
    started_at          INTEGER NOT NULL DEFAULT 0,
    updated_at          INTEGER NOT NULL DEFAULT 0,
    note                TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, slot)
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL DEFAULT 0,
    level      TEXT    NOT NULL DEFAULT 'info',
    kind       TEXT    NOT NULL DEFAULT '',
    group_id   INTEGER NOT NULL DEFAULT 0,
    server_id  INTEGER NOT NULL DEFAULT 0,
    title      TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS flags (
    key        TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS traffic_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id  INTEGER NOT NULL,
    ts         INTEGER NOT NULL DEFAULT 0,
    used       REAL    NOT NULL DEFAULT 0,
    status     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_samples_server_ts ON traffic_samples(server_id, ts DESC);
"""

DEFAULTS: dict[str, str] = {
    "admin_username": "",
    "admin_password": "",
    "initialized": "0",
    # 改密码时递增，用来让所有已发出的会话票据失效
    "session_epoch": "0",
    "timezone": "Asia/Shanghai",
    "traffic_threshold_pct": "94",
    "reconcile_interval": str(settings.RECONCILE_INTERVAL),
    # 费用/余额查询：需要 RAM 具备 BSS 只读权限，缺权限时关掉即可不显示
    "billing_enabled": "1",
    "daily_report_enabled": "1",
    "daily_report_time": "23:58",
    "notify_on_shift": "1",
    "notify_on_failover": "1",
    "notify_on_recovery": "1",
    "notify_on_breaker": "1",
    "notify_on_limit_stop": "1",
    "notify_on_error": "1",
    "notify_on_keepalive": "1",
    # DDNS
    "ddns_enabled": "0",
    "ddns_provider": "cloudflare",
    "ddns_cf_auth_mode": "token",
    "ddns_cf_token": "",
    "ddns_cf_email": "",
    "ddns_cf_key": "",
    "ddns_zone_id": "",
    "ddns_record": "",
    "ddns_ttl": "60",
    "ddns_proxied": "0",
    "ddns_last_ip": "",
    "ddns_last_status": "",
    "ddns_last_at": "0",
    # Telegram
    "notify_tg_enabled": "0",
    "notify_tg_token": "",
    "notify_tg_chat_id": "",
    "notify_tg_proxy": "",
    # 邮件
    "notify_mail_enabled": "0",
    "notify_mail_host": "",
    "notify_mail_port": "465",
    "notify_mail_security": "ssl",
    "notify_mail_username": "",
    "notify_mail_password": "",
    "notify_mail_to": "",
    # Webhook
    "notify_wh_enabled": "0",
    "notify_wh_url": "",
    "notify_wh_method": "POST",
    "notify_wh_headers": "",
    "notify_wh_body": "",
}


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    settings.ensure_dirs()
    conn = sqlite3.connect(str(settings.DB_PATH), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    _local.conn = conn
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """补齐老库缺的列。CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。"""
    additions = {
        "servers": {
            "site": "TEXT NOT NULL DEFAULT 'china'",
            "monthly_cost": "REAL",
            "balance_amount": "REAL",
            "billing_currency": "TEXT NOT NULL DEFAULT ''",
            "billing_cycle": "TEXT NOT NULL DEFAULT ''",
            "billing_updated_at": "INTEGER NOT NULL DEFAULT 0",
            "billing_error": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in additions.items():
        existing = _columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = connect()
        conn.executescript(SCHEMA)
        _migrate(conn)
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        _initialized = True


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    return connect().execute(sql, tuple(params))


def insert(sql: str, params: Iterable[Any] = ()) -> int:
    cur = connect().execute(sql, tuple(params))
    return int(cur.lastrowid or 0)
