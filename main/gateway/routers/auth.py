"""``/api/auth`` routes: user registration, login (JWT issuance), profile update,
and ``/me``; also exposes ``get_current_user`` used as auth dependency elsewhere."""

import logging
import os
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Header, Request
from sqlmodel import Session, select

from api.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    decode_access_token,
    get_password_hash,
    verify_password,
)
from api.core.config import (
    WORKSPACE_DIR,
    user_workspace_dir,
)
from api.database import get_session
from api.models import Token, User, UserCreate, UserLogin, UserRead, UserUpdate
from api.models.defaults import DEFAULT_MCP_NAMESPACE_HINTS
from ai_runtime.inference.ai_service import ensure_default_ai_for_user
from api.services.access import auth_settings, ice_settings
from api.services import email_service
from api.services.model_presets import model_presets_json
from api.core.settings import settings
from pydantic import BaseModel


logger = logging.getLogger(__name__)

router = APIRouter()
PREFIX = "/api/auth"


def _user_payload(user: User) -> dict:
    """构造用户响应：数据库列 + 已迁出到 KnowledgeBase/system 的系统提示词文本。

    用 model_dump 合并文件值，避免在 ORM 实例上设置瞬态属性（更稳健）。"""
    from api.services.knowledge import kb_store

    data = {
        "id": getattr(user, "id", None),
        "name": getattr(user, "name", ""),
        "account": getattr(user, "account", ""),
        "avatar": getattr(user, "avatar", None),
        "email": getattr(user, "email", None),
        "role": getattr(user, "role", "member"),
        "admin_api_key": getattr(user, "admin_api_key", ""),
        "admin_base_url": getattr(user, "admin_base_url", ""),
        "admin_model": getattr(user, "admin_model", ""),
        "mcp_max_steps": getattr(user, "mcp_max_steps", 0),
        "mcp_history_result_max_chars": getattr(user, "mcp_history_result_max_chars", 8000),
        "conversation_auto_compress_enabled": getattr(user, "conversation_auto_compress_enabled", True),
        "role_mcp_permissions": getattr(user, "role_mcp_permissions", ""),
        "tavily_api_key": getattr(user, "tavily_api_key", ""),
        "model_presets": getattr(user, "model_presets", ""),
        "default_supervision_idle_seconds": getattr(user, "default_supervision_idle_seconds", 0),
        "ai_message_inquiry_reminder_seconds": getattr(user, "ai_message_inquiry_reminder_seconds", 0),
        "ui_theme_mode": getattr(user, "ui_theme_mode", ""),
        "ui_font_size": getattr(user, "ui_font_size", ""),
        "ui_brain_view_mode": getattr(user, "ui_brain_view_mode", ""),
    }
    try:
        data.update(kb_store.user_prompt_dict(user))
    except Exception:
        pass
    return data


def _normalize_public_url(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    return value.rstrip("/")


def _first_forwarded_value(request: Request, header: str) -> str:
    return str(request.headers.get(header) or "").split(",", 1)[0].strip()


def _forwarded_public_url(request: Request) -> str:
    host = _first_forwarded_value(request, "x-forwarded-host")
    if not host:
        return ""
    # Reject path/control-character injection while still allowing host:port
    # and bracketed IPv6 literals.
    if any(char.isspace() or char in "/\\" for char in host):
        return ""
    scheme = _first_forwarded_value(request, "x-forwarded-proto").lower()
    if scheme not in {"http", "https"}:
        scheme = request.url.scheme if request.url.scheme in {"http", "https"} else "http"
    return f"{scheme}://{host}"


def _agent_socket_url(request: Request) -> str:
    configured = _normalize_public_url(settings.agent_socket_url)
    if configured:
        return configured
    public_base = _normalize_public_url(settings.public_base_url)
    if public_base:
        return public_base
    forwarded = _forwarded_public_url(request)
    if forwarded:
        return forwarded
    return str(request.base_url).rstrip("/")

def ensure_user_workspace(user_id: int) -> None:
    """Ensure the per-user workspace root exists.

    Per-AI working directories and KnowledgeBase subfolders are created lazily
    when something actually writes files into them.
    """
    user_dir = user_workspace_dir(user_id)
    try:
        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        if not os.path.exists(user_dir):
            os.makedirs(user_dir)
            logger.info(f"Created user directory: {user_dir}")
    except Exception as exc:
        logger.exception(f"Error ensuring user directories for user {user_id}: {exc}")

def get_current_user_from_token(token: str = Depends(lambda x: x), session: Session = Depends(get_session)):
    # This dependency can be used to get user from Bearer token
    # FastAPI OAuth2PasswordBearer would be better but keeping it simple for now
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return get_current_user(token, session)

def get_current_user(token: Optional[str], session: Session = Depends(get_session)):
    try:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authentication token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if token.startswith("Bearer "):
            token = token.split(" ")[1]
        payload = decode_access_token(token)
        if payload is None:
             raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        username: str = payload.get("sub")
        if username is None:
             raise HTTPException(status_code=401, detail="Invalid token")
             
        statement = select(User).where(User.account == username)
        user = session.exec(statement).first()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user
    except HTTPException:
         raise
    except Exception as e:
         logger.exception(f"Error in get_current_user: {e}")
         raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )

