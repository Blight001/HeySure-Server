"""Bridge external-member conversations to the first-party Codex device."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from sqlmodel import Session, select

from api.database import engine
from api.models.external_control import ExternalControllerTurn
from api.models.maintenance import MaintenanceEvent, MaintenanceTask
from api.services.external_control.device_turns import (
    claim_for_device, complete_from_device, requeue_orphaned_device_turns, turn_context,
)
from api.services.maintenance import CreateTaskSpec, EventRecord, MaintenanceService
from api.services.maintenance.views import run_start_payload
from api.sio import agents, sio


logger = logging.getLogger(__name__)
_DEDUPE_PREFIX = "external_turn:"


async def run_conversation_bridge(stop_event: asyncio.Event) -> None:
    with Session(engine) as session:
        recovered = requeue_orphaned_device_turns(session)
        if recovered:
            logger.warning("requeued %s orphaned Codex conversation turns", recovered)
    while not stop_event.is_set():
        try:
            await dispatch_queued_turns()
        except Exception:
            logger.exception("Codex conversation bridge sweep failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            continue


async def dispatch_queued_turns(limit: int = 10) -> int:
    with Session(engine) as session:
        rows = session.exec(select(ExternalControllerTurn).where(
            ExternalControllerTurn.status == "queued",
        ).order_by(ExternalControllerTurn.created_at.asc()).limit(limit)).all()
        candidates = [(row.turn_id, _live_device(row.user_id, row.ai_config_id)) for row in rows]
    dispatched = 0
    for turn_id, target in candidates:
        if target is None:
            continue
        sid, device_id = target
        if await _dispatch_turn(turn_id, sid, device_id):
            dispatched += 1
    return dispatched


def _live_device(user_id: int, ai_config_id: int) -> Optional[tuple[str, str]]:
    for sid, agent in list(agents.items()):
        if int(agent.get("userId") or 0) != int(user_id):
            continue
        if str(agent.get("platform") or "").lower() != "codex-maintainer":
            continue
        bound = {int(value) for value in agent.get("boundAiConfigIds") or []}
        if int(ai_config_id) in bound and bool(agent.get("dispatchable", True)):
            return sid, str(agent.get("id") or "")
    return None


async def _dispatch_turn(turn_id: str, sid: str, device_id: str) -> bool:
    with Session(engine) as session:
        turn = session.get(ExternalControllerTurn, turn_id)
        if turn is None or turn.status != "queued":
            return False
        context = turn_context(session, turn)
        task = _conversation_task(session, turn, device_id, context)
        claimed = claim_for_device(session, turn_id, device_id)
        if claimed is None:
            return False
        command_id = f"run_start:{task.run_id}"
        payload = {"command_id": command_id, "command": "run_start", **run_start_payload(task)}
        event = MaintenanceService(session).append_event(task, EventRecord(
            "command.run_start", "member", payload,
            event_id=f"cmd:{command_id}", actor_id=str(turn.ai_config_id),
        ))
        session.commit()
        session.refresh(task)
        session.refresh(event)
        mapped = {("commandId" if key == "command_id" else key): value for key, value in payload.items()}
    await sio.emit("codex:run_start", {
        "taskId": task.task_id, "runId": task.run_id,
        "lastSequence": task.last_sequence,
        "lastDeviceSequence": task.last_device_sequence,
        **mapped,
    }, to=sid)
    logger.info("dispatched external conversation turn=%s task=%s", turn_id, task.task_id)
    return True


def _conversation_task(
    session: Session, turn: ExternalControllerTurn, device_id: str, context: dict[str, Any]
) -> MaintenanceTask:
    history = "\n".join(
        f"{item['role']}: {item['content']}" for item in context.get("history", [])
    )[-80_000:]
    prompt = (
        "这是用户在 HeySure 网页端与数字成员的直接对话。请直接回答最新用户消息；"
        "若需要维护当前项目，可在隔离工作树中检查和修改。最终答案会自动回写原会话，"
        "不要要求用户去其他页面查看。\n\n会话历史：\n" + history
    )
    return MaintenanceService(session).create_task(turn.user_id, CreateTaskSpec(
        maintainer_ai_config_id=turn.ai_config_id,
        reporter_ai_config_id=turn.ai_config_id,
        device_id=device_id,
        title=f"德克萨斯对话：{turn.session_name}"[:500],
        description=prompt,
        acceptance_criteria="返回清晰的最终答复，并将结果写回原 HeySure 会话。",
        affected_repo="HeySure_AI_2.0",
        source_session_id=turn.session_id,
        severity="normal",
        dedupe_key=f"{_DEDUPE_PREFIX}{turn.turn_id}",
    ))


def complete_conversation_task(task: MaintenanceTask) -> bool:
    turn_id = _turn_id(task)
    if not turn_id:
        return False
    with Session(engine) as session:
        turn = session.get(ExternalControllerTurn, turn_id)
        if turn is None:
            return False
        if turn.status == "succeeded" and turn.assistant_message_id is not None:
            return True
        if turn.status != "running":
            return False
        summary = str(task.summary or "").strip() or _latest_final_answer(session, task.task_id)
        complete_from_device(
            session, turn_id, task.device_id, status=task.status,
            content=summary, error=task.error_code,
        )
    logger.info("completed external conversation turn=%s task=%s", turn_id, task.task_id)
    return True


def _turn_id(task: MaintenanceTask) -> str:
    key = str(task.dedupe_key or "")
    return key[len(_DEDUPE_PREFIX):] if key.startswith(_DEDUPE_PREFIX) else ""


def _latest_final_answer(session: Session, task_id: str) -> str:
    rows = session.exec(select(MaintenanceEvent).where(
        MaintenanceEvent.task_id == task_id,
        MaintenanceEvent.event_type == "item/completed",
    ).order_by(MaintenanceEvent.sequence.desc()).limit(20)).all()
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        item = ((payload.get("data") or {}).get("item") or {})
        if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
            return str(item.get("text") or "")[:100_000]
    return ""
