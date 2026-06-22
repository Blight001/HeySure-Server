"""AI-runtime owned inference modules.

- ``core``               — chat-run inference loop (formerly api/routers/chat_worker.py).
- ``ai_service``         — AI-config bootstrap and token-usage migration helpers.
- ``ai_message_service`` — AI-to-AI message routing and reply tracking.

External callers reach these through ``ai_runtime.inference.<module>`` directly;
this ``__init__`` keeps imports explicit (no package-level re-exports).
"""