class SendCodePayload(BaseModel):
    email: str
    purpose: str  # register | login


class EmailLoginPayload(BaseModel):
    email: str
    code: str


@router.get("/config")
def auth_config(session: Session = Depends(get_session)) -> dict:
    """Public auth capabilities used by the login modal before sign-in."""
    email_enabled = auth_settings.smtp_configured(session)
    return {
        "registration_mode": auth_settings.get_registration_mode(session),
        "email_enabled": email_enabled,
    }


@router.post("/send-code")
def send_code(payload: SendCodePayload, session: Session = Depends(get_session)) -> dict:
    """发送邮箱验证码（注册 / 登录）。同步 def：smtplib 阻塞 I/O 走线程池。"""
    purpose = (payload.purpose or "").strip().lower()
    if purpose not in email_service.PURPOSES:
        raise HTTPException(status_code=400, detail="无效的验证码用途")
    email = auth_settings.normalize_email(payload.email)
    if not auth_settings.is_valid_email(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if not auth_settings.smtp_configured(session):
        raise HTTPException(status_code=503, detail="邮件服务未配置，请联系管理员")

    existing = session.exec(select(User).where(User.email == email)).first()
    if purpose == "register":
        if auth_settings.get_registration_mode(session) == "closed":
            raise HTTPException(status_code=403, detail="当前服务器已关闭注册")
        if existing:
            raise HTTPException(status_code=400, detail="该邮箱已被注册")
    else:  # login
        # 不暴露邮箱是否注册：未注册也返回成功但不发信，防止枚举。
        if not existing:
            logger.info(f"login code requested for unknown email {email}; skipped")
            return {"ok": True}

    try:
        email_service.issue_code(session, email, purpose)
    except email_service.EmailSendError as exc:
        raise HTTPException(status_code=429 if "频繁" in str(exc) else 502, detail=str(exc))
    return {"ok": True}


@router.post("/register", response_model=UserRead)
def register(
    user_create: UserCreate,
    session: Session = Depends(get_session)
):
    registration_mode = auth_settings.get_registration_mode(session)
    if registration_mode == "closed":
        raise HTTPException(status_code=403, detail="当前服务器已关闭注册，请联系管理员")

    statement = select(User).where(User.account == user_create.account)
    existing_user = session.exec(statement).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Account already registered")

    email: Optional[str] = None
    if registration_mode == "email":
        email = auth_settings.normalize_email(user_create.email or "")
        if not auth_settings.is_valid_email(email):
            raise HTTPException(status_code=400, detail="请填写有效的邮箱")
        if session.exec(select(User).where(User.email == email)).first():
            raise HTTPException(status_code=400, detail="该邮箱已被注册")
        if not email_service.verify_code(session, email, user_create.email_code or "", "register"):
            raise HTTPException(status_code=400, detail="验证码错误或已过期")

    hashed_password = get_password_hash(user_create.password)
    # The very first account on a fresh install becomes the 房主 (owner) so
    # there is always someone who can reach the admin panel; everyone after
    # defaults to 成员 (member) until promoted.
    owner_exists = session.exec(select(User).where(User.role == "owner")).first()
    db_user = User(
        name=user_create.name,
        account=user_create.account,
        hashed_password=hashed_password,
        avatar=user_create.avatar,
        email=email,
        role="owner" if not owner_exists else "member",
    )
    session.add(db_user)
    session.commit()
    session.refresh(db_user)

    # 自动在 workspace 目录中创建对应用户的数据库 ID 目录名。
    ensure_user_workspace(db_user.id)
    ensure_default_ai_for_user(session, db_user.id)
    return _user_payload(db_user)

@router.post("/login", response_model=Token)
def login(user_in: UserLogin, request: Request, session: Session = Depends(get_session)):
    statement = select(User).where(User.account == user_in.account)
    user = session.exec(statement).first()
    if not user or not verify_password(user_in.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 登录时检查并确保用户的 workspace 目录存在
    ensure_user_workspace(user.id)
    ensure_default_ai_for_user(session, user.id)
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.account, "user_id": user.id}, expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": _user_payload(user),
        "agent_socket_url": _agent_socket_url(request),
        "ice_servers": ice_settings.build_ice_servers(session),
    }


