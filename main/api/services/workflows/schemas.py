"""HTTP/service DTOs for workflow cards."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    tags: List[str] = Field(default_factory=list)
    access_scope: str = "all"
    allowed_ai_config_ids: List[int] = Field(default_factory=list, max_length=200)
    risk_level: str = "read_only"
    definition: Dict[str, Any] = Field(default_factory=dict)
    device_id: Optional[str] = None
    default_device_id: Optional[str] = None
    device_ids: List[str] = Field(default_factory=list, max_length=20)


class CardUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=4000)
    tags: Optional[List[str]] = None
    access_scope: Optional[str] = None
    allowed_ai_config_ids: Optional[List[int]] = Field(default=None, max_length=200)
    risk_level: Optional[str] = None
    definition: Optional[Dict[str, Any]] = None
    device_id: Optional[str] = None
    default_device_id: Optional[str] = None
    device_ids: Optional[List[str]] = Field(default=None, max_length=20)


class TraceDraftRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    tags: List[str] = Field(default_factory=list)
    risk_level: str = "normal"
    calls: List[Dict[str, Any]] = Field(min_length=1, max_length=50)


class RunCreate(BaseModel):
    device_id: str = Field(default="", max_length=256)
    input: Dict[str, Any] = Field(default_factory=dict)
    version_id: Optional[str] = None
    idempotency_key: str = Field(min_length=1, max_length=200)


class RunCancel(BaseModel):
    reason: str = Field(default="cancelled by user", max_length=500)


class RunConfirm(BaseModel):
    approved: bool


class RunRetry(BaseModel):
    idempotency_key: Optional[str] = Field(default=None, max_length=200)
