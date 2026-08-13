"""Connector-owned iLink getupdates long-poll workers."""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict

from sqlmodel import Session, select

from api.database import engine
from api.models import BotConnection
from api.services.bot_credentials import decrypt_credentials
from .ilink_client import ILinkClient


logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_THREADS: Dict[str, threading.Thread] = {}
_STOPS: Dict[str, threading.Event] = {}
_STATES: Dict[str, Dict[str, str]] = {}


def _state(connection_ref: str, status: str, message: str) -> None:
    with _LOCK:
        _STATES[connection_ref] = {"status": status, "mode": "ilink", "label": "成功" if status == "success" else "连接中" if status == "starting" else "失败", "message": message}


def get_wechat_connection_state(config_id: int, connection_ref: str = "") -> Dict[str, str]:
    with Session(engine) as session:
        stmt = select(BotConnection).where(
            BotConnection.channel == "wechat", BotConnection.ai_config_id == config_id
        )
        if connection_ref:
            stmt = stmt.where(BotConnection.connection_ref == connection_ref)
        rows = session.exec(stmt.order_by(BotConnection.is_default.desc())).all()
    with _LOCK:
        for row in rows:
            current = _STATES.get(row.connection_ref)
            if current:
                return dict(current)
    row = rows[0] if rows else None
    if row and row.state == "connected":
        return {"status": "starting", "mode": "ilink", "label": "连接中", "message": "等待微信长轮询启动"}
    return {"status": "failed", "mode": "ilink", "label": "未连接", "message": "请扫码连接微信机器人"}


def _is_enabled(connection_ref: str) -> bool:
    with Session(engine) as session:
        row = session.exec(select(BotConnection).where(
            BotConnection.connection_ref == connection_ref,
            BotConnection.channel == "wechat",
        )).first()
        return bool(row and row.enabled and row.state == "connected")


def _save_cursor(connection_ref: str, cursor: str) -> None:
    with Session(engine) as session:
        row = session.exec(select(BotConnection).where(
            BotConnection.channel == "wechat", BotConnection.connection_ref == connection_ref
        )).first()
        if row:
            row.sync_cursor = str(cursor or "")
            row.last_seen_at = time.time()
            row.updated_at = time.time()
            session.add(row)
            session.commit()


def _load_connection(connection_ref: str) -> BotConnection | None:
    with Session(engine) as session:
        return session.exec(select(BotConnection).where(
            BotConnection.channel == "wechat", BotConnection.connection_ref == connection_ref
        )).first()


def _poll_once(connection_ref: str) -> bool:
    from .router import handle_wechat_message

    row = _load_connection(connection_ref)
    if row is None or row.state != "connected":
        _state(connection_ref, "failed", "请扫码连接微信机器人")
        return False
    token = str(decrypt_credentials(row.credentials_encrypted).get("bot_token") or "")
    payload = ILinkClient(base_url=row.base_url, token=token).get_updates(row.sync_cursor)
    if int(payload.get("errcode") or 0) == -14:
        _state(connection_ref, "failed", "微信会话已失效，请重新扫码")
        return False
    if int(payload.get("ret") or 0) != 0:
        raise RuntimeError("iLink getupdates returned an error")
    _state(connection_ref, "success", "微信机器人已连接")
    cursor = str(payload.get("get_updates_buf") or row.sync_cursor or "")
    if cursor != row.sync_cursor:
        _save_cursor(connection_ref, cursor)
    for message in payload.get("msgs") or []:
        if isinstance(message, dict):
            handle_wechat_message(int(row.ai_config_id), message, connection_ref=connection_ref)
    return True


def _run(connection_ref: str, stop: threading.Event) -> None:
    failures = 0
    while not stop.is_set() and _is_enabled(connection_ref):
        try:
            if not _poll_once(connection_ref):
                return
            failures = 0
        except Exception as exc:
            failures += 1
            logger.warning("wechat poll failed connection_ref=%s error_type=%s", connection_ref, type(exc).__name__)
            if failures >= 3:
                _state(connection_ref, "failed", "微信连接异常，正在重试")
            stop.wait(min(30.0, 2.0 ** min(failures, 4)))


def start_wechat_long_connections() -> int:
    with Session(engine) as session:
        rows = session.exec(select(BotConnection).where(
            BotConnection.channel == "wechat",
            BotConnection.enabled.is_(True),
            BotConnection.state == "connected",
        )).all()
    active = {row.connection_ref for row in rows if row.connection_ref}
    started = 0
    with _LOCK:
        for connection_ref, stop in list(_STOPS.items()):
            if connection_ref not in active:
                stop.set()
                _STOPS.pop(connection_ref, None)
                _THREADS.pop(connection_ref, None)
        for connection_ref in active:
            thread = _THREADS.get(connection_ref)
            if thread and thread.is_alive():
                continue
            stop = threading.Event()
            thread = threading.Thread(target=_run, args=(connection_ref, stop), daemon=True, name=f"wechat-ilink-{connection_ref}")
            _STOPS[connection_ref] = stop
            _THREADS[connection_ref] = thread
            thread.start()
            started += 1
    return started
