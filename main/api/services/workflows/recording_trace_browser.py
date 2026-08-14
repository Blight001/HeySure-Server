"""Conservative cleanup of recorded browser calls before card compilation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


RESOLVER_FIELDS = ("ariaLabel", "name", "placeholder", "text")


def _tool(call: Dict[str, Any]) -> str:
    return str(call.get("tool") or call.get("name") or "")


def _arguments(call: Dict[str, Any]) -> Dict[str, Any]:
    value = call.get("arguments")
    return value if isinstance(value, dict) else {}


def _device_key(call: Dict[str, Any]) -> str:
    return str(call.get("device_id") or call.get("deviceId") or "__default__").strip()


def _suffix(call: Dict[str, Any]) -> str:
    tool = _tool(call)
    return tool.rsplit("browser+", 1)[-1].lower() if ".browser+" in tool else ""


def _is_acquire(call: Dict[str, Any]) -> bool:
    return _suffix(call) == "control" and str(_arguments(call).get("action") or "").lower() == "acquire"


def _is_release(call: Dict[str, Any]) -> bool:
    return _suffix(call) == "control" and str(_arguments(call).get("action") or "").lower() == "release"


def _is_write(call: Dict[str, Any]) -> bool:
    suffix = _suffix(call)
    action = str(_arguments(call).get("action") or "").lower()
    if not suffix or suffix in {"observe", "screenshot", "wait", "control"}:
        return False
    if suffix == "tab":
        return action != "list"
    if suffix == "file":
        return action not in {"info", "save_session"}
    return True


def _observe_items(call: Dict[str, Any]) -> tuple[list[Dict[str, Any]], str] | None:
    value: Any = call.get("result")
    path = "items"
    for _depth in range(4):
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("items"), list):
            items = [item for item in value["items"] if isinstance(item, dict)]
            return items, path
        value = value.get("result")
        path = f"result.{path}"
    return None


def _required_ready_indexes(calls: list[Dict[str, Any]]) -> set[int]:
    reset_index = next((index for index, call in enumerate(calls) if (
        _suffix(call) == "tab"
        and str(_arguments(call).get("action") or "").lower() in {"reload", "replace"}
    )), None)
    if reset_index is None:
        return set()
    ready_index = next((index for index in range(reset_index + 1, len(calls)) if (
        _suffix(calls[index]) in {"wait", "observe"}
    )), None)
    return {ready_index} if ready_index is not None else set()


def _drop_redundant_empty_observes(
    calls: list[Dict[str, Any]],
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    required = _required_ready_indexes(calls)
    retained: list[Dict[str, Any]] = []
    warnings: list[Dict[str, Any]] = []
    for index, call in enumerate(calls):
        observed = _observe_items(call)
        explicitly_empty = _suffix(call) == "observe" and observed is not None and observed[0] == []
        can_drop = explicitly_empty and index not in required and index != len(calls) - 1
        if can_drop:
            warnings.append({
                "code": "DROPPED_EMPTY_BROWSER_OBSERVATION",
                "sourceSequence": index + 1,
            })
            continue
        retained.append(call)
    return retained, warnings


def _control_call(write_call: Dict[str, Any]) -> Dict[str, Any]:
    prefix = _tool(write_call).rsplit("browser+", 1)[0]
    call: Dict[str, Any] = {
        "tool": f"{prefix}browser+control",
        "arguments": {"action": "acquire"},
        "_recordingWarnings": ["AUTO_BROWSER_ACQUIRE"],
    }
    for key in ("device_id", "deviceId"):
        if write_call.get(key):
            call[key] = write_call[key]
    return call


def _insert_acquires(calls: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    prepared: list[Dict[str, Any]] = []
    acquired: set[str] = set()
    for call in calls:
        key = _device_key(call)
        if _is_acquire(call):
            acquired.add(key)
        elif _is_release(call):
            acquired.discard(key)
        elif _is_write(call) and key not in acquired:
            prepared.append(_control_call(call))
            acquired.add(key)
        prepared.append(call)
    return prepared


def prepare_browser_calls(
    source_calls: list[Dict[str, Any]],
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Copy calls, discard only redundant empty observes, and add takeover."""
    calls = [deepcopy(call) for call in source_calls]
    retained, warnings = _drop_redundant_empty_observes(calls)
    return _insert_acquires(retained), warnings


def _semantic_value(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > 160 or not any(char.isalpha() for char in text):
        return ""
    return text


def _item_matches(
    item: Dict[str, Any], *, field: str, expected: str, kind: str, tag: str,
) -> bool:
    if bool(item.get("disabled")):
        return False
    if kind and str(item.get("kind") or "") != kind:
        return False
    if tag and str(item.get("tag") or "").casefold() != tag.casefold():
        return False
    actual = " ".join(str(item.get(field) or "").split()).casefold()
    return actual == expected.casefold()


def _resolver(items: list[Dict[str, Any]], ref: Any, items_ref: str) -> Dict[str, Any] | None:
    targets = [item for item in items if str(item.get("id") or "") == str(ref)]
    if len(targets) != 1:
        return None
    target = targets[0]
    kind, tag = str(target.get("kind") or ""), str(target.get("tag") or "")
    for field in RESOLVER_FIELDS:
        expected = _semantic_value(target.get(field))
        if not expected:
            continue
        matches = [item for item in items if _item_matches(
            item, field=field, expected=expected, kind=kind, tag=tag,
        )]
        if len(matches) == 1:
            result: Dict[str, Any] = {
                "items": items_ref, "text": expected, "fields": [field], "exact": True,
            }
            result.update({"kind": kind} if kind else {})
            result.update({"tag": tag} if tag else {})
            return result
    return None


def stabilize_browser_refs(
    calls: list[Dict[str, Any]], save_names: list[str],
) -> Dict[int, Dict[str, Any]]:
    """Replace a ref only when a fresh observation identifies it uniquely."""
    resolvers: Dict[int, Dict[str, Any]] = {}
    observations: Dict[str, tuple[int, list[Dict[str, Any]], str]] = {}
    for index, call in enumerate(calls):
        key = _device_key(call)
        if _suffix(call) == "observe":
            observed = _observe_items(call)
            if observed is not None:
                items, result_path = observed
                observations[key] = (index, items, result_path)
                if not items:
                    call.setdefault("_recordingWarnings", []).append("EMPTY_BROWSER_OBSERVATION")
            continue
        if not _is_write(call):
            continue
        ref, observation = _arguments(call).get("ref"), observations.get(key)
        resolver = None
        if ref not in (None, "") and observation is not None:
            observe_index, items, result_path = observation
            items_ref = f"${{steps.{save_names[observe_index]}.result.{result_path}}}"
            resolver = _resolver(items, ref, items_ref)
        if resolver is not None:
            call["arguments"] = {key: value for key, value in _arguments(call).items() if key != "ref"}
            resolvers[index] = resolver
        elif ref not in (None, ""):
            call.setdefault("_recordingWarnings", []).append("UNSTABLE_BROWSER_REF")
        observations.pop(key, None)
    return resolvers
