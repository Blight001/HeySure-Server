"""Minimal client for Tencent's official QQ Bot QR binding protocol.

Protocol behavior mirrors ``@tencent-connect/qqbot-connector`` 1.2.0 while
keeping the Python runtime independent from OpenClaw and Node.js.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


QQ_BIND_BASE = "https://q.qq.com"


def _post_bind(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(
        f"{QQ_BIND_BASE}{path}",
        json=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or int(body.get("retcode", -1)) != 0:
        raise RuntimeError(str(body.get("msg") or "QQ 扫码绑定服务返回异常"))
    return body.get("data") if isinstance(body.get("data"), dict) else {}


def create_bind_task() -> tuple[str, str]:
    key = base64.b64encode(os.urandom(32)).decode("ascii")
    task_id = str(_post_bind("/lite/create_bind_task", {"key": key}).get("task_id") or "")
    if not task_id:
        raise RuntimeError("QQ 扫码绑定服务未返回任务编号")
    return task_id, key


def poll_bind_task(task_id: str) -> Dict[str, Any]:
    return _post_bind("/lite/poll_bind_result", {"task_id": task_id})


def decrypt_app_secret(encrypted: str, key: str) -> str:
    blob = base64.b64decode(str(encrypted or ""), validate=True)
    clear_key = base64.b64decode(str(key or ""), validate=True)
    if len(clear_key) != 32 or len(blob) < 29:
        raise ValueError("QQ 扫码凭据格式无效")
    iv, ciphertext, tag = blob[:12], blob[12:-16], blob[-16:]
    decryptor = Cipher(algorithms.AES(clear_key), modes.GCM(iv, tag)).decryptor()
    return (decryptor.update(ciphertext) + decryptor.finalize()).decode("utf-8")


def build_qr_url(task_id: str) -> str:
    query = urlencode({"task_id": task_id, "source": "heysure", "_wv": "2"})
    return f"{QQ_BIND_BASE}/qqbot/openclaw/connect.html?{query}"
