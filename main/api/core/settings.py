"""Single source of truth for environment-driven configuration.

Every ``HEYSURE_*`` / ``DATABASE_URL`` / runtime-toggle env var the server
reads lives here. Other modules import :data:`settings` instead of
calling ``os.environ`` directly so:

- adding / renaming a knob is one diff;
- IDE jump-to-definition works;
- ``.env.example`` can be generated from field metadata;
- typos surface at startup, not three hours into a debug session.

The module is *also* re-exported from :mod:`api.core.config` under the
historical constant names (``DATABASE_URL`` / ``INTERNAL_TOKEN`` / …)
so existing call sites keep working until they're individually migrated.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


SERVER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
REPOSITORY_DIR = os.path.dirname(SERVER_DIR)
DATA_DIR = os.path.join(SERVER_DIR, "data")
# Retained only for the explicit one-shot SQLite-to-Postgres migration tool.
SQLITE_FILE = os.path.join(DATA_DIR, "heysure.db")


class Settings(BaseSettings):
    """All env-driven knobs.

    Fields without an ``HEYSURE_`` prefix in the env (``DATABASE_URL``,
    ``AGENT_TOKEN``, ``MCP_RUNTIME_URL``, ``CONNECTOR_RUNTIME_URL``,
    ``AI_DISPATCH_MODE``, ``TAVILY_API_KEY``, ``TAVILY_API_URL``) keep their historical names
    via ``alias=`` so the .env files we already ship don't need editing.
    """

    model_config = SettingsConfigDict(
        env_prefix="HEYSURE_",
        env_file=os.path.join(REPOSITORY_DIR, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ---- Identity / role -----------------------------------------------------

    service_role: Literal["gateway", "worker", "mcp", "connector"] = Field(
        default="gateway",
        description="Which process is running. ``api.sio`` flips between a real "
        "Socket.IO server and a remote proxy based on this value.",
    )

    # ---- Database ------------------------------------------------------------

    database_url: str = Field(
        ...,
        alias="DATABASE_URL",
        description="Required PostgreSQL SQLAlchemy URL.",
    )
    db_auto_migrate: bool = Field(
        default=True,
        alias="HEYSURE_DB_AUTO_MIGRATE",
        description="Run Alembic ``upgrade head`` on app startup. Set to false to "
        "decouple migration from startup (run ``python -m api.db migrate`` as a "
        "separate deploy step / init-container instead); the app then only checks "
        "that the schema is present.",
    )
    db_pool_size: int = Field(
        default=10,
        alias="HEYSURE_DB_POOL_SIZE",
        description="Base size of the SQLAlchemy connection pool per process. "
        "Gateway runs sync DB routes in a threadpool, so concurrent requests each "
        "need a connection; this must be large enough to avoid checkout waits. "
        "Total per process can reach db_pool_size + db_max_overflow; keep "
        "(processes × that sum) under the Postgres ``max_connections`` limit.",
    )
    db_max_overflow: int = Field(
        default=20,
        alias="HEYSURE_DB_MAX_OVERFLOW",
        description="Extra connections opened on demand above db_pool_size when the "
        "pool is saturated (closed again when idle).",
    )

    # ---- Service mesh --------------------------------------------------------

    internal_token: str = Field(
        default="",
        description="Bearer secret guarding /internal/* endpoints between split "
        "processes. Empty = monolith (loopback-only).",
    )
    api_gateway_url: str = Field(
        default="",
        description="HTTP base of api-gateway (used by ai-runtime to relay Socket.IO emits).",
    )
    mcp_runtime_url: str = Field(
        default="",
        alias="MCP_RUNTIME_URL",
        description="HTTP base of mcp-runtime; empty = in-process registry.",
    )
    connector_runtime_url: str = Field(
        default="",
        alias="CONNECTOR_RUNTIME_URL",
        description="HTTP base of connector-runtime; empty = in-process bots.",
    )
    ai_runtime_url: str = Field(
        default="",
        alias="AI_RUNTIME_URL",
        description="HTTP base of the ai-runtime worker's internal status server "
        "(health + console tail). Empty = admin panel skips the worker.",
    )
    connector_runtime_port: int = Field(default=3002)
    mcp_runtime_port: int = Field(default=3001)
    ai_runtime_port: int = Field(default=3003)

    # ---- Chat / AI runtime ---------------------------------------------------

    chat_max_steps: int = Field(
        default=48,
        description="Default ceiling on chat-worker tool-call iterations.",
    )
    ai_runtime_max_concurrent: int = Field(
        default=16,
        description="Concurrency cap per ai-runtime worker process.",
    )
    ai_dispatch_mode: Literal["local", "remote"] = Field(
        default="local",
        alias="AI_DISPATCH_MODE",
        description="``remote`` routes chat runs through the shared queue + ai-runtime.",
    )
    ai_debug: bool = Field(default=False, description="Verbose ai-runtime stdout.")
    ai_debug_color: bool = Field(default=True, description="ANSI color for ai-runtime logs.")

    # ---- Gateway -------------------------------------------------------------

    server_reload: bool = Field(
        default=False,
        description="Enable uvicorn --reload (only useful in dev).",
    )
    public_base_url: str = Field(
        default="",
        description="Public HTTP(S) base URL used when generating externally fetchable "
        "links, for example https://api.example.com. Empty = derive from request host.",
    )
    agent_socket_url: str = Field(
        default="",
        description="Public Socket.IO base URL endpoint agents should connect to. "
        "Empty = derive from the login/API request URL.",
    )
    temp_image_ttl_seconds: int = Field(
        default=60 * 60 * 24,
        description="How long temporary images remain available before cleanup.",
    )
    temp_image_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        description="Maximum size accepted by the temporary image upload endpoints.",
    )
    # ---- Auth / Socket.IO ----------------------------------------------------

    jwt_secret: str = Field(
        default="heysure-ai-secret-key-change-this-in-production",
        description="HS256 key. MUST be changed in production.",
    )
    agent_token: str = Field(
        default="",
        alias="AGENT_TOKEN",
        description="Static bearer agents present when registering on the Socket.IO ``/agent`` namespace.",
    )

    # ---- Email (SMTP) ----------------------------------------------------------
    # Env-level defaults for the mailer used by email-code register/login.
    # Values saved from the admin console (SystemSetting table) take
    # precedence; these only seed fresh installs / headless deploys.

    smtp_host: str = Field(default="", description="SMTP server host; empty disables email features.")
    smtp_port: int = Field(default=465, description="SMTP server port (465 for SSL, 587 for STARTTLS).")
    smtp_username: str = Field(default="", description="SMTP auth username (usually the mailbox).")
    smtp_password: str = Field(default="", description="SMTP auth password / app-specific token.")
    smtp_from: str = Field(default="", description="From address; empty falls back to smtp_username.")
    smtp_encryption: Literal["ssl", "starttls", "none"] = Field(
        default="ssl",
        description="Transport security used when talking to the SMTP server.",
    )

    # ---- Third-party ---------------------------------------------------------

    tavily_api_key: str = Field(
        default="",
        alias="TAVILY_API_KEY",
        description="Tavily web-search API key (used by the workspace.search MCP tool).",
    )
    tavily_api_url: str = Field(
        default="https://api.tavily.com/search",
        alias="TAVILY_API_URL",
        description="Direct Tavily-compatible search endpoint used by workspace.search.",
    )
    clawhub_registry_url: str = Field(
        default="https://clawhub.ai",
        alias="CLAWHUB_REGISTRY_URL",
        description="ClawHub registry/site base URL used for skill discovery and download.",
    )
    clawhub_api_token: str = Field(
        default="",
        alias="CLAWHUB_API_TOKEN",
        description="Optional ClawHub API token for private reads or authenticated verification calls.",
    )
    clawhub_use_env_proxy: bool = Field(
        default=False,
        alias="CLAWHUB_USE_ENV_PROXY",
        description="When true, ClawHub HTTP requests inherit HTTPS_PROXY/HTTP_PROXY from the process environment.",
    )

    # ---- Logging -------------------------------------------------------------

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Root log level for stdlib ``logging`` (see api/core/logging_config.py).",
    )
    log_json: bool = Field(
        default=False,
        description="Emit JSON-formatted logs (set true in containerized deploys).",
    )

    # -------------------------------------------------------------------------
    # Derived helpers
    # -------------------------------------------------------------------------

    @field_validator("database_url", mode="before")
    @classmethod
    def _strip_database_url(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
        if not isinstance(value, str) or not value:
            raise ValueError("DATABASE_URL is required and must point to PostgreSQL")
        if not value.lower().startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("DATABASE_URL must use PostgreSQL; SQLite is no longer supported")
        return value

    @field_validator(
        "internal_token",
        "api_gateway_url",
        "mcp_runtime_url",
        "connector_runtime_url",
        "ai_runtime_url",
        "agent_token",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from",
        "tavily_api_key",
        "tavily_api_url",
        "clawhub_registry_url",
        "clawhub_api_token",
        "public_base_url",
        "agent_socket_url",
        mode="before",
    )
    @classmethod
    def _strip_str(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @property
    def database_dialect(self) -> str:
        return "postgresql"

    @property
    def psycopg_dsn(self) -> str:
        """libpq-compatible Postgres URL (psycopg.connect does not understand
        SQLAlchemy's ``postgresql+psycopg://`` driver suffix)."""
        url = self.database_url
        if url.lower().startswith("postgresql+") or url.lower().startswith("postgres+"):
            return "postgresql://" + url.split("://", 1)[1]
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so we don't re-parse env on every import. Tests that need to
    override values should call ``get_settings.cache_clear()`` after
    mutating ``os.environ``.
    """
    return Settings()


# Single shared instance. Import this — don't construct ``Settings()`` ad-hoc.
settings = get_settings()
