"""Service-monitoring operations used by the admin HTTP routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from api.core.logging_config import get_recent_logs
from api.core.settings import settings
from api.runtime.internal_http import InternalClient


@dataclass(frozen=True)
class ServiceTarget:
    key: str
    name: str
    base_url: str


class ServiceRequestError(RuntimeError):
    pass


def service_registry() -> list[ServiceTarget]:
    return [
        ServiceTarget("gateway", "API 网关", ""),
        ServiceTarget("mcp", "MCP 运行时", settings.mcp_runtime_url),
        ServiceTarget("connector", "连接器运行时", settings.connector_runtime_url),
        ServiceTarget("ai", "AI 运行时", settings.ai_runtime_url),
    ]


def service_target(key: str) -> Optional[ServiceTarget]:
    return next((target for target in service_registry() if target.key == key), None)


def probe_service(target: ServiceTarget) -> dict:
    if target.key == "gateway":
        return {
            "key": target.key,
            "name": target.name,
            "status": "running",
            "detail": {"role": "gateway"},
            "url": "(self)",
        }
    if not target.base_url:
        return {
            "key": target.key,
            "name": target.name,
            "status": "local",
            "detail": {"note": "未配置独立服务地址（单体模式）"},
            "url": "",
        }
    client = InternalClient(target.base_url, timeout=4.0)
    try:
        payload = client.get("/internal/health")
        status = "running" if payload.get("ok") else "degraded"
        return {
            "key": target.key,
            "name": target.name,
            "status": status,
            "detail": payload,
            "url": target.base_url,
        }
    except Exception as exc:
        return {
            "key": target.key,
            "name": target.name,
            "status": "down",
            "detail": {"error": str(exc)},
            "url": target.base_url,
        }
    finally:
        client.close()


def list_service_statuses() -> list[dict]:
    return [probe_service(target) for target in service_registry()]


def fetch_service_logs(
    target: ServiceTarget,
    *,
    limit: int,
    level: Optional[str],
) -> dict:
    if target.key == "gateway":
        return {
            "key": target.key,
            "name": target.name,
            "lines": get_recent_logs(limit=limit, level=level),
        }
    if not target.base_url:
        return {
            "key": target.key,
            "name": target.name,
            "lines": [],
            "note": "单体模式：日志与网关合并",
        }
    client = InternalClient(target.base_url, timeout=4.0)
    try:
        params = {"limit": limit}
        if level:
            params["level"] = level
        payload = client.get("/internal/logs", params=params)
        return {
            "key": target.key,
            "name": target.name,
            "lines": payload.get("lines", []),
        }
    except Exception as exc:
        raise ServiceRequestError(f"无法获取 {target.name} 日志: {exc}") from exc
    finally:
        client.close()


def restart_remote_service(target: ServiceTarget) -> dict:
    client = InternalClient(target.base_url, timeout=5.0)
    try:
        return client.post("/internal/restart")
    except Exception as exc:
        raise ServiceRequestError(f"无法重启 {target.name}: {exc}") from exc
    finally:
        client.close()
