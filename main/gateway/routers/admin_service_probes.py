"""Read-only health probes for the administrator service overview."""

from __future__ import annotations

import time
from typing import Any, Dict

import httpx
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.models import WorkflowSchedulerHeartbeat
from api.runtime.health import database_detail, state_for
from api.runtime.internal_http import InternalClient, internal_headers


def _result(status: str, summary: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": status, "summary": summary, "detail": detail}


def probe_gateway() -> Dict[str, Any]:
    snapshot = state_for("gateway").snapshot()
    database = database_detail()
    detail = {**snapshot, "database": database}
    ok = bool(snapshot.get("ready") and database.get("ok"))
    summary = "已就绪并接受请求" if ok else str(snapshot.get("readiness_error") or "未就绪")
    return _result("running" if ok else "degraded", summary, detail)


def probe_postgres() -> Dict[str, Any]:
    detail = database_detail()
    if detail.get("ok"):
        return _result("running", f"连接正常 · {detail.get('latency_ms')} ms", detail)
    return _result("down", "数据库连接失败", detail)


def probe_migrations() -> Dict[str, Any]:
    try:
        from api.db import current_schema_revisions, expected_schema_revisions

        current = sorted(current_schema_revisions(engine))
        expected = sorted(expected_schema_revisions())
        detail = {"current_revisions": current, "expected_revisions": expected, "at_head": current == expected}
        if current == expected:
            return _result("completed", "数据库结构已迁移到代码 Head", detail)
        return _result("degraded", "数据库 revision 与代码不一致", detail)
    except Exception as exc:
        return _result("down", "无法读取数据库迁移状态", {"error": type(exc).__name__})


def _http_probe(
    url: str,
    path: str,
    *,
    headers: Dict[str, str] | None = None,
    trust_env: bool = True,
) -> tuple[dict, float]:
    started = time.monotonic()
    with httpx.Client(
        base_url=url.rstrip("/"),
        timeout=4.0,
        follow_redirects=True,
        trust_env=trust_env,
    ) as client:
        response = client.get(path, headers=headers)
    latency = round((time.monotonic() - started) * 1000, 2)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    return {"status_code": response.status_code, "body": body}, latency


def probe_web(url: str) -> Dict[str, Any]:
    if not url:
        return _result("disabled", "未配置 Web 内部探测地址", {})
    try:
        response, latency = _http_probe(url, "/")
        detail = {"http_status": response["status_code"], "latency_ms": latency}
        ok = 200 <= response["status_code"] < 400
        return _result("running" if ok else "degraded", "网页入口可访问" if ok else "网页入口响应异常", detail)
    except Exception as exc:
        return _result("down", "网页入口不可访问", {"error": type(exc).__name__})


def probe_repo_updater(url: str) -> Dict[str, Any]:
    if not url:
        return _result("disabled", "未配置宿主仓库更新器", {})
    try:
        response, latency = _http_probe(url, "/health")
        body = response["body"] if isinstance(response["body"], dict) else {}
        state = body.get("state") if isinstance(body.get("state"), dict) else {}
        detail = {
            "http_status": response["status_code"],
            "latency_ms": latency,
            "phase": state.get("phase") or "idle",
            "running": bool(state.get("running")),
            "token_configured": bool(body.get("token_configured")),
        }
        ok = response["status_code"] == 200 and bool(body.get("ok"))
        return _result("running" if ok else "degraded", "宿主更新器可用" if ok else "宿主更新器响应异常", detail)
    except Exception as exc:
        return _result("down", "宿主更新器不可访问", {"error": type(exc).__name__})


def probe_agent_socket(url: str, *, required: bool) -> Dict[str, Any]:
    if not url:
        status = "down" if required else "disabled"
        return _result(status, "未配置设备公网 Socket 地址", {"configured": False})
    try:
        response, latency = _http_probe(
            url,
            "/internal/health/ready",
            headers=internal_headers(),
            trust_env=False,
        )
        body = response["body"] if isinstance(response["body"], dict) else {}
        role = body.get("service_role")
        ok = response["status_code"] == 200 and role == "connector" and bool(body.get("ready"))
        detail = {
            "configured": True,
            "http_status": response["status_code"],
            "latency_ms": latency,
            "service_role": role,
            "ready": bool(body.get("ready")),
        }
        summary = "公网地址正确指向 Connector" if ok else "公网地址未正确指向 Connector"
        return _result("running" if ok else "degraded", summary, detail)
    except Exception as exc:
        return _result("down", "设备公网 Socket 不可访问", {"configured": True, "error": type(exc).__name__})


def probe_workflow_scheduler() -> Dict[str, Any]:
    if not settings.workflow_scheduler_enabled:
        return _result("disabled", "工作流调度器未启用", {"enabled": False})
    now = time.time()
    threshold = max(30.0, float(settings.workflow_scheduler_interval_seconds) * 10)
    with Session(engine) as session:
        row = session.exec(
            select(WorkflowSchedulerHeartbeat).order_by(WorkflowSchedulerHeartbeat.heartbeat_at.desc())
        ).first()
    age = max(0.0, now - row.heartbeat_at) if row else None
    ok = bool(row and age is not None and age <= threshold and not row.last_error)
    detail = {
        "enabled": True,
        "heartbeat_age_seconds": round(age, 3) if age is not None else None,
        "stale_threshold_seconds": threshold,
        "last_tick_duration_ms": row.last_tick_duration_ms if row else None,
        "last_error": row.last_error if row else "not started",
    }
    return _result("running" if ok else "degraded", "调度心跳正常" if ok else "调度心跳异常", detail)


def probe_bot_connections(connector_url: str) -> Dict[str, Any]:
    if not connector_url:
        return _result("disabled", "单体模式未提供独立机器人状态", {})
    client = InternalClient(connector_url, timeout=4.0)
    try:
        payload = client.get("/internal/bot/statuses")
    except Exception as exc:
        return _result("down", "机器人状态不可读取", {"error": type(exc).__name__})
    finally:
        client.close()
    states = []
    for key, mapping in payload.items():
        if not key.endswith("_statuses") or not isinstance(mapping, dict):
            continue
        states.extend(item for item in mapping.values() if isinstance(item, dict))
    enabled = [item for item in states if item.get("status") not in (None, "disabled")]
    healthy = [item for item in enabled if item.get("status") == "success"]
    detail = {"configured_count": len(enabled), "healthy_count": len(healthy), "reported_count": len(states)}
    if not enabled:
        return _result("disabled", "没有启用机器人长连接", detail)
    ok = len(healthy) == len(enabled)
    return _result("running" if ok else "degraded", f"机器人长连接 {len(healthy)}/{len(enabled)} 正常", detail)
