"""AI-to-AI message normalization and model-visible prompt rendering."""

from dataclasses import dataclass
from typing import Any


SYSTEM_NOTICE_USER_REPLAY_TAG_PREFIXES = (
    "ai_message_inbound:",
    "task_completion_notice:",
    "kb_review_request",
)


def normalize_ai_message_type(value: Any, require_reply: bool) -> str:
    text = str(value or "").strip().lower()
    if text in {"inquiry", "reply", "notify"}:
        return text
    return "inquiry" if require_reply else "notify"


def should_replay_system_notice_as_user(tags: str) -> bool:
    normalized = str(tags or "").strip()
    return any(normalized.startswith(prefix) for prefix in SYSTEM_NOTICE_USER_REPLAY_TAG_PREFIXES)


@dataclass(frozen=True)
class AIMessagePrompt:
    from_ai_name: str
    from_ai_config_id: int
    target_ai_name: str
    target_ai_config_id: int
    message_id: str
    current_session_id: str
    content: str
    message_type: str
    require_reply: bool


def render_ai_message_system_prompt(prompt: AIMessagePrompt) -> str:
    from_ai_name = prompt.from_ai_name
    from_ai_config_id = prompt.from_ai_config_id
    target_ai_name = prompt.target_ai_name
    target_ai_config_id = prompt.target_ai_config_id
    message_id = prompt.message_id
    current_session_id = prompt.current_session_id
    content = prompt.content
    message_type = prompt.message_type
    require_reply = prompt.require_reply
    should_reply = bool(require_reply) or message_type == "inquiry"
    message_type_guide = (
        "- inquiry（询问）：发送方在提问、请求状态或请求结果，通常需要你答复。\n"
        "- reply（回复）：发送方在答复你之前发出的 inquiry，通常不需要再答复，除非内容明确提出新问题。\n"
        "- notify（通知）：发送方在单向告知状态、结果或提醒，不期待你回复。"
    )
    reply_rule = (
        "这条消息需要你回复。回复时调用 MCP 工具 `message.send+to`，"
        f"参数必须包含 `to=\"{from_ai_config_id}\"`、`message_type=\"reply\"`、"
        "`require_reply=false`、"
        f"`reply_to_message_id=\"{message_id}\"`、`current_session_id=\"{current_session_id}\"`。"
        if should_reply
        else "这条消息不要求回复。除非内容明确要求你另起一个新问题，否则不要回信。"
    )
    return (
        "[系统提示]\n[AI 间通信 · 强制插入]\n"
        "当前 AI 运行已被这条消息打断。你必须先处理这条系统提示，再继续原本任务。\n\n"
        f"- 收件方（你）: {target_ai_name}（ai_config_id={target_ai_config_id}）\n"
        f"- 发送方: {from_ai_name}（ai_config_id={from_ai_config_id}）\n"
        f"- 消息编号: {message_id}\n- 当前会话: {current_session_id}\n"
        f"- 消息类型: {message_type}\n- 是否要求回复: {'是' if should_reply else '否'}\n\n"
        f"[消息内容]\n{content}\n\n[发送类型说明]\n{message_type_guide}\n\n"
        "[处理规则]\n你以后调用 MCP 工具 `message.send+to` 给其他 AI 发消息时，"
        "`message_type` 是必填字段，不能省略。\n"
        f"{reply_rule}"
    )
