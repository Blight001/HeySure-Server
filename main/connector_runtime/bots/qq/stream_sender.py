"""Typewriter-style streaming of an in-flight answer to a QQ session.

The inference loop (``ai_runtime``) grows the live answer via
``_set_run_live_text``; that chokepoint forwards every update to the
:class:`QQStreamSession` registered for the run (see
``api.chat_runtime.run_state``). The session throttles those updates into QQ
"stream" packets so the user sees the answer materialise live, then emits a
final ``state=完成`` packet when the run ends.

Multi-turn / tool-using runs emit *one message bubble per turn*. A run that
calls tools streams several assistant turns: each turn first produces visible
text, then a tool call, then the loop continues. The inference loop signals a
turn boundary by pushing an empty live-text snapshot
(``_set_run_live_text(run_id, "")``) after each turn. On that signal we
finalize the current bubble (its own ``state=完成`` packet, or a full send if
streaming never opened) and roll a fresh bubble for the next turn. Without this,
every turn overwrote ``_last_text`` and only the final turn's text was ever
delivered — intermediate assistant text accompanying tool calls was lost.

Robustness contract — streaming must never lose or duplicate a message:

* While a session is registered, the normal post-persistence delivery
  (``QQBot.notify_assistant_message``) is suppressed for *assistant* messages
  of that session — the stream owns delivery of every turn.
* Each turn's bubble is delivered exactly once: a ``state=完成`` packet on the
  happy path, or a plain/markdown full send when streaming was rejected or the
  bubble never opened (QQ streaming is a whitelist capability).
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional, Set

from sqlmodel import Session, select

from api.chat_runtime.mcp_parser import strip_tool_call_blocks
from api.chat_runtime.run_state import pop_run_stream, register_run_stream
from api.database import engine
from api.models import AssistantAIConfig, BotSessionRoute
from ._config import read_qq_config
from .service import post_qq_stream_packet, send_qq_markdown_message

logger = logging.getLogger(__name__)

CHANNEL = "qq"

# QQ stream ``state`` enum (per the access-restricted official ``send.html``):
#   1  = 生成中 (first / intermediate packet)
#   10 = 完成   (final packet)
# Verify against your open-platform console before relying on these values.
_STREAM_STATE_GENERATING = 1
_STREAM_STATE_FINISHED = 10

# Minimum gap between intermediate packets — keeps us well under QQ's passive
# reply throughput while still feeling live.
_STREAM_THROTTLE_SECONDS = 0.7

# Sessions with an active stream. Read by ``QQBot.notify_assistant_message`` to
# suppress the duplicate full-message delivery.
_ACTIVE_SESSIONS: Set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def _visible(text: str) -> str:
    """Strip MCP-call blocks/partials so tool traffic never streams to chat.

    Mirrors ``connector_runtime.bots.notify._visible_content`` so the live
    stream and the final persisted message render the same body.
    """
    body = str(text or "")
    if not body:
        return ""
    return strip_tool_call_blocks(body)


def is_stream_active(session_id: str) -> bool:
    """True while a QQ stream owns delivery for ``session_id``."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    with _ACTIVE_LOCK:
        return sid in _ACTIVE_SESSIONS


