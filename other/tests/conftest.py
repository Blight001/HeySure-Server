"""Deterministic test-process configuration.

Unit tests must not inherit a developer's production database URL. Tests that
need PostgreSQL are marked ``integration`` and receive their URL from CI.
"""

import os


os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://heysure_test:heysure_test@127.0.0.1:55432/heysure_test",
)
os.environ.setdefault("HEYSURE_DB_AUTO_MIGRATE", "0")
os.environ.setdefault("HEYSURE_DB_CONNECT_TIMEOUT_SECONDS", "1")
os.environ.setdefault("HEYSURE_INTERNAL_TOKEN", "test-internal-token-at-least-thirty-two-characters")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-thirty-two-characters")
