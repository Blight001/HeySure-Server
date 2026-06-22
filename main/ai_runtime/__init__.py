"""``ai-runtime`` process package — consumes queued ``ChatRun`` rows.

No HTTP port. Talks to api-gateway / mcp-runtime / connector-runtime over
``/internal/*`` HTTP and emits Socket.IO events via the gateway's relay.
Shared library code lives in ``api``.
"""
