"""Immutable worker request DTO and entry lifecycle helpers."""

from dataclasses import dataclass
from typing import Optional

from api.chat_runtime.chat_prompt_utils import _set_run_live_meta
from api.chat_runtime.chat_runtime_helpers import _run_set_status, _run_should_stop
from api.services.model_presets import session_model_preset_entry
from ai_runtime.inference.debug_support import (
    ai_debug_stage,
    ai_short,
    ai_short_run_id,
)


@dataclass(frozen=True)
class WorkerRequest:
    run_id: str
    user_id: int
    ai_config_id: Optional[int]
    ai_kind: str
    session_id: str
    session_name: str
    model_user_content: Optional[str] = None
    merged_system_prompt: Optional[str] = None
    max_steps: Optional[int] = None
    current_user_message_id: Optional[int] = None
    selected_mcp_tools: Optional[frozenset[str]] = None

    @classmethod
    def create(cls, **values):
        selected = values.get("selected_mcp_tools")
        values["selected_mcp_tools"] = frozenset(selected) if selected else None
        return cls(**values)

    def unpack(self):
        return (
            self.run_id,
            self.user_id,
            self.ai_config_id,
            self.ai_kind,
            self.session_id,
            self.session_name,
            self.model_user_content,
            self.merged_system_prompt,
            self.max_steps,
            self.current_user_message_id,
            self.selected_mcp_tools,
        )


def start_worker_run(request: WorkerRequest) -> bool:
    """Mark a request running and publish its initial observable metadata."""

    if _run_should_stop(request.run_id):
        _run_set_status(request.run_id, "stopped", finished=True)
        return False
    _run_set_status(request.run_id, "running")
    _set_run_live_meta(
        request.run_id,
        user_id=request.user_id,
        ai_config_id=request.ai_config_id,
        ai_kind=request.ai_kind,
        session_id=request.session_id,
        session_name=request.session_name,
    )
    ai_debug_stage(
        "START",
        f"{ai_short_run_id(request.run_id)} u={request.user_id} "
        f"cfg={request.ai_config_id if request.ai_config_id is not None else '-'} "
        f"kind={request.ai_kind} sess={ai_short(request.session_id, 24)}",
        "36",
    )
    return True


def resolve_session_preset_entry(session, user, cfg, session_id: str, ai_kind: str):
    return session_model_preset_entry(session, user, cfg, session_id, ai_kind)
