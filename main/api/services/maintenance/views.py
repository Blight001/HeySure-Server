"""Stable public DTO projections shared by Gateway and Connector Runtime."""

import json

from api.models.maintenance import MaintenanceApproval, MaintenanceEvent, MaintenanceTask

from .service import MaintenanceService


def task_dto(row: MaintenanceTask) -> dict:
    data = MaintenanceService.task_payload(row)
    return {
        **data, "id": row.task_id, "completed_at": row.finished_at,
        "result_summary": row.summary, "branch_name": row.branch_name, "base_sha": row.base_sha,
    }


def event_dto(row: MaintenanceEvent) -> dict:
    raw = MaintenanceService.event_payload(row)
    metadata = raw["payload"] if isinstance(raw["payload"], dict) else {"value": raw["payload"]}
    nested = metadata.get("data") if isinstance(metadata.get("data"), dict) else {}
    item = nested.get("item") if isinstance(nested.get("item"), dict) else {}
    kind = str(metadata.get("type") or raw["event_type"])[:100]
    summary = str(
        metadata.get("summary") or metadata.get("message") or nested.get("delta")
        or nested.get("message") or nested.get("command") or item.get("type")
        or nested.get("status") or kind
    )
    detail = nested.get("detail") or metadata.get("detail") or ""
    return {
        **raw, "kind": kind, "summary": summary[:1000],
        "detail": str(detail)[:100_000],
        "actor": raw["actor_type"], "visibility": "user", "metadata": metadata,
    }


def approval_dto(row: MaintenanceApproval) -> dict:
    try:
        detail = json.loads(row.detail_json or "{}")
    except Exception:
        detail = {}
    return {
        "id": row.approval_id, "approval_id": row.approval_id, "task_id": row.task_id,
        "run_id": row.run_id, "kind": row.approval_type, "approval_type": row.approval_type,
        "title": row.title, "description": str(detail.get("description") or ""),
        "detail": detail, "status": row.status, "decision": row.decision,
        "created_at": row.created_at, "expires_at": row.expires_at, "decided_at": row.decided_at,
    }


def run_start_payload(task: MaintenanceTask) -> dict:
    controller = str(task.dedupe_key or "").startswith("external_turn:")
    identity = (
        "你是 OpenAI Codex，是当前 HeySure_AI_2.0 项目的独立核心维护控制器。"
        "你不是德克萨斯，也不是任何数字成员；网页中的成员名称只是把用户消息转发给你的入口。"
        "普通对话直接回答，不要为了确认身份调用工具。需要检查、修改或部署项目时，"
        "使用当前 Codex 环境已经配置的 HeySure MCP、baota MCP 和本地工具，并遵守项目 AGENTS.md。\n\n"
        if controller else ""
    )
    request_label = "远程请求" if controller else "维护工单"
    payload = {
        "task": task_dto(task),
        "prompt": (
            identity + f"{request_label} {task.task_id}：{task.title}\n\n{task.description}\n\n"
            f"验收标准：\n{task.acceptance_criteria or '完成问题修复并通过相关验证。'}\n\n"
            f"影响仓库：{task.affected_repo or '请先诊断'}"
        ),
        "approvalPolicy": "unlessTrusted",
        "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False},
    }
    if controller:
        payload.update({
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "workspaceMode": "current",
            "trustedMcpServers": [f"heysure_member_{task.reporter_ai_config_id}", "baota"],
        })
    return payload
