"""Unified toolbox MCP for private, AI-owned workflow cards and their runs."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.core.settings import settings
from api.database import engine
from api.runtime.run_context import get_run_session_context
from api.models import (
    ChatRun,
    DevicePresence,
    WorkflowCard,
    WorkflowCardVersion,
    WorkflowConfirmation,
    WorkflowRun,
    WorkflowStepRun,
)
from api.services.workflows.ai_interaction import (
    ai_review_payload,
    create_validated_run,
    respond_ai_interaction,
)
from api.services.workflows.audit import add_audit
from api.services.workflows.card_service import (
    card_payload,
    create_card,
    delete_card,
    update_card,
    validate_card,
    refresh_tool_contracts,
    version_payload,
)
from api.services.workflows.compiler import WorkflowValidationError
from api.services.workflows.run_service import RunActorContext, cancel_run, run_payload
from api.services.workflows.schemas import CardCreate, CardUpdate
from api.services.workflows.secrets import decrypt_json
from api.services.workflows.trace import definition_from_trace
from api.services.workflows.patch_service import patch_card_definition
from api.services.workflows.definition_replace_service import replace_card_definition
from api.services.workflows.payload_selection import select_card_payload
from api.services.workflows.preview_token import consume_preview_token
from api.services.workflows.recording_service import (
    active_recording,
    recording_payload,
    start_recording,
    stop_recording,
)
from tools.automation_access import (
    _admin_actor,
    _card_visible,
    _creation_tags,
    _pending_ai_review_guidance,
    _public_card_creator,
    _updated_tags,
)



TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}
PAUSABLE_RUN_STATUSES = {
    "pending", "running", "retry_wait", "paused_offline",
    "waiting_ai",
}

AUTOMATION_DEFINITION_GUIDANCE = (
    "definition 使用工作流 Schema v1：顶层至少包含 schemaVersion=1、startStepId 和非空 steps；"
    "可选 inputSchema、limits、output、compatibility。steps 是以步骤 ID 为键的对象，支持以下节点："
    "① mcp：{type:'mcp',toolRef:{namespace:'device',name,deviceId},arguments:{},saveAs,next}。"
    "deviceId 可指向当前 AI 有权调用的任意已绑定设备；不同节点可分别使用桌面端、Linux、浏览器、"
    "Android 或自建设备上的 MCP，不限于浏览器自动化。服务端自动从节点汇总契约设备。"
    "可选 timeoutSeconds、totalTimeoutSeconds、resultProjection、retryPolicy、onError、targetResolver；"
    "② condition：{type:'condition',expression,onTrue,onFalse}，expression 只支持 "
    "eq/ne/gt/gte/lt/lte/exists/contains/startsWith/endsWith/and/or/not；"
    "③ delay：{type:'delay',delaySeconds,next}；"
    "④ ai：{type:'ai',prompt,saveAs,next}，运行到此节点时暂停并把此前完整步骤轨迹和 prompt 返回给负责的 AI；"
    "AI 完成审核或指定任务后调用 action=respond，通过 parameters 回传参数并从 next 继续，"
    "可用 onError 指向拒绝或失败分支；⑤ end：{type:'end'}，可选 output；"
    "⑥ card：{type:'card',cardRef:{id,versionId},input:{},saveAs,next,onError}，用于确定性调用子卡片。"
    "发布时服务端会校验同一用户所有权和调用权限，把省略的 versionId 固定为当时最新版，并将固定版本编译进父卡；"
    "条件分支选择图片/视频等已有卡片时必须直接使用 condition→card，不要用 ai 节点判断后再调用 automation.manage。"
    "card input 按子卡 inputSchema 校验，完成输出保存到 steps.<saveAs>.result；子卡失败、超时或超过转换限制"
    "按 onError 传播（省略或 fail 则父运行失败），取消父运行会一并取消当前子卡执行。禁止直接或间接循环引用。"
    "参数和输出可用 ${input.<字段>}、${steps.<saveAs>.result.<字段>}、"
    "${steps.<saveAs>.error.<字段>} 模板；input 字段必须先在 inputSchema.properties 声明。"
    "录制后逐节点核对实际返回结构，避免多写或少写 result 层；JSON Schema 的 default 不会注入 input，"
    "模板也不支持 ${input.x || 'fallback'} 这类短路表达式，可选输入应使用 condition 明确分支兜底。"
    "condition 一元表达式使用 {op:'exists',value:'${input.x}'}，二元表达式使用 "
    "{op:'eq',left:'${input.x}',right:'value'}；模板引用必须写在 ${} 中，不能把 input.x 当普通字符串。"
    "浏览器 resolver 必须能唯一命中；页面变化或重新 observe 后不得复用旧 ref。点击或导航后的 DOM 过渡"
    "应增加合理 delay，并为关键 observe/click 配置有限 retryPolicy，非重试型歧义应重新定位而非盲目重试。"
    "新增步骤后同步检查 limits.maxTransitions；ai 节点等待人工/AI 确认时应给足 timeoutSeconds（长交互通常"
    "建议 1800 秒），避免 respond 时交互已过期。"
    "浏览器工作流必须声明 compatibility.initialEnvironment 的 description、resetStepId、readyStepId；"
    "reset 必须是 browser+tab reload/replace（replace 还需 url），ready 必须是 browser+wait/observe，"
    "且 start→reset→ready 必须在初始化 next 链上。绑定契约设备在线时，服务端自动回填并校验 schemaDigest；"
    "不要手工猜测摘要，设备离线时应先让设备上线。"
    "所有跳转目标必须存在，流程不得成环，且每条可达路径最终必须到达 end。"
)


AUTOMATION_DEFINITION_SCHEMA = {
    "type": "object",
    "properties": {
        "schemaVersion": {"type": "integer", "enum": [1]},
        "inputSchema": {"type": "object"},
        "startStepId": {"type": "string"},
        "steps": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["mcp", "condition", "delay", "ai", "end", "card"],
                    },
                    "cardRef": {
                        "type": "object",
                        "description": "type=card 时必填；id 必填，versionId 可省略并在发布时固定为最新版。",
                        "properties": {
                            "id": {"type": "string"},
                            "versionId": {"type": "string"},
                            "name": {"type": "string"},
                        },
                        "required": ["id"],
                    },
                    "input": {"type": "object"},
                    "saveAs": {"type": "string"},
                    "next": {"type": "string"},
                    "onError": {"type": "string"},
                },
                "required": ["type"],
            },
        },
        "limits": {"type": "object"},
        "output": {},
        "compatibility": {"type": "object"},
    },
    "required": ["schemaVersion", "startStepId", "steps"],
}


def _require_enabled(*, run: bool = False) -> None:
    if not settings.workflow_cards_enabled:
        raise HTTPException(status_code=404, detail="workflow cards are disabled")
    if run and not settings.workflow_scheduler_enabled:
        raise HTTPException(status_code=503, detail="workflow scheduler is disabled")


def _load(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _accessible_card(
    session: Session,
    user_id: int,
    card_id: str,
    ai_config_id: Optional[int],
    *,
    admin_read: bool = False,
) -> WorkflowCard:
    card = session.exec(select(WorkflowCard).where(
        WorkflowCard.id == str(card_id or ""),
        WorkflowCard.user_id == user_id,
        WorkflowCard.deleted_at.is_(None),
    )).first()
    admin_allowed = bool(card and admin_read and _admin_actor(session, user_id, ai_config_id))
    if not card or (not admin_allowed and not _card_visible(card, ai_config_id)):
        raise HTTPException(status_code=404, detail="CARD_NOT_FOUND")
    return card


def _version(session: Session, card: WorkflowCard, version_id: Optional[str]) -> WorkflowCardVersion:
    selected = str(version_id or card.latest_version_id or "")
    row = session.exec(select(WorkflowCardVersion).where(
        WorkflowCardVersion.id == selected,
        WorkflowCardVersion.card_id == card.id,
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="CARD_VERSION_NOT_FOUND")
    return row


def _compatible_with_device(
    session: Session,
    user_id: int,
    version: Optional[WorkflowCardVersion],
    device_id: str,
) -> bool:
    if not device_id:
        return True
    device = session.exec(select(DevicePresence).where(
        DevicePresence.user_id == user_id,
        DevicePresence.device_id == device_id,
    )).first()
    if not device or not version:
        return False
    contracts = _load(version.tool_contracts_json, {})
    return all(
        not item.get("provider") or item.get("provider") == device.device_type
        for item in contracts.values()
        if isinstance(item, dict)
    )


def _list_cards(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip().lower()
    wanted_tags = {str(item).strip().lower() for item in args.get("tags", []) if str(item).strip()}
    wanted_status = str(args.get("status") or "").strip()
    device_id = str(args.get("device_id") or "").strip()
    limit = min(100, max(1, int(args.get("limit") or 50)))
    with Session(engine) as session:
        statement = select(WorkflowCard).where(
            WorkflowCard.user_id == user_id,
            WorkflowCard.deleted_at.is_(None),
            WorkflowCard.status != "archived",
        )
        if wanted_status:
            statement = statement.where(WorkflowCard.status == wanted_status)
        rows = session.exec(statement.order_by(WorkflowCard.updated_at.desc())).all()
        items = []
        for card in rows:
            if not _card_visible(card, ai_config_id):
                continue
            tags = [str(item) for item in _load(card.tags_json, [])]
            lowered = {item.lower() for item in tags}
            if query and query not in f"{card.name} {card.description} {' '.join(tags)}".lower():
                continue
            if wanted_tags and not wanted_tags.issubset(lowered):
                continue
            latest = session.get(WorkflowCardVersion, card.latest_version_id) if card.latest_version_id else None
            if not _compatible_with_device(session, user_id, latest, device_id):
                continue
            payload = card_payload(card)
            payload.pop("definition", None)
            items.append(payload)
            if len(items) >= limit:
                break
        return {"items": items, "count": len(items)}


def _create_card(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "")
    definition = args.get("definition") if isinstance(args.get("definition"), dict) else {}
    if action == "from_trace":
        definition = definition_from_trace(
            args.get("calls") if isinstance(args.get("calls"), list) else [],
            name=str(args.get("name") or "MCP 轨迹卡片"),
            description=str(args.get("description") or ""),
        )
    with Session(engine) as session:
        owner_id = None if _public_card_creator(session, user_id, ai_config_id) else ai_config_id
        body = CardCreate(
            name=str(args.get("name") or ("MCP 轨迹卡片" if action == "from_trace" else "")),
            description=str(args.get("description") or ""),
            tags=_creation_tags(args.get("tags"), owner_id),
            access_scope="all" if owner_id is None else "owner",
            risk_level=str(args.get("risk_level") or ("normal" if action == "from_trace" else "read_only")),
            definition=definition,
            device_id=str(args.get("device_id") or "") or None,
            default_device_id=str(args.get("default_device_id") or args.get("device_id") or "") or None,
            device_ids=args.get("device_ids") if isinstance(args.get("device_ids"), list) else [],
        )
        return card_payload(create_card(session, user_id, body))


def _edit_card(
    session: Session,
    card: WorkflowCard,
    args: Dict[str, Any],
    user_id: int,
) -> Dict[str, Any]:
    if "definition" in args:
        raise HTTPException(
            status_code=409,
            detail="FULL_DEFINITION_REPLACE_DISABLED: use action=patch with base_version_id",
        )
    values = {
        key: args[key]
        for key in (
            "name", "description", "risk_level", "definition", "device_id", "default_device_id", "device_ids",
            "access_scope", "allowed_ai_config_ids",
        )
        if key in args
    }
    if "tags" in args:
        values["tags"] = _updated_tags(card, args.get("tags"))
    return card_payload(update_card(session, card, CardUpdate(**values), user_id=user_id))


def _manage_card(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action == "list":
        return _list_cards(user_id, args, ai_config_id)
    if action in {"create", "from_trace"}:
        return _create_card(user_id, args, ai_config_id)
    with Session(engine) as session:
        admin_read = action in {"get", "validate", "versions", "get_version"}
        card = _accessible_card(
            session,
            user_id,
            str(args.get("card_id") or ""),
            ai_config_id,
            admin_read=admin_read,
        )
        if action == "get":
            payload = card_payload(card)
            if args.get("version_id"):
                payload["version"] = version_payload(_version(session, card, str(args["version_id"])), include_definition=True)
            return select_card_payload(payload, args)
        if action in {"edit", "update"}:
            return _edit_card(session, card, args, user_id)
        if action == "patch":
            base_version_id = str(args.get("base_version_id") or "")
            operations = args.get("operations") if isinstance(args.get("operations"), list) else []
            if args.get("preview_token") and not args.get("dry_run"):
                operations = consume_preview_token(
                    str(args["preview_token"]), action="patch", user_id=user_id,
                    card_id=card.id, base_version_id=base_version_id,
                )
            return patch_card_definition(
                session, card=card, user_id=user_id,
                base_version_id=base_version_id, operations=operations,
                dry_run=bool(args.get("dry_run")),
            )
        if action == "replace_definition":
            base_version_id = str(args.get("base_version_id") or "")
            definition = args.get("definition")
            if args.get("preview_token") and not args.get("dry_run"):
                definition = consume_preview_token(
                    str(args["preview_token"]), action="replace_definition", user_id=user_id,
                    card_id=card.id, base_version_id=base_version_id,
                )
            if not isinstance(definition, dict):
                raise WorkflowValidationError(["replace_definition requires definition or preview_token"])
            return replace_card_definition(
                session, card=card, user_id=user_id,
                base_version_id=base_version_id, definition=definition,
                dry_run=bool(args.get("dry_run")),
            )
        if action == "delete":
            delete_card(session, card)
            return {"deleted": True, "card_id": card.id}
        if action == "validate":
            return validate_card(
                card, session,
                contract_check=str(args.get("contract_check") or "live"),
                version_id=str(args.get("version_id") or "") or None,
            )
        if action == "refresh_contracts":
            return refresh_tool_contracts(
                session, row=card, user_id=user_id,
                base_version_id=str(args.get("base_version_id") or ""),
                tools=args.get("tools") if isinstance(args.get("tools"), list) else None,
                step_ids=args.get("contract_step_ids") if isinstance(args.get("contract_step_ids"), list) else None,
                only_incompatible=bool(args.get("only_incompatible")),
                dry_run=bool(args.get("dry_run")),
            )
        if action == "versions":
            rows = session.exec(select(WorkflowCardVersion).where(
                WorkflowCardVersion.card_id == card.id,
            ).order_by(WorkflowCardVersion.version_number.desc())).all()
            return {"items": [version_payload(row, include_contracts=args.get("trace_mode") == "full") for row in rows]}
        if action == "get_version":
            return version_payload(
                _version(session, card, str(args.get("version_id") or "")), include_definition=True,
                include_contracts=args.get("trace_mode") == "full",
            )
    raise HTTPException(status_code=400, detail="unsupported card action")


def _run_for_ai(
    session: Session,
    user_id: int,
    run_id: str,
    ai_config_id: Optional[int],
    *,
    assigned_interaction: bool = False,
    lock: bool = False,
) -> WorkflowRun:
    statement = select(WorkflowRun).where(
        WorkflowRun.id == str(run_id or ""),
        WorkflowRun.user_id == user_id,
    )
    if lock:
        statement = statement.with_for_update()
    run = session.exec(statement).first()
    if not run:
        raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
    if not ai_config_id:
        return run
    if run.actor_type == "ai" and run.actor_id == str(ai_config_id):
        return run
    if assigned_interaction:
        pending = session.exec(select(WorkflowConfirmation.id).where(
            WorkflowConfirmation.run_id == run.id,
            WorkflowConfirmation.ai_config_id == int(ai_config_id),
            WorkflowConfirmation.confirmation_type == "ai_review",
            WorkflowConfirmation.status == "pending",
        )).first()
        if pending:
            return run
    raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")


def _chat_origin(args: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Normalize origin context before creating a workflow run.

    The MCP call may cross the AI/runtime boundary with only the explicit
    current_session_id still available.  Do not require both fields from the
    context, otherwise the notifier silently creates an isolated chat.
    """
    context = get_run_session_context() or {}
    args = args if isinstance(args, dict) else {}
    run_id = str(
        context.get("run_id")
        or context.get("chat_run_id")
        or args.get("origin_run_id")
        or ""
    ).strip()
    session_id = str(
        context.get("session_id")
        or context.get("current_session_id")
        or args.get("current_session_id")
        or ""
    ).strip()
    ai_config_id = str(context.get("ai_config_id") or "").strip()
    if not session_id:
        return {}
    origin = {"session_id": session_id}
    if run_id:
        origin["run_id"] = run_id
    if ai_config_id:
        origin["ai_config_id"] = ai_config_id
    return origin


