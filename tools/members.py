"""``member.manage`` — library-bound AI digital-member administration.

Member deletion is deliberately absent.  The web console remains the only
place where a human can confirm that destructive operation.
"""

import json
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import (
    AITaskJob,
    AssistantAIConfig,
    DeviceAiBinding,
    DevicePresence,
    User,
)
from api.services.model_presets import normalize_model_presets
from api.services.tasks.task_system import compact_system_auto_control
from mcp_runtime.mcp.permissions import clamp_tools_json, config_role_tier
from tools.tasks import TASK_MANAGE_SCHEMA, _task_manage


MEMBER_ACTIONS = ("list", "get", "create", "update")
TASK_ACTIONS = ("task_list", "task_create", "task_update", "task_delete")


def _owned_config(session: Session, user_id: int, value: Any) -> AssistantAIConfig:
    try:
        config_id = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="member_id must be an integer")
    row = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.id == config_id,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="AI digital member not found")
    return row


def _identity(value: Any) -> str:
    raw = str(value or "member").strip().lower()
    aliases = {"manager": "manager", "管理者": "manager", "member": "member", "成员": "member"}
    if raw not in aliases:
        raise HTTPException(status_code=400, detail="identity must be member or manager")
    return aliases[raw]


def _token_limit(value: Any, default: int = 10000) -> int:
    try:
        parsed = int(default if value is None else value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="token_limit must be an integer")
    if parsed < 1 or parsed > 2_000_000:
        raise HTTPException(status_code=400, detail="token_limit must be between 1 and 2000000")
    return parsed


def _model_fields(user: User, args: Dict[str, Any], current: Optional[AssistantAIConfig] = None) -> Dict[str, str]:
    presets = normalize_model_presets(user.model_presets, user)
    preset_id = str(args.get("model_preset_id") or "").strip()
    model = str(args.get("model") or "").strip()
    if not preset_id and not model and current is not None:
        return {}
    selected = None
    if preset_id:
        selected = next((item for item in presets if item["id"] == preset_id), None)
    elif model:
        selected = next((item for item in presets if item["model"] == model or item["id"] == model), None)
    elif presets:
        selected = presets[0]
    if selected is None:
        raise HTTPException(status_code=400, detail="Selected model preset not found")
    return {
        "api_key": selected["api_key"],
        "base_url": selected["base_url"],
        "model": selected["model"],
        "model_preset_id": selected["id"],
    }


def _device_rows(session: Session, user_id: int, config_id: int) -> list[dict]:
    bindings = session.exec(
        select(DeviceAiBinding).where(
            DeviceAiBinding.user_id == user_id,
            DeviceAiBinding.ai_config_id == config_id,
        )
    ).all()
    ids = sorted({str(row.device_id or "").strip() for row in bindings if str(row.device_id or "").strip()})
    presence = session.exec(
        select(DevicePresence).where(
            DevicePresence.user_id == user_id,
            DevicePresence.device_id.in_(ids),
        )
    ).all() if ids else []
    by_id = {str(row.device_id): row for row in presence}
    return [
        {
            "device_id": device_id,
            "name": str(getattr(by_id.get(device_id), "remark", "") or getattr(by_id.get(device_id), "name", "") or device_id),
            "type": str(getattr(by_id.get(device_id), "device_type", "") or "custom"),
            "platform": str(getattr(by_id.get(device_id), "platform", "") or ""),
            "online": bool(getattr(by_id.get(device_id), "online", False)),
        }
        for device_id in ids
    ]


def _tasks(session: Session, user_id: int, config_id: int) -> list[dict]:
    rows = session.exec(
        select(AITaskJob).where(
            AITaskJob.user_id == user_id,
            AITaskJob.ai_config_id == config_id,
        ).order_by(AITaskJob.created_at.desc())
    ).all()
    return [
        {
            "job_id": row.job_id,
            "title": row.title,
            "instruction": row.instruction,
            "status": row.status,
            "priority": row.priority,
            "trigger_type": row.trigger_type,
        }
        for row in rows[:100]
    ]


