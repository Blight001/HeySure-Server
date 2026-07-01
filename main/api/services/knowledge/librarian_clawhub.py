"""librarian_clawhub — ClawHub 集成（搜索/安装/更新/删除）。"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from typing import Any, Dict, List, Optional

import logging

from .librarian_core import (
    _normalize_endpoint,
    _resolve_endpoint_kind,
    _split_frontmatter,
    _clawhub_installed_items,
    _thought_meta_to_row,
    _find_thought,
    _upsert_thought,
    _delete_thought_file,
    _BUILTIN_UPDATED_AT,
)
from ...integrations import clawhub

logger = logging.getLogger(__name__)


# ---------- 传承思想 / ClawHub ----------

_SAFE_CLAWHUB_REMOTE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@/-]{0,160}$")
# manual/npx 本地快照 slug 允许中文等 Unicode（与 _slugify 一致），ClawHub 远程 slug 仍限 ASCII
_SAFE_INSTALLED_SKILL_SLUG = re.compile(r"^(?:manual|npx)/[^\x00-\x1f\x7f]{1,200}$")


def _normalize_clawhub_slug(slug: str) -> str:
    value = str(slug or "").strip().strip("/")
    if not value or ".." in value.split("/"):
        raise ValueError("invalid ClawHub skill slug")
    if value.startswith("manual/") or value.startswith("npx/"):
        if not _SAFE_INSTALLED_SKILL_SLUG.match(value):
            raise ValueError("invalid installed skill slug")
    elif not _SAFE_CLAWHUB_REMOTE_SLUG.match(value):
        raise ValueError("invalid ClawHub skill slug")
    return value


def search_clawhub_skills(*, user_id: int, query: str, limit: int = 20) -> Dict[str, Any]:
    data = clawhub.search_skills(query, limit=limit, non_suspicious_only=True)
    results = data.get("results") if isinstance(data.get("results"), list) else []
    installed = {item["slug"] for item in _clawhub_installed_items(user_id) if item.get("slug")}
    for item in results:
        if isinstance(item, dict):
            slug = str(item.get("slug") or "")
            item["installed"] = slug in installed
    return {
        "registry_url": clawhub.registry_base_url(),
        "results": results,
        "total": len(results),
    }


def clawhub_skill_detail(*, user_id: int, slug: str) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    detail = clawhub.skill_detail(slug)
    version = _latest_clawhub_version(detail)
    skill_card = ""
    scan: Dict[str, Any] = {}
    try:
        skill_card = clawhub.skill_file(slug, "SKILL.md", version=version)
    except Exception as exc:
        skill_card = f"SKILL.md 读取失败：{exc}"
    try:
        scan = clawhub.skill_scan(slug, version=version) if version else clawhub.skill_scan(slug, tag="latest")
    except Exception as exc:
        scan = {"error": str(exc)}
    return {
        "registry_url": clawhub.registry_base_url(),
        "slug": slug,
        "detail": detail,
        "version": version,
        "skill_card": skill_card,
        "scan": scan,
        "installed": any(item.get("slug") == slug for item in _clawhub_installed_items(user_id)),
    }


def install_clawhub_skill(
    *,
    user_id: int,
    slug: str,
    version: Optional[str] = None,
    force: bool = False,
    endpoint_kind: Optional[str] = None,
    ai_config_id: Optional[int] = None,
) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    resolved_endpoint = _resolve_endpoint_kind(int(user_id), ai_config_id, endpoint_kind)
    detail = clawhub.skill_detail(slug)
    resolved_version = str(version or _latest_clawhub_version(detail) or "").strip() or None
    scan: Dict[str, Any] = {}
    try:
        scan = clawhub.skill_scan(slug, version=resolved_version) if resolved_version else clawhub.skill_scan(slug, tag="latest")
    except Exception as exc:
        scan = {"error": str(exc)}
    _raise_if_clawhub_blocked(detail, scan)

    existing = _find_thought(int(user_id), slug)
    if existing is not None and not force:
        raise ValueError("skill already installed; set force=true to update")

    blob = clawhub.download_skill_zip(slug, version=resolved_version, tag=None if resolved_version else "latest")
    # 解压到临时目录，仅取技能卡 SKILL.md 内容落成单文件 .md（其余 zip 文件不随技能落盘）。
    tmp_dir = tempfile.mkdtemp(prefix="clawhub-")
    try:
        _extract_skill_zip(blob, tmp_dir)
        card_path = os.path.join(tmp_dir, "SKILL.md")
        if not os.path.isfile(card_path):
            raise ValueError("ClawHub skill has no SKILL.md")
        with open(card_path, "r", encoding="utf-8") as f:
            raw = f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    fm, stripped = _split_frontmatter(raw)
    body_text = stripped if fm else raw

    skill = detail.get("skill") if isinstance(detail.get("skill"), dict) else {}
    owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
    # 未显式指定端归类时，force 更新保留旧值，否则采用推断结果。
    if (endpoint_kind is None or str(endpoint_kind).strip() == "") and existing is not None:
        prior_kind = _normalize_endpoint(existing[2].get("endpoint_kind"))
        effective_endpoint = prior_kind if prior_kind != "any" else resolved_endpoint
    else:
        effective_endpoint = resolved_endpoint
    row = {
        "slug": slug,
        "displayName": str(skill.get("displayName") or (fm or {}).get("name") or slug),
        "summary": str(skill.get("summary") or (fm or {}).get("description") or ""),
        "version": resolved_version,
        "ownerHandle": str(owner.get("handle") or skill.get("ownerHandle") or ""),
        "source": "remote:clawhub",
        "registry_url": clawhub.registry_base_url(),
        "installed_at": time.time(),
        "auto_enabled": False,
        "endpoint_kind": effective_endpoint,
        "trust": _clawhub_trust_summary(detail, scan),
    }
    merged = _upsert_thought(int(user_id), row, body=body_text)
    from .librarian_builtins import _builtin_entry
    return {
        "installed": True,
        "skill": merged,
        "entry": _builtin_entry("builtin.inheritance_tools", user_id=user_id, with_body=True) or {},
    }


def clawhub_installed_skill_detail(*, user_id: int, slug: str) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    found = _find_thought(int(user_id), slug)
    if found is None:
        raise ValueError("installed skill not found")
    rel, abs_p, meta, body = found
    item = _thought_meta_to_row(meta, rel)
    item["endpoint_kind"] = _normalize_endpoint(item.get("endpoint_kind"))
    trust = item.get("trust") if isinstance(item.get("trust"), dict) else {}
    return {
        "slug": slug,
        "skill": item,
        "skill_card": body,
        "metadata": {
            "source": item.get("source"),
            "version": item.get("version"),
            "trust_verdict": trust.get("verdict"),
            "registry_url": item.get("registry_url"),
        },
        "path": rel,
        "present": os.path.isfile(abs_p),
    }


def update_clawhub_installed_skill(*, user_id: int, slug: str, skill_card: str) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    if _find_thought(int(user_id), slug) is None:
        raise ValueError("installed skill files are missing")
    _upsert_thought(int(user_id), {"slug": slug}, body=str(skill_card or ""))
    from .librarian_builtins import _builtin_entry
    return {
        "updated": True,
        "detail": clawhub_installed_skill_detail(user_id=user_id, slug=slug),
        "entry": _builtin_entry("builtin.inheritance_tools", user_id=user_id, with_body=True) or {},
    }


def set_inheritance_thought_endpoint(*, user_id: int, slug: str, endpoint_kind: Any) -> Dict[str, Any]:
    """改端：更新一条已安装传承思想的端归类（any/desktop/browser）。"""
    slug = _normalize_clawhub_slug(slug)
    kind = _normalize_endpoint(endpoint_kind)
    if _find_thought(int(user_id), slug) is None:
        raise ValueError("installed skill not found")
    _upsert_thought(int(user_id), {"slug": slug, "endpoint_kind": kind})
    return {
        "updated": True,
        "slug": slug,
        "endpoint_kind": kind,
        "detail": clawhub_installed_skill_detail(user_id=user_id, slug=slug),
    }


def delete_clawhub_installed_skill(*, user_id: int, slug: str) -> Dict[str, Any]:
    slug = _normalize_clawhub_slug(slug)
    rel = _delete_thought_file(int(user_id), slug)
    if rel is None:
        raise ValueError("installed skill not found")
    from .librarian_builtins import _builtin_entry
    return {
        "deleted": True,
        "slug": slug,
        "entry": _builtin_entry("builtin.inheritance_tools", user_id=user_id, with_body=True) or {},
    }


def _latest_clawhub_version(detail: Dict[str, Any]) -> Optional[str]:
    latest = detail.get("latestVersion") if isinstance(detail.get("latestVersion"), dict) else {}
    version = str(latest.get("version") or "").strip()
    if version:
        return version
    skill = detail.get("skill") if isinstance(detail.get("skill"), dict) else {}
    tags = skill.get("tags") if isinstance(skill.get("tags"), dict) else {}
    latest_tag = str(tags.get("latest") or "").strip()
    return latest_tag or None


def _clawhub_trust_summary(detail: Dict[str, Any], scan: Dict[str, Any]) -> Dict[str, Any]:
    moderation = detail.get("moderation") if isinstance(detail.get("moderation"), dict) else {}
    scan_moderation = scan.get("moderation") if isinstance(scan.get("moderation"), dict) else {}
    security = scan.get("security") if isinstance(scan.get("security"), dict) else {}
    return {
        "verdict": str(moderation.get("verdict") or security.get("status") or ""),
        "isSuspicious": bool(moderation.get("isSuspicious") or scan_moderation.get("isSuspicious")),
        "isMalwareBlocked": bool(moderation.get("isMalwareBlocked") or scan_moderation.get("isMalwareBlocked")),
        "hasScanResult": bool(security.get("hasScanResult")),
        "blockedFromDownload": bool(security.get("blockedFromDownload")),
        "capabilityTags": security.get("capabilityTags") if isinstance(security.get("capabilityTags"), list) else [],
    }


def _raise_if_clawhub_blocked(detail: Dict[str, Any], scan: Dict[str, Any]) -> None:
    trust = _clawhub_trust_summary(detail, scan)
    verdict = str(trust.get("verdict") or "").lower()
    if trust.get("blockedFromDownload") or trust.get("isMalwareBlocked") or verdict in {"malicious", "blocked"}:
        raise ValueError("ClawHub blocked this skill as unsafe")
    if trust.get("isSuspicious") or verdict == "suspicious":
        raise ValueError("ClawHub marked this skill as suspicious")


def _extract_skill_zip(blob: bytes, dest_dir: str) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("ClawHub download is not a valid zip") from exc
    dest_abs = os.path.abspath(dest_dir)
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.startswith("/") or name.startswith("../") or "/../" in name:
            raise ValueError(f"unsafe zip path: {info.filename}")
        target = os.path.abspath(os.path.join(dest_abs, name))
        if not target.startswith(dest_abs + os.sep) and target != dest_abs:
            raise ValueError(f"unsafe zip path: {info.filename}")
    archive.extractall(dest_abs)
    if not os.path.exists(os.path.join(dest_abs, "SKILL.md")):
        entries = [name for name in os.listdir(dest_abs) if name != "__MACOSX"]
        if len(entries) == 1:
            wrapped_root = os.path.join(dest_abs, entries[0])
            wrapped_card = os.path.join(wrapped_root, "SKILL.md")
            if os.path.isdir(wrapped_root) and os.path.exists(wrapped_card):
                for child in os.listdir(wrapped_root):
                    shutil.move(os.path.join(wrapped_root, child), os.path.join(dest_abs, child))
                shutil.rmtree(wrapped_root, ignore_errors=True)
        if not os.path.exists(os.path.join(dest_abs, "SKILL.md")):
            logger.info("installed ClawHub skill without root SKILL.md at %s", dest_abs)
