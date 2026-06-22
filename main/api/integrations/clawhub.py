"""Small ClawHub HTTP client for server-side skill discovery/install flows."""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from api.core.settings import settings


class ClawHubError(RuntimeError):
    pass


def _api_base() -> str:
    base = (settings.clawhub_registry_url or "https://clawhub.ai").strip().rstrip("/")
    if base.endswith("/api/v1"):
        return base
    if base.endswith("/api"):
        return f"{base}/v1"
    return f"{base}/api/v1"


def registry_base_url() -> str:
    return _api_base().removesuffix("/api/v1")


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    token = (settings.clawhub_api_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = bool(settings.clawhub_use_env_proxy)
    return session


def _get_json(path: str, *, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Dict[str, Any]:
    url = f"{_api_base()}/{path.lstrip('/')}"
    try:
        with _session() as session:
            response = session.get(url, params=params, headers=_headers(), timeout=timeout)
    except requests.RequestException as exc:
        raise ClawHubError(f"ClawHub request failed: {exc}") from exc
    if response.status_code >= 400:
        raise ClawHubError(f"ClawHub HTTP {response.status_code}: {response.text[:240]}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ClawHubError("ClawHub returned non-JSON response") from exc
    if not isinstance(data, dict):
        raise ClawHubError("ClawHub returned unexpected response shape")
    return data


def search_skills(query: str, *, limit: int = 20, non_suspicious_only: bool = True) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise ClawHubError("query is required")
    limit = max(1, min(50, int(limit or 20)))
    return _get_json(
        "search",
        params={
            "q": query,
            "limit": limit,
            "nonSuspiciousOnly": "true" if non_suspicious_only else "false",
        },
    )


def skill_detail(slug: str) -> Dict[str, Any]:
    return _get_json(f"skills/{_clean_slug(slug)}")


def skill_scan(slug: str, *, version: Optional[str] = None, tag: Optional[str] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if version:
        params["version"] = version
    elif tag:
        params["tag"] = tag
    return _get_json(f"skills/{_clean_slug(slug)}/scan", params=params or None)


def skill_file(slug: str, path: str = "SKILL.md", *, version: Optional[str] = None, tag: Optional[str] = None) -> str:
    params: Dict[str, Any] = {"path": path or "SKILL.md"}
    if version:
        params["version"] = version
    elif tag:
        params["tag"] = tag
    url = f"{_api_base()}/skills/{_clean_slug(slug)}/file"
    try:
        with _session() as session:
            response = session.get(url, params=params, headers=_headers(), timeout=20.0)
    except requests.RequestException as exc:
        raise ClawHubError(f"ClawHub file request failed: {exc}") from exc
    if response.status_code >= 400:
        raise ClawHubError(f"ClawHub file HTTP {response.status_code}: {response.text[:240]}")
    return response.text


def download_skill_zip(slug: str, *, version: Optional[str] = None, tag: Optional[str] = None) -> bytes:
    params: Dict[str, Any] = {"slug": _clean_slug(slug)}
    if version:
        params["version"] = version
    elif tag:
        params["tag"] = tag
    url = f"{_api_base()}/download"
    try:
        with _session() as session:
            response = session.get(url, params=params, headers=_headers(), timeout=60.0)
    except requests.RequestException as exc:
        raise ClawHubError(f"ClawHub download failed: {exc}") from exc
    if response.status_code >= 400:
        raise ClawHubError(f"ClawHub download HTTP {response.status_code}: {response.text[:240]}")
    return response.content


def _clean_slug(slug: str) -> str:
    value = str(slug or "").strip()
    if not value:
        raise ClawHubError("slug is required")
    return value
