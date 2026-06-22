import time
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import AssistantAIConfig, User
from api.services.governance import assert_can_manage_or_legacy
from mcp_runtime.mcp.permissions import ROLE_ASSISTANT_ADMIN, ROLE_MANAGER


SYSTEM_PROMPT_FIELDS = {
    "admin_prompt": "旧版/兜底管理员 prompt",
    "mcp_call_method": "全局 MCP 调用方法 prompt",
    "mcp_namespace_hints": "MCP namespace 说明配置",
    "mcp_dynamic_rule": "MCP 动态工具暴露规则",
    "mcp_format_error_hint": "MCP 格式错误提示 prompt",
    "prompt_ai_message_notify": "AI 间消息·通知模板（notify / 单向通知）",
    "prompt_ai_message_inquiry": "AI 间消息·询问模板（inquiry）",
    "prompt_ai_message_inquiry_reminder": "AI 间询问未回复提醒模板",
    "prompt_ai_message_reply": "AI 间消息·回复模板（reply）",
    "prompt_ai_message_chitchat": "AI 间消息·闲聊模板（chitchat）",
    "prompt_ai_message_reply_success": "AI 间消息回复成功提示 prompt",
    "prompt_user_message_notice": "用户消息发送提示 prompt",
    "default_start_task_prompt": "默认任务启动 prompt",
    "default_resume_task_prompt": "默认任务恢复 prompt",
    "default_supervision_prompt": "默认监督 prompt",
    "default_compression_prompt": "默认对话压缩 prompt",
    "task_plan_flow_prompt": "任务分阶段流程 prompt",
}

SYSTEM_PROMPT_USAGE = {
    "admin_prompt": "仅用于没有指定 AI 配置的旧版管理员运行路径；当前 AI 卡片/飞书/任务运行通常不直接使用它。",
    "mcp_call_method": "旧版文本 MCP 调用模板；当前默认不再注入运行 prompt，工具通过 native schema 动态暴露。",
    "mcp_namespace_hints": "JSON 对象，配置 {MCP} 占位符渲染第一层 namespace 时的说明文本。",
    "mcp_dynamic_rule": "MCP 工具 schema 如何按需动态暴露给模型的全局规则。",
    "mcp_format_error_hint": "当模型输出的 MCP 调用格式无效时，作为系统纠错提示模板使用。",
    "prompt_ai_message_notify": "message_type=\"notify\" 时注入。系统会自动签收，模板应明确告知 AI 不要回应。",
    "prompt_ai_message_inquiry": "message_type=\"inquiry\" 时注入。模板可提示对方用 message.send_to_ai(message_type=\"reply\") 答复。",
    "prompt_ai_message_inquiry_reminder": "inquiry 的目标 AI 停止运行且仍未回复超过 ai_message_inquiry_reminder_seconds 后注入目标 AI 原会话，用于催促其回复原消息。",
    "prompt_ai_message_reply": "message_type=\"reply\" 时注入。模板用于展示对方回复内容。",
    "prompt_ai_message_chitchat": "message_type=\"chitchat\" 时注入。用于展示闲聊内容。",
    "prompt_ai_message_reply_success": "AI 间消息被自动签收后，恢复原工作流的提示。",
    "prompt_user_message_notice": "message.send_to_user 成功后返回的用户消息送达提示。",
    "default_start_task_prompt": "任务首次启动时的默认注入提示，不是 AI 基础人格 prompt。",
    "default_resume_task_prompt": "任务传承/恢复时的默认注入提示，不是 AI 基础人格 prompt。",
    "default_supervision_prompt": "任务空闲监督时的默认追问提示，不是 AI 基础人格 prompt。",
    "default_compression_prompt": "数字成员会话 token 达到阈值时，用于把较早的对话历史压缩成摘要的模板，不是 AI 基础人格 prompt。",
    "task_plan_flow_prompt": "任务运行时注入的分阶段流程说明（先规划→逐阶段执行→plan.finish 收尾），由系统强制驱动。",
}


def _get_owned_ai_config(session: Session, user_id: int, ai_config_id: int) -> AssistantAIConfig:
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.id == ai_config_id,
        )
    ).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="AI config not found")
    return cfg


def _get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _resolve_prompt_text(args: Dict[str, Any]) -> str:
    value = args.get("prompt")
    if value is None:
        value = args.get("content")
    if value is None:
        value = args.get("text")
    if value is None:
        raise HTTPException(status_code=400, detail="prompt/content/text is required")
    return str(value)


