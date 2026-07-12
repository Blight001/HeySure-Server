"""One-shot inheritance MCP test: LLM picks tool args from schema text, then dispatches."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional

from api.runtime.http_client import ai_http_post
from api.models import User
from api.services.model_presets import normalize_model_presets
from connector_runtime.dispatch.device_dispatch import dispatch_task_to_agent


def _to_native_tool_name(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "__", str(name or "").strip())
    safe = safe.strip("_") or "tool"
    return safe[:64]


def _schema_type_from_label(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if "整数" in text or text == "integer":
        return "integer"
    if "数字" in text or text == "number":
        return "number"
    if "布尔" in text or text == "boolean":
        return "boolean"
    if "数组" in text or text == "array":
        return "array"
    if "对象" in text or text == "object":
        return "object"
    return "string"


def _build_tool_input_schema(
    *,
    input_schema: Optional[Dict[str, Any]],
    parameters: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    if isinstance(input_schema, dict) and isinstance(input_schema.get("properties"), dict):
        required = input_schema.get("required")
        return {
            "type": "object",
            "properties": input_schema.get("properties") or {},
            "required": required if isinstance(required, list) else [],
            "additionalProperties": False,
        }

    props: Dict[str, Any] = {}
    required: List[str] = []
    for row in parameters or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        props[name] = {
            "type": _schema_type_from_label(row.get("type")),
            "description": str(row.get("description") or "").strip() or name,
        }
        if row.get("required"):
            required.append(name)
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _format_param_lines(
    *,
    parameters: Optional[List[Dict[str, Any]]],
    input_schema: Optional[Dict[str, Any]],
) -> List[str]:
    schema = input_schema if isinstance(input_schema, dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    lines: List[str] = []
    rows = parameters if isinstance(parameters, list) else []
    if rows:
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            required = "必填" if row.get("required") else "可选"
            param_type = str(row.get("type") or "任意").strip() or "任意"
            desc = str(row.get("description") or "").strip()
            if not desc and isinstance(properties.get(name), dict):
                desc = str(properties[name].get("description") or "").strip()
            suffix = f"：{desc}" if desc else ""
            lines.append(f"- `{name}`（{required}，{param_type}）{suffix}")
        return lines

    for name, spec in sorted(properties.items()):
        if not isinstance(spec, dict):
            continue
        param_type = str(spec.get("type") or "any").strip() or "any"
        desc = str(spec.get("description") or "").strip()
        suffix = f"：{desc}" if desc else ""
        lines.append(f"- `{name}`（{param_type}）{suffix}")
    return lines


def _format_implementation(implementation: Optional[Dict[str, Any]]) -> str:
    if not isinstance(implementation, dict) or not implementation:
        return "（无底层实现说明）"
    kind = str(implementation.get("kind") or "").strip()
    if kind:
        return f"类型：{kind}"
    return json.dumps(implementation, ensure_ascii=False, indent=2)[:1200]


def _build_system_prompt(
    *,
    tool: str,
    device_id: str,
    device_type: str,
    description: str,
    parameters: Optional[List[Dict[str, Any]]],
    input_schema: Optional[Dict[str, Any]],
    implementation: Optional[Dict[str, Any]],
) -> str:
    param_lines = _format_param_lines(parameters=parameters, input_schema=input_schema)
    params_block = "\n".join(param_lines) if param_lines else "- 无参数，调用时传空对象 `{}` 即可。"
    return (
        "你是 MCP 工具测试助手。请阅读下方工具说明，构造合理、可执行的测试参数，"
        "并通过工具调用完成一次真实测试。\n\n"
        f"工具名称：`{tool}`\n"
        f"目标设备：{device_type or 'device'}（{device_id or '未知'}）\n"
        f"工具描述：{description or '（无描述）'}\n\n"
        "参数说明：\n"
        f"{params_block}\n\n"
        "底层实现：\n"
        f"{_format_implementation(implementation)}\n\n"
        "要求：\n"
        f"1. 必须调用工具 `{tool}`，不要只描述计划。\n"
        "2. 根据参数说明填写真实可运行的测试值；若无必填参数，可传 `{}`。\n"
        "3. 调用后请在回复中简要说明测试意图与参数选择理由。"
    )


def _resolve_preset(user: User, preset_id: str) -> Dict[str, str]:
    presets = normalize_model_presets(getattr(user, "model_presets", ""), user)
    selected = next((item for item in presets if item["id"] == preset_id), None)
    if selected is None and preset_id:
        selected = next(
            (item for item in presets if item["model"] == preset_id or item["name"] == preset_id),
            None,
        )
    if selected is None:
        if presets:
            selected = presets[0]
        else:
            model = str(getattr(user, "admin_model", "") or "").strip()
            api_key = str(getattr(user, "admin_api_key", "") or "").strip()
            base_url = str(getattr(user, "admin_base_url", "") or "").strip()
            if model and api_key and base_url:
                selected = {
                    "id": model,
                    "name": model,
                    "api_key": api_key,
                    "base_url": base_url,
                    "model": model,
                }
    if not selected:
        raise ValueError("未配置可用模型，请先在系统设置中添加模型预设")
    if str(selected.get("provider") or "").strip().lower() == "cli":
        raise ValueError("CLI 模型不支持 MCP 工具测试，请选择 API 类型的模型预设")
    if not str(selected.get("api_key") or "").strip():
        raise ValueError("所选模型缺少 API Key")
    if not str(selected.get("base_url") or "").strip():
        raise ValueError("所选模型缺少 Base URL")
    if not str(selected.get("model") or "").strip():
        raise ValueError("所选模型缺少 model 名称")
    return selected


def _response_error_hint(response) -> str:
    try:
        return str(response.text or "")[:600]
    except Exception:
        return ""


def _should_retry_without_tool_choice(status_code: int, error_hint: str) -> bool:
    if status_code != 400:
        return False
    lowered = error_hint.lower()
    return (
        "tool_choice" in lowered
        or "thinking mode" in lowered
        or "thinking_mode" in lowered
    )


def _should_retry_without_parallel_tool_calls(status_code: int, error_hint: str) -> bool:
    if status_code != 200:
        lowered = error_hint.lower()
        return "parallel_tool_calls" in lowered and (
            "unsupported" in lowered
            or "unknown" in lowered
            or "invalid" in lowered
            or "extra" in lowered
        )
    return False


def _build_tool_call_payload(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    native_name: str,
    tool_name: str,
    description: str,
    schema: Dict[str, Any],
    use_tools: bool,
    tool_choice: Optional[Any],
    parallel_tool_calls: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
    }
    if use_tools:
        payload["tools"] = [{
            "type": "function",
            "function": {
                "name": native_name,
                "description": description or f"Test endpoint MCP tool {tool_name}",
                "parameters": schema,
            },
        }]
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls:
            payload["parallel_tool_calls"] = False
    return payload


def _request_model_payload(
    *,
    base_url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
):
    return ai_http_post(base_url, headers=headers, json=payload, timeout=120, stream=False)


def _call_model_for_tool_args(
    *,
    base_url: str,
    headers: Dict[str, str],
    model: str,
    system_prompt: str,
    user_message: str,
    native_name: str,
    tool_name: str,
    description: str,
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    attempts: List[Dict[str, Any]] = [
        {"use_tools": True, "tool_choice": "auto", "parallel_tool_calls": True},
        {"use_tools": True, "tool_choice": None, "parallel_tool_calls": True},
        {"use_tools": True, "tool_choice": "auto", "parallel_tool_calls": False},
        {"use_tools": True, "tool_choice": None, "parallel_tool_calls": False},
    ]
    last_error = ""
    for attempt in attempts:
        payload = _build_tool_call_payload(
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            native_name=native_name,
            tool_name=tool_name,
            description=description,
            schema=schema,
            use_tools=attempt["use_tools"],
            tool_choice=attempt["tool_choice"],
            parallel_tool_calls=attempt["parallel_tool_calls"],
        )
        response = _request_model_payload(base_url=base_url, headers=headers, payload=payload)
        if response.status_code == 200:
            try:
                return response.json()
            except Exception as exc:
                raise ValueError(f"模型响应不是合法 JSON：{exc}") from exc

        error_hint = _response_error_hint(response)
        last_error = f"HTTP {response.status_code}：{error_hint}"
        if _should_retry_without_tool_choice(response.status_code, error_hint):
            continue
        if _should_retry_without_parallel_tool_calls(response.status_code, error_hint):
            continue
        break

    json_prompt = (
        f"{system_prompt}\n\n"
        "当前模型不支持原生工具调用。请只输出一个 JSON 对象，格式为：\n"
        '{"arguments": {...}}\n'
        "不要输出 markdown 代码块或其它说明文字。"
    )
    json_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": json_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
    }
    response = _request_model_payload(base_url=base_url, headers=headers, payload=json_payload)
    if response.status_code != 200:
        raise ValueError(f"模型请求失败 {last_error or _response_error_hint(response)}")
    try:
        return response.json()
    except Exception as exc:
        raise ValueError(f"模型响应不是合法 JSON：{exc}") from exc


def _parse_json_arguments(reply: str) -> Dict[str, Any]:
    text = str(reply or "").strip()
    if not text:
        return {}
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(text[start:end + 1])
        except Exception:
            return {}
    if not isinstance(parsed, dict):
        return {}
    args = parsed.get("arguments")
    if isinstance(args, dict):
        return args
    return parsed if all(not str(key).startswith("_") for key in parsed.keys()) else {}


def _parse_llm_tool_call(
    payload: Dict[str, Any],
    *,
    native_name: str,
    tool_name: str,
) -> tuple[str, Optional[str], Dict[str, Any]]:
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    reasoning = str(message.get("reasoning_content") or "").strip()
    reply = str(message.get("content") or "").strip()
    if reasoning and reply:
        combined_reply = f"{reasoning}\n\n{reply}"
    else:
        combined_reply = reasoning or reply
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    if not tool_calls:
        json_args = _parse_json_arguments(combined_reply)
        if json_args:
            return combined_reply, tool_name, json_args
        return combined_reply, None, {}
    first = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
    fn = first.get("function") if isinstance(first.get("function"), dict) else {}
    called_name = str(fn.get("name") or "").strip() or native_name
    if called_name != native_name:
        called_name = native_name
    raw_args = fn.get("arguments")
    if isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            args = json.loads(str(raw_args or "{}") or "{}")
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return combined_reply, tool_name, args


async def run_inheritance_mcp_test(
    *,
    user: User,
    model_preset_id: str,
    tool: str,
    device_id: str,
    device_type: str = "desktop",
    description: str = "",
    parameters: Optional[List[Dict[str, Any]]] = None,
    input_schema: Optional[Dict[str, Any]] = None,
    implementation: Optional[Dict[str, Any]] = None,
    user_hint: str = "",
) -> Dict[str, Any]:
    tool_name = str(tool or "").strip()
    target_device_id = str(device_id or "").strip()
    if not tool_name:
        raise ValueError("tool 不能为空")
    if not target_device_id:
        raise ValueError("device_id 不能为空")

    preset = _resolve_preset(user, str(model_preset_id or "").strip())
    api_key = preset["api_key"]
    base_url = preset["base_url"]
    model = preset["model"]
    native_name = _to_native_tool_name(tool_name)
    schema = _build_tool_input_schema(input_schema=input_schema, parameters=parameters)

    system_prompt = _build_system_prompt(
        tool=tool_name,
        device_id=target_device_id,
        device_type=device_type,
        description=description,
        parameters=parameters,
        input_schema=input_schema,
        implementation=implementation,
    )
    user_message = (
        f"请立即测试 MCP 工具 `{tool_name}`。"
        "根据上方参数说明主动构造测试参数并调用该工具。"
    )
    hint = str(user_hint or "").strip()
    if hint:
        user_message += f"\n\n补充说明：{hint}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = _call_model_for_tool_args(
        base_url=base_url,
        headers=headers,
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        native_name=native_name,
        tool_name=tool_name,
        description=description,
        schema=schema,
    )

    model_reply, called_tool, tool_args = _parse_llm_tool_call(
        payload,
        native_name=native_name,
        tool_name=tool_name,
    )
    if not called_tool:
        return {
            "ok": False,
            "model_preset": {
                "id": preset["id"],
                "name": preset["name"],
                "model": model,
            },
            "model_reply": model_reply,
            "tool_call": None,
            "tool_result": None,
            "detail": "模型未返回工具调用，请换用支持 function calling 的模型后重试",
        }

    session_id = f"inheritance_mcp_test_{uuid.uuid4().hex[:12]}"
    tool_result = await dispatch_task_to_agent(
        device_id=target_device_id,
        user_id=int(user.id),
        ai_config_id=None,
        ai_kind="inheritance_test",
        session_id=session_id,
        session_name="MCP 工具测试",
        model=model,
        instruction=f"Run inheritance MCP test for {tool_name}",
        tool=tool_name,
        args=tool_args,
        allowed_tools=[tool_name],
        wait_for_result=True,
        timeout_seconds=120,
        suppress_session_message=True,
    )
    success = bool(tool_result.get("success"))
    return {
        "ok": success,
        "model_preset": {
            "id": preset["id"],
            "name": preset["name"],
            "model": model,
        },
        "model_reply": model_reply,
        "tool_call": {
            "tool": called_tool,
            "arguments": tool_args,
        },
        "tool_result": tool_result,
        "detail": (
            "工具调用完成"
            if success
            else str(tool_result.get("error") or tool_result.get("summary") or "工具调用失败")
        ),
    }