def _member_payload(session: Session, user_id: int, cfg: AssistantAIConfig, *, include_prompt: bool, include_tasks: bool) -> dict:
    from api.devices.workshop_bindings import workshop_device_ids_for_config
    from api.services.knowledge import kb_store

    data = {
        "member_id": int(cfg.id or 0),
        "name": cfg.name,
        "identity": cfg.digital_member_role,
        "platform": cfg.platform,
        "model": cfg.model,
        "model_preset_id": cfg.model_preset_id,
        "token_limit": cfg.token_limit,
        "description": cfg.description,
        "devices": _device_rows(session, user_id, int(cfg.id or 0)),
        "workshop_device_ids": workshop_device_ids_for_config(user_id, cfg.id),
    }
    if include_prompt:
        data["prompt"] = kb_store.effective_ai_prompt(user_id, cfg)
    if include_tasks:
        data["tasks"] = _tasks(session, user_id, int(cfg.id or 0))
    return data


def _write_prompt(user_id: int, cfg: AssistantAIConfig, prompt: str) -> None:
    from api.services.knowledge import kb_store

    kb_store.write_persona(user_id, cfg, prompt=str(prompt or ""))


def _replace_device_bindings(session: Session, user_id: int, config_id: int, raw_ids: Any) -> None:
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="device_ids must be an array")
    wanted = {str(item or "").strip() for item in raw_ids if str(item or "").strip()}
    if len(wanted) != len(raw_ids):
        raise HTTPException(status_code=400, detail="device_ids contains an empty or duplicate id")
    from library.engine import is_builtin_workshop_device_id
    from tools.engine import toolbox_device_id_for_user

    if any(is_builtin_workshop_device_id(item) or item == toolbox_device_id_for_user(user_id) for item in wanted):
        raise HTTPException(status_code=400, detail="Library/toolbox bindings must be changed by a human in Workshop")
    known = session.exec(select(DevicePresence).where(DevicePresence.user_id == user_id)).all()
    known_ids = {str(row.device_id or "").strip() for row in known}
    existing = session.exec(select(DeviceAiBinding).where(DeviceAiBinding.user_id == user_id)).all()
    known_ids |= {str(row.device_id or "").strip() for row in existing}
    missing = sorted(wanted - known_ids)
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown device ids: {', '.join(missing)}")

    now = time.time()
    by_id = {str(row.device_id or "").strip(): row for row in existing}
    for row in existing:
        if row.ai_config_id == config_id and str(row.device_id or "").strip() not in wanted:
            session.delete(row)
    for device_id in wanted:
        row = by_id.get(device_id)
        if row:
            row.ai_config_id = config_id
            row.updated_at = now
            session.add(row)
        else:
            session.add(DeviceAiBinding(user_id=user_id, device_id=device_id, ai_config_id=config_id))
    for row in known:
        device_id = str(row.device_id or "").strip()
        if device_id in wanted:
            row.ai_config_id = config_id
            row.updated_at = now
            session.add(row)
        elif row.ai_config_id == config_id:
            row.ai_config_id = None
            row.updated_at = now
            session.add(row)


