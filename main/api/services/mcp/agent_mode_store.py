# -*- coding: utf-8 -*-
"""工作模式（AgentMode）存储与解析层。

模式 = 一段模式说明 + 一个工具门禁。AI 对话前判断当前工作环境，用 ``mode.manage``
切换模式：``use`` 只在**工具结果**里返回该模式说明（不改写人格 / 系统 prompt），
而 ``AssistantAIConfig.current_mode_key`` 决定**这轮能拿到哪些 MCP 工具**。

默认的「初始对话模式」（``initial``）视为「不在工作房间」：只保留系统自带的基础
对话工具（切换模式 / 工具自省 / 收发消息），收走全部设备 / 工作 MCP；只有切到
task / learning / fix 等工作模式，系统才把设备 MCP 交回——像离开聊天、走进工作间
拿起工具再干活。旧版独立的 ``chat`` 模式已并入 ``initial``。

本模块是 REST / MCP 工具 / 运行时门禁三方共用的唯一权威实现：
- 4 个内置模式（初始对话 / 任务 / 学习 / 修复）按 user 幂等播种；
- 增删改查 + 切换（use）；
- ``is_chat_only_mode`` / ``resolve_current_mode_key`` 供 chat_runtime 做工具门禁（只读、DB 为准）。
"""

import re
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from api.database import engine
from api.models import AgentMode, AssistantAIConfig


# 每个 AI 的初始/默认模式 key。任何 AI 都至少处于该模式（不存在「无模式」）。
DEFAULT_MODE_KEY = "initial"


# ---------------------------------------------------------------------------
# 内置模式：初始对话模式 + 3 个工作模式。可改 prompt，不可删除。
# ---------------------------------------------------------------------------
BUILTIN_MODES: List[Dict[str, str]] = [
    {
        "mode_key": DEFAULT_MODE_KEY,
        "name": "初始对话模式",
        "description": "默认模式：普通聊天，不进工作间——只有基础对话工具，看不到设备/工作 MCP；要干活先切到 task/learning/fix。",
        "prompt": (
            "你现在处于「初始对话模式」（默认模式，等同普通聊天）。\n"
            "- 这是「不在工作房间」的状态：只做普通交流、答疑、闲聊，回答简洁直接。\n"
            "- 本模式下你**只有基础对话工具**（切换模式 mode.manage、查询工具说明 mcp.describe_tool、"
            "收发消息 message.*）；**看不到、也无法调用任何设备 / 工作类 MCP**"
            "（桌面、浏览器、安卓、文件、命令、任务、知识库、系统治理等）。\n"
            "- 一旦需要真正干活，先判断属于哪类，再用 mode.manage(action=use, mode_key=...) 切换：\n"
            "  · 有明确任务 / 目标要交付 → task 任务模式；\n"
            "  · 讲解 / 教学 → learning 学习模式；\n"
            "  · 排查 / 修复缺陷或故障 → fix 修复模式。\n"
            "- 切到工作模式后，系统才会把对应的设备 / 工作 MCP 工具交给你——就像离开聊天、"
            "走进工作间拿起工具再干活。若只是聊天、无需动手，保持本模式即可。"
        ),
    },
    {
        "mode_key": "task",
        "name": "任务模式",
        "description": "承接明确任务/目标时进入，强调规划、分步执行、交付与复盘。",
        "prompt": (
            "你现在处于「任务模式」。\n"
            "- 目标：把用户交代的任务可靠地完成并交付。\n"
            "- 先复述目标与验收标准，必要时用 plan.create 拆分阶段，再逐阶段推进。\n"
            "- 每步说明「要做什么、为什么、如何验证」，遇到阻塞先反馈再决定。\n"
            "- 优先复用已有经验（可先检索知识库），避免重复踩坑。\n"
            "- 完成后给出结果、验证情况与可复用经验，不谎报进度。"
        ),
    },
    {
        "mode_key": "learning",
        "name": "学习模式",
        "description": "用户想理解某个概念/技术时进入，强调讲解、拆解与循序渐进。",
        "prompt": (
            "你现在处于「学习模式」。\n"
            "- 目标：帮助用户真正理解，而不是替他做完。\n"
            "- 由浅入深、循序渐进地讲解，用类比和具体例子降低理解门槛。\n"
            "- 拆解概念之间的关系，指出常见误区，并在关键处停下确认用户是否跟上。\n"
            "- 适度提问、给小练习，引导用户主动思考，而非直接抛答案。\n"
            "- 术语首次出现时先解释再使用。"
        ),
    },
    {
        "mode_key": "fix",
        "name": "修复模式",
        "description": "排查与修复缺陷/故障时进入，强调定位根因、最小改动、验证回归。",
        "prompt": (
            "你现在处于「修复模式」。\n"
            "- 目标：定位并修复问题的根因，而非只压掉表面症状。\n"
            "- 先复现或收集证据（报错、日志、复现步骤），形成对根因的假设再动手。\n"
            "- 改动最小化、可回滚，避免顺手大改无关代码。\n"
            "- 修复后必须验证：说明如何确认问题已解决、是否引入回归。\n"
            "- 说清「原因 → 修改点 → 验证方式」，不确定处如实标注。"
        ),
    },
]

