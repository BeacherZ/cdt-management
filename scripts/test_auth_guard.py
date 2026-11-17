#!/usr/bin/env python3
"""回归测试：会话黑名单查不到时必须保守拒绝（fail-closed）。

背景：会话是无状态的，退出登录只能靠 revoked_sessions 这张表吊销。以前查表
抛异常（库锁住 / 文件损坏）时按「未吊销」放行 —— 已注销、甚至是被盗的票据
在这段时间里照样能用满 14 天，退出登录等于没做。

现在改成：签名验过但黑名单查不到 → 按未登录处理（401）。库真锁住了，其它接口
本来也用不了，让人重登一次比静默放行安全。

跑法：python scripts/test_auth_guard.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前设好
_TMP = tempfile.mkdtemp(prefix="cdt-auth-guard-")
os.environ["CDT_DATA_DIR"] = _TMP
os.environ["CDT_DRY_RUN"] = "1"

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app import api, db, repo, security, settings  # noqa: E402


def _req(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/overview",
            "headers": [(b"cookie", f"{settings.SESSION_COOKIE}={token}".encode())],
            "client": ("127.0.0.1", 54321),
            "query_string": b"",
        }
    )


class SessionFailClosed(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        db.execute("DELETE FROM revoked_sessions")

    def _boom(self, _sid: str) -> bool:
        raise RuntimeError("database is locked")

    def test_valid_session_passes(self) -> None:
        token = security.issue_session("admin", 0)
        self.assertEqual(api._current_user(_req(token)), "admin")  # noqa: SLF001

    def test_revoked_session_rejected(self) -> None:
        token = security.issue_session("admin", 0)
        repo.revoke_session(security.session_id(token), int(time.time()) + 60)
        self.assertIsNone(api._current_user(_req(token)))  # noqa: SLF001

    def test_blacklist_error_rejects(self) -> None:
        """黑名单查询抛异常时不能放行 —— 这正是本次修掉的 fail-open。"""
        token = security.issue_session("admin", 0)
        original = repo.is_session_revoked
        repo.is_session_revoked = self._boom  # type: ignore[assignment]
        try:
            self.assertIsNone(api._current_user(_req(token)))  # noqa: SLF001
        finally:
            repo.is_session_revoked = original  # type: ignore[assignment]

    def test_blacklist_error_raises_401(self) -> None:
        token = security.issue_session("admin", 0)
        original = repo.is_session_revoked
        repo.is_session_revoked = self._boom  # type: ignore[assignment]
        try:
            with self.assertRaises(HTTPException) as ctx:
                api.require_admin(_req(token))
            self.assertEqual(ctx.exception.status_code, 401)
        finally:
            repo.is_session_revoked = original  # type: ignore[assignment]

    def test_no_cookie_is_401(self) -> None:
        with self.assertRaises(HTTPException):
            api.require_admin(_req(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
