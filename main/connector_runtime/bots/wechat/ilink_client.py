"""Small HTTP client for Tencent's iLink bot JSON protocol."""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote, urljoin, urlparse

import requests


LOGIN_BASE_URL = "https://ilinkai.weixin.qq.com"
PLUGIN_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_CLIENT_VERSION = (2 << 16) | (4 << 8) | 6
ALLOWED_HOST_SUFFIXES = (".weixin.qq.com", ".qq.com")


def _safe_base_url(value: str) -> str:
    raw = str(value or LOGIN_BASE_URL).strip().rstrip("/")
    parsed = urlparse(raw)
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(
        hostname == suffix[1:] or hostname.endswith(suffix)
        for suffix in ALLOWED_HOST_SUFFIXES
    ):
        raise ValueError("iLink returned an untrusted API base URL")
    return raw


def _uin() -> str:
    value = str(secrets.randbits(32)).encode("ascii")
    return base64.b64encode(value).decode("ascii")


class ILinkClient:
    def __init__(self, *, base_url: str = LOGIN_BASE_URL, token: str = "", bot_agent: str = "HeySureAI/2.0.0") -> None:
        self.base_url = _safe_base_url(base_url)
        self.token = str(token or "").strip()
        self.bot_agent = str(bot_agent or "HeySureAI/2.0.0").strip()[:256]

    def _headers(self, *, authenticated_shape: bool) -> Dict[str, str]:
        headers = {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_CLIENT_VERSION),
        }
        if authenticated_shape:
            headers.update({
                "Content-Type": "application/json",
                "AuthorizationType": "ilink_bot_token",
                "X-WECHAT-UIN": _uin(),
            })
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _url(self, endpoint: str) -> str:
        return urljoin(self.base_url + "/", endpoint.lstrip("/"))

    def _base_info(self) -> Dict[str, str]:
        return {"channel_version": PLUGIN_VERSION, "bot_agent": self.bot_agent}

    def _post(self, endpoint: str, body: Dict[str, Any], *, timeout: float = 15.0) -> Dict[str, Any]:
        response = requests.post(
            self._url(endpoint),
            headers=self._headers(authenticated_shape=True),
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid iLink JSON response")
        return payload

    def create_qr(self, local_tokens: Iterable[str] = ()) -> Dict[str, Any]:
        return self._post(
            "ilink/bot/get_bot_qrcode?bot_type=3",
            {"local_token_list": [str(item) for item in local_tokens if str(item).strip()][:10]},
        )

    def poll_qr(self, qrcode: str, verify_code: str = "") -> Dict[str, Any]:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(str(qrcode), safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(str(verify_code), safe='')}"
        response = requests.get(
            self._url(endpoint),
            headers=self._headers(authenticated_shape=False),
            timeout=40.0,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def get_updates(self, cursor: str) -> Dict[str, Any]:
        return self._post(
            "ilink/bot/getupdates",
            {"get_updates_buf": str(cursor or ""), "base_info": self._base_info()},
            timeout=40.0,
        )

    def send_text(self, *, to_user_id: str, context_token: str, text: str) -> Dict[str, Any]:
        return self._post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "to_user_id": str(to_user_id),
                    "context_token": str(context_token),
                    "item_list": [{"type": 1, "text_item": {"text": str(text)}}],
                },
                "base_info": self._base_info(),
            },
        )

    def get_upload_url(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._post(
            "ilink/bot/getuploadurl",
            {**body, "base_info": self._base_info()},
        )

    def send_item(
        self,
        *,
        to_user_id: str,
        context_token: str,
        item: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": str(to_user_id),
                    "client_id": f"heysure-wechat-{uuid.uuid4().hex}",
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [item],
                    "context_token": str(context_token),
                },
                "base_info": self._base_info(),
            },
        )

    def notify(self, action: str) -> None:
        self._post(f"ilink/bot/msg/notify{action}", {"base_info": self._base_info()}, timeout=10.0)
