"""密码哈希、会话签名、凭据加密。

只用 cryptography 一个第三方依赖（AES-GCM），其余走标准库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import settings

PBKDF2_ROUNDS = 260_000


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(expected, actual)
    except Exception:
        return False


# --------------------------------------------------------------------------
# 主密钥 + 字段加密
# --------------------------------------------------------------------------

def _load_master_key() -> bytes:
    settings.ensure_dirs()
    path = settings.MASTER_KEY_PATH
    if path.exists():
        raw = path.read_bytes()
        if len(raw) >= 32:
            return raw[:32]
        # 文件在但内容不完整：绝不能悄悄重新生成，否则已存的 Secret 全部解不开
        raise RuntimeError(
            f"主密钥文件 {path} 内容不完整（{len(raw)} 字节，至少需要 32）。"
            "请从备份恢复；如果确实要重来，删掉该文件后重新录入所有凭据。"
        )
    key = os.urandom(32)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


_MASTER_KEY: bytes | None = None


def master_key() -> bytes:
    global _MASTER_KEY
    if _MASTER_KEY is None:
        _MASTER_KEY = _load_master_key()
    return _MASTER_KEY


def encrypt(plain: str) -> str:
    """返回 `v1:<base64(nonce+ct)>`；空串原样返回，方便前端「留空表示不修改」。"""
    if plain is None or plain == "":
        return ""
    nonce = os.urandom(12)
    blob = AESGCM(master_key()).encrypt(nonce, plain.encode(), None)
    return "v1:" + base64.b64encode(nonce + blob).decode()


def decrypt(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith("v1:"):
        # 兼容历史明文
        return stored
    raw = base64.b64decode(stored[3:])
    return AESGCM(master_key()).decrypt(raw[:12], raw[12:], None).decode()


def mask(secret: str) -> str:
    """把凭据打码成可安全展示 / 写日志的形式。"""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-4:]}"


# --------------------------------------------------------------------------
# 会话票据：HMAC 签名，无服务端状态，重启不掉线
# --------------------------------------------------------------------------

def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(master_key(), payload, hashlib.sha256).digest()
    ).decode().rstrip("=")


def issue_session(username: str, epoch: int = 0) -> str:
    payload = json.dumps(
        {
            "u": username,
            "exp": int(time.time()) + settings.SESSION_TTL_SECONDS,
            # epoch 变了等于让所有旧票据失效（改密码时用）
            "e": int(epoch),
        },
        separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def read_session(token: str, epoch: int = 0) -> str | None:
    """校验通过返回用户名，否则 None。"""
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = base64.urlsafe_b64decode(padded)
    except Exception:
        return None
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    if int(data.get("e", 0)) != int(epoch):
        return None
    return str(data.get("u") or "") or None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)
