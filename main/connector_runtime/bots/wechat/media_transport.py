"""Network and AES primitives for the trusted WeChat media CDN."""

from __future__ import annotations

import base64
import ipaddress
import socket
import time
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import HTTPException


CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
CDN_HOST_SUFFIXES = (".weixin.qq.com", ".qq.com")
MAX_INBOUND_BYTES = 100 * 1024 * 1024


def aes_encrypt(data: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def aes_decrypt(data: bytes, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def parse_aes_key(value: str, *, raw_hex: bool = False) -> bytes:
    try:
        decoded = bytes.fromhex(value) if raw_hex else base64.b64decode(value, validate=True)
        if len(decoded) == 32 and all(chr(char) in "0123456789abcdefABCDEF" for char in decoded):
            decoded = bytes.fromhex(decoded.decode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid WeChat media AES key") from exc
    if len(decoded) != 16:
        raise ValueError("invalid WeChat media AES key length")
    return decoded


def safe_cdn_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("untrusted WeChat CDN URL")
    if not any(hostname == suffix[1:] or hostname.endswith(suffix) for suffix in CDN_HOST_SUFFIXES):
        raise ValueError("untrusted WeChat CDN host")
    return raw


def _public_remote_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="微信媒体 URL 必须使用公开 HTTPS 地址")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except OSError as exc:
        raise HTTPException(status_code=400, detail="微信媒体 URL 无法解析") from exc
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise HTTPException(status_code=400, detail="微信媒体 URL 不允许访问内网地址")
    return raw


def read_public_remote(url: str, limit: int) -> tuple[bytes, str, str]:
    response = requests.get(_public_remote_url(url), timeout=20.0, stream=True, allow_redirects=False)
    response.raise_for_status()
    size = 0
    chunks = []
    for chunk in response.iter_content(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=400, detail="微信媒体文件超过大小限制")
        chunks.append(chunk)
    name = urlparse(url).path.rsplit("/", 1)[-1] or "media.bin"
    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
    return b"".join(chunks), name, content_type


def upload_cdn(*, upload_full_url: str, upload_param: str, filekey: str, data: bytes) -> str:
    url = str(upload_full_url or "").strip()
    if not url:
        if not upload_param:
            raise HTTPException(status_code=502, detail="微信未返回媒体上传地址")
        url = f"{CDN_BASE_URL}/upload?encrypted_query_param={quote(upload_param, safe='')}&filekey={quote(filekey, safe='')}"
    return _post_cdn(safe_cdn_url(url), data)


def _post_cdn(url: str, data: bytes) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = requests.post(
                url, data=data, headers={"Content-Type": "application/octet-stream"},
                timeout=30.0, allow_redirects=False,
            )
            if 400 <= response.status_code < 500:
                raise HTTPException(status_code=502, detail="微信 CDN 拒绝媒体上传")
            response.raise_for_status()
            value = str(response.headers.get("x-encrypted-param") or "").strip()
            if not value:
                raise RuntimeError("WeChat CDN omitted media reference")
            return value
        except HTTPException:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * (2 ** attempt))
    raise HTTPException(status_code=502, detail="微信 CDN 媒体上传失败") from last_error


def download_and_decrypt(media: Dict[str, Any], *, raw_hex_key: str = "") -> bytes:
    key = parse_aes_key(raw_hex_key, raw_hex=True) if raw_hex_key else parse_aes_key(str(media.get("aes_key") or ""))
    return aes_decrypt(_download_bytes(media), key)


def download_plain(media: Dict[str, Any]) -> bytes:
    return _download_bytes(media, padding_allowance=0)


def _download_bytes(media: Dict[str, Any], *, padding_allowance: int = 16) -> bytes:
    response = requests.get(_download_url(media), timeout=30.0, stream=True, allow_redirects=False)
    response.raise_for_status()
    chunks = []
    size = 0
    for chunk in response.iter_content(1024 * 1024):
        size += len(chunk)
        if size > MAX_INBOUND_BYTES + padding_allowance:
            raise ValueError("WeChat inbound media exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _download_url(media: Dict[str, Any]) -> str:
    full_url = str(media.get("full_url") or "").strip()
    if full_url:
        return safe_cdn_url(full_url)
    param = str(media.get("encrypt_query_param") or "").strip()
    if not param:
        raise ValueError("WeChat media reference is missing")
    return f"{CDN_BASE_URL}/download?encrypted_query_param={quote(param, safe='')}"
