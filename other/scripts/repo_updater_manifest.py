"""Fetch and apply the immutable image manifest produced by CI."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Blight001/heysureai2/"
    "release-manifest/release-manifest.json"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseManifestError(RuntimeError):
    """The CI release manifest is missing, invalid, or does not match Git."""


def manifest_url(read_env) -> str:
    return (
        os.environ.get("HEYSURE_RELEASE_MANIFEST_URL")
        or read_env("HEYSURE_RELEASE_MANIFEST_URL")
        or DEFAULT_MANIFEST_URL
    ).strip()


def fetch_manifest(read_env, *, timeout: float = 20.0) -> dict[str, Any]:
    url = manifest_url(read_env)
    headers = {"Accept": "application/json", "User-Agent": "heysure-repo-updater/1"}
    token = os.environ.get("HEYSURE_RELEASE_MANIFEST_TOKEN") or read_env(
        "HEYSURE_RELEASE_MANIFEST_TOKEN"
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover - network errors are integration concerns
        raise ReleaseManifestError(f"无法读取 CI release-manifest：{exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseManifestError("release-manifest 格式无效")
    images = payload.get("images")
    if not isinstance(images, dict):
        raise ReleaseManifestError("release-manifest 缺少 images")
    for service in ("server", "web"):
        image = images.get(service)
        if not isinstance(image, dict) or not _DIGEST.fullmatch(str(image.get("digest") or "")):
            raise ReleaseManifestError(f"release-manifest 的 {service} digest 无效")
        reference = str(image.get("reference") or "")
        if not reference.endswith("@" + image["digest"]):
            raise ReleaseManifestError(f"release-manifest 的 {service} reference 不一致")
    if not str(payload.get("root_commit") or ""):
        raise ReleaseManifestError("release-manifest 缺少 root_commit")
    return payload


def apply_manifest(root: Path, data_root: Path, payload: dict[str, Any]) -> Path:
    """Atomically update image references, retaining a mode-0600 backup."""
    env_path = root / ".env"
    if not env_path.is_file():
        raise ReleaseManifestError("服务器 .env 不存在，无法应用 release-manifest")
    backup_dir = data_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"env-before-{str(payload['root_commit'])[:12]}-{int(time.time())}.bak"
    shutil.copy2(env_path, backup)
    os.chmod(backup, 0o600)
    values = {
        "HEYSURE_SERVER_IMAGE": payload["images"]["server"]["reference"],
        "HEYSURE_WEB_IMAGE": payload["images"]["web"]["reference"],
    }
    lines = env_path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in values:
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in values.items() if key not in seen)
    tmp = env_path.with_suffix(".manifest.tmp")
    tmp.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(tmp, env_path.stat().st_mode & 0o777)
    tmp.replace(env_path)
    return backup
