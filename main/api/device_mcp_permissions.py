"""Backward-compat shim — real implementation moved to api.devices.mcp_permissions."""
from api.devices.mcp_permissions import *  # noqa: F401, F403
from api.devices.mcp_permissions import (  # noqa: F401
    get_scope,
    set_scope,
    reconcile_scope_with_capabilities,
)