def _mark_active(session_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_SESSIONS.add(str(session_id or "").strip())


def _mark_inactive(session_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_SESSIONS.discard(str(session_id or "").strip())


class QQStreamSession:
    """Accumulates live answer text and flushes it as QQ stream packets."""

    def __init__(
        self,
        *,
        user_id: int,
        ai_config_id: int,
        ai_kind: str,
        session_id: str,
        target_id: str,
        target_type: str,
        msg_id: str,
        event_id: str,
        start_seq: int,
        markdown_mode: str,
        template_id: str,
    ) -> None:
        self.user_id = int(user_id)
        self.ai_config_id = int(ai_config_id)
        self.ai_kind = str(ai_kind or "core")
        self.session_id = str(session_id)
        self.target_id = str(target_id)
        self.target_type = str(target_type or "c2c")
        self.msg_id = str(msg_id or "")
        self.event_id = str(event_id or "")
        self.markdown_mode = str(markdown_mode or "native")
        self.template_id = str(template_id or "")

        self._lock = threading.Lock()
        self._close_evt = threading.Event()
        self._seq = max(1, int(start_seq or 1))
        self._done = False          # whole session finalized (guards finish)
        # Texts of completed turns awaiting their final packet. The inference
        # loop marks a turn boundary by pushing an empty live-text snapshot;
        # ``update`` moves the just-finished bubble's text here so the flush
        # thread can close it and roll a fresh bubble for the next turn.
        self._finalize_queue: List[str] = []
        self._last_text = ""        # current bubble's latest answer text
        # Per-bubble send state — reset by ``_roll_bubble`` between turns.
        self._index = 0
        self._stream_id = ""
        self._started = False
        self._failed = False
        self._last_sent_text = ""   # last full text we successfully pushed

        # Packets are flushed on a dedicated thread so the inference loop's
        # ``_set_run_live_text`` call (the source of ``update``) is never blocked
        # by a QQ HTTP round-trip.
        self._thread = threading.Thread(
            target=self._loop, name=f"qqstream-{self.session_id}", daemon=True
        )
        self._thread.start()

    # ---- live-text observer interface (called from _set_run_live_text) -----

    def update(self, text: str) -> None:
        body = _visible(text)
        with self._lock:
            if not body:
                # Empty snapshot = turn boundary. Hand the just-finished
                # bubble's text to the flush thread to close, and start a fresh
                # bubble for the next turn. Empty/tool-only turns are skipped.
                if self._last_text:
                    self._finalize_queue.append(self._last_text)
                    self._last_text = ""
                return
            self._last_text = body

    # ---- lifecycle --------------------------------------------------------

    def finish(self) -> None:
        """Signal completion; the flush thread delivers every pending bubble."""
        with self._lock:
            if self._done:
                return
        self._close_evt.set()
        self._thread.join(timeout=25.0)
        _mark_inactive(self.session_id)

    # ---- internals --------------------------------------------------------

    def _loop(self) -> None:
        """Throttled flush loop; finalizes each turn's bubble in order."""
        while not self._close_evt.is_set():
            if self._tick():
                # Did delivery work (finalized a bubble) — keep draining the
                # queue promptly instead of waiting out the throttle window.
                continue
            self._close_evt.wait(_STREAM_THROTTLE_SECONDS)
        self._drain_on_close()

    def _tick(self) -> bool:
        """One flush step. Returns True when a completed bubble was finalized."""
        with self._lock:
            fin_text = self._finalize_queue.pop(0) if self._finalize_queue else None
            live_text = self._last_text
        if fin_text is not None:
            self._finalize_bubble(fin_text)
            return True
        if live_text and live_text != self._last_sent_text and not self._failed:
            self._send_packet(live_text, final=False)
        return False

    def _drain_on_close(self) -> None:
        """Flush every queued bubble, then the in-flight one, exactly once."""
        with self._lock:
            if self._done:
                return
            self._done = True
        try:
            while True:
                with self._lock:
                    fin_text = self._finalize_queue.pop(0) if self._finalize_queue else None
                if fin_text is None:
                    break
                self._finalize_bubble(fin_text)
            with self._lock:
                text = self._last_text
                self._last_text = ""
            if text:
                self._finalize_bubble(text)
        finally:
            _mark_inactive(self.session_id)

    def _finalize_bubble(self, text: str) -> None:
        """Close the current bubble with ``text`` then roll to a fresh one."""
        text = str(text or "")
        if not text:
            self._roll_bubble()
            return
        try:
            if self._started and not self._failed:
                # Happy path: close the live stream with a 完成 packet.
                self._send_packet(text, final=True, force=(text == self._last_sent_text))
                if self._failed:
                    self._fallback_full_send(text)
            else:
                # Stream never opened or was rejected mid-flight — deliver the
                # whole turn once as an ordinary (markdown→plain) message.
                self._fallback_full_send(text)
        finally:
            self._roll_bubble()

    def _roll_bubble(self) -> None:
        """Reset per-bubble state so the next turn streams as a new message."""
        self._index = 0
        self._stream_id = ""
        self._started = False
        self._failed = False
        self._last_sent_text = ""
        self._seq += 1

    def _send_packet(self, text: str, *, final: bool, force: bool = False) -> None:
        if self._failed and not force:
            return
        packet_text = str(text or "")
        if not packet_text:
            return
        # QQ stream packets are safer as full snapshots: packet ``index`` gives
        # ordering and ``reset`` replaces the in-flight markdown body. Sending
        # only deltas can look like the answer jumps from the first fragment to
        # the final packet on bots where intermediate appends are dropped.
        reset = self._started
        seq = self._seq
        state = _STREAM_STATE_FINISHED if final else _STREAM_STATE_GENERATING
        try:
            data = post_qq_stream_packet(
                self.user_id,
                self.ai_config_id,
                text=packet_text,
                target_id=self.target_id,
                target_type=self.target_type,
                stream_id=self._stream_id,
                stream_index=self._index,
                stream_state=state,
                reset=reset,
                msg_id=self.msg_id,
                event_id=self.event_id,
                msg_seq=seq if self.msg_id else None,
                markdown_mode=self.markdown_mode,
                template_id=self.template_id,
            )
            # Prefer a server-assigned stream id once we have one.
            if isinstance(data, dict):
                assigned = str(data.get("id") or (data.get("data") or {}).get("id") or "").strip()
                if assigned:
                    self._stream_id = assigned
            self._started = True
            self._index += 1
            self._last_sent_text = text
            if final:
                self._bump_route_sequence(seq + 1)
        except Exception as exc:
            self._failed = True
            logger.info(f"qq stream packet failed session={self.session_id}: {exc}")

    def _fallback_full_send(self, text: str) -> None:
        # If a stream packet already opened a passive reply, use the following
        # msg_seq for the fallback full message. If streaming was rejected on
        # the first packet, the original sequence is still available.
        seq = self._seq + 1 if self._started else self._seq
        try:
            send_qq_markdown_message(
                self.user_id,
                self.ai_config_id,
                text=text,
                target_id=self.target_id,
                target_type=self.target_type,
                msg_id=self.msg_id,
                event_id=self.event_id,
                msg_seq=seq if self.msg_id else None,
                markdown_mode=self.markdown_mode,
                template_id=self.template_id,
                fallback_plain=True,
            )
            self._bump_route_sequence(seq + 1)
        except Exception as exc:
            logger.warning(f"qq stream fallback send failed session={self.session_id}: {exc}")

    def _bump_route_sequence(self, next_seq: int) -> None:
        if not self.msg_id:
            return
        try:
            with Session(engine) as session:
                row = _load_route_row(
                    session,
                    user_id=self.user_id,
                    ai_config_id=self.ai_config_id,
                    ai_kind=self.ai_kind,
                    session_id=self.session_id,
                )
                if row is None:
                    return
                row.next_msg_seq = max(int(row.next_msg_seq or 1), int(next_seq))
                session.add(row)
                session.commit()
        except Exception:
            logger.debug(
                f"qq stream route sequence bump failed session={self.session_id}",
                exc_info=True,
            )


def _load_route_row(session: Session, *, user_id: int, ai_config_id: int, ai_kind: str, session_id: str):
    return session.exec(
        select(BotSessionRoute).where(
            BotSessionRoute.channel == CHANNEL,
            BotSessionRoute.user_id == int(user_id),
            BotSessionRoute.ai_config_id == int(ai_config_id),
            BotSessionRoute.ai_kind == str(ai_kind or "core"),
            BotSessionRoute.session_id == str(session_id or ""),
        )
    ).first()


def maybe_start_qq_stream(
    *,
    run_id: str,
    user_id: int,
    ai_config_id: Optional[int],
    ai_kind: str,
    session_id: str,
) -> Optional[QQStreamSession]:
    """Register a stream session for ``run_id`` when QQ streaming applies.

    Returns the session (already registered as the run's live-text observer)
    or ``None`` when streaming is disabled / not a QQ session / no route. Never
    raises — a failure here must not affect the run.
    """
    try:
        if not ai_config_id or not str(session_id or "").startswith("qq_"):
            return None
        import json as _json

        with Session(engine) as session:
            cfg = session.get(AssistantAIConfig, int(ai_config_id))
            if not cfg or int(cfg.user_id or 0) != int(user_id):
                return None
            if str(cfg.bot_channel or "feishu").strip().lower() != CHANNEL:
                return None
            bot_cfg = read_qq_config(cfg)
            if not bot_cfg.get("enabled") or not bool(bot_cfg.get("stream_enabled", True)):
                return None
            if str(bot_cfg.get("markdown_mode") or "native").strip().lower() == "off":
                # Streaming rides on markdown messages; without markdown there is
                # nothing to stream into.
                return None
            row = _load_route_row(
                session,
                user_id=user_id,
                ai_config_id=int(ai_config_id),
                ai_kind=ai_kind,
                session_id=session_id,
            )
            if row is None:
                return None
            try:
                target = _json.loads(row.target_json or "{}")
            except Exception:
                target = {}
            target_id = str(target.get("target_id") or "").strip()
            if not target_id:
                return None
            target_type = str(target.get("target_type") or "c2c").strip().lower()
            if target_type != "c2c":
                return None
            stream = QQStreamSession(
                user_id=int(user_id),
                ai_config_id=int(ai_config_id),
                ai_kind=str(ai_kind or "core"),
                session_id=str(session_id),
                target_id=target_id,
                target_type=target_type,
                msg_id=str(row.source_message_id or ""),
                event_id=str(row.source_event_id or ""),
                start_seq=int(row.next_msg_seq or 1),
                markdown_mode=str(bot_cfg.get("markdown_mode") or "native"),
                template_id=str(bot_cfg.get("markdown_template_id") or ""),
            )
        _mark_active(session_id)
        register_run_stream(run_id, stream)
        return stream
    except Exception as exc:
        logger.info(f"qq stream not started run={run_id}: {exc}")
        return None


def finish_qq_stream(run_id: str, *, session_id: str = "") -> None:
    """Finalize and detach the stream session for ``run_id`` (if any)."""
    hook = pop_run_stream(run_id)
    if isinstance(hook, QQStreamSession):
        try:
            hook.finish()
        except Exception as exc:
            logger.warning(f"qq stream finish failed run={run_id}: {exc}")
    elif session_id:
        # Defensive: ensure suppression is cleared even if the hook is gone.
        _mark_inactive(session_id)
