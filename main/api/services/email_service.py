"""SMTP mail sending + email verification codes.

Codes are 6-digit, single-use, expire after ``CODE_TTL_SECONDS`` and allow
at most ``MAX_ATTEMPTS`` mismatches before being burned. Re-sending is
rate-limited per email+purpose. All SMTP I/O is blocking (smtplib) — call
these helpers from sync (``def``) FastAPI endpoints so they run in the
thread pool.
"""

from __future__ import annotations

import logging
import secrets
import smtplib
import ssl
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from sqlmodel import Session, select

from api.models import EmailVerificationCode
from api.services import auth_settings


logger = logging.getLogger(__name__)

CODE_TTL_SECONDS = 10 * 60
RESEND_INTERVAL_SECONDS = 60
MAX_ATTEMPTS = 5
PURPOSES = ("register", "login")

SENDER_NAME = "HeySure"


class EmailSendError(Exception):
    """SMTP delivery failed; message is safe to surface to the client."""


def send_email(session: Session, to_addr: str, subject: str, body: str) -> None:
    cfg = auth_settings.get_smtp_config(session)
    if not cfg["host"] or not cfg["from_addr"]:
        raise EmailSendError("邮件服务未配置，请联系管理员")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(SENDER_NAME, "utf-8")), cfg["from_addr"]))
    msg["To"] = to_addr

    try:
        if cfg["encryption"] == "ssl":
            client = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
        try:
            if cfg["encryption"] == "starttls":
                client.starttls(context=ssl.create_default_context())
            if cfg["username"] and cfg["password"]:
                client.login(cfg["username"], cfg["password"])
            client.sendmail(cfg["from_addr"], [to_addr], msg.as_string())
        finally:
            client.quit()
    except EmailSendError:
        raise
    except Exception as exc:
        logger.exception(f"SMTP send to {to_addr} failed")
        raise EmailSendError(f"邮件发送失败：{exc}") from exc


def issue_code(session: Session, email: str, purpose: str) -> None:
    """Mint + email a verification code; raises on rate limit / SMTP failure."""
    if purpose not in PURPOSES:
        raise ValueError(f"invalid purpose: {purpose}")
    email = auth_settings.normalize_email(email)
    now = time.time()

    latest = session.exec(
        select(EmailVerificationCode)
        .where(EmailVerificationCode.email == email, EmailVerificationCode.purpose == purpose)
        .order_by(EmailVerificationCode.created_at.desc())
    ).first()
    if latest and now - latest.created_at < RESEND_INTERVAL_SECONDS:
        wait = int(RESEND_INTERVAL_SECONDS - (now - latest.created_at)) + 1
        raise EmailSendError(f"发送过于频繁，请 {wait} 秒后再试")

    # 同邮箱同用途的旧验证码一律作废，保证任意时刻只有一个有效码。
    stale = session.exec(
        select(EmailVerificationCode).where(
            EmailVerificationCode.email == email,
            EmailVerificationCode.purpose == purpose,
            EmailVerificationCode.used == False,  # noqa: E712
        )
    ).all()
    for row in stale:
        row.used = True
        session.add(row)

    code = f"{secrets.randbelow(1_000_000):06d}"
    record = EmailVerificationCode(
        email=email,
        code=code,
        purpose=purpose,
        expires_at=now + CODE_TTL_SECONDS,
    )
    session.add(record)
    session.commit()

    action = "注册" if purpose == "register" else "登录"
    try:
        send_email(
            session,
            email,
            f"HeySure {action}验证码",
            f"你的{action}验证码是：{code}\n\n"
            f"验证码 {CODE_TTL_SECONDS // 60} 分钟内有效，请勿泄露给他人。\n"
            "如果这不是你本人的操作，请忽略本邮件。\n\n"
            "—— HeySure · 数字社会操作系统",
        )
    except EmailSendError:
        # 投递失败的验证码立即作废，同时不占用重发冷却窗口。
        record.used = True
        record.created_at = 0.0
        session.add(record)
        session.commit()
        raise


def verify_code(session: Session, email: str, code: str, purpose: str) -> bool:
    """Check + consume a verification code. Mismatches burn an attempt."""
    email = auth_settings.normalize_email(email)
    code = (code or "").strip()
    if not code:
        return False
    now = time.time()

    row = session.exec(
        select(EmailVerificationCode)
        .where(
            EmailVerificationCode.email == email,
            EmailVerificationCode.purpose == purpose,
            EmailVerificationCode.used == False,  # noqa: E712
        )
        .order_by(EmailVerificationCode.created_at.desc())
    ).first()
    if row is None or row.expires_at < now or row.attempts >= MAX_ATTEMPTS:
        return False

    if not secrets.compare_digest(row.code, code):
        row.attempts += 1
        session.add(row)
        session.commit()
        return False

    row.used = True
    session.add(row)
    session.commit()
    return True