def _create_member(session: Session, user: User, args: Dict[str, Any]) -> AssistantAIConfig:
    name = str(args.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    model_fields = _model_fields(user, args)
    cfg = AssistantAIConfig(
        user_id=int(user.id or 0),
        name=name,
        description=str(args.get("description") or ""),
        avatar="ai_avatars1.png",
        **model_fields,
        ai_role="digital_member",
        digital_member_role=_identity(args.get("identity")),
        platform=str(args.get("platform") or "Server-Core").strip() or "Server-Core",
        token_limit=_token_limit(args.get("token_limit")),
        enabled=True,
        mcp_enabled=True,
        switch_key=f"assistant_{int(time.time() * 1000)}",
        system_auto_control=compact_system_auto_control(json.dumps({"enabled": True, "tasks": []}, ensure_ascii=False)),
    )
    cfg.mcp_tools = clamp_tools_json(user, config_role_tier(cfg), cfg.mcp_tools)
    session.add(cfg)
    session.flush()
    _write_prompt(int(user.id or 0), cfg, str(args.get("prompt") or ""))
    if "device_ids" in args:
        _replace_device_bindings(session, int(user.id or 0), int(cfg.id or 0), args.get("device_ids"))
    session.commit()
    session.refresh(cfg)
    try:
        from mcp_runtime.mcp import get_project_root
        get_project_root(user.id, cfg.id)
    except Exception:
        pass
    try:
        from tools.engine import bind_config_to_toolbox
        bind_config_to_toolbox(user.id, cfg.id)
    except Exception:
        pass
    return cfg


def _update_member(session: Session, user: User, cfg: AssistantAIConfig, args: Dict[str, Any]) -> None:
    scalar = {
        "name": "name",
        "description": "description",
        "platform": "platform",
    }
    for arg_name, field_name in scalar.items():
        if arg_name in args:
            value = str(args.get(arg_name) or "").strip()
            if arg_name == "name" and not value:
                raise HTTPException(status_code=400, detail="name cannot be empty")
            setattr(cfg, field_name, value)
    if "identity" in args:
        cfg.digital_member_role = _identity(args.get("identity"))
    if "token_limit" in args:
        cfg.token_limit = _token_limit(args.get("token_limit"))
    for key, value in _model_fields(user, args, cfg).items():
        setattr(cfg, key, value)
    cfg.ai_role = "digital_member"
    cfg.enabled = True
    cfg.mcp_tools = clamp_tools_json(user, config_role_tier(cfg), cfg.mcp_tools)
    cfg.updated_at = time.time()
    session.add(cfg)
    if "prompt" in args:
        _write_prompt(int(user.id or 0), cfg, str(args.get("prompt") or ""))
    if "device_ids" in args:
        _replace_device_bindings(session, int(user.id or 0), int(cfg.id or 0), args.get("device_ids"))
    session.commit()
    session.refresh(cfg)


def _member_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Dict[str, Any]:
    action = str((args or {}).get("action") or "").strip().lower()
    if action in TASK_ACTIONS:
        task_args = dict(args or {})
        task_args["action"] = action.removeprefix("task_")
        if task_args.get("member_id") is not None:
            task_args["target_ai_config_id"] = task_args["member_id"]
        return _task_manage(user_id, task_args, ai_config_id)
    if action not in MEMBER_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported action: {action}. Member deletion is human-only; available: {', '.join(MEMBER_ACTIONS + TASK_ACTIONS)}",
        )
    with Session(engine) as session:
        user = session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if action == "list":
            rows = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user_id,
                    AssistantAIConfig.ai_role == "digital_member",
                ).order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
            ).all()
            return {"count": len(rows), "members": [_member_payload(session, user_id, row, include_prompt=False, include_tasks=False) for row in rows]}
        if action == "get":
            cfg = _owned_config(session, user_id, args.get("member_id"))
            return {"member": _member_payload(session, user_id, cfg, include_prompt=True, include_tasks=bool(args.get("include_tasks", True)))}
        if action == "create":
            cfg = _create_member(session, user, args)
            return {"ok": True, "created": True, "member": _member_payload(session, user_id, cfg, include_prompt=True, include_tasks=True)}
        cfg = _owned_config(session, user_id, args.get("member_id"))
        if str(cfg.ai_role or "") != "digital_member":
            raise HTTPException(status_code=400, detail="Only digital members can be edited with member.manage")
        _update_member(session, user, cfg, args)
        return {"ok": True, "updated": True, "member": _member_payload(session, user_id, cfg, include_prompt=True, include_tasks=True)}


_TASK_PROPERTIES = {key: value for key, value in TASK_MANAGE_SCHEMA["properties"].items() if key != "action"}
MEMBER_MANAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(MEMBER_ACTIONS + TASK_ACTIONS),
            "description": "list/get/create/update 管理成员；task_list/task_create/task_update/task_delete 管理指定成员的后台任务。没有成员删除动作，成员删除必须由人在控制台确认。",
        },
        "member_id": {"type": "integer", "description": "get/update/task_* 的目标数字成员 id。"},
        "name": {"type": "string", "description": "create 必填 / update 可选：成员名称。"},
        "identity": {"type": "string", "enum": ["member", "manager"], "description": "成员身份。"},
        "platform": {"type": "string", "description": "运行平台，例如 Server-Core。"},
        "model": {"type": "string", "description": "账号模型预设中的模型名或预设 id。"},
        "model_preset_id": {"type": "string", "description": "账号中已有的模型预设 id。"},
        "prompt": {"type": "string", "description": "该成员的人格 Prompt；写入 KnowledgeBase/personas 文件。"},
        "token_limit": {"type": "integer", "description": "成员 Token 上限，1-2000000。"},
        "description": {"type": "string", "description": "成员职责/身份说明。"},
        "device_ids": {"type": "array", "items": {"type": "string"}, "description": "create/update：替换该成员绑定的实体端侧设备列表。图书馆和工具箱绑定必须由人在界面操作。"},
        "include_tasks": {"type": "boolean", "description": "get 是否附带任务，默认 true。"},
        **_TASK_PROPERTIES,
    },
    "required": ["action"],
    "additionalProperties": False,
}
