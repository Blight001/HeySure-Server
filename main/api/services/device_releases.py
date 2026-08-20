"""Device Hall release catalog and artifact resolution.

The mutable catalog and packages live below ``data/device_releases`` so a
container replacement cannot erase published installers.  A checked-in
catalog is used only as a discoverable empty-state until an operator publishes
the first artifact.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from api.core.settings import DATA_DIR, SERVER_DIR


RELEASE_ROOT = Path(DATA_DIR) / "device_releases"
DEFAULT_CATALOG = Path(SERVER_DIR) / "main" / "static" / "device_hall" / "catalog.json"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class DeviceReleaseError(ValueError):
    """Raised when the persisted catalog is invalid or unsafe."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeviceReleaseError(f"invalid device release catalog: {path.name}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise DeviceReleaseError("device release catalog must contain a products array")
    return payload


def load_catalog(root: Path = RELEASE_ROOT) -> dict[str, Any]:
    override = root / "catalog.json"
    source = override if override.is_file() else DEFAULT_CATALOG
    return _read_json(source)


def _safe_id(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not SAFE_ID.fullmatch(text):
        raise DeviceReleaseError(f"invalid {field}")
    return text


def _artifact_path(root: Path, relative: Any) -> Path | None:
    value = str(relative or "").strip().replace("\\", "/")
    if not value:
        return None
    candidate = (root / "artifacts" / value).resolve()
    artifact_root = (root / "artifacts").resolve()
    if candidate == artifact_root or artifact_root not in candidate.parents:
        raise DeviceReleaseError("artifact path escapes release root")
    return candidate


def _download_url(base_url: str, product_id: str, target_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/device-hall/download/{product_id}/{target_id}"


def _release_page_url(base_url: str, product_id: str, target_id: str) -> str:
    query = urlencode({"device-hall": "1", "product": product_id, "target": target_id})
    return f"{base_url.rstrip('/')}/?{query}"


def _external_url(value: Any) -> str:
    url = str(value or "").strip()
    if url and not (url.startswith("https://") or url.startswith("http://")):
        raise DeviceReleaseError("external release URL must use http(s)")
    return url


def public_catalog(base_url: str, root: Path = RELEASE_ROOT) -> dict[str, Any]:
    catalog = deepcopy(load_catalog(root))
    public_products: list[dict[str, Any]] = []
    for raw_product in catalog.get("products", []):
        if not isinstance(raw_product, dict):
            continue
        product = dict(raw_product)
        product_id = _safe_id(product.get("id"), "product id")
        targets: list[dict[str, Any]] = []
        for raw_target in product.get("targets", []):
            if not isinstance(raw_target, dict):
                continue
            target = dict(raw_target)
            target_id = _safe_id(target.get("id"), "target id")
            artifact = _artifact_path(root, target.pop("artifact", ""))
            external_url = _external_url(target.get("external_url"))
            available = bool(external_url or (artifact and artifact.is_file()))
            target["available"] = available
            target["download_url"] = (
                external_url or _download_url(base_url, product_id, target_id)
            ) if available else None
            target.pop("external_url", None)
            if artifact and artifact.is_file():
                target["size_bytes"] = artifact.stat().st_size
            targets.append(target)
        product["id"] = product_id
        product["targets"] = targets
        public_products.append(product)
    return {
        "schema_version": int(catalog.get("schema_version") or 1),
        "updated_at": catalog.get("updated_at"),
        "products": public_products,
    }


def find_target(product_id: str, target_id: str, root: Path = RELEASE_ROOT) -> tuple[dict[str, Any], dict[str, Any]]:
    wanted_product = _safe_id(product_id, "product id")
    wanted_target = _safe_id(target_id, "target id")
    for product in load_catalog(root).get("products", []):
        if not isinstance(product, dict) or str(product.get("id") or "").lower() != wanted_product:
            continue
        for target in product.get("targets", []):
            if isinstance(target, dict) and str(target.get("id") or "").lower() == wanted_target:
                return product, target
    raise DeviceReleaseError("device release target not found")


def resolve_artifact(product_id: str, target_id: str, root: Path = RELEASE_ROOT) -> Path:
    _product, target = find_target(product_id, target_id, root)
    artifact = _artifact_path(root, target.get("artifact"))
    if not artifact or not artifact.is_file():
        raise DeviceReleaseError("device release artifact unavailable")
    return artifact


def version_key(version: Any) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(version or ""))
    return tuple(int(item) for item in numbers[:4]) or (0,)


def update_info(
    base_url: str,
    product_id: str,
    target_id: str,
    current_version: str,
    root: Path = RELEASE_ROOT,
) -> dict[str, Any]:
    product, target = find_target(product_id, target_id, root)
    artifact = _artifact_path(root, target.get("artifact"))
    external_url = _external_url(target.get("external_url"))
    latest = str(target.get("version") or "0.0.0")
    downloadable = bool(external_url or (artifact and artifact.is_file()))
    available = downloadable and version_key(latest) > version_key(current_version)
    return {
        "product_id": str(product.get("id")),
        "target_id": str(target.get("id")),
        "current_version": current_version,
        "latest_version": latest,
        "update_available": available,
        "mandatory": bool(target.get("mandatory", False)),
        "release_notes": str(target.get("release_notes") or ""),
        "sha256": str(target.get("sha256") or ""),
        "size_bytes": artifact.stat().st_size if artifact and artifact.is_file() else target.get("size_bytes"),
        "download_url": (
            external_url or _download_url(base_url, str(product.get("id")), str(target.get("id")))
        ) if downloadable else None,
        "release_page_url": _release_page_url(
            base_url,
            str(product.get("id")),
            str(target.get("id")),
        ),
    }
