"""``knowledge.manage`` — unified knowledge-base (library) tool.

Dispatches ``action`` to built-in knowledge handlers (传承思想 / 内置技能 /
内置人格 / 系统 prompt). Library binding is enforced at registry call time
and is the only authorization boundary for these actions.
"""

from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException

from library import handlers as knowledge_handlers

KnowledgeHandler = Callable[[int, Dict[str, Any], Optional[int]], Any]

_KNOWLEDGE_ACTIONS: Dict[str, KnowledgeHandler] = {
    "record_experience": knowledge_handlers.record_experience,
    "list_thoughts": knowledge_handlers.list_inheritance_thoughts,
    "get_thought": knowledge_handlers.get_inheritance_thought,
    "create_thought": knowledge_handlers.create_inheritance_thought,
    "edit_thought": knowledge_handlers.edit_inheritance_thought,
    "delete_thought": knowledge_handlers.delete_inheritance_thought,
    "install_skill_package": knowledge_handlers.install_skill_package,
    "read_inheritance_skills": knowledge_handlers.read_inheritance_skills,
    "read_skills": knowledge_handlers.read_intrinsic_skills,
    "update_skills": knowledge_handlers.update_intrinsic_skills,
    "read_personas": knowledge_handlers.read_intrinsic_personas,
    "update_persona": knowledge_handlers.update_intrinsic_persona,
    "read_system_prompts": knowledge_handlers.read_system_prompts,
    "update_system_prompts": knowledge_handlers.update_system_prompts,
}

_KNOWLEDGE_ACTION_ALIASES = {
    "list": "list_thoughts",
    "get": "get_thought",
    "create": "create_thought",
    "edit": "edit_thought",
    "delete": "delete_thought",
    "install": "install_skill_package",
}


def _knowledge_manage(user_id: int, args: Dict[str, Any], ai_config_id: Optional[int]) -> Any:
    """Dispatch ``action`` to the matching knowledge handler."""
    raw = str((args or {}).get("action") or "").strip().lower()
    action = _KNOWLEDGE_ACTION_ALIASES.get(raw, raw)
    if not action:
        raise HTTPException(status_code=400, detail="action is required for knowledge.manage")
    spec = _KNOWLEDGE_ACTIONS.get(action)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported action: {action}. 可用: {', '.join(sorted(_KNOWLEDGE_ACTIONS))}",
        )

    sub_args: Dict[str, Any] = {}
    nested = (args or {}).get("params")
    if isinstance(nested, dict):
        sub_args.update(nested)
    for key, value in (args or {}).items():
        if key in ("action", "params"):
            continue
        sub_args[key] = value

    return spec(int(user_id), sub_args, ai_config_id)


KNOWLEDGE_MANAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(_KNOWLEDGE_ACTIONS),
            "description": (
                "操作类型（知识库 / 图书馆）：\n"
                "- record_experience 把一段可复用经验/教训直接沉淀进 topics 程序性记忆（status 直接 active，无需用户审批；写入后即可被 knowledge.search 与派任务前简报命中）；\n"
                "- list_thoughts 列出传承思想；get_thought 读取某条传承思想正文；\n"
                "- create_thought 新建传承思想；edit_thought 按行编辑；delete_thought 删除；\n"
                "- install_skill_package 安装 Skill 包；\n"
                "- read_inheritance_skills 读取传承技能总览；read_skills 读取服务端固定 MCP；read_personas 读取固有人格；read_system_prompts 读取固有思想；\n"
                "- update_skills 改写服务端 MCP 说明；update_system_prompts 改写系统提示词；update_persona 改写 AI 人格。\n"
                "需要该 AI 已绑定图书馆；绑定后不区分 AI 身份。各 action 的具体参数可放在 params 对象或直接平铺在顶层。"
            ),
        },
        "params": {
            "type": "object",
            "description": (
                "所选 action 的参数（也可直接平铺在顶层）。"
                "record_experience: title、steps（必填），scenario、gotchas、triggers、scope 可选；"
                "create_thought: name、content（必填），summary、endpoint_kind 可选；"
                "get_thought/edit_thought/delete_thought: id；"
                "edit_thought: mode/line/text 或 edits 数组，可选 endpoint_kind、expected_sha256；"
                "install_skill_package: package（必填），timeout、endpoint_kind 可选；"
                "update_skills: tools 数组；update_persona: ai_config_id、prompt；"
                "update_system_prompts: prompts 数组。"
            ),
        },
        "id": {"type": "string", "description": "get_thought / edit_thought / delete_thought 的目标传承思想 id。"},
        "scenario": {"type": "string", "description": "record_experience：什么场景/触发条件下用这条经验。"},
        "steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "record_experience：可复用的操作步骤（必填，至少一条）。",
        },
        "gotchas": {
            "type": "array",
            "items": {"type": "string"},
            "description": "record_experience：注意事项 / 已知坑 / 典型错误。",
        },
        "triggers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "record_experience：触发词/关键词，用于检索与派任务前简报命中。",
        },
        "scope": {"type": "string", "description": "record_experience：知识作用域，默认 global。"},
        "name": {"type": "string", "description": "create_thought 的技能名/标题（必填）。"},
        "content": {"type": "string", "description": "create_thought 的正文，写入 SKILL.md body（必填）。"},
        "summary": {"type": "string", "description": "create_thought 的可选摘要，写入 frontmatter description。"},
        "endpoint_kind": {
            "type": "string",
            "description": "create_thought / install_skill_package / edit_thought 的端侧归类（如 desktop、browser）。",
        },
        "package": {"type": "string", "description": "install_skill_package 的 npx 包名（必填）。"},
        "timeout": {"type": "number", "description": "install_skill_package 安装超时秒数。"},
        "edits": {
            "type": "array",
            "description": (
                "edit_thought 的行编辑列表。每项含 mode（replace_line/insert_before/insert_after/"
                "delete_line/append/prepend/replace_all）、line 或 start_line、text 或 content。"
            ),
        },
        "mode": {"type": "string", "description": "edit_thought 单行编辑模式（未传 edits 数组时使用）。"},
        "line": {"type": "integer", "description": "edit_thought 目标行号（1-based）。"},
        "text": {"type": "string", "description": "edit_thought 写入文本；create_thought 时等同 content（兼容别名）。"},
        "title": {"type": "string", "description": "create_thought 时等同 name（兼容别名，优先使用 name）。"},
        "expected_sha256": {
            "type": "string",
            "description": "edit_thought 乐观锁：与 get_thought 返回的 content_sha256 一致才允许编辑。",
        },
        "tools": {"type": "array", "description": "update_skills 的工具覆盖列表（非空数组，必填）。"},
        "ai_config_id": {"type": "integer", "description": "update_persona 的目标 AI 配置 id（必填）。"},
        "prompt": {"type": "string", "description": "update_persona 的内置人格 prompt（必填）。"},
        "prompts": {"type": "array", "description": "update_system_prompts 的系统 prompt 列表（非空数组，必填）。"},
    },
    "required": ["action"],
}