@router.post("/login-email", response_model=Token)
def login_with_email(payload: EmailLoginPayload, request: Request, session: Session = Depends(get_session)):
    """邮箱验证码登录：验证一次性验证码后为绑定该邮箱的用户签发 JWT。"""
    email = auth_settings.normalize_email(payload.email)
    if not auth_settings.is_valid_email(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    user = session.exec(select(User).where(User.email == email)).first()
    # 先核销验证码再判断用户，保证两种失败对外不可区分。
    code_ok = email_service.verify_code(session, email, payload.code, "login")
    if not code_ok or user is None:
        raise HTTPException(status_code=401, detail="验证码错误或已过期")

    ensure_user_workspace(user.id)
    ensure_default_ai_for_user(session, user.id)

    access_token = create_access_token(
        data={"sub": user.account, "user_id": user.id},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": _user_payload(user),
        "agent_socket_url": _agent_socket_url(request),
        "ice_servers": ice_settings.build_ice_servers(session),
    }


@router.get("/agent-endpoint")
def agent_endpoint(
    request: Request,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    get_current_user(authorization, session)
    return {
        "agent_socket_url": _agent_socket_url(request),
        "ice_servers": ice_settings.build_ice_servers(session),
    }


@router.put("/profile", response_model=UserRead)
def update_profile(
    user_update: UserUpdate,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session)
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    user = get_current_user(authorization, session)
    
    update_data = user_update.model_dump(exclude_unset=True)
    if "ui_theme_mode" in update_data:
        raw_theme = str(update_data.get("ui_theme_mode") or "").lower()
        update_data["ui_theme_mode"] = "light" if raw_theme == "light" else "dark"
    if "ui_font_size" in update_data:
        raw_font = str(update_data.get("ui_font_size") or "").lower()
        update_data["ui_font_size"] = raw_font if raw_font in {"sm", "md", "lg"} else "md"
    if "ui_brain_view_mode" in update_data:
        raw_mode = str(update_data.get("ui_brain_view_mode") or "").lower()
        update_data["ui_brain_view_mode"] = raw_mode if raw_mode in {"sections", "all"} else "sections"
    if "mcp_max_steps" in update_data:
        try:
            update_data["mcp_max_steps"] = max(1, min(999, int(update_data.get("mcp_max_steps") or 48)))
        except Exception:
            update_data["mcp_max_steps"] = 48
    if "mcp_history_result_max_chars" in update_data:
        try:
            update_data["mcp_history_result_max_chars"] = max(
                20, min(10000, int(update_data.get("mcp_history_result_max_chars") or 8000))
            )
        except Exception:
            update_data["mcp_history_result_max_chars"] = 8000
    if "ai_message_inquiry_reminder_seconds" in update_data:
        try:
            update_data["ai_message_inquiry_reminder_seconds"] = max(
                0,
                min(3600, int(update_data.get("ai_message_inquiry_reminder_seconds") or 0)),
            )
        except Exception:
            update_data["ai_message_inquiry_reminder_seconds"] = 3
    if "model_presets" in update_data:
        update_data["model_presets"] = model_presets_json(update_data.get("model_presets"), user)
    if "mcp_call_method" in update_data:
        update_data["mcp_call_method"] = "\n".join(
            line for line in str(update_data.get("mcp_call_method") or "").splitlines()
            if "Call exactly one tool per <mcp-call> block; never join two tool names into one name." not in line
        ).strip()
    if "mcp_namespace_hints" in update_data:
        raw_hints = str(update_data.get("mcp_namespace_hints") or "").strip()
        if raw_hints:
            import json
            try:
                parsed = json.loads(raw_hints)
                if not isinstance(parsed, dict):
                    raise ValueError("mcp_namespace_hints must be a JSON object")
                update_data["mcp_namespace_hints"] = json.dumps(
                    {str(k).strip(): str(v).strip() for k, v in parsed.items() if str(k).strip() and str(v).strip()},
                    ensure_ascii=False,
                )
            except Exception:
                raise HTTPException(status_code=400, detail="mcp_namespace_hints must be a JSON object")
        else:
            update_data["mcp_namespace_hints"] = DEFAULT_MCP_NAMESPACE_HINTS
    
    # System prompts no longer belong to the User table. Keep them out of the
    # ORM update and persist them to KnowledgeBase/system after the DB commit.
    from api.services.knowledge import kb_store

    file_keys = {key for key, _kind in kb_store.SYSTEM_PROMPT_KEYS}
    prompt_updates = {
        key: update_data.pop(key)
        for key in tuple(update_data)
        if key in file_keys
    }

    if "password" in update_data:
        password = update_data.pop("password")
        if password:
            user.hashed_password = get_password_hash(password)
            
    for key, value in update_data.items():
        setattr(user, key, value)

    session.add(user)
    session.commit()
    session.refresh(user)
    # 文件为真相源：写入后回读确认，响应由 _user_payload 合并文件值。
    for key, value in prompt_updates.items():
        text = str(value or "")
        kb_store.write_system_prompt(user.id, key, text)
        if kb_store.read_system_prompt(user.id, key) != text.strip():
            raise HTTPException(status_code=500, detail=f"Failed to persist system prompt: {key}")
    return _user_payload(user)

@router.get("/me", response_model=UserRead)
def read_users_me(authorization: Optional[str] = Header(None), session: Session = Depends(get_session)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    return _user_payload(get_current_user(authorization, session))