def _pending_interaction(session: Session, run_id: str) -> Optional[WorkflowConfirmation]:
    return session.exec(select(WorkflowConfirmation).where(
        WorkflowConfirmation.run_id == run_id,
        WorkflowConfirmation.confirmation_type == "ai_review",
        WorkflowConfirmation.status == "pending",
    ).order_by(WorkflowConfirmation.created_at.desc())).first()


def _pending_ai_review_result(
    session: Session,
    run: WorkflowRun,
    pending: WorkflowConfirmation,
    ai_config_id: Optional[int],
) -> Dict[str, Any]:
    payload = ai_review_payload(session, run, pending)
    payload.update(_pending_ai_review_guidance(pending, ai_config_id))
    return payload


def _origin_chat_stopped(session: Session, row: WorkflowRun) -> bool:
    variables = _load(row.variables_json, {})
    origin = variables.get("_chat_origin") if isinstance(variables, dict) else None
    origin_run_id = str(origin.get("run_id") or "").strip() if isinstance(origin, dict) else ""
    chat_run = session.exec(select(ChatRun).where(ChatRun.run_id == origin_run_id)).first()
    return bool(chat_run and chat_run.stop_requested)


def _wait_for_original_chat_run(
    run_id: str,
    ai_config_id: Optional[int],
    *,
    poll_seconds: float = 0.5,
) -> Dict[str, Any]:
    """Park the originating MCP call until terminal state or an AI review node."""
    while True:
        with Session(engine) as session:
            row = session.get(WorkflowRun, run_id)
            if not row:
                raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
            if _origin_chat_stopped(session, row):
                row = cancel_run(session, row, "originating chat run was stopped")
            payload = run_payload(row)
            if row.status in TERMINAL_RUN_STATUSES:
                payload["resumed_in_original_chat"] = True
                return payload
            pending = _pending_interaction(session, row.id)
            if pending and pending.confirmation_type == "ai_review":
                payload["pending_ai_review"] = _pending_ai_review_result(
                    session, row, pending, ai_config_id
                )
                payload["resumed_in_original_chat"] = True
                return payload
        time.sleep(max(0.1, poll_seconds))


