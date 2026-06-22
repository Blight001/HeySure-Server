"""Filesystem-backed temporary image store under ``DATA_DIR/temp_images``: save,
validate, resolve, and TTL-expire images served at ``/tmp-images/<filename>``."""

import base64
import binascii
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

from fastapi import HTTPException

from api.core.settings import DATA_DIR, settings


TEMP_IMAGE_DIR = Path(DATA_DIR) / "temp_images"
DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/(?P<ext>png|jpeg|jpg|webp));base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)
EXT_TO_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
MAGIC_PREFIXES = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
}


def ensure_temp_image_dir() -> Path:
    TEMP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_IMAGE_DIR


def cleanup_expired_temp_images(now: Optional[float] = None) -> int:
    root = ensure_temp_image_dir()
    cutoff = (now or time.time()) - max(60, int(settings.temp_image_ttl_seconds))
    removed = 0
    for path in root.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def _normalize_ext(ext: str) -> str:
    value = str(ext or "").lower().lstrip(".")
    if value == "jpeg":
        return "jpg"
    if value not in {"png", "jpg", "webp"}:
        raise HTTPException(status_code=400, detail="unsupported image type; expected png, jpg, or webp")
    return value


def _validate_image_bytes(raw: bytes, ext: str) -> None:
    if not raw:
        raise HTTPException(status_code=400, detail="empty image payload")
    if len(raw) > int(settings.temp_image_max_bytes):
        raise HTTPException(status_code=413, detail="image exceeds temporary upload size limit")
    prefixes = MAGIC_PREFIXES.get(ext)
    if prefixes and not any(raw.startswith(prefix) for prefix in prefixes):
        raise HTTPException(status_code=400, detail="image bytes do not match declared type")
    if ext == "webp" and raw[8:12] != b"WEBP":
        raise HTTPException(status_code=400, detail="image bytes do not match declared type")


def save_temp_image(raw: bytes, ext: str) -> Tuple[str, str, int]:
    ext = _normalize_ext(ext)
    _validate_image_bytes(raw, ext)
    root = ensure_temp_image_dir()
    filename = f"{int(time.time() * 1000)}_{secrets.token_urlsafe(18)}.{ext}"
    path = root / filename
    path.write_bytes(raw)
    return filename, EXT_TO_MIME[ext], len(raw)


def save_temp_image_data_url(data_url: str) -> Tuple[str, str, int]:
    match = DATA_URL_RE.match(str(data_url or ""))
    if not match:
        raise HTTPException(status_code=400, detail="expected data:image/png|jpg|webp;base64,...")
    ext = _normalize_ext(match.group("ext"))
    try:
        raw = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="invalid base64 image payload")
    return save_temp_image(raw, ext)


def resolve_temp_image(filename: str) -> Tuple[Path, str]:
    safe_name = os.path.basename(str(filename or ""))
    if safe_name != filename:
        raise HTTPException(status_code=404, detail="temporary image not found")
    ext = _normalize_ext(Path(safe_name).suffix)
    path = ensure_temp_image_dir() / safe_name
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="temporary image not found")
    root = ensure_temp_image_dir().resolve()
    if root not in resolved.parents:
        raise HTTPException(status_code=404, detail="temporary image not found")
    return resolved, EXT_TO_MIME[ext]
