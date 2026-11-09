"""密码哈希、会话签名、凭据加密。

只用 cryptography 一个第三方依赖（AES-GCM），其余走标准库。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import settings

log = logging.getLogger("cdt-management.security")

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

def _repair_text_mode_key(path, raw: bytes) -> bytes:
    """修掉「文本模式写入把 0x0A 写成 0x0D 0x0A」的 33 字节密钥文件。

    1.2.8 在 Windows 上用 os.open 写密钥时没带 O_BINARY，CRT 按文本模式打开，
    32 字节随机数里只要有 0x0A（约 12% 概率）就会写成 33 字节。这种文件的前 32
    字节不是真密钥，拿去解密会把所有凭据判成「master.key 被替换」。

    按插入位置还原成 32 字节并写回磁盘，用户无感。不是这种形态就原样取前 32 字节。
    """
    if len(raw) != 33 or b"\r\n" not in raw:
        return raw[:32]
    fixed = raw.replace(b"\r\n", b"\n", 1)
    if len(fixed) != 32:
        return raw[:32]
    try:
        with open(path, "wb") as fh:
            fh.write(fixed)
        log.warning("master.key 是 Windows 文本模式写坏的 33 字节文件，已自动还原为 32 字节")
    except OSError:  # noqa: BLE001
        # 写不回去也不影响本次使用：还原出来的密钥是对的
        pass
    return fixed


def _load_master_key() -> bytes:
    settings.ensure_dirs()
    path = settings.MASTER_KEY_PATH
    try:
        # O_EXCL：并发首调时只有一个线程/进程能创建成功，另一个落到下面的读分支。
        # 原来用「先判存在、再 write_bytes」，两个调用方会各生成一把密钥，后写的
        # 把先写的覆盖掉 —— 用另一把加密的凭据从此解不开。
        #
        # O_BINARY（Windows 才有，Linux 上是 0）：不加它，CRT 会以文本模式打开，
        # 密钥里出现 0x0A 时被写成 0x0D 0x0A，文件变 33 字节、内容也变了。32 字节
        # 随机数里有 0x0A 的概率约 12%，等于十几分之一的机会重启后所有凭据都解不开。
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        # 刚创建、内容还没写完的瞬间也要等一等：读到 0 字节不是「文件坏了」。
        raw = b""
        for _ in range(50):
            raw = path.read_bytes()
            if len(raw) >= 32:
                break
            time.sleep(0.01)
        if len(raw) < 32:
            # 文件在但内容不完整：绝不能悄悄重新生成，否则已存的 Secret 全部解不开
            raise RuntimeError(
                f"主密钥文件 {path} 内容不完整（{len(raw)} 字节，至少需要 32）。"
                "请从备份恢复；如果确实要重来，删掉该文件后重新录入所有凭据。"
            )
        return _repair_text_mode_key(path, raw)
    try:
        key = os.urandom(32)
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


_MASTER_KEY: bytes | None = None
_KEY_LOCK = threading.Lock()


def master_key() -> bytes:
    global _MASTER_KEY
    if _MASTER_KEY is None:
        with _KEY_LOCK:
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


def session_id(token: str) -> str:
    """票据的稳定短标识，用于注销黑名单。

    只对签名做 SHA-256 再截断，库里落的是标识而不是票据原文 —— 黑名单表被
    读走也换不回一个能用的会话。同一秒内登录两次会得到同一张票据，标识相同，
    注销时一起失效，这正是我们想要的。
    """
    if not token or "." not in token:
        return ""
    _, signature = token.rsplit(".", 1)
    return hashlib.sha256(signature.encode()).hexdigest()[:32]
