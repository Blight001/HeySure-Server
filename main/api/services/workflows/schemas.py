"""HTTP/service DTOs for workflow cards."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    tags: List[str] = Field(default_factory=list)
    risk_level: str = "read_only"
    definition: Dict[str, Any] = Field(default_factory=dict)


class CardUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=4000)
    tags: Optional[List[str]] = None
    risk_level: Optional[str] = None
    definition: Optional[Dict[str, Any]] = None


class PublishRequest(BaseModel):
    device_id: Optional[str] = None


class RunCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=256)
    input: Dict[str, Any] = Field(default_factory=dict)
    version_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=200)


class RunCancel(BaseModel):
    reason: str = Field(default="cancelled by user", max_length=500)
