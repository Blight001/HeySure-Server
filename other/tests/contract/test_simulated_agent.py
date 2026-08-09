import asyncio

from other.tests.fixtures.simulated_agent import (
    AgentBehavior,
    SimulatedAgent,
    SimulatedAgentConfig,
)


class FakeSocketClient:
    def __init__(self):
        self.handlers = {}
        self.emitted = []
        self.connected = None
        self.disconnected = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def connect(self, url, **kwargs):
        self.connected = (url, kwargs)

    async def emit(self, event, payload, **kwargs):
        self.emitted.append((event, payload, kwargs))

    async def disconnect(self):
        self.disconnected = True

    async def trigger(self, event, payload):
        await self.handlers[event](payload)


def _run(coro):
    return asyncio.run(coro)


def test_simulated_agent_registers_with_declared_capabilities():
    client = FakeSocketClient()
    agent = SimulatedAgent(
        SimulatedAgentConfig(capabilities=("browser_navigate", "browser_click")),
        client=client,
    )
    _run(agent.connect("http://connector:3002", token="jwt", user_id=7))

    event, payload, kwargs = client.emitted[0]
    assert event == "device:register"
    assert payload["token"] == "jwt"
    assert payload["userId"] == 7
    assert payload["capabilities"] == ["browser_navigate", "browser_click"]
    assert kwargs["namespace"] == "/"


def test_simulated_agent_can_duplicate_progress_and_result():
    client = FakeSocketClient()
    agent = SimulatedAgent(
        SimulatedAgentConfig(duplicate_progress=1, duplicate_result=1), client=client
    )
    _run(
        client.trigger(
            "task:dispatch",
            {"taskId": "task-1", "tool": "browser_navigate", "args": {"url": "https://example.test"}},
        )
    )

    events = [event for event, _payload, _kwargs in client.emitted]
    assert events == ["task:progress", "task:progress", "task:result", "task:result"]
    assert agent.seen_task_ids == ["task-1"]


def test_simulated_agent_supports_error_and_no_response():
    error_client = FakeSocketClient()
    error_agent = SimulatedAgent(
        SimulatedAgentConfig(behavior=AgentBehavior.ERROR), client=error_client
    )
    _run(error_client.trigger("task:dispatch", {"taskId": "error", "tool": "x"}))
    assert [item[0] for item in error_client.emitted] == ["task:progress", "task:error"]

    silent_client = FakeSocketClient()
    silent_agent = SimulatedAgent(
        SimulatedAgentConfig(behavior=AgentBehavior.NO_RESPONSE), client=silent_client
    )
    _run(silent_client.trigger("task:dispatch", {"taskId": "silent", "tool": "x"}))
    assert silent_client.emitted == []
    assert silent_agent.seen_task_ids == ["silent"]
