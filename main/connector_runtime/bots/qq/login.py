"""Official QQ Bot QR authorization for Tencent's OpenClaw binding flow."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from api.database import engine
from api.models import BotConnection
from api.services.bot_directory import (
    ensure_connection,
    release_connection_binding,
    update_connection_config,
)
from ._config import QQ_DEFAULTS
from .qr_protocol import (
    build_qr_url,
    create_bind_task,
    decrypt_app_secret,
    poll_bind_task,
)


logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 2.0


@dataclass
class QQLoginAttempt:
    session_key: str
    config_id: int
    user_id: int
    connection_ref: str
    task_id: str
    decrypt_key: str
    qrcode_url: str
    state: str = "awaiting_scan"
    message: str = "请使用手机 QQ 扫描二维码完成机器人绑定"
    started_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 300)


class QQLoginManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: Dict[str, QQLoginAttempt] = {}

    def _connection_ref(self, config_id: int, user_id: int, connection_ref: str) -> str:
        with Session(engine) as session:
            row = ensure_connection(
                session,
                user_id=user_id,
                ai_config_id=config_id,
                channel="qq",
                name="QQ机器人",
                connection_ref=connection_ref,
            )
            session.commit()
            return row.connection_ref

    def start(self, config_id: int, user_id: int, connection_ref: str = "") -> Dict[str, Any]:
        connection_ref = self._connection_ref(config_id, user_id, connection_ref)
        task_id, key = create_bind_task()
        attempt = QQLoginAttempt(
            session_key=str(uuid.uuid4()),
            config_id=config_id,
            user_id=user_id,
            connection_ref=connection_ref,
            task_id=task_id,
            decrypt_key=key,
            qrcode_url=build_qr_url(task_id),
        )
        with self._lock:
            self._attempts[connection_ref] = attempt
        threading.Thread(
            target=self._poll,
            args=(attempt,),
            daemon=True,
            name=f"qq-login-{connection_ref}",
        ).start()
        return self.snapshot(config_id, connection_ref)

    def snapshot(self, config_id: int, connection_ref: str = "") -> Dict[str, Any]:
        with self._lock:
            attempt = self._attempts.get(connection_ref) if connection_ref else next(
                (item for item in self._attempts.values() if item.config_id == config_id), None
            )
            if attempt:
                return {
                    "session_key": attempt.session_key,
                    "state": attempt.state,
                    "message": attempt.message,
                    "qrcode_url": attempt.qrcode_url if attempt.state == "awaiting_scan" else "",
                    "expires_at": attempt.expires_at,
                    "connected": attempt.state == "connected",
                }
        with Session(engine) as session:
            stmt = select(BotConnection).where(
                BotConnection.channel == "qq",
                BotConnection.ai_config_id == config_id,
                BotConnection.state != "deleted",
            )
            if connection_ref:
                stmt = stmt.where(BotConnection.connection_ref == connection_ref)
            row = session.exec(stmt.order_by(BotConnection.is_default.desc())).first()
        configured = bool(row and row.credentials_encrypted and row.provider_account_id)
        return {
            "state": "connected" if configured else "disconnected",
            "message": "QQ机器人凭据已配置" if configured else "尚未连接 QQ机器人",
            "connected": configured,
            "account_id": row.provider_account_id if row else "",
        }

    def logout(self, config_id: int, connection_ref: str = "") -> Dict[str, Any]:
        with self._lock:
            if connection_ref:
                self._attempts.pop(connection_ref, None)
        with Session(engine) as session:
            stmt = select(BotConnection).where(
                BotConnection.channel == "qq", BotConnection.ai_config_id == config_id
            )
            if connection_ref:
                stmt = stmt.where(BotConnection.connection_ref == connection_ref)
            row = session.exec(stmt.order_by(BotConnection.is_default.desc())).first()
            if row:
                release_connection_binding(row)
                session.add(row)
                session.commit()
        self._refresh_connections()
        return {"state": "disconnected", "message": "已断开 QQ机器人", "connected": False}

    def _is_current(self, attempt: QQLoginAttempt) -> bool:
        with self._lock:
            return self._attempts.get(attempt.connection_ref) is attempt

    def _set_attempt(self, attempt: QQLoginAttempt, state: str, message: str) -> None:
        with self._lock:
            if self._attempts.get(attempt.connection_ref) is attempt:
                attempt.state = state
                attempt.message = message

    def _save_connection(self, attempt: QQLoginAttempt, response: Dict[str, Any]) -> None:
        app_id = str(response.get("bot_appid") or "").strip()
        encrypted_secret = str(response.get("bot_encrypt_secret") or "").strip()
        app_secret = decrypt_app_secret(encrypted_secret, attempt.decrypt_key)
        if not app_id or not app_secret:
            raise ValueError("扫码成功，但 QQ 未返回完整机器人凭据")
        now = time.time()
        with Session(engine) as session:
            stale_rows = session.exec(select(BotConnection).where(
                BotConnection.channel == "qq",
                BotConnection.provider_account_id == app_id,
                BotConnection.state == "deleted",
            )).all()
            for stale in stale_rows:
                release_connection_binding(stale, deleted=True)
                session.add(stale)
            if stale_rows:
                session.flush()
            row = session.exec(select(BotConnection).where(
                BotConnection.channel == "qq",
                BotConnection.ai_config_id == attempt.config_id,
                BotConnection.connection_ref == attempt.connection_ref,
            )).first()
            if row is None:
                row = ensure_connection(
                    session,
                    user_id=attempt.user_id,
                    ai_config_id=attempt.config_id,
                    channel="qq",
                    name="QQ机器人",
                    connection_ref=attempt.connection_ref,
                )
            update_connection_config(
                row,
                {"enabled": True, "app_id": app_id, "app_secret": app_secret},
                QQ_DEFAULTS,
            )
            row.provider = "tencent_qqbot"
            row.provider_account_id = app_id
            row.owner_external_id = str(response.get("user_openid") or "")
            row.state = "configured"
            row.last_error_code = ""
            row.last_seen_at = now
            row.updated_at = now
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("该 QQ机器人已绑定到另一个 AI") from exc

    @staticmethod
    def _refresh_connections() -> None:
        from .long_connection import start_qq_long_connection_clients

        start_qq_long_connection_clients()

    def _poll(self, attempt: QQLoginAttempt) -> None:
        while time.time() < attempt.expires_at and self._is_current(attempt):
            try:
                response = poll_bind_task(attempt.task_id)
                status = int(response.get("status") or 0)
                if status == 2:
                    try:
                        self._save_connection(attempt, response)
                        self._set_attempt(attempt, "connected", "QQ机器人扫码绑定成功")
                        self._refresh_connections()
                    except Exception as exc:
                        logger.warning(
                            "qq login save failed connection_ref=%s error_type=%s",
                            attempt.connection_ref,
                            type(exc).__name__,
                        )
                        self._set_attempt(attempt, "failed", str(exc) or "QQ机器人绑定失败")
                    return
                if status == 3:
                    self._set_attempt(attempt, "expired", "二维码已过期，请重新生成")
                    return
            except Exception as exc:
                logger.warning(
                    "qq login poll failed connection_ref=%s error_type=%s",
                    attempt.connection_ref,
                    type(exc).__name__,
                )
            time.sleep(POLL_INTERVAL_SECONDS)
        self._set_attempt(attempt, "expired", "二维码已过期，请重新生成")


manager = QQLoginManager()
