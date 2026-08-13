import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from socketio.exceptions import ConnectionRefusedError

from api import socket_events


class FakeSio:
    def __init__(self):
        self.handlers = {}
        self.sessions = {}
        self.enter_room = AsyncMock()

    def on(self, event):
        def decorator(handler):
            self.handlers[event] = handler
            return handler
        return decorator

    async def save_session(self, sid, data):
        self.sessions[sid] = data

    async def get_session(self, sid):
        if sid not in self.sessions:
            raise KeyError(sid)
        return self.sessions[sid]


def test_socket_rejects_missing_or_invalid_token(monkeypatch):
    fake = FakeSio()
    monkeypatch.setattr(socket_events, "sio", fake)
    monkeypatch.setattr(socket_events, "resolve_user_token", lambda _token: None)
    socket_events.register_user_socket_events()

    async def exercise():
        with pytest.raises(ConnectionRefusedError):
            await fake.handlers["connect"]("sid-1", {}, None)
        with pytest.raises(ConnectionRefusedError):
            await fake.handlers["connect"]("sid-2", {}, {"token": "invalid"})

    asyncio.run(exercise())


def test_socket_room_is_derived_from_token_not_client_user_id(monkeypatch):
    fake = FakeSio()
    emitted = AsyncMock()
    monkeypatch.setattr(socket_events, "sio", fake)
    monkeypatch.setattr(socket_events, "resolve_user_token", lambda token: (7, "alice") if token == "valid" else None)
    monkeypatch.setattr(socket_events, "decode_access_token", lambda _token: {"exp": time.time() + 60})
    monkeypatch.setattr(socket_events, "emit_agent_list_for_user", emitted)
    socket_events.register_user_socket_events()

    async def exercise():
        await fake.handlers["connect"]("sid-7", {}, {"token": "valid"})
        await fake.handlers["ui:join"]("sid-7", {"userId": 999})

    asyncio.run(exercise())

    fake.enter_room.assert_awaited_once_with("sid-7", "user_7")
    emitted.assert_awaited_once_with(7, to="sid-7")
