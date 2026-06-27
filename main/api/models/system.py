"""Platform-wide key/value settings + email verification codes.

``SystemSetting`` stores admin-configurable, server-scoped knobs (e.g. the
registration mode and SMTP mailer credentials) so they can be changed from
the admin console at runtime without editing env files. Read/write helpers
live in ``api.services.access.auth_settings``.

``EmailVerificationCode`` backs the email-code register/login flows; codes
are single-use, expire quickly and carry an attempt counter so they cannot
be brute-forced.
"""

import time
from typing import Optional

from sqlmodel import Field, SQLModel


class SystemSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = Field(default="")
    updated_at: float = Field(default_factory=time.time)


class EmailVerificationCode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    code: str
    # ``register`` | ``login`` — a code minted for one flow is not valid in the other.
    purpose: str = Field(index=True)
    created_at: float = Field(default_factory=time.time)
    expires_at: float
    attempts: int = Field(default=0)
    used: bool = Field(default=False)