def _resolve_edit_text(args: Dict[str, Any], required: bool = True) -> str:
    value = args.get("text")
    if value is None:
        value = args.get("content")
    if value is None:
        value = args.get("prompt")
    if value is None:
        value = args.get("replace")
    if value is None:
        value = args.get("value")
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail="text/content/prompt is required for this line edit")
        return ""
    return str(value)


def _to_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field_name} must be an integer")
    return parsed


def _line_index(value: Any, field_name: str, line_count: int, allow_end: bool = False) -> int:
    line_no = _to_int(value, field_name)
    upper = line_count + 1 if allow_end else line_count
    if line_no < 1 or line_no > max(1, upper):
        raise HTTPException(status_code=400, detail=f"{field_name} out of range: {line_no}")
    return line_no - 1


def _line_range(edit: Dict[str, Any], line_count: int) -> tuple[int, int]:
    start_raw = edit.get("start_line", edit.get("line", edit.get("line_number")))
    end_raw = edit.get("end_line", start_raw)
    if start_raw is None:
        raise HTTPException(status_code=400, detail="line/line_number/start_line is required")
    start_idx = _line_index(start_raw, "start_line", line_count)
    end_idx = _line_index(end_raw, "end_line", line_count)
    if end_idx < start_idx:
        raise HTTPException(status_code=400, detail="end_line must be >= start_line")
    return start_idx, end_idx


def _apply_one_line_edit(lines: list[str], edit: Dict[str, Any]) -> None:
    mode = str(edit.get("mode") or edit.get("op") or "").strip().lower()
    if not mode:
        if any(key in edit for key in ("line", "line_number", "start_line")):
            mode = "replace_line"
        else:
            raise HTTPException(
                status_code=400,
                detail="mode is required. Use replace_line/insert_before/insert_after/delete_line/append/prepend/replace_all.",
            )

    if mode in {"replace_all", "set"}:
        lines[:] = _resolve_prompt_text(edit).splitlines()
        return
    if mode in {"replace_line", "replace_lines", "replace"}:
        start_idx, end_idx = _line_range(edit, len(lines))
        replacement = _resolve_edit_text(edit).splitlines()
        lines[start_idx:end_idx + 1] = replacement
        return
    if mode in {"delete_line", "delete_lines", "delete", "remove"}:
        start_idx, end_idx = _line_range(edit, len(lines))
        del lines[start_idx:end_idx + 1]
        return
    if mode == "insert_before":
        raw = edit.get("line", edit.get("line_number", edit.get("start_line")))
        if raw is None:
            raise HTTPException(status_code=400, detail="line/line_number is required")
        idx = _line_index(raw, "line", len(lines), allow_end=True)
        lines[idx:idx] = _resolve_edit_text(edit).splitlines()
        return
    if mode == "insert_after":
        raw = edit.get("line", edit.get("line_number", edit.get("start_line")))
        if raw is None:
            raise HTTPException(status_code=400, detail="line/line_number is required")
        idx = _line_index(raw, "line", len(lines)) + 1
        lines[idx:idx] = _resolve_edit_text(edit).splitlines()
        return
    if mode == "append":
        lines.extend(_resolve_edit_text(edit).splitlines())
        return
    if mode == "prepend":
        lines[:0] = _resolve_edit_text(edit).splitlines()
        return
    raise HTTPException(status_code=400, detail=f"Unsupported prompt edit mode: {mode}")


def _apply_prompt_line_edits(current: str, args: Dict[str, Any]) -> tuple[str, int]:
    raw_edits = args.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        edits = [item for item in raw_edits if isinstance(item, dict)]
        if len(edits) != len(raw_edits):
            raise HTTPException(status_code=400, detail="edits must be an array of objects")
    else:
        edits = [args]

    lines = str(current or "").splitlines()
    original_had_trailing_newline = str(current or "").endswith("\n")
    for edit in edits:
        _apply_one_line_edit(lines, edit)
    updated = "\n".join(lines)
    if original_had_trailing_newline and updated:
        updated += "\n"
    return updated, len(edits)


