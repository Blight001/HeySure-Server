"""Database-backed assembly for the administrator capability selftest."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from api.database import engine
from api.models import AssistantAIConfig, ChatSession, DevicePresence, User
from api.services.mcp.capability_diagnostics import (
    inspect_described_cache,
    inspect_online_device_catalogs,
    inspect_scoped_tool_view,
)
from api.services.mcp.capability_view import ToolViewRequest, resolve_scoped_tool_view


def scoped_views_check(user_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        user = session.get(User, user_id)
        configs = session.exec(
            select(AssistantAIConfig).where(AssistantAIConfig.user_id == user_id)
        ).all()
        reports = [
            inspect_scoped_tool_view(resolve_scoped_tool_view(
                session, user, cfg, ToolViewRequest(ai_config_id=cfg.id)
            ))
            for cfg in configs
        ] if user else []
    failed = sum(1 for report in reports if not report["ok"])
    eligible_total = sum(int(report["eligible_count"]) for report in reports)
    ok = user is not None and failed == 0
    return {
        "ok": ok,
        "detail": (
            f"{len(reports)} 个 AI capability revision 可稳定重算，eligible 工具投影共 {eligible_total} 项"
            if ok else f"{failed} 个 AI 的 capability surface 不一致"
        ),
        "info": {"ai_configs": len(reports), "eligible_total": eligible_total, "failed": failed},
    }


def described_exposure_check(user_id: int) -> dict[str, Any]:
    reports, unscoped = _described_cache_reports(user_id)
    totals = {
        key: sum(int(report[key]) for report in reports)
        for key in ("restorable_count", "stale_count", "malformed_count", "ineligible_exposed_count")
    }
    ok = totals["malformed_count"] == 0 and totals["ineligible_exposed_count"] == 0
    return {
        "ok": ok,
        "detail": (
            f"describe exposure 可恢复 {totals['restorable_count']} 项，待自动清理的旧 schema {totals['stale_count']} 项"
            if ok else
            f"发现 exposure 异常：格式错误 {totals['malformed_count']}、越出 eligible {totals['ineligible_exposed_count']}"
        ),
        "info": {"sessions": len(reports), "unscoped": unscoped, **totals},
    }


def _described_cache_reports(user_id: int) -> tuple[list[dict[str, Any]], int]:
    with Session(engine) as session:
        user = session.get(User, user_id)
        configs = {
            int(cfg.id): cfg for cfg in session.exec(
                select(AssistantAIConfig).where(AssistantAIConfig.user_id == user_id)
            ).all() if cfg.id is not None
        }
        rows = session.exec(select(ChatSession).where(ChatSession.user_id == user_id)).all()
        views, reports, unscoped = {}, [], 0
        for row in rows:
            raw = str(getattr(row, "described_tools_json", "") or "").strip()
            config_id = getattr(row, "ai_config_id", None)
            cfg = configs.get(int(config_id)) if config_id is not None else None
            if not raw or raw == "{}":
                continue
            if user is None or cfg is None:
                unscoped += 1
                continue
            if int(cfg.id) not in views:
                views[int(cfg.id)] = resolve_scoped_tool_view(
                    session, user, cfg, ToolViewRequest(ai_config_id=cfg.id)
                )
            reports.append(inspect_described_cache(views[int(cfg.id)], raw))
    return reports, unscoped


def device_catalogs_check(user_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        rows = session.exec(select(DevicePresence).where(DevicePresence.user_id == user_id)).all()
    report = inspect_online_device_catalogs(rows)
    return {
        "ok": report["ok"],
        "detail": (
            f"{report['online_count']} 台在线设备的 catalog generation/hash 完整"
            if report["ok"] else f"{report['invalid_count']} 个在线设备 catalog 异常"
        ),
        "info": report,
    }
