"""Slash commands shared by every inbound bot channel.

Commands are handled before a user message is persisted or an AI run is
started, so operational actions never pollute the model's conversation history.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from api.chat_runtime.chat_runtime_helpers import (
    _resolve_ai_runtime,
    build_runtime_system_prompt_and_tools,
)
from api.models import (
    AssistantAIConfig,
    BotSessionRoute,
    BotUserCursor,
    ChatMessage,
    ChatRun,
    ChatSession,
    User,
)
from api.services.chat.chat_media import delete_message_media
from api.services.chat.chat_persistence import _rebuild_usage_snapshots
from api.services.model_presets import (
    find_model_preset,
    normalize_model_presets,
    resolve_model_preset_entry,
)
from connector_runtime.bots.session_cursor import list_ai_sessions, set_active_session_id


HELP_TEXT = """机器人指令：
/help — 查看所有指令
/list — 查看对话历史及 ID
/change [id] — 切换对话
/delete [id] — 删除对话
/stop — 停止当前对话的运行
/clear — 清空当前对话
/mcp — 查看当前对话可用的 MCP
/models — 查看模型列表
/models [id] — 切换当前对话的模型
/prompt — 查看当前对话的 Prompt"""


@dataclass(frozen=True)
class BotCommandResult:
    command: str
    text: str


def parse_bot_command(text: str) -> Optional[tuple[str, str]]:
    body = str(text or "").strip()
    if not body.startswith("/"):
        return None
    head, _, tail = body.partition(" ")
    command = head[1:].strip().lower()
    if not command:
        return None
    return command, tail.strip()


def _scope_stmt(stmt, *, user_id: int, ai_config_id: int, ai_kind: str):
    return stmt.where(
        ChatSession.user_id == int(user_id),
        ChatSession.ai_config_id == int(ai_config_id),
        ChatSession.ai_kind == str(ai_kind or "core"),
    )


def _session_row(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
) -> Optional[ChatSession]:
    return session.exec(
        _scope_stmt(
            select(ChatSession).where(ChatSession.session_id == str(session_id)),
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
        )
    ).first()


def _ensure_session_row(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
    session_name: str,
) -> ChatSession:
    row = _session_row(
        session,
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        session_id=session_id,
    )
    if row is not None:
        return row
    row = ChatSession(
        user_id=int(user_id),
        ai_config_id=int(ai_config_id),
        ai_kind=str(ai_kind or "core"),
        session_id=str(session_id),
        session_name=str(session_name or "机器人对话"),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _resolve_session_ref(
    session: Session,
    ref: str,
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
) -> Optional[ChatSession]:
    value = str(ref or "").strip()
    if not value:
        return None
    stmt = _scope_stmt(
        select(ChatSession),
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
    )
    if value.isdigit():
        by_id = session.exec(stmt.where(ChatSession.id == int(value))).first()
        if by_id is not None:
            return by_id
    return session.exec(stmt.where(ChatSession.session_id == value)).first()


def _active_runs(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    session_id: str,
) -> list[ChatRun]:
    return list(
        session.exec(
            select(ChatRun).where(
                ChatRun.user_id == int(user_id),
                ChatRun.ai_config_id == int(ai_config_id),
                ChatRun.ai_kind == str(ai_kind or "core"),
                ChatRun.session_id == str(session_id),
                ChatRun.status.in_(["queued", "running"]),
            ).order_by(ChatRun.updated_at.desc())
        ).all()
    )


def _format_time(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp or 0)).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


def _list_command(
    session: Session,
    *,
    user_id: int,
    ai_config_id: int,
    ai_kind: str,
    current_session_id: str,
) -> str:
    rows = list_ai_sessions(
        session,
        user_id=user_id,
        ai_config_id=ai_config_id,
        ai_kind=ai_kind,
        active_session_id=current_session_id,
        limit=50,
    )
    counts = {
        str(sid): int(count or 0)
        for sid, count in session.exec(
            select(ChatMessage.session_id, func.count(ChatMessage.id)).where(
                ChatMessage.user_id == int(user_id),
                ChatMessage.ai_config_id == int(ai_config_id),
                ChatMessage.ai_kind == str(ai_kind or "core"),
            ).group_by(ChatMessage.session_id)
        ).all()
    }
    if not rows:
        return "暂无对话历史。发送普通消息即可创建当前对话。"
    lines = [f"对话历史（{len(rows)}）："]
    for item in rows:
        marker = "→ " if item.get("is_active") else "  "
        sid = str(item.get("session_id") or "")
        lines.append(
            f"{marker}[{item.get('id')}] {item.get('name') or '未命名对话'} "
            f"· {item.get('source') or 'web'} · {counts.get(sid, 0)} 条 "
            f"· {_format_time(float(item.get('updated_at') or 0))}"
        )
    lines.append("使用 /change [id] 切换，/delete [id] 删除。")
    return "\n".join(lines)


def _models_command(
    session: Session,
    *,
    user: User,
    cfg: AssistantAIConfig,
    current_session_id: str,
    current_session_name: str,
    ai_kind: str,
    requested_id: str,
) -> str:
    presets = normalize_model_presets(user.model_presets, user)
    if not presets:
        return "当前账号没有可用的模型预设。"
    row = _ensure_session_row(
        session,
        user_id=int(user.id or 0),
        ai_config_id=int(cfg.id or 0),
        ai_kind=ai_kind,
        session_id=current_session_id,
        session_name=current_session_name,
    )
    wanted = str(requested_id or "").strip()
    if wanted:
        selected = find_model_preset(user, wanted)
        if selected is None:
            return f"未找到模型 ID：{wanted}\n发送 /models 查看可用模型。"
        row.model_preset_id = selected["id"]
        row.updated_at = time.time()
        session.add(row)
        session.commit()
        return (
            f"已将当前对话模型切换为 [{selected['id']}] {selected['name']} "
            f"({selected['model']})。从下一条消息开始生效。"
        )

    override_id = str(getattr(row, "model_preset_id", "") or "")
    default_entry = resolve_model_preset_entry(user, cfg) or {}
    current_id = override_id or str(default_entry.get("id") or "")
    lines = ["模型列表："]
    for item in presets:
        marker = "→ " if item["id"] == current_id else "  "
        suffix = "（当前对话）" if item["id"] == current_id else ""
        lines.append(f"{marker}[{item['id']}] {item['name']} · {item['model']}{suffix}")
    lines.append("使用 /models [id] 切换当前对话模型。")
    return "\n".join(lines)


def handle_bot_command(
    session: Session,
    *,
    text: str,
    channel: str,
    user: User,
    cfg: AssistantAIConfig,
    ai_kind: str,
    identity_key: str,
    current_session_id: str,
    current_session_name: str,
    home_session_id: str,
) -> Optional[BotCommandResult]:
    parsed = parse_bot_command(text)
    if parsed is None:
        return None
    command, argument = parsed
    user_id = int(user.id or 0)
    ai_config_id = int(cfg.id or 0)

    if command == "help":
        return BotCommandResult(command, HELP_TEXT)

    if command == "list":
        return BotCommandResult(
            command,
            _list_command(
                session,
                user_id=user_id,
                ai_config_id=ai_config_id,
                ai_kind=ai_kind,
                current_session_id=current_session_id,
            ),
        )

    if command == "change":
        if not argument:
            return BotCommandResult(command, "用法：/change [id]\n发送 /list 查看对话 ID。")
        target = _resolve_session_ref(
            session,
            argument,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
        )
        if target is None:
            return BotCommandResult(command, f"未找到对话 ID：{argument}\n发送 /list 查看对话历史。")
        set_active_session_id(
            session,
            channel=channel,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            identity_key=identity_key,
            session_id=target.session_id,
        )
        return BotCommandResult(
            command,
            f"已切换到对话 [{target.id}] {target.session_name}。下一条消息将进入该对话。",
        )

    if command == "delete":
        if not argument:
            return BotCommandResult(command, "用法：/delete [id]\n发送 /list 查看对话 ID。")
        target = _resolve_session_ref(
            session,
            argument,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
        )
        if target is None:
            return BotCommandResult(command, f"未找到对话 ID：{argument}")
        if _active_runs(
            session,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=target.session_id,
        ):
            return BotCommandResult(command, "该对话仍在运行，请先切换到它并发送 /stop。")
        messages = session.exec(
            select(ChatMessage).where(
                ChatMessage.user_id == user_id,
                ChatMessage.ai_config_id == ai_config_id,
                ChatMessage.ai_kind == ai_kind,
                ChatMessage.session_id == target.session_id,
            )
        ).all()
        delete_message_media(session, messages)
        for item in messages:
            session.delete(item)
        for route in session.exec(
            select(BotSessionRoute).where(
                BotSessionRoute.user_id == user_id,
                BotSessionRoute.ai_config_id == ai_config_id,
                BotSessionRoute.ai_kind == ai_kind,
                BotSessionRoute.session_id == target.session_id,
            )
        ).all():
            session.delete(route)
        target_id = int(target.id or 0)
        target_name = str(target.session_name or "未命名对话")
        target_session_id = str(target.session_id)
        session.delete(target)
        cursors = session.exec(
            select(BotUserCursor).where(
                BotUserCursor.user_id == user_id,
                BotUserCursor.ai_config_id == ai_config_id,
                BotUserCursor.ai_kind == ai_kind,
                BotUserCursor.active_session_id == target_session_id,
            )
        ).all()
        for cursor in cursors:
            cursor.active_session_id = (
                home_session_id
                if str(cursor.channel) == str(channel)
                and str(cursor.identity_key) == str(identity_key)
                else ""
            )
            cursor.updated_at = time.time()
            session.add(cursor)
        session.commit()
        _rebuild_usage_snapshots(session, user_id, ai_kind, ai_config_id)
        return BotCommandResult(command, f"已删除对话 [{target_id}] {target_name}，共 {len(messages)} 条消息。")

    if command == "stop":
        runs = _active_runs(
            session,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=current_session_id,
        )
        if not runs:
            return BotCommandResult(command, "当前对话没有正在运行的任务。")
        now = time.time()
        for run in runs:
            run.stop_requested = True
            run.status = "stopped"
            run.finished_at = run.finished_at or now
            run.updated_at = now
            session.add(run)
        session.commit()
        return BotCommandResult(command, f"已发送停止请求，共停止 {len(runs)} 个运行。")

    if command == "clear":
        if _active_runs(
            session,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=current_session_id,
        ):
            return BotCommandResult(command, "当前对话仍在运行，请先发送 /stop，再发送 /clear。")
        row = _session_row(
            session,
            user_id=user_id,
            ai_config_id=ai_config_id,
            ai_kind=ai_kind,
            session_id=current_session_id,
        )
        messages = session.exec(
            select(ChatMessage).where(
                ChatMessage.user_id == user_id,
                ChatMessage.ai_config_id == ai_config_id,
                ChatMessage.ai_kind == ai_kind,
                ChatMessage.session_id == current_session_id,
            )
        ).all()
        delete_message_media(session, messages)
        for item in messages:
            session.delete(item)
        if row is not None:
            row.described_tools_json = ""
            row.updated_at = time.time()
            session.add(row)
        session.commit()
        _rebuild_usage_snapshots(session, user_id, ai_kind, ai_config_id)
        return BotCommandResult(command, f"已清空当前对话，共删除 {len(messages)} 条消息。")

    if command == "mcp":
        if not bool(cfg.mcp_enabled):
            return BotCommandResult(command, "当前 AI 的 MCP 功能未启用。")
        resolved_cfg, _, _, _, base_prompt = _resolve_ai_runtime(
            session, user, ai_kind, ai_config_id, current_session_id
        )
        _, tools = build_runtime_system_prompt_and_tools(
            session,
            user,
            ai_kind=ai_kind,
            ai_config_id=ai_config_id,
            session_id=current_session_id,
            cfg=resolved_cfg,
            base_system_prompt=base_prompt,
        )
        names = sorted(str(item) for item in tools if str(item).strip())
        body = "\n".join(f"{index}. {name}" for index, name in enumerate(names, 1))
        return BotCommandResult(command, f"当前对话可用 MCP（{len(names)}）：\n{body}" if names else "当前对话没有可用 MCP。")

    if command == "models":
        return BotCommandResult(
            command,
            _models_command(
                session,
                user=user,
                cfg=cfg,
                current_session_id=current_session_id,
                current_session_name=current_session_name,
                ai_kind=ai_kind,
                requested_id=argument,
            ),
        )

    if command == "prompt":
        resolved_cfg, _, _, _, base_prompt = _resolve_ai_runtime(
            session, user, ai_kind, ai_config_id, current_session_id
        )
        prompt, _ = build_runtime_system_prompt_and_tools(
            session,
            user,
            ai_kind=ai_kind,
            ai_config_id=ai_config_id,
            session_id=current_session_id,
            cfg=resolved_cfg,
            base_system_prompt=base_prompt,
        )
        return BotCommandResult(command, f"当前对话 Prompt：\n\n{prompt}")

    return BotCommandResult(command, f"未知指令：/{command}\n\n{HELP_TEXT}")