_BUILTIN_KEYS = {m["mode_key"] for m in BUILTIN_MODES}

# 「初始/对话」模式集合：默认，且视为「不在工作房间」——只留基础对话工具，收走设备/工作 MCP。
# "chat" 是历史别名（旧版曾有独立 chat 模式，现并入 initial），一并按对话模式处理。
CHAT_MODE_KEYS = {DEFAULT_MODE_KEY, "chat"}

# 对话模式下仍保留的「系统自带」基础工具：切换模式 + 工具自省 + 收发消息。
# 其余（设备端 desktop/browser/android、workspace、task、knowledge、admin… 全部工作类）在此模式收走。
CHAT_MODE_TOOL_WHITELIST = {
    "mode.manage",
    "mcp.describe_tool",
    "message.send_to_user",
    "message.send_to_ai",
}


def resolve_current_mode_key(user_id: int, ai_config_id: Optional[int]) -> str:
    """读取某 AI 的当前模式 key（只读；空 → 初始模式；旧 chat 归一为初始）。"""
    if not ai_config_id:
        return DEFAULT_MODE_KEY
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == ai_config_id,
            )
        ).first()
    key = str(getattr(cfg, "current_mode_key", "") or "").strip() if cfg else ""
    if not key or key == "chat":
        return DEFAULT_MODE_KEY
    return key


def is_chat_only_mode(user_id: int, ai_config_id: Optional[int]) -> bool:
    """当前是否处于「初始 / 对话」模式（不在工作房间，应收走设备 / 工作 MCP）。只读。"""
    return resolve_current_mode_key(user_id, ai_config_id) in CHAT_MODE_KEYS


def _serialize(row: AgentMode) -> Dict[str, Any]:
    return {
        "mode_key": row.mode_key,
        "name": row.name,
        "description": row.description,
        "prompt": row.prompt,
        "is_builtin": bool(row.is_builtin),
        "sort_order": int(row.sort_order or 0),
        "updated_at": row.updated_at,
    }


def _slugify_key(name: str) -> str:
    """从模式名派生一个稳定 key；CJK 等无法转 slug 时回退随机 key。"""
    raw = str(name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if slug:
        return slug[:40]
    return f"custom_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 播种 / 查询
# ---------------------------------------------------------------------------
def ensure_builtin_modes(user_id: int) -> None:
    """幂等地为该 user 播种 4 个内置模式（仅补缺失，不覆盖用户已改的 prompt）。

    同时做一次性自愈：旧版独立的 ``chat`` 内置模式已并入 ``initial``，这里删除遗留的
    ``chat`` 内置行，并把仍指向 ``chat`` 的 AI 归一到 ``initial``。
    """
    with Session(engine) as session:
        rows = session.exec(
            select(AgentMode).where(AgentMode.user_id == user_id)
        ).all()
        existing = {r.mode_key: r for r in rows}
        changed = False
        for idx, spec in enumerate(BUILTIN_MODES):
            if spec["mode_key"] in existing:
                continue
            session.add(
                AgentMode(
                    user_id=user_id,
                    mode_key=spec["mode_key"],
                    name=spec["name"],
                    description=spec["description"],
                    prompt=spec["prompt"],
                    is_builtin=True,
                    sort_order=idx,
                )
            )
            changed = True
        # 清理旧版 chat 内置模式（已合并进 initial），并把仍指向 chat 的 AI 归一到 initial。
        legacy_chat = existing.get("chat")
        if legacy_chat is not None and bool(legacy_chat.is_builtin):
            session.delete(legacy_chat)
            for cfg in session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user_id,
                    AssistantAIConfig.current_mode_key == "chat",
                )
            ).all():
                cfg.current_mode_key = DEFAULT_MODE_KEY
                cfg.updated_at = time.time()
                session.add(cfg)
            changed = True
        if changed:
            session.commit()


def list_modes(user_id: int) -> List[Dict[str, Any]]:
    ensure_builtin_modes(user_id)
    with Session(engine) as session:
        rows = session.exec(
            select(AgentMode)
            .where(AgentMode.user_id == user_id)
            .order_by(AgentMode.sort_order.asc(), AgentMode.created_at.asc())
        ).all()
    return [_serialize(r) for r in rows]


def get_mode(user_id: int, mode_key: str) -> Optional[Dict[str, Any]]:
    """按 key 读取单个模式（只读；运行时注入用，不做播种以免每轮写库）。"""
    key = str(mode_key or "").strip()
    if not key:
        return None
    with Session(engine) as session:
        row = session.exec(
            select(AgentMode).where(
                AgentMode.user_id == user_id,
                AgentMode.mode_key == key,
            )
        ).first()
    return _serialize(row) if row else None


