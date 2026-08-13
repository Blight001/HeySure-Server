"""Encrypted WeChat CDN upload/download and structured media messages."""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException

from .ilink_client import ILinkClient
from .media_transport import (
    aes_decrypt as _aes_decrypt,
    aes_encrypt as _aes_encrypt,
    download_and_decrypt as _download_and_decrypt,
    download_plain as _download_plain,
    parse_aes_key as _parse_aes_key,
    read_public_remote as _read_remote,
    safe_cdn_url as _safe_cdn_url,
    upload_cdn,
)


logger = logging.getLogger(__name__)
MAX_OUTBOUND_BYTES = 30 * 1024 * 1024
MAX_ITEMS = 5
UPLOAD_MEDIA_TYPES = {"image": 1, "video": 2, "file": 3}


def _load_outbound(path: str, url: str, file_name: str) -> tuple[bytes, str, str]:
    if path:
        candidate = os.path.realpath(path)
        if not os.path.isfile(candidate):
            raise HTTPException(status_code=404, detail="微信媒体文件不存在")
        size = os.path.getsize(candidate)
        if size <= 0 or size > MAX_OUTBOUND_BYTES:
            raise HTTPException(status_code=400, detail="微信媒体文件为空或超过 30 MB")
        with open(candidate, "rb") as handle:
            data = handle.read(MAX_OUTBOUND_BYTES + 1)
        name = _safe_name(file_name or Path(candidate).name)
        mime_type = _sniff_mime(data, mimetypes.guess_type(name)[0] or "application/octet-stream")
        return data, name, mime_type
    data, remote_name, content_type = _read_remote(url, MAX_OUTBOUND_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="微信媒体文件为空")
    name = _safe_name(file_name or remote_name)
    fallback = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return data, name, _sniff_mime(data, fallback)


def _sniff_mime(data: bytes, fallback: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    normalized = str(fallback or "application/octet-stream").lower()
    return "application/octet-stream" if normalized.startswith(("image/", "video/")) else normalized


def _safe_name(value: str) -> str:
    return (Path(str(value or "media.bin")).name or "media.bin")[:180]


def _media_kind(mime_type: str, hint: str) -> str:
    normalized = str(hint or "").strip().lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type == "application/octet-stream" and normalized in {"image", "video"}:
        return normalized
    return "file"


def _upload(client: ILinkClient, *, data: bytes, to_user_id: str, kind: str) -> Dict[str, Any]:
    key = secrets.token_bytes(16)
    filekey = secrets.token_hex(16)
    encrypted = _aes_encrypt(data, key)
    response = client.get_upload_url({
        "filekey": filekey,
        "media_type": UPLOAD_MEDIA_TYPES[kind],
        "to_user_id": to_user_id,
        "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data, usedforsecurity=False).hexdigest(),  # nosec: protocol field
        "filesize": len(encrypted),
        "no_need_thumb": True,
        "aeskey": key.hex(),
    })
    if int(response.get("ret") or 0) != 0:
        raise HTTPException(status_code=502, detail="微信媒体上传授权失败")
    download_param = upload_cdn(
        upload_full_url=str(response.get("upload_full_url") or ""),
        upload_param=str(response.get("upload_param") or ""),
        filekey=filekey,
        data=encrypted,
    )
    return {
        "download_param": download_param,
        "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
        "raw_size": len(data),
        "cipher_size": len(encrypted),
    }

def _message_item(kind: str, uploaded: Dict[str, Any], file_name: str) -> Dict[str, Any]:
    media = {
        "encrypt_query_param": uploaded["download_param"],
        "aes_key": uploaded["aes_key"],
        "encrypt_type": 1,
    }
    if kind == "image":
        return {"type": 2, "image_item": {"media": media, "mid_size": uploaded["cipher_size"]}}
    if kind == "video":
        return {"type": 5, "video_item": {"media": media, "video_size": uploaded["cipher_size"]}}
    return {"type": 4, "file_item": {"media": media, "file_name": _safe_name(file_name), "len": str(uploaded["raw_size"])}}


def send_media(
    client: ILinkClient, *, to_user_id: str, context_token: str, text: str,
    path: str, url: str, media_type: str, file_name: str,
) -> Dict[str, Any]:
    data, name, mime_type = _load_outbound(path, url, file_name)
    kind = _media_kind(mime_type, media_type)
    uploaded = _upload(client, data=data, to_user_id=to_user_id, kind=kind)
    if text:
        text_result = client.send_text(to_user_id=to_user_id, context_token=context_token, text=text)
        if int(text_result.get("ret") or 0) != 0:
            raise HTTPException(status_code=502, detail="微信媒体说明文字发送失败")
    result = client.send_item(
        to_user_id=to_user_id,
        context_token=context_token,
        item=_message_item(kind, uploaded, name),
    )
    if int(result.get("ret") or 0) != 0:
        raise HTTPException(status_code=502, detail="微信媒体消息发送失败")
    return {"success": True, "media_type": kind, "file_name": name, "bytes": len(data)}


def download_items(items: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    records = []
    supported = [item for item in items if int(item.get("type") or 0) in {2, 3, 4, 5}]
    for item in supported[:MAX_ITEMS]:
        try:
            record = _download_item(item)
            if record:
                records.append(record)
        except Exception as exc:
            logger.warning("wechat inbound media skipped error_type=%s", type(exc).__name__)
    return records


def _download_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    item_type = int(item.get("type") or 0)
    field = {2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}.get(item_type)
    if not field or not isinstance(item.get(field), dict):
        return None
    detail = item[field]
    media = detail.get("media")
    if not isinstance(media, dict):
        return None
    raw_hex = str(detail.get("aeskey") or "") if item_type == 2 else ""
    has_key = bool(raw_hex or str(media.get("aes_key") or ""))
    data = _download_and_decrypt(media, raw_hex_key=raw_hex) if has_key else _download_plain(media)
    _verify_inbound_metadata(item_type, detail, data)
    defaults = {
        2: ("wechat-image.bin", "application/octet-stream"),
        3: ("wechat-voice.silk", "audio/silk"),
        4: (_safe_name(str(detail.get("file_name") or "wechat-file.bin")), "application/octet-stream"),
        5: ("wechat-video.mp4", "video/mp4"),
    }
    name, mime_type = defaults[item_type]
    detected = _sniff_mime(data, mimetypes.guess_type(name)[0] or mime_type)
    if item_type == 2:
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(detected, ".bin")
        name = f"wechat-image{suffix}"
    return {"data": data, "file_name": name, "mime_type": detected, "item_type": item_type}


def _verify_inbound_metadata(item_type: int, detail: Dict[str, Any], data: bytes) -> None:
    expected_md5 = str(detail.get("md5") or detail.get("video_md5") or "").strip().lower()
    is_hex_md5 = len(expected_md5) == 32 and all(char in "0123456789abcdef" for char in expected_md5)
    if is_hex_md5 and hashlib.md5(data, usedforsecurity=False).hexdigest() != expected_md5:
        raise ValueError("WeChat inbound media MD5 mismatch")
    if item_type != 4 or not str(detail.get("len") or "").strip():
        return
    try:
        expected_size = int(detail["len"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid WeChat inbound file length") from exc
    if expected_size != len(data):
        raise ValueError("WeChat inbound file length mismatch")
