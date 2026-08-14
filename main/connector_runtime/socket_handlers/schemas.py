"""Validated endpoint Agent Socket.IO payload contracts."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentRegistrationPayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: Optional[str] = None
    name: Optional[str] = None
    platform: Optional[str] = None
    version: Optional[str] = None
    device_type: Optional[str] = Field(default=None, alias="deviceType")
    token: Optional[str] = None
    user_id: Optional[int] = Field(default=None, alias="userId")
    capabilities: List[str] = Field(default_factory=list, max_length=256)
    tool_defs: List[Dict[str, Any]] = Field(default_factory=list, alias="toolDefs", max_length=256)
    ai_description: Optional[str] = Field(default=None, alias="aiDescription", max_length=2000)
    catalog_generation: Optional[int] = Field(default=None, alias="catalogGeneration", ge=0)
    catalog_protocol_version: int = Field(default=1, alias="catalogProtocolVersion", ge=1, le=100)
    dynamic_tools: List[Dict[str, Any]] = Field(default_factory=list, alias="dynamicTools")


class TaskProgressPayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    task_id: str = Field(alias="taskId", min_length=1)
    device_id: Optional[str] = Field(default=None, alias="deviceId")
    message: str = ""


class TaskResultPayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    task_id: str = Field(alias="taskId", min_length=1)
    device_id: Optional[str] = Field(default=None, alias="deviceId")
    tool: Optional[str] = None
    success: bool = True
    summary: Optional[str] = None
    result: Any = None


class TaskErrorPayload(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    task_id: str = Field(alias="taskId", min_length=1)
    device_id: Optional[str] = Field(default=None, alias="deviceId")
    tool: Optional[str] = None
    error: str = Field(min_length=1)


def validated_payload(model, raw: object) -> Dict[str, Any]:
    return model.model_validate(raw if isinstance(raw, dict) else {}).model_dump(
        by_alias=True, exclude_none=True
    )
