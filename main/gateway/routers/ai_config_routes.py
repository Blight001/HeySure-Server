"""``/api/ai`` config routes: CRUD for ``AssistantAIConfig`` members plus
parent binding, governance tree, cloning, and MCP enable/disable."""

IS_ROUTER_ENTRY = False

import logging
import re
import time
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from connector_runtime.bots import all_channels, iter_bots


logger = logging.getLogger(__name__)
from api.database import get_session
from api.core.settings import settings
from mcp_runtime.mcp.permissions import clamp_tools_json, config_role_tier
from api.models import (
    AIRuntimeStatus,
    AssistantAIConfig,
    AssistantAIConfigCreate,
    AssistantAIConfigUpdate,
)
from api.services.access.access_guards import get_ai_config_or_404
from .auth import get_current_user
from ai_runtime.inference.ai_service import ensure_default_ai_for_user
from api.services.model_presets import normalize_model_presets
from api.services.tasks.task_system import compact_system_auto_control
from .ai_base import (
    _default_system_auto_control_for_user,
    _normalize_ai_role,
    _normalize_digital_member_role,
    router,
)


def _normalize_bot_channel(value) -> str:
    """Snap an incoming ``bot_channel`` to a registered bot, defaulting to ``feishu``."""
    channel = str(value or "feishu").strip().lower()
    return channel if channel in set(all_channels()) else "feishu"


def _bot_runtime_snapshot(cfg: AssistantAIConfig) -> dict:
    """Return the bot-facing fields that can affect long connections."""
    out = {"bot_channel": str(getattr(cfg, "bot_channel", "") or "").strip().lower()}
    for bot in iter_bots():
        out[bot.channel] = {
            "enabled": bool(bot.is_enabled(cfg)),
            "config": bot.read_config(cfg),
        }
    return out


def _refresh_bot_long_connections_if_needed(changed: bool) -> None:
    """Best-effort refresh for in-process bot long-connection clients.

    In split deployments connector-runtime owns the upstream clients and
    already polls config every few seconds. Starting them from gateway would
    duplicate clients and can block config saves.
    """
    if not changed:
        return
    if settings.connector_runtime_url:
        logger.debug("skip gateway bot refresh; connector-runtime owns bot clients")
        return
    for bot in iter_bots():
        try:
            bot.start_long_connections()
        except Exception:
            logger.exception(f"start {bot.channel} long_connections failed")


def _resolve_config_model_fields(user, preset_id: Optional[str], fallback_model: Optional[str] = None) -> dict:
    presets = normalize_model_presets(getattr(user, "model_presets", ""), user)
    requested_id = str(preset_id or "").strip()
    selected = None
    if requested_id:
        selected = next((item for item in presets if item["id"] == requested_id), None)
    if selected is None and fallback_model:
        model_name = str(fallback_model or "").strip()
        selected = next((item for item in presets if item["model"] == model_name or item["id"] == model_name), None)
    if selected is None and presets and not requested_id:
        selected = presets[0]
    if selected is None:
        raise HTTPException(status_code=400, detail="Selected model preset not found")
    return {
        "api_key": selected["api_key"],
        "base_url": selected["base_url"],
        "model": selected["model"],
        "model_preset_id": selected["id"],
    }


