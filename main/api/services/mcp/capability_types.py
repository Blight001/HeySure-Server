"""Immutable, process-independent MCP capability view DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ToolCapability:
    canonical_name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    implementation: Optional[Mapping[str, Any]] = None
    schema_version: str = ""
    source_kind: str = "server"
    provider_id: Optional[str] = None
    device_id: Optional[str] = None
    destructive: bool = False


@dataclass(frozen=True)
class ToolBlock:
    name: str
    reason: str


@dataclass(frozen=True)
class DevicePromptMetadata:
    device_id: str
    name: str
    device_type: str
    purpose: str = ""
    tool_count: int = 0
    catalog_generation: int = 0
    catalog_hash: str = ""


@dataclass(frozen=True)
class ScopedToolView:
    revision: str
    eligible: Mapping[str, ToolCapability]
    blocked: Mapping[str, ToolBlock] = field(default_factory=dict)
    devices: Tuple[DevicePromptMetadata, ...] = ()
    device_tool_names: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @property
    def eligible_names(self) -> frozenset[str]:
        return frozenset(self.eligible)
