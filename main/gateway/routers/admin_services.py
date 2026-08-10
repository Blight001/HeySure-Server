"""Service-monitoring operations used by the admin HTTP routes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from api.core.logging_config import get_recent_logs
from api.core.settings import settings
from api.runtime.internal_http import InternalClient
from gateway.routers import admin_service_probes as probes


@dataclass(frozen=True)
class ServiceTarget:
    key: str
    name: str
    base_url: str
    group: str = "runtime"
    restartable: bool = False
    logs_available: bool = False


class ServiceRequestError(RuntimeError):
    pass


def service_registry() -> list[ServiceTarget]:
    return [
        ServiceTarget("gateway", "API 网关", "(self)", restartable=True, logs_available=True),
        ServiceTarget("mcp", "MCP 运行时", settings.mcp_runtime_url, restartable=True, logs_available=True),
        ServiceTarget(
            "connector", "连接器运行时", settings.connector_runtime_url,
            restartable=True, logs_available=True,
        ),
        ServiceTarget("ai", "AI 运行时", settings.ai_runtime_url, restartable=True, logs_available=True),
        ServiceTarget("web", "Web 控制台", settings.web_runtime_url, group="infrastructure"),
        ServiceTarget("postgres", "PostgreSQL", "DATABASE_URL", group="infrastructure"),
        ServiceTarget("migrations", "数据库迁移", "Alembic", group="infrastructure"),
        ServiceTarget("repo_updater", "仓库更新器", settings.repo_updater_url, group="infrastructure"),
        ServiceTarget("agent_socket", "设备公网 Socket", settings.agent_socket_url, group="channel"),
        ServiceTarget("workflow_scheduler", "工作流调度器", "connector", group="channel"),
        ServiceTarget("bot_connections", "机器人长连接", settings.connector_runtime_url, group="channel"),
    ]


def service_target(key: str) -> Optional[ServiceTarget]:
    return next((target for target in service_registry() if target.key == key), None)


def probe_service(target: ServiceTarget) -> dict:
    local_probes = {
        "gateway": probes.probe_gateway,
        "web": lambda: probes.probe_web(target.base_url),
        "postgres": probes.probe_postgres,
        "migrations": probes.probe_migrations,
        "repo_updater": lambda: probes.probe_repo_updater(target.base_url),
        "agent_socket": lambda: probes.probe_agent_socket(
            target.base_url, required=bool(settings.connector_runtime_url)
        ),
        "workflow_scheduler": probes.probe_workflow_scheduler,
        "bot_connections": lambda: probes.probe_bot_connections(settings.connector_runtime_url),
    }
    if target.key in local_probes:
        result = local_probes[target.key]()
        return _service_payload(target, result)
    if not target.base_url:
        return _service_payload(target, {
            "status": "local",
            "summary": "未配置独立服务地址（单体模式）",
            "detail": {"note": "未配置独立服务地址（单体模式）"},
        })
    client = InternalClient(target.base_url, timeout=4.0)
    try:
        payload = client.get("/internal/health")
        status = "running" if payload.get("ok") else "degraded"
        result = {"status": status, "summary": _runtime_summary(payload), "detail": payload}
        return _service_payload(target, result)
    except Exception as exc:
        return _service_payload(target, {
            "status": "down",
            "summary": "内部健康检查失败",
            "detail": {"error": type(exc).__name__},
        })
    finally:
        client.close()


def _runtime_summary(payload: dict) -> str:
    if payload.get("draining"):
        return "正在排空任务"
    if not payload.get("ready"):
        return str(payload.get("readiness_error") or "未就绪")
    if payload.get("service_role") == "connector":
        return f"在线设备 {payload.get('connected_agent_count', 0)} · 等待分发 {payload.get('pending_dispatch_count', 0)}"
    if payload.get("service_role") == "worker":
        return f"活动任务 {payload.get('active_run_count', 0)} · 排队 {payload.get('queued_run_count', 0)}"
    if payload.get("service_role") == "mcp":
        return f"已注册工具 {payload.get('registered_tool_count', 0)}"
    return "已就绪并接受任务"


def _service_payload(target: ServiceTarget, result: dict) -> dict:
    return {
        "key": target.key,
        "name": target.name,
        "group": target.group,
        "status": result.get("status", "unknown"),
        "summary": result.get("summary", ""),
        "detail": result.get("detail") or {},
        "url": target.base_url,
        "restartable": target.restartable,
        "logs_available": target.logs_available,
    }


def list_service_statuses() -> list[dict]:
    targets = service_registry()
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
        results = list(executor.map(_safe_probe_service, targets))
    return results


def _safe_probe_service(target: ServiceTarget) -> dict:
    try:
        return probe_service(target)
    except Exception as exc:
        return _service_payload(target, {
            "status": "down",
            "summary": "状态探测发生异常",
            "detail": {"error": type(exc).__name__},
        })


def fetch_service_logs(
    target: ServiceTarget,
    *,
    limit: int,
    level: Optional[str],
) -> dict:
    if not target.logs_available:
        return {
            "key": target.key,
            "name": target.name,
            "lines": [],
            "note": "该组件提供结构化健康信息，不提供应用日志流。",
        }
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
    if not target.restartable:
        raise ServiceRequestError(f"{target.name} 不支持进程内重启")
    client = InternalClient(target.base_url, timeout=5.0)
    try:
        return client.post("/internal/restart")
    except Exception as exc:
        raise ServiceRequestError(f"无法重启 {target.name}: {exc}") from exc
    finally:
        client.close()
