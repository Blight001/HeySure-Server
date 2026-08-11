"""Minimal HMS Push Kit REST client with cached client-credentials tokens."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable

import aiohttp

from api.core.settings import settings


SUCCESS_CODE = "80000000"
_token_lock = asyncio.Lock()
_access_token = ""
_access_token_expires_at = 0.0


@dataclass(frozen=True)
class HuaweiPushResult:
    delivered: bool
    error_code: str = ""


def is_configured() -> bool:
    return bool(settings.huawei_push_client_id and settings.huawei_push_client_secret)


def build_message(tokens: Iterable[str], notification: Dict[str, Any]) -> Dict[str, Any]:
    token_list = [str(value).strip() for value in tokens if str(value).strip()][:1000]
    data = json.dumps({
        "notification_id": str(notification.get("notification_id") or "")[:160],
        "kind": str(notification.get("kind") or "message")[:40],
        "action_url": str(notification.get("action_url") or "")[:500],
    }, ensure_ascii=False, separators=(",", ":"))
    return {
        "validate_only": False,
        "message": {
            "notification": {
                "title": str(notification.get("title") or "HeySure")[:120],
                "body": str(notification.get("body") or "你收到一条新消息")[:500],
            },
            "android": {
                "notification": {
                    "click_action": {"type": 3},
                },
            },
            "data": data,
            "token": token_list,
        },
    }


async def _request_json(
    url: str, *, data: Any, headers: Dict[str, str], form: bool = False,
) -> tuple[int, Dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=float(settings.huawei_push_timeout_seconds))
    async with aiohttp.ClientSession(timeout=timeout) as client:
        kwargs = {"data": data} if form else {"json": data}
        async with client.post(url, headers=headers, **kwargs) as response:
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                payload = {}
            return response.status, payload if isinstance(payload, dict) else {}


async def _get_access_token() -> str:
    global _access_token, _access_token_expires_at
    now = time.time()
    if _access_token and _access_token_expires_at > now + 60:
        return _access_token
    async with _token_lock:
        now = time.time()
        if _access_token and _access_token_expires_at > now + 60:
            return _access_token
        status, payload = await _request_json(
            settings.huawei_push_auth_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.huawei_push_client_id,
                "client_secret": settings.huawei_push_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            form=True,
        )
        token = str(payload.get("access_token") or "")
        if status < 200 or status >= 300 or not token:
            code = str(payload.get("sub_error") or payload.get("error") or status)
            raise RuntimeError(f"hms_auth_{code[:40]}")
        _access_token = token
        _access_token_expires_at = now + max(60, int(payload.get("expires_in") or 3600))
        return token


async def send_notification(tokens: Iterable[str], notification: Dict[str, Any]) -> HuaweiPushResult:
    token_list = [str(value).strip() for value in tokens if str(value).strip()]
    if not token_list:
        return HuaweiPushResult(False, "no_token")
    if not is_configured():
        return HuaweiPushResult(False, "not_configured")
    try:
        access_token = await _get_access_token()
        url = (
            f"{settings.huawei_push_api_base.rstrip('/')}"
            f"/v1/{settings.huawei_push_client_id}/messages:send"
        )
        status, payload = await _request_json(
            url,
            data=build_message(token_list, notification),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        return HuaweiPushResult(False, type(exc).__name__[:40])
    code = str(payload.get("code") or status)
    return HuaweiPushResult(status in range(200, 300) and code == SUCCESS_CODE, code[:80])
