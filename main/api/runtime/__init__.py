"""Cross-service runtime helpers shared by the split processes.

Submodules:
- ``internal_http`` — token-gated FastAPI dependency + httpx client wrapper.
- ``heartbeat``    — ChatRun heartbeat + watchdog reaper.

Per-process apps (gateway / ai_runtime / mcp_runtime / connector_runtime)
live in their own top-level packages at the ``server/`` root.
"""