def _wait_for_debug_run(
    run_id: str,
    ai_config_id: Optional[int],
    *,
    poll_seconds: float = 0.25,
) -> Dict[str, Any]:
    """Wait until a continued debug run pauses, reaches AI review, or terminates."""
    while True:
        with Session(engine) as session:
            row = session.get(WorkflowRun, run_id)
            if not row:
                raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
            payload = run_payload(row)
            if row.status in TERMINAL_RUN_STATUSES or row.status == "paused":
                return payload
            pending = _pending_interaction(session, row.id)
            if pending and pending.confirmation_type == "ai_review":
                payload["pending_ai_review"] = _pending_ai_review_result(
                    session, row, pending, ai_config_id
                )
                return payload
        time.sleep(max(0.1, poll_seconds))


def _start_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    origin = _chat_origin(args)
    with Session(engine) as session:
        _accessible_card(
            session,
            user_id,
            str(args.get("card_id") or ""),
            ai_config_id,
        )
        try:
            row = create_validated_run(
                session,
                user_id=user_id,
                card_id=str(args.get("card_id") or ""),
                device_id=str(args.get("device_id") or ""),
                input_value=args.get("input") if isinstance(args.get("input"), dict) else {},
                version_id=str(args.get("version_id") or "") or None,
                idempotency_key=str(args.get("idempotency_key") or "") or None,
                actor=RunActorContext(
                    actor_type="ai" if ai_config_id else "user",
                    actor_id=str(ai_config_id or user_id),
                    initial_variables={"_chat_origin": origin} if origin else {},
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        payload = run_payload(row)
    if origin:
        return _wait_for_original_chat_run(row.id, ai_config_id)
    return payload


def _list_runs(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    limit = min(100, max(1, int(args.get("limit") or 50)))
    with Session(engine) as session:
        statement = select(WorkflowRun).where(WorkflowRun.user_id == user_id)
        if ai_config_id:
            statement = statement.where(
                WorkflowRun.actor_type == "ai",
                WorkflowRun.actor_id == str(ai_config_id),
            )
        if args.get("card_id"):
            card = _accessible_card(session, user_id, str(args["card_id"]), ai_config_id)
            statement = statement.where(WorkflowRun.card_id == card.id)
        if args.get("status"):
            statement = statement.where(WorkflowRun.status == str(args["status"]))
        rows = session.exec(statement.order_by(WorkflowRun.created_at.desc()).limit(limit)).all()
        return {"items": [run_payload(row) for row in rows], "count": len(rows)}


def _pause_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    with Session(engine) as session:
        run = _run_for_ai(session, user_id, str(args.get("run_id") or ""), ai_config_id, lock=True)
        if run.status == "paused":
            return run_payload(run)
        if run.status not in PAUSABLE_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="RUN_NOT_PAUSABLE")
        busy = session.exec(select(WorkflowStepRun.id).where(
            WorkflowStepRun.run_id == run.id,
            WorkflowStepRun.status.in_(["dispatching", "waiting_device"]),
        )).first()
        if busy:
            raise HTTPException(status_code=409, detail="RUN_BUSY_CANNOT_PAUSE")
        now = time.time()
        variables = _load(run.variables_json, {"steps": {}})
        variables["_automation_control"] = {
            "paused_from": run.status,
            "paused_at": now,
            "paused_wakeup_at": run.next_wakeup_at,
        }
        previous = run.status
        run.variables_json = json.dumps(variables, ensure_ascii=False)
        run.status = "paused"
        run.next_wakeup_at = None
        run.updated_at = now
        run.lock_version += 1
        session.add(run)
        add_audit(session, event_type="run_paused", run=run, status_from=previous, status_to="paused")
        session.commit()
        session.refresh(run)
        return run_payload(run)


def _resume_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    with Session(engine) as session:
        run = _run_for_ai(session, user_id, str(args.get("run_id") or ""), ai_config_id, lock=True)
        if run.status != "paused":
            raise HTTPException(status_code=409, detail="RUN_NOT_PAUSED")
        now = time.time()
        variables = _load(run.variables_json, {"steps": {}})
        control = variables.pop("_automation_control", {})
        if "_debug_single_step" in args:
            debug = variables.setdefault("_debug", {})
            debug["pause_after_step"] = bool(args.get("_debug_single_step"))
        paused_at = float(control.get("paused_at") or now)
        shift = max(0.0, now - paused_at)
        restored = str(control.get("paused_from") or "pending")
        if restored not in PAUSABLE_RUN_STATUSES:
            restored = "pending"
        run.deadline_at += shift
        run.status = restored
        run.variables_json = json.dumps(variables, ensure_ascii=False)
        paused_wakeup = control.get("paused_wakeup_at")
        run.next_wakeup_at = (
            float(paused_wakeup) + shift
            if paused_wakeup is not None and restored in {"retry_wait", "paused_offline"}
            else now if restored in {"pending", "running", "retry_wait", "paused_offline"} else None
        )
        run.updated_at = now
        run.lock_version += 1
        steps = session.exec(select(WorkflowStepRun).where(
            WorkflowStepRun.run_id == run.id,
            WorkflowStepRun.status.notin_(["succeeded", "failed", "timed_out", "cancelled"]),
        )).all()
        for step in steps:
            step.deadline_at += shift
            session.add(step)
        confirmations = session.exec(select(WorkflowConfirmation).where(
            WorkflowConfirmation.run_id == run.id,
            WorkflowConfirmation.status == "pending",
        )).all()
        for item in confirmations:
            item.expires_at += shift
            session.add(item)
        session.add(run)
        add_audit(session, event_type="run_resumed", run=run, status_from="paused", status_to=restored)
        session.commit()
        session.refresh(run)
        return run_payload(run)


def _respond_to_ai_review(
    user_id: int,
    args: Dict[str, Any],
    ai_config_id: Optional[int],
) -> Dict[str, Any]:
    if not ai_config_id:
        raise HTTPException(status_code=403, detail="AI_INTERACTION_REQUIRES_AI")
    with Session(engine) as session:
        run = _run_for_ai(
            session,
            user_id,
            str(args.get("run_id") or ""),
            ai_config_id,
            assigned_interaction=True,
            lock=True,
        )
        pending = _pending_interaction(session, run.id)
        guidance = _pending_ai_review_guidance(pending, ai_config_id) if pending else None
        if guidance and not guidance["can_respond"]:
            raise HTTPException(
                status_code=409,
                detail={"code": "AI_INTERACTION_ACCESS_DENIED", "pending_ai_review": guidance},
            )
        try:
            responded = respond_ai_interaction(
                session,
                run=run,
                user_id=user_id,
                ai_config_id=int(ai_config_id),
                approved=bool(args.get("approved")),
                parameters=args.get("parameters") if isinstance(args.get("parameters"), dict) else {},
                message=str(args.get("message") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        run_id = responded.id
    return _wait_for_original_chat_run(run_id, ai_config_id)


def _manage_run(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action in {"start", "run"}:
        return _start_run(user_id, args, ai_config_id)
    if action == "list_runs":
        return _list_runs(user_id, args, ai_config_id)
    if action == "pause":
        return _pause_run(user_id, args, ai_config_id)
    if action == "resume":
        return _resume_run(user_id, args, ai_config_id)
    if action == "respond":
        return _respond_to_ai_review(user_id, args, ai_config_id)
    if action in {"debug_step", "debug_continue"}:
        resumed_args = dict(args)
        resumed_args["_debug_single_step"] = action == "debug_step"
        resumed = _resume_run(user_id, resumed_args, ai_config_id)
        if action == "debug_continue":
            return _wait_for_debug_run(str(resumed["run_id"]), ai_config_id)
        return resumed
    if action == "debug_start":
        with Session(engine) as session:
            card = _accessible_card(session, user_id, str(args.get("card_id") or ""), ai_config_id)
            seed_steps = args.get("seed_steps") if isinstance(args.get("seed_steps"), dict) else {}
            requested_start = str(args.get("start_step_id") or "")
            prepare_environment = bool(args.get("prepare_environment"))
            debug_state = {"pause_after_step": False}
            actual_start = requested_start
            if prepare_environment and requested_start:
                version = session.get(WorkflowCardVersion, str(args.get("version_id") or card.latest_version_id))
                definition = _load(version.definition_json, {}) if version else {}
                initial = (definition.get("compatibility") or {}).get("initialEnvironment") or {}
                reset_step = str(initial.get("resetStepId") or "")
                ready_step = str(initial.get("readyStepId") or "")
                if not reset_step or not ready_step:
                    raise HTTPException(status_code=422, detail="DEBUG_INITIAL_ENVIRONMENT_NOT_DECLARED")
                actual_start = reset_step
                debug_state.update({
                    "prepare_ready_step_id": ready_step,
                    "prepare_target_step_id": requested_start,
                })
            try:
                row = create_validated_run(
                    session,
                    user_id=user_id,
                    card_id=card.id,
                    device_id=str(args.get("device_id") or ""),
                    input_value=args.get("input") if isinstance(args.get("input"), dict) else {},
                    version_id=str(args.get("version_id") or "") or None,
                    idempotency_key=str(args.get("idempotency_key") or "") or f"debug:{uuid.uuid4().hex}",
                    actor=RunActorContext(
                        actor_type="ai" if ai_config_id else "user",
                        actor_id=str(ai_config_id or user_id),
                        initial_variables={
                            "steps": seed_steps,
                            "_debug": debug_state,
                            "_run_debug_options": {
                                "start_step_id": actual_start,
                                "start_paused": True,
                            },
                        },
                    ),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            return run_payload(row)
    with Session(engine) as session:
        run = _run_for_ai(
            session,
            user_id,
            str(args.get("run_id") or ""),
            ai_config_id,
            lock=action in {"cancel", "debug_restart"},
        )
        if action == "status":
            payload = run_payload(run)
            pending = session.exec(select(WorkflowConfirmation).where(
                WorkflowConfirmation.run_id == run.id,
                WorkflowConfirmation.confirmation_type == "ai_review",
                WorkflowConfirmation.status == "pending",
            ).order_by(WorkflowConfirmation.created_at.desc())).first()
            if pending:
                payload["pending_ai_review"] = _pending_ai_review_result(
                    session, run, pending, ai_config_id
                )
            return payload
        if action == "cancel":
            return run_payload(cancel_run(session, run, str(args.get("reason") or "cancelled by AI")))
        if action == "debug_restart":
            try:
                return run_payload(create_validated_run(
                    session,
                    user_id=user_id,
                    card_id=run.card_id,
                    device_id=str(args.get("device_id") or run.device_id),
                    input_value=args.get("input") if isinstance(args.get("input"), dict) else decrypt_json(run.input_json),
                    version_id=str(args.get("version_id") or run.card_version_id),
                    idempotency_key=str(args.get("idempotency_key") or "") or f"debug-restart:{run.id}:{uuid.uuid4().hex}",
                    actor=RunActorContext(
                        actor_type="ai" if ai_config_id else "user",
                        actor_id=str(ai_config_id or user_id),
                        initial_variables={
                            "steps": args.get("seed_steps") if isinstance(args.get("seed_steps"), dict) else {},
                            "_debug": {"pause_after_step": False},
                            "_run_debug_options": {
                                "start_step_id": str(args.get("start_step_id") or run.current_step_id),
                                "start_paused": True,
                            },
                        },
                    ),
                ))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
    raise HTTPException(status_code=400, detail="unsupported run action")


def _created_card_reference(card: WorkflowCard, version: Optional[WorkflowCardVersion]) -> Dict[str, str]:
    return {
        "card_id": card.id,
        "version_id": card.latest_version_id,
        "definition_digest": version.definition_digest if version else "",
    }


def _manage_recording(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    with Session(engine) as session:
        if action == "record_start":
            row = start_recording(
                session,
                user_id=user_id,
                ai_config_id=ai_config_id,
                name=str(args.get("name") or "操作录制"),
                description=str(args.get("description") or ""),
                default_device_id=str(args.get("default_device_id") or args.get("device_id") or ""),
                device_ids=args.get("device_ids") if isinstance(args.get("device_ids"), list) else [],
            )
            payload = recording_payload(session, row)
            payload["guidance"] = (
                "录制已开启；继续正常调用工具，完成后调用 record_stop。浏览器流程的首屏重置应使用 "
                "browser+tab reload/replace；若误用带 url 的 navigate，停止录制时会自动归一化为 replace。"
                "browser+wait 无参数时默认仅等待 1000ms，需要更长等待请显式传 ms。"
            )
            return payload
        row = active_recording(session, user_id, ai_config_id, lock=action in {"record_stop", "record_cancel"})
        if action == "record_status":
            return recording_payload(session, row, include_events=True) if row else {"status": "idle"}
        stopped = stop_recording(session, user_id, ai_config_id, cancel=action == "record_cancel")
        if not stopped:
            raise HTTPException(status_code=404, detail="ACTIVE_RECORDING_NOT_FOUND")
        payload = recording_payload(session, stopped, include_events=True)
        if action == "record_cancel" or not bool(args.get("create_card")):
            return payload
        calls = [
            item for item in payload.get("calls", [])
            if item.get("success") and str(item.get("device_id") or "").strip()
        ]
        if not calls:
            raise HTTPException(status_code=422, detail="recording has no successful device calls")
        definition = definition_from_trace(
            calls, name=stopped.name, description=stopped.description,
            compact=bool(args.get("compact_recording", True)),
        )
        owner_id = None if _public_card_creator(session, user_id, ai_config_id) else ai_config_id
        body = CardCreate(
            name=str(args.get("name") or stopped.name or "录制生成卡片"),
            description=str(args.get("description") or stopped.description or ""),
            tags=_creation_tags(args.get("tags"), owner_id),
            access_scope="owner" if owner_id else "all",
            risk_level=str(args.get("risk_level") or "normal_change"),
            definition=definition,
            default_device_id=stopped.default_device_id or None,
            device_ids=_load(stopped.device_ids_json, []),
        )
        card = create_card(session, user_id, body)
        payload["created_card"] = card_payload(card)
        created_version = session.get(WorkflowCardVersion, card.latest_version_id)
        payload.update(_created_card_reference(card, created_version))
        return payload


def _automation_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    _require_enabled(run=action in {"start", "run", "resume", "respond", "debug_start", "debug_step", "debug_continue", "debug_restart"})
    try:
        if action in {
            "list", "get", "create", "from_trace", "edit", "update", "patch", "replace_definition",
            "delete", "validate", "refresh_contracts", "versions", "get_version",
        }:
            return _manage_card(user_id, args, ai_config_id)
        if action in {
            "start", "run", "list_runs", "status", "pause", "resume",
            "cancel", "respond",
            "debug_start", "debug_step", "debug_continue", "debug_restart",
        }:
            return _manage_run(user_id, args, ai_config_id)
        if action in {"record_start", "record_status", "record_stop", "record_cancel"}:
            return _manage_recording(user_id, args, ai_config_id)
    except WorkflowValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "CARD_VALIDATION_FAILED", "errors": exc.errors, "warnings": exc.warnings},
        )
    raise HTTPException(status_code=400, detail="unsupported automation.manage action")


AUTOMATION_MANAGE_SCHEMA = {
    "type": "object",
    "description": (
        "自动化卡片创建原则：完整流程必须优先使用录制方案，因为录制会保存实际成功的工具名、参数、"
        "设备绑定和调用顺序，比 AI 凭空手写 definition 更稳定。标准流程是 record_start → 在真实环境中"
        "逐步调用工具完成任务 → record_stop(create_card=true) → validate → debug_start/debug_step 验证。"
        "录制生成后若只有参数模板、变量路径、等待时间等小细节需要调整，使用 patch + "
        "base_version_id 做最小局部修改。不要为了小改动整体替换卡片；只有基于已审核定义进行结构性重构时，"
        "才使用 replace_definition 并先 dry_run。仅在无法进入真实环境录制、或用户明确提供了完整且已审核的"
        "结构化定义时，才使用 create/from_trace。卡片元数据使用 edit。"
    ),
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "list", "get", "create", "from_trace", "edit", "patch", "replace_definition", "delete",
                "validate", "refresh_contracts", "versions", "get_version", "start",
                "list_runs", "status", "pause", "resume", "cancel", "respond",
                "record_start", "record_status", "record_stop", "record_cancel",
                "debug_start", "debug_step", "debug_continue", "debug_restart",
            ],
            "description": (
                "唯一动作选择器；聊天中的 start 会停留在当前 MCP 调用，并持续到卡片终态或 AI 审核节点，"
                "新建完整卡片优先选择 record_start/record_stop，录制后的小细节使用 patch；"
                "不要默认使用 create/from_trace 手写完整流程；"
                "不要重复轮询 status；status 查看已有运行且会返回 pending_ai_review；"
                "respond 仅处理分配给当前 AI 的 waiting_ai 审核交互；"
                "pause/resume 暂停恢复；已有卡片的小改动用 patch，结构性重构用 replace_definition；"
                "debug_start 从任意步骤暂停创建调试运行，debug_step 单步，debug_continue 连续运行。"
            ),
        },
        "card_id": {"type": "string"},
        "version_id": {"type": "string"},
        "fields": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "仅用于 action=get 的结构化返回字段过滤；省略时保持原完整返回。card 选择卡片顶层字段，"
                "definition 选择 definition 顶层字段，version 选择指定版本的顶层字段。步骤选择启用时，"
                "card 必须包含 definition 或 version，对应 definition 字段必须包含 steps。card 支持 card_id→id、version_id→latest_version_id 别名。"
            ),
            "properties": {
                "card": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                "definition": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
                "version": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
            },
        },
        "step_ids": {
            "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100,
            "description": "action=get 时按给定顺序只返回指定步骤；与 tail、步骤分页参数互斥。",
        },
        "step_offset": {
            "type": "integer", "minimum": 0, "maximum": 10000,
            "description": "action=get 的步骤分页偏移，默认 0；与 step_ids、tail 互斥。",
        },
        "step_limit": {
            "type": "integer", "minimum": 1, "maximum": 100,
            "description": "action=get 的步骤分页数量，默认 20；与 step_ids、tail 互斥。",
        },
        "tail": {
            "type": "integer", "minimum": 1, "maximum": 100,
            "description": "action=get 时只返回 definition 最后 N 个步骤；与 step_ids、步骤分页参数互斥。",
        },
        "base_version_id": {
            "type": "string",
            "description": "action=patch/replace_definition 必填，必须等于最新版本 ID，用于防止 AI 覆盖其他人的新修改。",
        },
        "contract_check": {
            "type": "string", "enum": ["live", "definition"], "default": "live",
            "description": "validate 默认 live：同时检查当前设备在线状态与工具 Schema；definition 仅静态校验。",
        },
        "tools": {
            "type": "array", "items": {"type": "string"}, "maxItems": 100,
            "description": "refresh_contracts 可选工具名范围；省略时刷新全卡片 MCP 契约。",
        },
        "contract_step_ids": {
            "type": "array", "items": {"type": "string"}, "maxItems": 100,
            "description": "refresh_contracts 可选步骤范围；省略时按 tools 或全卡片刷新。",
        },
        "only_incompatible": {
            "type": "boolean", "default": False,
            "description": "refresh_contracts 仅刷新实时摘要不兼容的步骤；为 true 时可省略 base_version_id，自动基于最新版刷新。",
        },
        "trace_mode": {
            "type": "string", "enum": ["summary", "full", "none"], "default": "summary",
            "description": "start/status/respond 的审核轨迹视图；默认 summary，完整轨迹需显式 full。",
        },
        "wait_until": {
            "type": "string", "enum": ["created", "ai_or_terminal"], "default": "ai_or_terminal",
            "description": "start 返回时机；created 仅创建运行，ai_or_terminal 等到 AI 审核或终态。",
        },
        "preview_token": {
            "type": "string",
            "description": (
                "patch/replace_definition dry_run 返回的 15 分钟短期提交令牌；正式提交时可只传 "
                "preview_token + base_version_id，无需重复 operations/definition。令牌绑定用户、卡片、动作和基础版本。"
            ),
        },
        "operations": {
            "type": "array", "minItems": 1, "maxItems": 100,
            "description": (
                "录制卡片后的首选修正方式。使用 RFC 6902 风格局部修改，只修正参数模板、变量路径、"
                "等待时间等小细节；仅支持 add/replace/remove/test，且仅允许修改 definition 流程字段。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["add", "replace", "remove", "test"]},
                    "path": {
                        "type": "string",
                        "description": (
                            "definition 内的绝对 JSON Pointer，但不要带 /definition 前缀。允许的顶层路径："
                            "/name、/description、/inputSchema、/startStepId、/steps、/limits、/output、"
                            "/requiredCapabilities、/compatibility。例如 /inputSchema/properties/prompt、"
                            "/steps/open_page/arguments/url 或 /steps/new_step。add 会创建缺失的中间对象。"
                        ),
                    },
                    "value": {},
                },
                "required": ["op", "path"],
            },
        },
        "run_id": {"type": "string"},
        "start_step_id": {
            "type": "string",
            "description": "debug_start/debug_restart 的起始步骤 ID；可从任意存在的步骤开始。",
        },
        "seed_steps": {
            "type": "object",
            "description": "从中间步骤启动时注入此前步骤变量，结构为 {saveAs: {result: ...}}。模板依赖缺失会安全失败。",
        },
        "current_session_id": {
            "type": "string",
            "description": "启动卡片的原始聊天会话 ID；跨 Runtime 时用于将 AI 控制节点回连到当前对话。",
        },
        "origin_run_id": {
            "type": "string",
            "description": "可选的原始 ChatRun ID；与 current_session_id 一起用于精确回连。",
        },
        "prepare_environment": {
            "type": "boolean",
            "description": (
                "debug_start 从中间步骤调试浏览器卡片时设为 true：先执行 "
                "compatibility.initialEnvironment 的 reset→ready 初始化链，完成后自动暂停在 start_step_id。"
            ),
        },
        "name": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "access_scope": {"type": "string", "enum": ["all", "owner", "selected"]},
        "allowed_ai_config_ids": {
            "type": "array", "items": {"type": "integer"}, "maxItems": 200,
            "description": "access_scope=selected 时允许调用卡片的 AI 成员配置 ID。",
        },
        "risk_level": {"type": "string"},
        "definition": {
            **AUTOMATION_DEFINITION_SCHEMA,
            "description": (
                "供 create 或 replace_definition 使用的完整定义。不要凭空手写复杂流程；优先实战录制生成，"
                "小细节用 patch；基于已审核版本做结构性重构时用 replace_definition。元数据仍使用 edit。"
                + AUTOMATION_DEFINITION_GUIDANCE
            ),
        },
        "dry_run": {
            "type": "boolean",
            "description": (
                "用于 patch/replace_definition；true 时执行完整编译、设备契约校验并返回路径级和步骤级 diff，"
                "但不修改卡片、不提交事务、不创建版本。响应会明确 applied/committed/version_created；"
                "确认后设为 false 才原子创建不可变新版本。"
            ),
        },
        "calls": {
            "type": "array", "minItems": 1, "maxItems": 200, "items": {"type": "object"},
            "description": (
                "仅供 from_trace 导入已真实执行、顺序明确的结构化调用轨迹；不要由 AI 猜测工具返回结构后伪造轨迹。"
            ),
        },
        "create_card": {
            "type": "boolean",
            "description": (
                "action=record_stop 时设为 true：把录制中真实成功的调用编译、验证并保存为不可变卡片版本。"
                "这是创建完整自动化卡片的默认推荐方案。"
            ),
        },
        "compact_recording": {
            "type": "boolean",
            "default": True,
            "description": (
                "action=record_stop(create_card=true) 时是否精简录制，默认 true：合并同一设备连续的 observe，"
                "保留最后一次供后续 ref/语义解析使用；设为 false 可完整保留成功调用。"
            ),
        },
        "device_id": {
            "type": "string",
            "description": "action=start/debug_start 时可覆盖卡片默认设备；省略时由各 mcp 节点的 toolRef.deviceId 决定。没有 mcp 节点的卡片无需设备。",
        },
        "default_device_id": {
            "type": "string",
            "description": "可选默认设备号；新卡片优先为每个 mcp 节点填写 toolRef.deviceId。",
        },
        "device_ids": {
            "type": "array", "items": {"type": "string"}, "maxItems": 20,
            "description": "可选兼容字段。服务端会从所有 mcp 节点的 toolRef.deviceId 自动汇总契约设备，无需重复填写。",
        },
        "input": {"type": "object"},
        "idempotency_key": {"type": "string"},
        "query": {"type": "string"},
        "status": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "reason": {"type": "string"},
        "approved": {
            "type": "boolean",
            "description": "仅用于 action=respond 且 status 返回 can_respond=true 的 AI 审核交互。",
        },
        "parameters": {"type": "object"},
        "message": {"type": "string"},
    },
    "required": ["action"],
}