# ---------------------------------------------------------------------------
# 增删改
# ---------------------------------------------------------------------------
def create_mode(
    user_id: int,
    *,
    name: str,
    prompt: str,
    description: str = "",
    mode_key: str = "",
) -> Dict[str, Any]:
    name = str(name or "").strip()
    prompt = str(prompt or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required to create a mode")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required to create a mode")
    key = str(mode_key or "").strip() or _slugify_key(name)
    ensure_builtin_modes(user_id)
    with Session(engine) as session:
        clash = session.exec(
            select(AgentMode).where(
                AgentMode.user_id == user_id,
                AgentMode.mode_key == key,
            )
        ).first()
        if clash:
            raise HTTPException(status_code=409, detail=f"mode_key already exists: {key}")
        row = AgentMode(
            user_id=user_id,
            mode_key=key,
            name=name,
            description=str(description or "").strip(),
            prompt=prompt,
            is_builtin=False,
            sort_order=1000,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)


def update_mode(
    user_id: int,
    mode_key: str,
    *,
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    key = str(mode_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required")
    ensure_builtin_modes(user_id)
    with Session(engine) as session:
        row = session.exec(
            select(AgentMode).where(
                AgentMode.user_id == user_id,
                AgentMode.mode_key == key,
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"mode not found: {key}")
        if name is not None:
            new_name = str(name).strip()
            if new_name:
                row.name = new_name
        if description is not None:
            row.description = str(description).strip()
        if prompt is not None:
            row.prompt = str(prompt).strip()
        row.updated_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)


def delete_mode(user_id: int, mode_key: str) -> Dict[str, Any]:
    key = str(mode_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required")
    if key in _BUILTIN_KEYS:
        raise HTTPException(status_code=400, detail=f"内置模式不可删除：{key}（可用 update 修改其 prompt）")
    with Session(engine) as session:
        row = session.exec(
            select(AgentMode).where(
                AgentMode.user_id == user_id,
                AgentMode.mode_key == key,
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"mode not found: {key}")
        session.delete(row)
        # 清理仍指向该模式的 AI，回退到初始模式，避免注入时找不到模式。
        affected = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.current_mode_key == key,
            )
        ).all()
        for cfg in affected:
            cfg.current_mode_key = DEFAULT_MODE_KEY
            cfg.updated_at = time.time()
            session.add(cfg)
        session.commit()
        return {"deleted": key, "cleared_ai_configs": [int(c.id or 0) for c in affected]}


# ---------------------------------------------------------------------------
# 切换（use）+ 运行时解析
# ---------------------------------------------------------------------------
def set_current_mode(user_id: int, ai_config_id: int, mode_key: str) -> Dict[str, Any]:
    """把某个 AI 的当前模式切换为 ``mode_key``。"""
    key = str(mode_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="mode_key is required")
    ensure_builtin_modes(user_id)
    with Session(engine) as session:
        mode = session.exec(
            select(AgentMode).where(
                AgentMode.user_id == user_id,
                AgentMode.mode_key == key,
            )
        ).first()
        if not mode:
            raise HTTPException(status_code=404, detail=f"mode not found: {key}")
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == ai_config_id,
            )
        ).first()
        if not cfg:
            raise HTTPException(status_code=404, detail="AI config not found")
        cfg.current_mode_key = key
        cfg.updated_at = time.time()
        session.add(cfg)
        session.commit()
        return {
            "ai_config_id": int(cfg.id or 0),
            "current_mode_key": key,
            "mode": _serialize(mode),
        }


def effective_mode_prompt(user_id: int, ai_config_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """运行时读取某 AI 当前模式的注入内容。无模式 / 模式已删 → None。

    只读、纯 DB 为准（gateway 预览与 ai_runtime 两进程一致），不做播种写库。
    """
    if not ai_config_id:
        return None
    with Session(engine) as session:
        cfg = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == ai_config_id,
            )
        ).first()
        if not cfg:
            return None
        # 不存在「无模式」：空 / 旧 chat 均回退到初始模式。
        key = str(getattr(cfg, "current_mode_key", "") or "").strip() or DEFAULT_MODE_KEY
        if key == "chat":
            key = DEFAULT_MODE_KEY
        mode = session.exec(
            select(AgentMode).where(
                AgentMode.user_id == user_id,
                AgentMode.mode_key == key,
            )
        ).first()
    # 初始模式可能尚未播种（老用户首次访问前）：兜底用内置定义直接注入。
    if not mode and key == DEFAULT_MODE_KEY:
        spec = next((m for m in BUILTIN_MODES if m["mode_key"] == DEFAULT_MODE_KEY), None)
        if spec:
            return {
                "mode_key": spec["mode_key"],
                "name": spec["name"],
                "description": spec.get("description", ""),
                "prompt": spec["prompt"],
                "is_builtin": True,
                "sort_order": 0,
                "updated_at": 0.0,
            }
    if not mode or not str(mode.prompt or "").strip():
        return None
    return _serialize(mode)
