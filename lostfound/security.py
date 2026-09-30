"""安全工具：不可猜测凭证、哈希比对、工作人员会话的签名 Cookie。

全部基于标准库。会话 Cookie 采用「时间戳|载荷|HMAC-SHA256」格式，
密钥持久化在数据目录的 secret.key 中，容器重启后会话仍然有效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
import time
from pathlib import Path
from typing import Any

import json


# ---------------------------------------------------------------- 凭证生成

def new_query_token() -> str:
    """访客查询凭证：URL 安全随机串，~256 位熵，不可猜测。"""
    return _secrets.token_urlsafe(32)


def new_handover_code() -> str:
    """一次性交还码：8 位数字，方便工作人员口头/当面核对。"""
    return f"{_secrets.randbelow(10**8):08d}"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 签名 Cookie

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, payload_b64: str) -> str:
    return _b64e(hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest())


def make_signed_cookie(name: str, secret: bytes, data: dict[str, Any], max_age: int) -> str:
    """返回可直接放入 Set-Cookie 的 ``名称=值; 属性...`` 字符串。"""
    envelope = {"d": data, "exp": int(time.time()) + max_age}
    payload = _b64e(json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode())
    value = f"{payload}.{_sign(secret, payload)}"
    return (
        f"{name}={value}; HttpOnly; Path=/; SameSite=Lax; Max-Age={max_age}"
    )


def read_signed_cookie(secret: bytes, raw_value: str | None) -> dict[str, Any] | None:
    """校验并解出 Cookie 数据；过期、损坏或被篡改一律返回 None。"""
    if not raw_value or "." not in raw_value:
        return None
    payload, sig = raw_value.rsplit(".", 1)
    expected = _sign(secret, payload)
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        envelope = json.loads(_b64d(payload))
    except Exception:
        return None
    if int(envelope.get("exp", 0)) < int(time.time()):
        return None
    data = envelope.get("d")
    return data if isinstance(data, dict) else None


def load_or_create_secret(path: str | Path) -> bytes:
    """读取持久化 HMAC/AEAD 密钥；不存在则生成（仅容器内部使用）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return p.read_bytes()
    secret = _secrets.token_bytes(32)
    p.write_bytes(secret)
    p.chmod(0o600)
    return secret


# ---------------------------------------------------------------- 对称加密

def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + b"stream" + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def encrypt_field(key: bytes, plaintext: str) -> str:
    """短小字段的认证加密（Encrypt-then-MAC），输出 URL 安全字符串。

    用于交还码密文存储：核码走哈希，回显需解密；密钥仅保存在数据目录。
    """
    nonce = _secrets.token_bytes(16)
    data = plaintext.encode("utf-8")
    cipher = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    mac = hmac.new(key, b"lf-v1" + nonce + cipher, hashlib.sha256).digest()
    return _b64e(nonce + mac + cipher)


def decrypt_field(key: bytes, blob: str) -> str | None:
    try:
        raw = _b64d(blob)
        nonce, mac, cipher = raw[:16], raw[16:48], raw[48:]
        expected = hmac.new(key, b"lf-v1" + nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, mac):
            return None
        data = bytes(a ^ b for a, b in zip(cipher, _keystream(key, nonce, len(cipher))))
        return data.decode("utf-8")
    except Exception:
        return None
