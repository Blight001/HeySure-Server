"""Backward-compat shim — real implementation moved to api.devices.presence."""
from api.devices.presence import *  # noqa: F401, F403
from api.devices.presence import (  # noqa: F401
    upsert_presence,
    set_offline,
    update_binding,
    mark_all_offline,
    online_devices_for_config,
    online_tool_names,
    online_workshop_agents_for_user,
    online_tool_defs,
    online_tool_defs_for_user,
    online_tool_catalog_for_user,
    tool_defs_for_agent,
)
