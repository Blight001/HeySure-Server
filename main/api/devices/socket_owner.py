"""Endpoint Socket.IO lifecycle ownership helpers.

Only the process that owns endpoint-agent sockets may reset the shared
``DevicePresence`` snapshot on startup. In the split deployment that process
is connector-runtime; in the legacy monolith it is api-gateway.
"""

from __future__ import annotations


def should_reset_endpoint_presence(
    service_role: str,
    connector_runtime_url: str,
) -> bool:
    """Return whether this process owns endpoint socket lifecycle state."""

    role = str(service_role or "").strip().lower()
    if role == "connector":
        return True
    if role == "gateway":
        # An external connector owns agent sockets in split deployments. A
        # gateway with no connector URL is the legacy monolith/socket owner.
        return not bool(str(connector_runtime_url or "").strip())
    return False


def endpoint_dispatch_url(
    api_gateway_url: str,
    connector_runtime_url: str,
) -> str:
    """Return the HTTP runtime that owns endpoint-agent Socket.IO sessions."""

    connector = str(connector_runtime_url or "").strip().rstrip("/")
    if connector:
        return connector
    return str(api_gateway_url or "").strip().rstrip("/")
