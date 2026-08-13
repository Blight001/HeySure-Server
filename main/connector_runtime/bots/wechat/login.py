"""Process-owned QR authorization state machine for Tencent iLink."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from api.database import engine
from api.models import BotConnection
from api.services.bot_credentials import decrypt_credentials, encrypt_credentials
from api.services.bot_directory import ensure_connection
from .ilink_client import ILinkClient, LOGIN_BASE_URL, _safe_base_url


@dataclass
class LoginAttempt:
    session_key: str
    config_id: int
    user_id: int
    qrcode: str
    qrcode_url: str
    connection_ref: str = ""
    state: str = "awaiting_scan"
    message: str = "请使用微信扫码并确认授权"
    verify_code: str = ""
    started_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 300)


class WeChatLoginManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: Dict[Any, LoginAttempt] = {}

    def _local_tokens(self) -> list[str]:
        tokens: list[str] = []
        with Session(engine) as session:
            rows = session.exec(
                select(BotConnection).where(BotConnection.channel == "wechat")
                .order_by(BotConnection.updated_at.desc())
            ).all()
        for row in rows:
            try:
                token = str(decrypt_credentials(row.credentials_encrypted).get("bot_token") or "")
            except ValueError:
                continue
            if token:
                tokens.append(token)
        return tokens[:10]

    def _connection_ref(self, config_id: int, user_id: int, connection_ref: str = "") -> str:
        with Session(engine) as session:
            row = ensure_connection(
                session, user_id=user_id, ai_config_id=config_id, channel="wechat",
                name="微信机器人", connection_ref=connection_ref,
            )
            session.commit()
            return row.connection_ref

    def start(self, config_id: int, user_id: int, connection_ref: str = "") -> Dict[str, Any]:
        connection_ref = self._connection_ref(config_id, user_id, connection_ref)
        payload = ILinkClient().create_qr(self._local_tokens())
        qrcode = str(payload.get("qrcode") or "").strip()
        qrcode_url = str(payload.get("qrcode_img_content") or "").strip()
        if not qrcode or not qrcode_url:
            raise RuntimeError("iLink 未返回有效二维码")
        attempt = LoginAttempt(
            str(uuid.uuid4()), config_id, user_id, qrcode, qrcode_url,
            connection_ref=connection_ref,
        )
        with self._lock:
            self._attempts[connection_ref] = attempt
        threading.Thread(target=self._poll, args=(attempt,), daemon=True, name=f"wechat-login-{connection_ref}").start()
        return self.snapshot(config_id, connection_ref)

    def submit_verify_code(self, config_id: int, value: str, connection_ref: str = "") -> Dict[str, Any]:
        code = str(value or "").strip()
        if not code.isdigit() or len(code) > 8:
            raise ValueError("请输入微信中显示的数字验证码")
        with self._lock:
            attempt = self._attempts.get(connection_ref) if connection_ref else next(
                (item for item in self._attempts.values() if item.config_id == config_id), None
            )
            if not attempt:
                raise ValueError("当前没有进行中的微信连接")
            attempt.verify_code = code
            attempt.state = "scanned"
            attempt.message = "正在验证配对码"
        return self.snapshot(config_id, attempt.connection_ref)

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
                    "qrcode_url": attempt.qrcode_url if attempt.state in {"awaiting_scan", "scanned", "need_verifycode"} else "",
                    "expires_at": attempt.expires_at,
                    "connected": attempt.state == "connected",
                    "needs_verify_code": attempt.state == "need_verifycode",
                }
        with Session(engine) as session:
            stmt = select(BotConnection).where(
                BotConnection.channel == "wechat", BotConnection.ai_config_id == config_id
            )
            if connection_ref:
                stmt = stmt.where(BotConnection.connection_ref == connection_ref)
            row = session.exec(stmt.order_by(BotConnection.is_default.desc())).first()
        if row:
            return {
                "state": row.state,
                "message": "微信机器人已连接" if row.state == "connected" else (row.last_error_code or "连接不可用"),
                "connected": row.state == "connected",
                "account_id": row.provider_account_id,
                "last_seen_at": row.last_seen_at,
            }
        return {"state": "disconnected", "message": "尚未连接微信", "connected": False}

    def logout(self, config_id: int, connection_ref: str = "") -> Dict[str, Any]:
        with self._lock:
            if connection_ref:
                self._attempts.pop(connection_ref, None)
        with Session(engine) as session:
            stmt = select(BotConnection).where(
                BotConnection.channel == "wechat", BotConnection.ai_config_id == config_id
            )
            if connection_ref:
                stmt = stmt.where(BotConnection.connection_ref == connection_ref)
            row = session.exec(stmt.order_by(BotConnection.is_default.desc())).first()
            if row:
                envelope = decrypt_credentials(row.credentials_encrypted) if row.credentials_encrypted else {}
                envelope.pop("bot_token", None)
                row.credentials_encrypted = encrypt_credentials(envelope) if envelope else ""
                row.state = "disconnected"
                row.provider_account_id = ""
                row.owner_external_id = ""
                session.add(row)
                session.commit()
        return {"state": "disconnected", "message": "已断开微信机器人", "connected": False}

    def _set_attempt(self, attempt: LoginAttempt, state: str, message: str) -> None:
        with self._lock:
            current = self._attempts.get(attempt.connection_ref or attempt.config_id)
            if current is attempt:
                current.state = state
                current.message = message

    def _is_current(self, attempt: LoginAttempt) -> bool:
        with self._lock:
            return self._attempts.get(attempt.connection_ref or attempt.config_id) is attempt

    @staticmethod
    def _has_connection(config_id: int, connection_ref: str) -> bool:
        with Session(engine) as session:
            row = session.exec(select(BotConnection).where(
                BotConnection.channel == "wechat",
                BotConnection.ai_config_id == config_id,
                BotConnection.connection_ref == connection_ref,
                BotConnection.state == "connected",
            )).first()
        return row is not None

    def _save_connection(self, attempt: LoginAttempt, response: Dict[str, Any]) -> None:
        account_id = str(response.get("ilink_bot_id") or "").strip()
        token = str(response.get("bot_token") or "").strip()
        if not account_id or not token:
            raise ValueError("扫码成功，但 iLink 未返回完整连接凭据")
        base_url = _safe_base_url(str(response.get("baseurl") or LOGIN_BASE_URL))
        now = time.time()
        with Session(engine) as session:
            row = session.exec(select(BotConnection).where(
                BotConnection.channel == "wechat",
                BotConnection.ai_config_id == attempt.config_id,
                BotConnection.connection_ref == attempt.connection_ref,
            )).first()
            if row is None:
                row = ensure_connection(
                    session,
                    user_id=attempt.user_id,
                    ai_config_id=attempt.config_id,
                    channel="wechat",
                    name="微信机器人",
                    connection_ref=attempt.connection_ref,
                )
            row.provider = "tencent_ilink"
            row.provider_account_id = account_id
            row.owner_external_id = str(response.get("ilink_user_id") or "")
            row.base_url = base_url
            envelope = decrypt_credentials(row.credentials_encrypted) if row.credentials_encrypted else {}
            envelope["bot_token"] = token
            row.credentials_encrypted = encrypt_credentials(envelope)
            row.state = "connected"
            row.last_error_code = ""
            row.last_seen_at = now
            row.updated_at = now
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("该微信机器人已绑定到另一个 AI") from exc

    def _apply_poll_response(
        self,
        attempt: LoginAttempt,
        response: Dict[str, Any],
        client: ILinkClient,
    ) -> tuple[bool, ILinkClient]:
        state = str(response.get("status") or "wait")
        if state == "wait":
            return False, client
        if state == "scaned":
            attempt.verify_code = ""
            self._set_attempt(attempt, "scanned", "已扫码，请在微信中确认")
            return False, client
        if state == "need_verifycode":
            self._set_attempt(attempt, "need_verifycode", "请输入微信中显示的数字验证码")
            return False, client
        if state == "verify_code_blocked":
            self._set_attempt(attempt, "failed", "验证码错误次数过多，请重新生成二维码")
            return True, client
        if state == "scaned_but_redirect":
            redirected = ILinkClient(base_url=f"https://{response.get('redirect_host') or ''}")
            self._set_attempt(attempt, "scanned", "已扫码，正在连接微信服务")
            return False, redirected
        if state == "binded_redirect":
            connected = self._has_connection(attempt.config_id, attempt.connection_ref)
            message = "该微信机器人已经连接" if connected else "该微信机器人已绑定，请先从原 AI 断开"
            self._set_attempt(attempt, "connected" if connected else "failed", message)
            return True, client
        if state == "confirmed":
            self._save_connection(attempt, response)
            self._set_attempt(attempt, "connected", "微信机器人连接成功")
            return True, client
        if state == "expired":
            self._set_attempt(attempt, "expired", "二维码已过期，请重新生成")
            return True, client
        return False, client

    def _poll(self, attempt: LoginAttempt) -> None:
        client = ILinkClient()
        while time.time() < attempt.expires_at and self._is_current(attempt):
            try:
                response = client.poll_qr(attempt.qrcode, attempt.verify_code)
                if not self._is_current(attempt):
                    return
                done, client = self._apply_poll_response(attempt, response, client)
                if done:
                    return
            except Exception as exc:
                self._set_attempt(attempt, "failed", f"微信连接失败：{type(exc).__name__}")
                return
        self._set_attempt(attempt, "expired", "二维码已过期，请重新生成")


manager = WeChatLoginManager()