@router.get("/configs")
def list_ai_configs(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    ensure_default_ai_for_user(session, user.id)
    rows = session.exec(
        select(AssistantAIConfig)
        .where(AssistantAIConfig.user_id == user.id)
        .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
    ).all()
    if _ensure_configs_enabled(session, user.id, rows):
        rows = session.exec(
            select(AssistantAIConfig)
            .where(AssistantAIConfig.user_id == user.id)
            .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
        ).all()
    return [_cfg_response(row, user.id) for row in rows]


def _ensure_configs_enabled(session: Session, user_id: int, rows: list[AssistantAIConfig]) -> bool:
    """One-time repair for configs stopped before AI stop controls were removed."""
    changed = False
    for cfg in rows:
        if cfg.enabled:
            continue
        cfg.enabled = True
        cfg.updated_at = time.time()
        session.add(cfg)
        status = session.exec(
            select(AIRuntimeStatus).where(
                AIRuntimeStatus.user_id == user_id,
                AIRuntimeStatus.ai_config_id == cfg.id,
                AIRuntimeStatus.ai_kind == "assistant",
            )
        ).first()
        if not status:
            status = AIRuntimeStatus(user_id=user_id, ai_config_id=cfg.id, ai_kind="assistant")
        status.running = True
        status.updated_at = time.time()
        session.add(status)
        changed = True
    if changed:
        session.commit()
    return changed

@router.post("/configs")
def create_ai_config(
    body: AssistantAIConfigCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    switch_key = body.switch_key or f"assistant_{int(time.time() * 1000)}"
    # 角色扁平化：不再支持新建辅助管理员——注册时系统已默认创建一个
    # （ensure_default_configs），其余 AI 一律按数字成员对待。请求里传
    # assistant_admin 会被静默归一为 digital_member；已存在的辅助管理员
    # 不受影响（更新路径仍保留其角色）。
    role = "digital_member"
    member_role = _normalize_digital_member_role(body.digital_member_role)
    token_limit = body.token_limit or 10000
    bot_channel = _normalize_bot_channel(body.bot_channel)
    model_fields = _resolve_config_model_fields(user, body.model_preset_id, body.model)
    raw_mcp_tools = body.mcp_tools or AssistantAIConfig.model_fields["mcp_tools"].default
    tier = config_role_tier(
        AssistantAIConfig(ai_role=role, digital_member_role=member_role)
    )
    clamped_mcp_tools = clamp_tools_json(user, tier, raw_mcp_tools)
    # Strip legacy server toolbox tools from mcp_tools (they are now exclusively controlled
    # via toolbox DeviceMcpScope). Prevents stale conflicts.
    try:
        import json
        from tools.engine import is_toolbox_gated_tool
        parsed = json.loads(clamped_mcp_tools or "[]")
        if isinstance(parsed, list):
            kept = [str(x) for x in parsed if not is_toolbox_gated_tool(x)]
            clamped_mcp_tools = json.dumps(kept, ensure_ascii=False)
    except Exception:
        pass
    raw_avatar = (body.avatar or "").strip()
    m = re.search(r'ai_avatars([1-9])', raw_avatar)
    avatar = f"ai_avatars{m.group(1)}.png" if m else "ai_avatars1.png"
    cfg = AssistantAIConfig(
        user_id=user.id,
        name=body.name,
        description=body.description or "",
        avatar=avatar,
        api_key=model_fields["api_key"],
        base_url=model_fields["base_url"],
        model=model_fields["model"],
        model_preset_id=model_fields["model_preset_id"],
        strip_markdown_symbols=bool(body.strip_markdown_symbols),
        ai_role=role,
        digital_member_role=member_role,
        platform=body.platform or "Server-Core",
        generation=body.generation or 1,
        token_limit=token_limit,
        lifecycle_status=body.lifecycle_status or "working",
        current_behavior=body.current_behavior or "等待指令...",
        workspace_root=None,
        database_uri=body.database_uri,
        bot_channel=bot_channel,
        project_id=body.project_id,
        project_name=body.project_name,
        sort_order=body.sort_order or 100,
        enabled=True,
        mcp_enabled=body.mcp_enabled if body.mcp_enabled is not None else True,
        switch_key=switch_key,
        mcp_tools=clamped_mcp_tools,
        system_auto_control=compact_system_auto_control(
            body.system_auto_control or _default_system_auto_control_for_user(user)
        ),
    )
    # Apply per-bot config slices via each adapter. Inactive channels are
    # auto-disabled so an "enabled" flag in a non-selected channel never
    # accidentally turns the wrong bot on.
    _apply_bot_configs_from_payload(cfg, body.bot_configs, active_channel=bot_channel)
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    _ensure_ai_workspace_dir(user.id, cfg.id)
    _write_persona_file(user.id, cfg, prompt=body.prompt or "")
    # 工具箱默认自动绑定：新建 AI 即获得默认工具集（多绑；之后完全由用户在作坊/AI配置中管理绑定与解绑）。
    try:
        from tools.engine import bind_config_to_toolbox

        bind_config_to_toolbox(user.id, cfg.id)
    except Exception:
        pass
    _refresh_bot_long_connections_if_needed(
        any(bool(item.get("enabled")) for item in _bot_runtime_snapshot(cfg).values() if isinstance(item, dict))
    )
    return _cfg_response(cfg, user.id)


def _write_persona_file(
    user_id: int,
    cfg: AssistantAIConfig,
    prompt: Optional[str] = None,
) -> None:
    """文件为真相源：写入 AI 人格文件。

    ``prompt`` 为本次请求显式提交的人格文本；不传时保留文件既有内容。
    """
    from api.services.knowledge import kb_store

    expected = prompt if prompt is not None else kb_store.effective_ai_prompt(user_id, cfg)
    kb_store.write_persona(user_id, cfg, prompt=expected)
    if kb_store.effective_ai_prompt(user_id, cfg) != str(expected or "").strip():
        raise HTTPException(status_code=500, detail=f"Failed to persist AI prompt: {cfg.id}")


def _cfg_response(cfg: AssistantAIConfig, user_id: int):
    """AI 配置响应：补回人格 Prompt（已迁出数据库，真相源在 personas 文件）。"""
    try:
        from api.services.knowledge import kb_store

        data = cfg.model_dump()
        data["prompt"] = kb_store.effective_ai_prompt(user_id, cfg)
        return data
    except Exception:
        return cfg


def _ensure_ai_workspace_dir(user_id: int, ai_config_id: int) -> None:
    """Eagerly create the AI's effective working directory on creation.

    Regular members and assistant admins get a readable ``<id>-<slug>`` folder;
    manager members use the user workspace root. Failures are best-effort since
    the directory is also created lazily on first workspace use.
    """
    try:
        from mcp_runtime.mcp import get_project_root

        get_project_root(user_id, ai_config_id)
    except Exception:
        logger.exception(f"failed to create workspace dir for ai_config {ai_config_id}")


def _apply_bot_configs_from_payload(
    cfg: AssistantAIConfig,
    payload: Optional[dict],
    *,
    active_channel: str,
) -> None:
    """Route the create/update ``bot_configs`` payload through each adapter.

    Bots whose channel is NOT the currently-active one have their
    ``enabled`` flag force-cleared so the AI config can't accidentally
    have two bots both flipped on at once.
    """
    payload = payload if isinstance(payload, dict) else {}
    for bot in iter_bots():
        slice_payload = payload.get(bot.channel) if isinstance(payload.get(bot.channel), dict) else {}
        if bot.channel != active_channel:
            # Force-disable inactive bots so a stray ``enabled=True`` in
            # the JSON can't turn the wrong channel on.
            slice_payload = {**slice_payload, "enabled": False}
        bot.apply_config_payload(cfg, slice_payload)

@router.put("/configs/{config_id}")
def update_ai_config(
    config_id: int,
    body: AssistantAIConfigUpdate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    bot_snapshot_before = _bot_runtime_snapshot(cfg)
    updates = body.model_dump(exclude_unset=True)
    if "model_preset_id" in updates or "model" in updates:
        updates.update(
            _resolve_config_model_fields(
                user,
                updates.get("model_preset_id", cfg.model_preset_id),
                updates.get("model", cfg.model),
            )
        )
    if "bot_channel" in updates:
        updates["bot_channel"] = _normalize_bot_channel(updates.get("bot_channel"))
    next_bot_channel = updates.get("bot_channel", cfg.bot_channel)
    # bot_configs is a nested dict on the wire; route it through each
    # adapter's apply_config_payload (which serializes to JSON onto cfg)
    # and remove it from the simple-scalar setattr loop below.
    bot_configs_payload = updates.pop("bot_configs", None)
    if bot_configs_payload is not None or "bot_channel" in updates:
        _apply_bot_configs_from_payload(
            cfg, bot_configs_payload, active_channel=next_bot_channel
        )
    if "ai_role" in updates:
        updates["ai_role"] = _normalize_ai_role(updates.get("ai_role"))
        # 角色扁平化：不允许把普通数字成员提升为辅助管理员（系统默认创建的
        # 那一个保持原角色不受影响）。
        if updates["ai_role"] == "assistant_admin" and str(cfg.ai_role or "") != "assistant_admin":
            updates["ai_role"] = "digital_member"
    next_ai_role = updates.get("ai_role", cfg.ai_role)
    if "digital_member_role" in updates or next_ai_role == "digital_member":
        updates["digital_member_role"] = _normalize_digital_member_role(
            updates.get("digital_member_role", cfg.digital_member_role)
        )
    updates.pop("workspace_root", None)
    # 人格 Prompt 列已删：从 setattr 循环里取出，单独落盘到 personas 文件。
    prompt_update = updates.pop("prompt", None)
    if "system_auto_control" in updates:
        updates["system_auto_control"] = compact_system_auto_control(updates["system_auto_control"])
    if next_ai_role == "assistant_admin":
        updates["token_limit"] = 0
    updates.pop("enabled", None)
    for key, value in updates.items():
        setattr(cfg, key, value)
    cfg.enabled = True
    # Narrow the saved tool set to what this AI's role tier is permitted to use.
    cfg.mcp_tools = clamp_tools_json(user, config_role_tier(cfg), cfg.mcp_tools)
    # Strip server toolbox-gated tools (now sourced only from toolbox scope).
    try:
        import json
        from tools.engine import is_toolbox_gated_tool
        parsed = json.loads(cfg.mcp_tools or "[]")
        if isinstance(parsed, list):
            kept = [str(x) for x in parsed if not is_toolbox_gated_tool(x)]
            cfg.mcp_tools = json.dumps(kept, ensure_ascii=False)
    except Exception:
        pass
    cfg.updated_at = time.time()
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    _write_persona_file(
        user.id,
        cfg,
        prompt=prompt_update,
    )
    _refresh_bot_long_connections_if_needed(
        bot_snapshot_before != _bot_runtime_snapshot(cfg)
    )

    status = session.exec(
        select(AIRuntimeStatus).where(
            AIRuntimeStatus.user_id == user.id,
            AIRuntimeStatus.ai_config_id == cfg.id,
            AIRuntimeStatus.ai_kind == "assistant",
        )
    ).first()
    if not status:
        status = AIRuntimeStatus(user_id=user.id, ai_config_id=cfg.id, ai_kind="assistant")
    status.running = cfg.enabled
    status.mcp_enabled = cfg.mcp_enabled
    status.updated_at = time.time()
    session.add(status)
    session.commit()
    return _cfg_response(cfg, user.id)

def _compute_root_manager(session: Session, user_id: int, cfg: AssistantAIConfig) -> int:
    seen = set()
    current = cfg
    root_id = int(current.id or 0)
    while current and current.parent_ai_config_id is not None:
        pid = int(current.parent_ai_config_id)
        if pid in seen:
            break
        seen.add(pid)
        parent = session.exec(
            select(AssistantAIConfig).where(
                AssistantAIConfig.user_id == user_id,
                AssistantAIConfig.id == pid,
            )
        ).first()
        if not parent:
            break
        root_id = int(parent.id or 0)
        current = parent
    return root_id


@router.post("/configs/{config_id}/bind-parent")
def bind_ai_parent(
    config_id: int,
    body: dict,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    raw_parent = body.get("parent_ai_config_id") if isinstance(body, dict) else None
    if raw_parent is None:
        raise HTTPException(status_code=400, detail="parent_ai_config_id is required")
    try:
        parent_id = int(raw_parent)
    except Exception:
        raise HTTPException(status_code=400, detail="parent_ai_config_id must be an integer")
    if parent_id == config_id:
        raise HTTPException(status_code=400, detail="An AI cannot be its own parent")
    parent = get_ai_config_or_404(session, parent_id, user.id, detail="Parent AI config not found")

    # Cycle guard: walking up from the prospective parent must not reach config_id.
    cursor = parent
    seen = set()
    while cursor and cursor.parent_ai_config_id is not None:
        pid = int(cursor.parent_ai_config_id)
        if pid == config_id:
            raise HTTPException(status_code=400, detail="Binding would create a management cycle")
        if pid in seen:
            break
        seen.add(pid)
        cursor = session.get(AssistantAIConfig, pid)

    cfg.parent_ai_config_id = parent_id
    cfg.root_manager_ai_config_id = _compute_root_manager(session, user.id, cfg)
    if str(cfg.management_scope or "self") == "self":
        cfg.management_scope = "children"
    # Ensure the parent can manage children by default.
    if str(parent.management_scope or "self") == "self":
        parent.management_scope = "children"
        session.add(parent)
    cfg.updated_at = time.time()
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return _cfg_response(cfg, user.id)


@router.post("/configs/{config_id}/unbind-parent")
def unbind_ai_parent(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    cfg.parent_ai_config_id = None
    cfg.root_manager_ai_config_id = int(cfg.id or 0)
    cfg.updated_at = time.time()
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return _cfg_response(cfg, user.id)


@router.get("/governance/tree")
def get_governance_tree(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    rows = session.exec(
        select(AssistantAIConfig)
        .where(AssistantAIConfig.user_id == user.id)
        .order_by(AssistantAIConfig.sort_order.asc(), AssistantAIConfig.created_at.asc())
    ).all()
    nodes = {}
    for cfg in rows:
        nodes[int(cfg.id or 0)] = {
            "ai_config_id": int(cfg.id or 0),
            "name": cfg.name,
            "ai_role": cfg.ai_role,
            "digital_member_role": cfg.digital_member_role,
            "parent_ai_config_id": cfg.parent_ai_config_id,
            "management_scope": cfg.management_scope,
            "project_id": cfg.project_id,
            "children": [],
        }
    roots = []
    for cfg in rows:
        node = nodes[int(cfg.id or 0)]
        pid = cfg.parent_ai_config_id
        if pid is not None and int(pid) in nodes:
            nodes[int(pid)]["children"].append(node)
        else:
            roots.append(node)
    return {"roots": roots, "count": len(rows)}


@router.post("/configs/{config_id}/clone")
def clone_ai_config(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    src = get_ai_config_or_404(session, config_id, user.id)
    from api.services.knowledge import kb_store

    src_prompt = kb_store.effective_ai_prompt(user.id, src)
    new_cfg = AssistantAIConfig(
        user_id=user.id,
        name=f"{src.name} (副本)",
        description=src.description,
        avatar=src.avatar or "ai_avatars1.png",
        api_key=src.api_key,
        base_url=src.base_url,
        model=src.model,
        model_preset_id=src.model_preset_id,
        ai_role=src.ai_role,
        digital_member_role=src.digital_member_role,
        platform=src.platform,
        generation=src.generation,
        token_limit=src.token_limit,
        lifecycle_status=src.lifecycle_status,
        current_behavior="等待指令...",
        workspace_root=None,
        database_uri=src.database_uri,
        bot_channel="feishu",
        feishu_enabled=False,
        qq_enabled=False,
        project_id=src.project_id,
        project_name=src.project_name,
        parent_ai_config_id=src.parent_ai_config_id,
        management_scope=src.management_scope,
        sort_order=(src.sort_order or 100) + 1,
        enabled=True,
        mcp_enabled=src.mcp_enabled,
        switch_key=f"assistant_{int(time.time() * 1000)}",
        mcp_tools=clamp_tools_json(user, config_role_tier(src), src.mcp_tools),
        system_auto_control=compact_system_auto_control(src.system_auto_control),
    )
    session.add(new_cfg)
    session.commit()
    session.refresh(new_cfg)
    _ensure_ai_workspace_dir(user.id, new_cfg.id)
    # Strip toolbox server tools from cloned mcp_tools (source of truth moved to toolbox scope)
    try:
        import json
        from tools.engine import is_toolbox_gated_tool
        p = json.loads(new_cfg.mcp_tools or "[]")
        if isinstance(p, list):
            new_cfg.mcp_tools = json.dumps([str(x) for x in p if not is_toolbox_gated_tool(str(x))], ensure_ascii=False)
    except Exception:
        pass
    _write_persona_file(user.id, new_cfg, prompt=src_prompt)
    return _cfg_response(new_cfg, user.id)


@router.post("/configs/{config_id}/toggle-mcp")
def toggle_ai_mcp(
    config_id: int,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    cfg = get_ai_config_or_404(session, config_id, user.id)
    cfg.mcp_enabled = not cfg.mcp_enabled
    cfg.updated_at = time.time()
    session.add(cfg)
    status = session.exec(
        select(AIRuntimeStatus).where(
            AIRuntimeStatus.user_id == user.id,
            AIRuntimeStatus.ai_config_id == cfg.id,
            AIRuntimeStatus.ai_kind == "assistant",
        )
    ).first()
    if not status:
        status = AIRuntimeStatus(user_id=user.id, ai_config_id=cfg.id, ai_kind="assistant")
    status.mcp_enabled = cfg.mcp_enabled
    status.updated_at = time.time()
    session.add(status)
    session.commit()
    return _cfg_response(cfg, user.id)
