"""Atomic administration of Device Hall release artifacts and metadata."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from api.core.settings import settings
from api.services.device_releases import (
    RELEASE_ROOT,
    DeviceReleaseError,
    _artifact_path,
    _safe_id,
    load_catalog,
    version_key,
)


CHUNK_SIZE = 1024 * 1024
ALLOWED_SUFFIXES = {
    "windows": (".exe", ".msi", ".zip"),
    "linux": (".tar.gz", ".tgz", ".zip", ".deb", ".rpm"),
    "browser": (".zip",),
    "android": (".apk",),
}
_CATALOG_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _allowed_suffix(filename: str, platform: str) -> str:
    if not filename or "/" in filename or "\\" in filename:
        raise DeviceReleaseError("invalid upload filename")
    name = Path(filename or "").name.lower()
    for suffix in ALLOWED_SUFFIXES.get(platform, ()):
        if name.endswith(suffix):
            return suffix
    raise DeviceReleaseError(f"file type is not allowed for {platform}")


def _find_refs(catalog: dict[str, Any], product_id: str, target_id: str):
    wanted_product = _safe_id(product_id, "product id")
    wanted_target = _safe_id(target_id, "target id")
    for product in catalog.get("products", []):
        if str(product.get("id", "")).lower() != wanted_product:
            continue
        for target in product.get("targets", []):
            if str(target.get("id", "")).lower() == wanted_target:
                return product, target
    raise DeviceReleaseError("device release target not found")


def _write_uploaded(stream: BinaryIO, temp_path: Path, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with temp_path.open("xb") as output:
        while chunk := stream.read(CHUNK_SIZE):
            total += len(chunk)
            if total > max_bytes:
                raise DeviceReleaseError("release artifact exceeds upload size limit")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if total == 0:
        raise DeviceReleaseError("release artifact is empty")
    return digest.hexdigest(), total


def _atomic_catalog(catalog: dict[str, Any], root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    backups = root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backups / f"catalog.{stamp}.{uuid.uuid4().hex[:8]}.json"
    backup.write_text(json.dumps(load_catalog(root), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp = root / f".catalog.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, root / "catalog.json")
    finally:
        tmp.unlink(missing_ok=True)
    return backup


def admin_catalog(root: Path = RELEASE_ROOT) -> dict[str, Any]:
    catalog = deepcopy(load_catalog(root))
    for product in catalog.get("products", []):
        for target in product.get("targets", []):
            artifact = _artifact_path(root, target.get("artifact"))
            target["available"] = bool(artifact and artifact.is_file())
            if artifact and artifact.is_file():
                target["size_bytes"] = artifact.stat().st_size
                target["filename"] = artifact.name
    return catalog


def publish_release(
    *, product_id: str, target_id: str, version: str, filename: str,
    stream: BinaryIO, release_notes: str = "", mandatory: bool = False,
    root: Path = RELEASE_ROOT, max_bytes: int | None = None,
) -> dict[str, Any]:
    version = str(version or "").strip()
    if not version or version_key(version) == (0,):
        raise DeviceReleaseError("invalid release version")
    upload_limit = int(max_bytes or settings.device_release_max_bytes)
    with _CATALOG_LOCK:
        catalog = deepcopy(load_catalog(root))
        product, target = _find_refs(catalog, product_id, target_id)
        releases = target.setdefault("releases", [])
        if any(str(item.get("version")) == version for item in releases):
            raise DeviceReleaseError("release version already exists")
        current_artifact = _artifact_path(root, target.get("artifact"))
        if str(target.get("version")) == version and current_artifact and current_artifact.is_file():
            raise DeviceReleaseError("release version already exists")
        platform = str(target.get("platform") or "").lower()
        suffix = _allowed_suffix(filename, platform)
        relative = f"{platform}/{product['id']}-{target['id']}-{version}{suffix}"
        final_path = _artifact_path(root, relative)
        assert final_path is not None
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp = root / f".upload.{uuid.uuid4().hex}.tmp"
        try:
            sha256, size = _write_uploaded(stream, temp, upload_limit)
            if final_path.exists():
                raise DeviceReleaseError("release artifact already exists")
            os.replace(temp, final_path)
            previous = _snapshot_current(target)
            if previous and previous["version"] != version and not any(
                item.get("version") == previous["version"] for item in releases
            ):
                releases.append(previous)
            published_at = _utc_now()
            releases.append(_release_entry(version, relative, sha256, size, release_notes, mandatory, published_at))
            target.update(releases[-1])
            target.pop("withdrawn_at", None)
            catalog["updated_at"] = published_at
            backup = _atomic_catalog(catalog, root)
        except Exception:
            temp.unlink(missing_ok=True)
            if final_path.exists() and not _catalog_references(root, relative):
                final_path.unlink(missing_ok=True)
            raise
    return {"ok": True, "product_id": product["id"], "target_id": target["id"],
            "release": deepcopy(releases[-1]), "catalog_backup": backup.name}


def _release_entry(version, artifact, sha256, size, notes, mandatory, published_at):
    return {"version": version, "artifact": artifact, "sha256": sha256,
            "size_bytes": size, "release_notes": notes, "mandatory": mandatory,
            "published_at": published_at, "status": "published"}


def _snapshot_current(target: dict[str, Any]) -> dict[str, Any] | None:
    if not target.get("artifact"):
        return None
    keys = ("version", "artifact", "sha256", "size_bytes", "release_notes", "mandatory", "published_at")
    result = {key: target.get(key) for key in keys}
    result["status"] = "published"
    return result


def _catalog_references(root: Path, relative: str) -> bool:
    try:
        return any(target.get("artifact") == relative for product in load_catalog(root).get("products", [])
                   for target in product.get("targets", []))
    except DeviceReleaseError:
        return False


def withdraw_release(product_id: str, target_id: str, *, version: str | None = None,
                     delete_artifact: bool = False, root: Path = RELEASE_ROOT) -> dict[str, Any]:
    with _CATALOG_LOCK:
        catalog = deepcopy(load_catalog(root))
        _product, target = _find_refs(catalog, product_id, target_id)
        wanted = version or str(target.get("version") or "")
        if not wanted:
            raise DeviceReleaseError("no published release to withdraw")
        releases = target.setdefault("releases", [])
        current = _snapshot_current(target)
        if current and not any(item.get("version") == current["version"] for item in releases):
            releases.append(current)
        match = next((item for item in releases if str(item.get("version")) == wanted), None)
        if not match:
            raise DeviceReleaseError("release version not found")
        match.update({"status": "withdrawn", "withdrawn_at": _utc_now()})
        if str(target.get("version")) == wanted:
            replacement = _latest_published(releases, root)
            _apply_replacement(target, replacement)
        artifact = _artifact_path(root, match.get("artifact"))
        trash = None
        can_delete = (
            delete_artifact and artifact and artifact.is_file()
            and str(target.get("artifact")) != match.get("artifact")
        )
        if can_delete:
            trash = root / f".withdraw.{uuid.uuid4().hex}.tmp"
            os.replace(artifact, trash)
            match["artifact_deleted"] = True
        catalog["updated_at"] = _utc_now()
        try:
            backup = _atomic_catalog(catalog, root)
        except Exception:
            if trash and artifact:
                os.replace(trash, artifact)
            raise
        if trash:
            trash.unlink(missing_ok=True)
    return {"ok": True, "version": wanted, "status": "withdrawn", "catalog_backup": backup.name}


def _latest_published(releases: list[dict[str, Any]], root: Path):
    choices = [item for item in releases if item.get("status") == "published"
               and (path := _artifact_path(root, item.get("artifact"))) and path.is_file()]
    return max(choices, key=lambda item: version_key(item.get("version")), default=None)


def _apply_replacement(target: dict[str, Any], replacement: dict[str, Any] | None) -> None:
    mutable = ("version", "artifact", "sha256", "size_bytes", "release_notes", "mandatory", "published_at", "status")
    if replacement:
        target.update({key: replacement.get(key) for key in mutable})
        return
    for key in mutable:
        target.pop(key, None)
