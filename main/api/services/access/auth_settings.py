"""Server-scoped auth settings: registration mode + SMTP mailer config.

Values live in the ``SystemSetting`` key/value table so the admin console
can change them at runtime. SMTP fields fall back to the ``HEYSURE_SMTP_*``
env settings when the DB value is empty, so headless deploys can still be
configured via env alone.
"""

from __future__ import annotations

import re
import time

from sqlmodel import Session

from api.core.settings import settings
from api.models import SystemSetting


REGISTRATION_MODE_KEY = "auth.registration_mode"
# open  — 账号 + 密码直接注册（历史行为）
# email — 注册必须提供邮箱并通过验证码验证
# closed — 关闭自助注册（仅管理员后台建号）
REGISTRATION_MODES = ("open", "email", "closed")

SMTP_KEYS = {
    "host": "email.smtp_host",
    "port": "email.smtp_port",
    "username": "email.smtp_username",
    "password": "email.smtp_password",
    "from_addr": "email.smtp_from",
    "encryption": "email.smtp_encryption",
}
SMTP_ENCRYPTIONS = ("ssl", "starttls", "none")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(SystemSetting, key)
    if row is None or not str(row.value).strip():
        return default
    return str(row.value).strip()


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key, value=value)
    else:
        row.value = value
        row.updated_at = time.time()
    session.add(row)


def get_registration_mode(session: Session) -> str:
    mode = get_setting(session, REGISTRATION_MODE_KEY, "open").lower()
    return mode if mode in REGISTRATION_MODES else "open"


def set_registration_mode(session: Session, mode: str) -> None:
    if mode not in REGISTRATION_MODES:
        raise ValueError(f"invalid registration mode: {mode}")
    set_setting(session, REGISTRATION_MODE_KEY, mode)


def get_smtp_config(session: Session) -> dict:
    """Effective SMTP config: DB value first, env (``HEYSURE_SMTP_*``) fallback."""
    raw_port = get_setting(session, SMTP_KEYS["port"], str(settings.smtp_port))
    try:
        port = max(1, min(65535, int(raw_port)))
    except (TypeError, ValueError):
        port = settings.smtp_port
    encryption = get_setting(session, SMTP_KEYS["encryption"], settings.smtp_encryption).lower()
    if encryption not in SMTP_ENCRYPTIONS:
        encryption = "ssl"
    username = get_setting(session, SMTP_KEYS["username"], settings.smtp_username)
    return {
        "host": get_setting(session, SMTP_KEYS["host"], settings.smtp_host),
        "port": port,
        "username": username,
        "password": get_setting(session, SMTP_KEYS["password"], settings.smtp_password),
        "from_addr": get_setting(session, SMTP_KEYS["from_addr"], settings.smtp_from) or username,
        "encryption": encryption,
    }


def smtp_configured(session: Session) -> bool:
    cfg = get_smtp_config(session)
    return bool(cfg["host"] and cfg["from_addr"])


def save_smtp_config(
    session: Session,
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    from_addr: str,
    encryption: str,
) -> None:
    """Persist SMTP settings. ``password=None`` keeps the stored password."""
    if encryption not in SMTP_ENCRYPTIONS:
        raise ValueError(f"invalid smtp encryption: {encryption}")
    set_setting(session, SMTP_KEYS["host"], host.strip())
    set_setting(session, SMTP_KEYS["port"], str(max(1, min(65535, int(port)))))
    set_setting(session, SMTP_KEYS["username"], username.strip())
    if password is not None:
        set_setting(session, SMTP_KEYS["password"], password)
    set_setting(session, SMTP_KEYS["from_addr"], from_addr.strip())
    set_setting(session, SMTP_KEYS["encryption"], encryption)