def _prompt_list_targets(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    from api.services import kb_store

    with Session(engine) as session:
        ai_rows = session.exec(
            select(AssistantAIConfig)
            .where(AssistantAIConfig.user_id == user_id)
            .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
        ).all()
    ai_prompts = []
    for row in ai_rows:
        effective_prompt = kb_store.effective_ai_prompt(user_id, row)
        ai_prompts.append({
            "ai_config_id": int(row.id or 0),
            "name": row.name,
            "ai_role": row.ai_role,
            "digital_member_role": row.digital_member_role,
            "prompt_length": len(str(effective_prompt or "")),
        })
    return {
        "ai_prompts": ai_prompts,
        "system_prompts": [
            {
                "key": key,
                "label": label,
                "usage": SYSTEM_PROMPT_USAGE.get(key, ""),
            }
            for key, label in SYSTEM_PROMPT_FIELDS.items()
        ],
        "note": (
            "AI 基础人格 prompt 用 prompt.manage(action=read_ai) 读取（来源 KnowledgeBase/personas/*.md）；"
            "system_prompts 多为全局注入模板或旧版兜底字段。"
        ),
    }


def _prompt_read_ai(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    target_id = args.get("target_ai_config_id", args.get("ai_config_id", ai_config_id))
    if not target_id:
        raise HTTPException(status_code=400, detail="target_ai_config_id is required")
    from api.services import kb_store

    with Session(engine) as session:
        cfg = _get_owned_ai_config(session, user_id, int(target_id))
        effective_prompt = kb_store.effective_ai_prompt(user_id, cfg)
        return {
            "ai_config_id": int(cfg.id or 0),
            "name": cfg.name,
            "ai_role": cfg.ai_role,
            "digital_member_role": cfg.digital_member_role,
            "prompt": effective_prompt,
            "prompt_length": len(str(effective_prompt or "")),
            "line_count": len(str(effective_prompt or "").splitlines()),
        }


def _prompt_write_ai(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    target_id = args.get("target_ai_config_id", args.get("ai_config_id", ai_config_id))
    if not target_id:
        raise HTTPException(status_code=400, detail="target_ai_config_id is required")
    with Session(engine) as session:
        cfg = _get_owned_ai_config(session, user_id, int(target_id))
        if ai_config_id is not None and int(ai_config_id) != int(cfg.id or 0):
            caller = _get_owned_ai_config(session, user_id, int(ai_config_id))
            denial = assert_can_manage_or_legacy(session, user_id, caller, cfg)
            if denial:
                raise HTTPException(status_code=403, detail=denial)
        # 行编辑基于文件真相源（缺失时为空人格）。
        from api.services import kb_store

        old_prompt = kb_store.effective_ai_prompt(user_id, cfg)
        old_length = len(str(old_prompt or ""))
        new_prompt, edit_count = _apply_prompt_line_edits(old_prompt, args)
        kb_store.write_persona(user_id, cfg, prompt=new_prompt)
        saved_prompt = kb_store.effective_ai_prompt(user_id, cfg)
        if saved_prompt != new_prompt.strip():
            raise HTTPException(status_code=500, detail=f"Failed to persist AI prompt: {cfg.id}")
        cfg.updated_at = time.time()
        session.add(cfg)
        session.commit()
        return {
            "success": True,
            "ai_config_id": int(cfg.id or 0),
            "name": cfg.name,
            "edit_count": edit_count,
            "old_prompt_length": old_length,
            "new_prompt_length": len(str(new_prompt or "")),
            "line_count": len(str(new_prompt or "").splitlines()),
            "updated_at": cfg.updated_at,
        }


def _prompt_read_system(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    from api.services import kb_store

    key = str(args.get("key") or "").strip()
    with Session(engine) as session:
        user = _get_user(session, user_id)
        if key:
            if key not in SYSTEM_PROMPT_FIELDS:
                raise HTTPException(status_code=400, detail=f"Unsupported system prompt key: {key}")
            value = kb_store.effective_system_value(user_id, key, getattr(user, key, ""))
            return {
                "key": key,
                "label": SYSTEM_PROMPT_FIELDS[key],
                "usage": SYSTEM_PROMPT_USAGE.get(key, ""),
                "prompt": value,
                "prompt_length": len(value),
            }
        return {
            "system_prompts": [
                {
                    "key": field,
                    "label": label,
                    "usage": SYSTEM_PROMPT_USAGE.get(field, ""),
                    "prompt": kb_store.effective_system_value(user_id, field, getattr(user, field, "")),
                }
                for field, label in SYSTEM_PROMPT_FIELDS.items()
            ],
            "note": (
                "这些不是 AI 卡片当前运行的基础 prompt（基础 prompt 请用 prompt.manage(action=read_ai) 读取），"
                "多为全局注入模板或旧版兜底字段。"
            ),
        }


def _prompt_write_system(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    key = str(args.get("key") or "").strip()
    if key not in SYSTEM_PROMPT_FIELDS:
        raise HTTPException(status_code=400, detail=f"Unsupported system prompt key: {key}")
    with Session(engine) as session:
        user = _get_user(session, user_id)
        from api.services import kb_store

        # These prompt columns have been removed from User. Read and write the
        # KnowledgeBase file directly instead of assigning a non-model field.
        old_prompt = kb_store.effective_system_value(user_id, key, getattr(user, key, ""))
        old_length = len(old_prompt)
        new_prompt, edit_count = _apply_prompt_line_edits(old_prompt, args)
        if key not in dict(kb_store.SYSTEM_PROMPT_KEYS):
            raise HTTPException(status_code=400, detail=f"System prompt is not file-backed: {key}")
        kb_store.write_system_prompt(user_id, key, new_prompt)
        saved_prompt = kb_store.read_system_prompt(user_id, key)
        if saved_prompt != new_prompt.strip():
            raise HTTPException(status_code=500, detail=f"Failed to persist system prompt: {key}")
        return {
            "success": True,
            "key": key,
            "label": SYSTEM_PROMPT_FIELDS[key],
            "edit_count": edit_count,
            "old_prompt_length": old_length,
            "new_prompt_length": len(new_prompt),
            "line_count": len(new_prompt.splitlines()),
        }


# Action → (handler, minimum role). ``None`` role means available to every tier.
_PROMPT_ACTIONS = {
    "list_targets": (_prompt_list_targets, None),
    "read_ai": (_prompt_read_ai, None),
    "write_ai": (_prompt_write_ai, ROLE_MANAGER),
    "read_system": (_prompt_read_system, ROLE_MANAGER),
    "write_system": (_prompt_write_system, ROLE_ASSISTANT_ADMIN),
}

_PROMPT_ACTION_ALIASES = {
    "list": "list_targets",
    "targets": "list_targets",
    "read": "read_ai",
    "write": "write_ai",
}


def _prompt_manage(user_id: int, args: dict, ai_config_id: Optional[int] = None):
    """Unified prompt tool. Dispatch by ``action`` to the concrete handler.

    Folds ``prompt.{list_targets,read_ai,write_ai,read_system,write_system}``
    behind one ``action`` parameter. Per-action minimum role is re-enforced here:
    reading an AI's own prompt is open to every tier, while AI-prompt writes are
    manager+, system-prompt reads are manager+, and system-prompt writes are
    assistant_admin+.
    """
    from mcp_runtime.mcp.permissions import enforce_min_role

    raw = str((args or {}).get("action") or "").strip().lower()
    action = _PROMPT_ACTION_ALIASES.get(raw, raw)
    if not action:
        raise HTTPException(status_code=400, detail="action is required for prompt.manage")
    spec = _PROMPT_ACTIONS.get(action)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported action: {action}. 可用: {', '.join(sorted(_PROMPT_ACTIONS))}",
        )
    handler, min_role = spec
    if min_role:
        enforce_min_role(user_id, ai_config_id, min_role)
    return handler(user_id, args or {}, ai_config_id)


PROMPT_MANAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(_PROMPT_ACTIONS),
            "description": (
                "操作类型："
                "list_targets 列出可改写的 AI 人格 prompt 目标与系统 prompt 键；"
                "read_ai 读取某 AI 配置的基础人格 prompt（省略 target_ai_config_id 读当前 AI）；"
                "write_ai 按行编辑某 AI 配置的人格 prompt（需管理者+）；"
                "read_system 读取全局/系统 prompt 模板（需管理者+）；"
                "write_system 按行编辑全局/系统 prompt 模板（需辅助管理员+）。"
            ),
        },
        "target_ai_config_id": {"type": "integer", "description": "read_ai/write_ai：目标 AI 配置 id；省略则使用当前 AI。"},
        "key": {
            "type": "string",
            "enum": list(SYSTEM_PROMPT_FIELDS),
            "description": "read_system（省略=全部）/write_system（必填）：系统 prompt 的键。",
        },
        "mode": {
            "type": "string",
            "enum": ["replace_line", "insert_before", "insert_after", "delete_line", "append", "prepend", "replace_all"],
            "description": "write_ai/write_system：按行编辑方式；整篇覆盖必须显式用 replace_all。",
        },
        "line": {"type": "integer", "description": "目标行号（从 1 开始）。"},
        "start_line": {"type": "integer", "description": "替换/删除的起始行号（从 1 开始）。"},
        "end_line": {"type": "integer", "description": "替换/删除的结束行号（从 1 开始）。"},
        "text": {"type": "string", "description": "要写入的文本，可多行；mode=replace_all 时作为整篇内容。"},
        "edits": {
            "type": "array",
            "items": {"type": "object"},
            "description": "批量按行编辑；每项支持 mode、line、start_line、end_line、text。",
        },
    },
    "required": ["action"],
}
