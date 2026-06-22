"""HTTP helpers for outbound AI provider requests."""

import threading
from typing import Any

import requests


_local = threading.local()


def ai_http_session() -> requests.Session:
    """Return a per-thread session that ignores process proxy variables."""
    session = getattr(_local, "ai_session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _local.ai_session = session
    return session


def ai_http_post(url: str, **kwargs: Any) -> requests.Response:
    return ai_http_session().post(url, **kwargs)
