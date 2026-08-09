"""Behavior-driven Socket.IO endpoint agent for CI and fault exercises."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Optional

import socketio


class AgentBehavior(str, Enum):
    SUCCESS = "success"
    DELAYED_SUCCESS = "delayed_success"
    ERROR = "error"
    NO_RESPONSE = "no_response"


@dataclass
class SimulatedAgentConfig:
    device_id: str = "simulated-agent"
    name: str = "CI simulated agent"
    platform: str = "pytest-socketio"
    capabilities: Iterable[str] = field(default_factory=lambda: ("browser_navigate",))
    dynamic_tools: Iterable[Dict[str, Any]] = field(default_factory=tuple)
    behavior: AgentBehavior = AgentBehavior.SUCCESS
    delay_seconds: float = 0.05
    duplicate_progress: int = 0
    duplicate_result: int = 0


class SimulatedAgent:
    """Small real-protocol agent with an injectable client for unit tests."""

    def __init__(self, config: Optional[SimulatedAgentConfig] = None, *, client=None):
        self.config = config or SimulatedAgentConfig()
        self.client = client or socketio.AsyncClient(reconnection=True)
        self._registered_event: Optional[asyncio.Event] = None
        self._rejected_event: Optional[asyncio.Event] = None
        self.seen_task_ids = []
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.client.on("device:registered", self._on_registered)
        self.client.on("device:register_rejected", self._on_rejected)
        self.client.on("task:dispatch", self._on_dispatch)

    async def connect(
        self,
        url: str,
        *,
        token: str,
        user_id: Optional[int] = None,
        namespace: str = "/",
    ) -> None:
        await self.client.connect(url, namespaces=[namespace])
        await self.client.emit(
            "device:register",
            self.registration_payload(token=token, user_id=user_id),
            namespace=namespace,
        )

    def registration_payload(self, *, token: str, user_id: Optional[int] = None) -> Dict[str, Any]:
        payload = {
            "id": self.config.device_id,
            "name": self.config.name,
            "platform": self.config.platform,
            "version": "test",
            "token": token,
            "capabilities": list(self.config.capabilities),
            "dynamicTools": list(self.config.dynamic_tools),
        }
        if user_id is not None:
            payload["userId"] = user_id
        return payload

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def wait_until_registered(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._event("registered").wait(), timeout=timeout)

    def _event(self, name: str) -> asyncio.Event:
        attribute = f"_{name}_event"
        event = getattr(self, attribute)
        if event is None:
            event = asyncio.Event()
            setattr(self, attribute, event)
        return event

    async def _on_registered(self, _payload: Dict[str, Any]) -> None:
        self._event("registered").set()

    async def _on_rejected(self, _payload: Dict[str, Any]) -> None:
        self._event("rejected").set()

    async def _on_dispatch(self, payload: Dict[str, Any]) -> None:
        task_id = str(payload.get("taskId") or "")
        self.seen_task_ids.append(task_id)
        if self.config.behavior == AgentBehavior.NO_RESPONSE:
            return
        if self.config.behavior == AgentBehavior.DELAYED_SUCCESS:
            await asyncio.sleep(max(0, self.config.delay_seconds))
        for index in range(1 + max(0, self.config.duplicate_progress)):
            await self.client.emit(
                "task:progress",
                {
                    "taskId": task_id,
                    "deviceId": self.config.device_id,
                    "message": f"simulated progress {index + 1}",
                },
            )
        if self.config.behavior == AgentBehavior.ERROR:
            await self.client.emit(
                "task:error",
                {
                    "taskId": task_id,
                    "deviceId": self.config.device_id,
                    "tool": payload.get("tool"),
                    "error": "simulated agent failure",
                },
            )
            return
        result = {
            "taskId": task_id,
            "deviceId": self.config.device_id,
            "tool": payload.get("tool"),
            "success": True,
            "summary": "simulated success",
            "result": {"echo": payload.get("args") or {}},
        }
        for _ in range(1 + max(0, self.config.duplicate_result)):
            await self.client.emit("task:result", result)
