"""Path constants + legacy aliases for env-driven settings.

Env vars live in :mod:`api.core.settings` now. This module re-exports the
historical constant names (``DATABASE_URL`` / ``INTERNAL_TOKEN`` / …)
so existing callers keep working — new code should import ``settings``
from ``api.core.settings`` directly.
"""

import os
import re

from .settings import SERVER_DIR, SQLITE_FILE, settings  # noqa: F401 (本模块刻意 re-export 这些历史别名)


# ---------- Paths (no env, no migration needed) ----------
DATA_DIR = os.path.join(SERVER_DIR, "data")
WORKSPACE_DIR = os.path.join(DATA_DIR, "workspace")

# User-shared workspace subfolders are created lazily by the services that
# actually write files into them. ``KnowledgeBase`` remains the file-backed
# source of truth, but empty category folders are no longer seeded at login.
USER_SHARED_SUBFOLDERS = ()

# Backwards-compatible alias for older imports.
USER_WORKSPACE_SUBFOLDERS = USER_SHARED_SUBFOLDERS

def user_workspace_dir(user_id: int) -> str:
    return os.path.join(WORKSPACE_DIR, str(user_id))


def user_shared_knowledge_dir(user_id: int) -> str:
    """The user-level, AI-shared knowledge base root."""
    return os.path.join(user_workspace_dir(user_id), "KnowledgeBase")


def _ai_dir_slug(name: str) -> str:
    """Readable, filesystem-safe slug from an AI name (keeps CJK)."""
    raw = str(name or "").strip().lower()
    cleaned = re.sub(r"[^0-9a-z一-鿿]+", "-", raw).strip("-")
    if len(cleaned) > 40:
        cleaned = cleaned[:40].strip("-")
    return cleaned or "ai"


def ai_workspace_dirname(ai_config_id, name, ai_role) -> str:
    """Per-AI working directory name under the user workspace.

    Every AI, including assistant admins, gets a readable ``<id>-<slug>``
    directory so workspace folders are no longer opaque integers or role-based
    shared buckets.
    """
    slug = _ai_dir_slug(name)
    cid = int(ai_config_id or 0)
    return f"{cid}-{slug}" if cid else slug


# ---------- Legacy aliases (delegate to settings) ----------
DATABASE_URL = settings.database_url
INTERNAL_TOKEN = settings.internal_token
MCP_RUNTIME_URL = settings.mcp_runtime_url
CONNECTOR_RUNTIME_URL = settings.connector_runtime_url
JWT_SECRET_KEY = settings.jwt_secret
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 day
AGENT_TOKEN = settings.agent_token
DEFAULT_CHAT_MAX_STEPS = settings.chat_max_steps


def database_dialect() -> str:
    return settings.database_dialect


def psycopg_dsn() -> str:
    return settings.psycopg_dsn
