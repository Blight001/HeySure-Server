import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import HTTPException


@dataclass
class MediaSource:
    path: str
    filename: str
    mime_type: str
    source_url: str = ""
    cleanup_path: str = ""

    def cleanup(self) -> None:
        if not self.cleanup_path:
            return
        try:
            os.remove(self.cleanup_path)
        except OSError:
            pass


def _guess_filename_from_url(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path or "").name
    return name or fallback


def _guess_mime_type(filename: str, content_type: str = "") -> str:
    value = str(content_type or "").split(";", 1)[0].strip().lower()
    if value and value != "application/octet-stream":
        return value
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def resolve_media_source(
    *,
    url: str = "",
    path: str = "",
    filename: str = "",
    max_bytes: int = 30 * 1024 * 1024,
) -> MediaSource:
    media_url = str(url or "").strip()
    media_path = str(path or "").strip()
    if media_url:
        if not media_url.lower().startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="media url must start with http:// or https://")
        try:
            res = requests.get(media_url, timeout=30, stream=True)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"media download failed: {exc}")
        if not res.ok:
            raise HTTPException(status_code=502, detail=f"media download failed: HTTP {res.status_code}")
        content_length = int(res.headers.get("content-length") or 0)
        if content_length > max_bytes:
            raise HTTPException(status_code=400, detail="media file is too large")
        final_name = filename or _guess_filename_from_url(media_url, "media.bin")
        suffix = Path(final_name).suffix or ""
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        total = 0
        try:
            with tmp:
                for chunk in res.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(status_code=400, detail="media file is too large")
                    tmp.write(chunk)
            return MediaSource(
                path=tmp.name,
                filename=final_name,
                mime_type=_guess_mime_type(final_name, res.headers.get("content-type") or ""),
                source_url=media_url,
                cleanup_path=tmp.name,
            )
        except Exception:
            try:
                os.remove(tmp.name)
            except OSError:
                pass
            raise

    if media_path:
        full_path = Path(media_path).expanduser()
        if not full_path.exists() or not full_path.is_file():
            raise HTTPException(status_code=400, detail="media path does not exist or is not a file")
        if full_path.stat().st_size <= 0:
            raise HTTPException(status_code=400, detail="media file is empty")
        if full_path.stat().st_size > max_bytes:
            raise HTTPException(status_code=400, detail="media file is too large")
        final_name = filename or full_path.name
        return MediaSource(
            path=str(full_path),
            filename=final_name,
            mime_type=_guess_mime_type(final_name),
        )

    raise HTTPException(status_code=400, detail="media url or path is required")


def infer_media_kind(source: MediaSource, explicit: str = "") -> str:
    value = str(explicit or "").strip().lower()
    if value in {"image", "video"}:
        return value
    mime = str(source.mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    suffix = Path(source.filename or source.path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".heic"}:
        return "image"
    if suffix in {".mp4", ".mov", ".m4v"}:
        return "video"
    raise HTTPException(status_code=400, detail="unsupported media type; expected image or video")
