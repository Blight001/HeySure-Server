"""Shared in-process state and identifiers for AI messages."""
from __future__ import annotations
import hashlib
import re
import threading
import uuid
from concurrent.futures import Future
from typing import Any, Dict, Optional

class _PendingReplyRegistry:
    """进程内的 message_id → Future 注册表。

    发送方调用 ``register`` 拿到一个 Future，目标 AI 调用 ``reply``
    成功后 ``resolve`` 会立即唤醒。等待方在 timeout/cancel 时主动调用
    ``discard`` 清理。所有方法均为线程安全。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: Dict[str, Future] = {}

    def register(self, message_id: str) -> Future:
        fut: Future = Future()
        with self._lock:
            old = self._waiters.pop(message_id, None)
            self._waiters[message_id] = fut
        if old is not None and not old.done():
            # 旧 waiter 异常残留——给个明确状态，不让对方永远挂着。
            old.set_result({"status": "failed", "failure_reason": "superseded by new waiter"})
        return fut

    def resolve(self, message_id: str, payload: Dict[str, Any]) -> bool:
        with self._lock:
            fut = self._waiters.pop(message_id, None)
        if fut is None:
            return False
        if not fut.done():
            fut.set_result(payload)
        return True

    def discard(self, message_id: str) -> None:
        with self._lock:
            self._waiters.pop(message_id, None)


_pending_replies = _PendingReplyRegistry()
_WAKE_LOCK = threading.Lock()
DEFAULT_REPLY_WAIT_SECONDS = 24 * 60 * 60


def _new_message_id() -> str:
    return f"mai_{uuid.uuid4().hex[:14]}"


def _content_requests_response(content: str) -> bool:
    text = (content or "").strip().lower()
    if not text:
        return False
    return bool(re.search(r"(回复|回信|回话|回应|答复|确认|收到|回我|回传|reply|respond|response|ack)", text))


def ai_pair_channel_id(*, user_id: int, ai_config_id_a: int, ai_config_id_b: int) -> str:
    """Return the stable human/debug identifier for an AI-to-AI channel.

    The two AIs may have different ChatSession IDs (one session per AI), but
    they still belong to one durable point-to-point channel.  Exposing this
    identifier in the tool result makes the selected route unambiguous and
    lets clients correlate later messages without guessing from session names.
    """
    left, right = sorted((int(ai_config_id_a), int(ai_config_id_b)))
    seed = f"{int(user_id)}:{left}:{right}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"ai_pair_{left}_{right}_{digest}"


def stable_peer_session_id(
    *,
    user_id: int,
    from_ai_config_id: int,
    to_ai_config_id: int,
    from_session_id: str,
) -> str:
    """Deterministic target-side session for one sender conversation."""
    from_session_id = (from_session_id or "").strip()
    seed = f"{int(user_id)}:{int(from_ai_config_id)}:{int(to_ai_config_id)}:{from_session_id}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"ai_mail_{int(from_ai_config_id)}_{int(to_ai_config_id)}_{digest}"


# ---------------------------------------------------------------------------
# Send / fetch / reply
# ---------------------------------------------------------------------------


_ALLOWED_MESSAGE_TYPES = {"inquiry", "reply", "notify"}


def _normalize_message_type(value: Optional[str], *, require_reply: bool) -> str:
    text = str(value or "").strip().lower()
    if text in _ALLOWED_MESSAGE_TYPES:
        return text
    # 兜底：require_reply=True 默认 inquiry，否则 notify。保持旧调用者无须显式指定。
    return "inquiry" if require_reply else "notify"



