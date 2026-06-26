"""Backward-compat shim — real implementation moved to api.devices.live."""
from api.devices.live import *  # noqa: F401, F403
from api.devices.live import (  # noqa: F401
    connected_agent_rows_for_user,
    emit_agent_list_for_user,
    device_tool_room,
    push_device_dynamic_tools,
    push_device_dynamic_tools_to_sid,
)
