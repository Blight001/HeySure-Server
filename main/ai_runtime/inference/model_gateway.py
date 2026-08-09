"""Provider-specific model request construction and stream dispatch."""

from dataclasses import dataclass
from typing import Dict, List

import requests

from api.chat_runtime.chat_stream import (
    stream_turn_anthropic,
    stream_turn_openai_compat,
)
from api.runtime.http_client import ai_http_post


@dataclass(frozen=True)
class ModelTurnRequest:
    run_id: str
    provider: str
    base_url: str
    api_key: str
    model: str
    conversation: List[Dict]
    provider_tools: List[Dict]
    native_name_map: Dict[str, str]
    headers: Dict[str, str]


def run_model_turn(request: ModelTurnRequest):
    if request.provider == "anthropic":
        return stream_turn_anthropic(
            run_id=request.run_id,
            base_url=request.base_url,
            api_key=request.api_key,
            model=request.model,
            convo=request.conversation,
            step_tools=request.provider_tools,
            native_tool_name_map=request.native_name_map,
        )
    payload = _openai_payload(request)
    response = _post_openai(request, payload)
    if not response.ok and _parallel_tools_unsupported(response, payload):
        payload.pop("parallel_tool_calls", None)
        response.close()
        response = _post_openai(request, payload)
    raise_for_upstream_error(response)
    return stream_turn_openai_compat(
        run_id=request.run_id,
        response=response,
        native_tool_name_map=request.native_name_map,
    )


def _openai_payload(request) -> dict:
    payload = {
        "model": request.model,
        "messages": request.conversation,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.provider_tools:
        payload.update({
            "tools": request.provider_tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        })
    return payload


def _post_openai(request, payload):
    return ai_http_post(
        request.base_url,
        headers=request.headers,
        json=payload,
        timeout=300,
        stream=True,
    )


def _parallel_tools_unsupported(response, payload) -> bool:
    if "parallel_tool_calls" not in payload:
        return False
    hint = str(response.text or "").lower()
    return "parallel_tool_calls" in hint and any(
        marker in hint for marker in ("unsupported", "unknown", "invalid", "extra")
    )


def format_upstream_error(
    response: requests.Response,
    max_body_len: int = 4000,
) -> str:
    status = f"HTTP {response.status_code}"
    reason = str(response.reason or "").strip()
    if reason:
        status = f"{status} {reason}"
    body = _response_error_body(response)
    if len(body) > max_body_len:
        body = f"{body[:max_body_len]}\n...<truncated>"
    return f"Upstream AI request failed: {status} for {response.url}\n{body}".strip()


def _response_error_body(response) -> str:
    body = str(response.text or "").strip()
    if not body:
        return ""
    try:
        parsed = response.json()
    except Exception:
        return body
    if not isinstance(parsed, dict):
        return body
    error = parsed.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    if not isinstance(error, dict):
        return body
    parts = [
        str(error.get(key) or "").strip()
        for key in ("message", "code", "type")
    ]
    return " | ".join(part for part in parts if part) or body


def raise_for_upstream_error(response: requests.Response) -> None:
    if not response.ok:
        raise RuntimeError(format_upstream_error(response))
