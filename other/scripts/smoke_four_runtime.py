"""Four-runtime smoke: auth plus Connector-to-simulated-Agent round trip."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict

import requests

SERVER_ROOT = Path(__file__).resolve().parents[2]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from other.tests.fixtures.simulated_agent import (
    AgentBehavior,
    SimulatedAgent,
    SimulatedAgentConfig,
)


TERMINAL = {"completed", "error", "timeout", "cancelled"}
SIMULATED_DEVICE_ID = "ci-simulated-browser"


def _request(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", (3.0, 15.0))
    return requests.request(method, url, **kwargs)


def _wait_ready(url: str, headers: Dict[str, str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = "not attempted"
    while time.monotonic() < deadline:
        try:
            response = _request("GET", url, headers=headers)
            if response.status_code == 200:
                return
            last = f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    raise RuntimeError(f"readiness timeout for {url}: {last}")


def _login_or_register(gateway: str, account: str, password: str) -> Dict[str, Any]:
    credentials = {"account": account, "password": password}
    response = _request("POST", f"{gateway}/api/auth/login", json=credentials)
    if response.status_code == 401:
        registered = _request(
            "POST",
            f"{gateway}/api/auth/register",
            json={"name": "Runtime Smoke", **credentials},
        )
        registered.raise_for_status()
        response = _request("POST", f"{gateway}/api/auth/login", json=credentials)
    response.raise_for_status()
    return response.json()


async def _disconnect_quietly(agent: SimulatedAgent) -> None:
    try:
        await agent.disconnect()
    except Exception:
        pass


async def _connect_simulated_agent(
    config: SimulatedAgentConfig,
    connector: str,
    token: str,
    timeout: float,
) -> SimulatedAgent:
    agent = SimulatedAgent(config)
    try:
        await agent.connect(connector, token=token)
        await agent.wait_until_registered(timeout=min(timeout, 10.0))
        return agent
    except Exception:
        await _disconnect_quietly(agent)
        raise


async def _successful_dispatch(
    config: SimulatedAgentConfig,
    connector: str,
    token: str,
    internal_headers: Dict[str, str],
    user_id: int,
    ai_config_id: int,
    timeout: float,
) -> None:
    agent = await _connect_simulated_agent(config, connector, token, timeout)
    try:
        payload = await _dispatch_and_wait(
            connector, internal_headers, user_id, ai_config_id, timeout,
        )
        _assert_success(payload)
    finally:
        await _disconnect_quietly(agent)


async def _timeout_dispatch(
    config: SimulatedAgentConfig,
    connector: str,
    token: str,
    internal_headers: Dict[str, str],
    user_id: int,
    ai_config_id: int,
    timeout: float,
) -> None:
    silent_config = replace(config, behavior=AgentBehavior.NO_RESPONSE)
    agent = await _connect_simulated_agent(silent_config, connector, token, timeout)
    try:
        payload = await _dispatch_and_wait(
            connector, internal_headers, user_id, ai_config_id, timeout, expire=True,
        )
        if payload["status"] != "timeout" or payload["success"] is not False:
            raise RuntimeError(f"silent dispatch did not time out: {payload}")
    finally:
        await _disconnect_quietly(agent)


async def _agent_round_trip(
    gateway: str,
    connector: str,
    internal_headers: Dict[str, str],
    login: Dict[str, Any],
    timeout: float,
    fault_matrix: bool,
) -> None:
    token = str(login["access_token"])
    user_id = int(login["user"]["id"])
    auth = {"Authorization": f"Bearer {token}"}
    configs = _request("GET", f"{gateway}/api/ai/configs", headers=auth)
    configs.raise_for_status()
    ai_config_id = int(configs.json()[0]["id"])

    config = SimulatedAgentConfig(
        device_id=SIMULATED_DEVICE_ID,
        platform="browser-extension-ci",
        capabilities=("browser_navigate",),
    )
    first = await _connect_simulated_agent(config, connector, token, timeout)
    try:
        bound = await asyncio.to_thread(
            _request,
            "POST",
            f"{gateway}/api/devices/bind",
            headers=auth,
            json={"deviceId": config.device_id, "aiConfigId": ai_config_id},
        )
        bound.raise_for_status()
    finally:
        await _disconnect_quietly(first)

    # Binding is shared through PostgreSQL; reconnect proves the Connector
    # process independently observes it instead of relying on Gateway memory.
    await _successful_dispatch(
        config, connector, token, internal_headers, user_id, ai_config_id, timeout,
    )

    if not fault_matrix:
        return
    await _timeout_dispatch(
        config, connector, token, internal_headers, user_id, ai_config_id, timeout,
    )
    await _successful_dispatch(
        config, connector, token, internal_headers, user_id, ai_config_id, timeout,
    )


async def _dispatch_and_wait(
    connector: str,
    internal_headers: Dict[str, str],
    user_id: int,
    ai_config_id: int,
    timeout: float,
    *,
    expire: bool = False,
) -> Dict[str, Any]:
    dispatched = await asyncio.to_thread(
        _request,
        "POST",
        f"{connector}/internal/agent/dispatch",
        headers=internal_headers,
        json={
            "user_id": user_id,
            "ai_config_id": ai_config_id,
            "tool": "browser_navigate",
            "arguments": {"url": "https://example.test"},
            "timeout_seconds": 10,
        },
    )
    dispatched.raise_for_status()
    task_id = str(dispatched.json()["task_id"])
    if expire:
        expired = await asyncio.to_thread(
            _request,
            "POST",
            f"{connector}/internal/agent/dispatch/expire/{task_id}",
            headers=internal_headers,
            json={"reason": "fault exercise network timeout"},
        )
        expired.raise_for_status()
        if not expired.json().get("expired"):
            raise RuntimeError(f"dispatch {task_id} was not expired")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        outcome = await asyncio.to_thread(
            _request,
            "GET",
            f"{connector}/internal/agent/dispatch/result/{task_id}",
            headers=internal_headers,
        )
        outcome.raise_for_status()
        payload = outcome.json()
        if payload["status"] in TERMINAL:
            return payload
        await asyncio.sleep(0.25)
    raise RuntimeError(f"dispatch {task_id} did not reach a terminal state")


def _assert_success(payload: Dict[str, Any]) -> None:
    if payload["status"] != "completed" or not payload["success"]:
        raise RuntimeError(f"simulated dispatch failed: {payload}")
    if payload.get("result", {}).get("echo", {}).get("url") != "https://example.test":
        raise RuntimeError(f"unexpected dispatch result: {payload}")


def _cleanup_simulated_device(gateway: str, token: str, timeout: float) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + max(1.0, min(timeout, 10.0))
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            response = _request(
                "DELETE",
                f"{gateway}/api/devices/{SIMULATED_DEVICE_ID}",
                headers=auth,
            )
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"simulated device cleanup failed: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://127.0.0.1:3000")
    parser.add_argument("--connector", default="http://127.0.0.1:3002")
    parser.add_argument("--internal-token", required=True)
    parser.add_argument("--account", default="runtime-smoke")
    parser.add_argument("--password", default="runtime-smoke")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--fault-matrix", action="store_true")
    args = parser.parse_args()
    gateway = args.gateway.rstrip("/")
    connector = args.connector.rstrip("/")
    headers = {"Authorization": f"Bearer {args.internal_token}"}
    login = None
    failure = None
    try:
        _wait_ready(f"{gateway}/internal/health/ready", headers, args.timeout)
        _wait_ready(f"{connector}/internal/health/ready", headers, args.timeout)
        login = _login_or_register(gateway, args.account, args.password)
        asyncio.run(
            _agent_round_trip(
                gateway,
                connector,
                headers,
                login,
                args.timeout,
                args.fault_matrix,
            )
        )
    except Exception as exc:
        failure = exc
    finally:
        if login:
            try:
                _cleanup_simulated_device(
                    gateway, str(login["access_token"]), args.timeout,
                )
            except Exception as cleanup_exc:
                failure = RuntimeError(
                    f"{failure}; {cleanup_exc}" if failure else str(cleanup_exc)
                )
    if failure:
        print(f"four-runtime smoke failed: {failure}")
        return 1
    suffix = " + timeout/disconnect/reconnect" if args.fault_matrix else ""
    print(f"four-runtime smoke passed: login + simulated endpoint dispatch{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
