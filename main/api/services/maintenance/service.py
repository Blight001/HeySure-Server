"""Transactional service for Codex maintenance work orders."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from sqlmodel import Session, select

from api.models import AssistantAIConfig, DeviceAiBinding, DevicePresence
from api.models.maintenance import MaintenanceApproval, MaintenanceEvent, MaintenanceTask

from .state import TERMINAL_STATUSES, validate_phase_transition, validate_status_transition


class MaintenanceNotFound(LookupError):
    pass


class MaintenanceConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class CreateTaskSpec:
    maintainer_ai_config_id: int
    device_id: str
    title: str
    description: str
    acceptance_criteria: str = ""
    affected_repo: str = ""
    reporter_ai_config_id: Optional[int] = None
    source_session_id: str = ""
    severity: str = "normal"
    dedupe_key: str = ""
    deadline_at: Optional[float] = None


@dataclass(frozen=True)
class EventRecord:
    event_type: str
    actor_type: str
    payload: Any = None
    event_id: str = ""
    sequence: Optional[int] = None
    actor_id: str = ""
    phase: str = ""
    status: str = ""


@dataclass(frozen=True)
class DeviceEventRecord:
    device_id: str
    run_id: str
    event_id: str
    sequence: int
    event_type: str
    payload: Any
    status: str = ""
    phase: str = ""
    lease_seconds: int = 300


@dataclass(frozen=True)
class ApprovalRequestRecord:
    event_id: str
    sequence: int
    approval_id: str
    approval_type: str
    title: str
    detail: Any
    expires_at: Optional[float]


_SECRET_KEY = re.compile(r"token|secret|password|cookie|authorization|private.?key", re.I)
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}")


def sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded, JSON-safe event payload with credential fields removed."""
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key)[:120]: "[redacted]" if _SECRET_KEY.search(str(key)) else sanitize_payload(item, depth=depth + 1)
            for key, item in list(value.items())[:200]
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item, depth=depth + 1) for item in list(value)[:200]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = _BEARER.sub("Bearer [redacted]", str(value))
    return text[:100_000]


def _json(value: Any) -> str:
    return json.dumps(sanitize_payload(value), ensure_ascii=False, separators=(",", ":"))


def _apply_device_result_fields(task: MaintenanceTask, record: DeviceEventRecord) -> None:
    payload = record.payload if isinstance(record.payload, dict) else {}
    if record.event_type == "run.started":
        task.branch_name = str(payload.get("branch") or payload.get("branch_name") or "")[:500]
        task.base_sha = str(payload.get("baseSha") or payload.get("base_sha") or "")[:100]
    if record.status in TERMINAL_STATUSES:
        task.summary = str(payload.get("summary") or "")[:100_000]
        fallback = "run_failed" if record.status == "failed" else ""
        task.error_code = str(payload.get("error_code") or fallback)[:200]


def _sanitized_device_payload(record: DeviceEventRecord) -> dict:
    payload = dict(record.payload) if isinstance(record.payload, dict) else {"value": record.payload}
    for private_path_key in ("workspace", "workspace_path", "workspacePath"):
        payload.pop(private_path_key, None)
    payload["device_sequence"] = int(record.sequence)
    return payload


class MaintenanceService:
    def __init__(self, session: Session):
        self.session = session

    def _owned_ai(self, user_id: int, ai_config_id: int) -> AssistantAIConfig:
        row = self.session.get(AssistantAIConfig, ai_config_id)
        if not row or row.user_id != user_id:
            raise MaintenanceNotFound("AI member not found")
        return row

    def _codex_device(self, user_id: int, ai_config_id: int, device_id: str) -> DevicePresence:
        row = self.session.exec(select(DevicePresence).where(
            DevicePresence.user_id == user_id, DevicePresence.device_id == device_id,
        )).first()
        if not row or str(row.platform or "").lower() != "codex-maintainer":
            raise MaintenanceConflict("a codex-maintainer device owned by this user is required")
        binding = self.session.exec(select(DeviceAiBinding).where(
            DeviceAiBinding.user_id == user_id,
            DeviceAiBinding.device_id == device_id,
            DeviceAiBinding.ai_config_id == ai_config_id,
        )).first()
        if not binding:
            raise MaintenanceConflict("codex-maintainer device is not bound to the maintainer member")
        return row

    def create_task(self, user_id: int, spec: CreateTaskSpec) -> MaintenanceTask:
        self._owned_ai(user_id, spec.maintainer_ai_config_id)
        if spec.reporter_ai_config_id is not None:
            self._owned_ai(user_id, spec.reporter_ai_config_id)
        self._codex_device(user_id, spec.maintainer_ai_config_id, spec.device_id)
        key = str(spec.dedupe_key or "").strip()[:200]
        if key:
            existing = self.session.exec(select(MaintenanceTask).where(
                MaintenanceTask.user_id == user_id, MaintenanceTask.dedupe_key == key,
                MaintenanceTask.status.notin_(list(TERMINAL_STATUSES)),
            ).order_by(MaintenanceTask.created_at.desc())).first()
            if existing:
                return existing
        task_id = f"mnt_{uuid.uuid4().hex}"
        row = MaintenanceTask(
            task_id=task_id, run_id=task_id, user_id=user_id,
            maintainer_ai_config_id=spec.maintainer_ai_config_id,
            reporter_ai_config_id=spec.reporter_ai_config_id, source_session_id=spec.source_session_id[:200],
            device_id=spec.device_id[:200], title=spec.title[:500], description=spec.description,
            acceptance_criteria=spec.acceptance_criteria, affected_repo=spec.affected_repo[:200],
            severity=spec.severity[:40], dedupe_key=key, deadline_at=spec.deadline_at,
        )
        self.session.add(row)
        self.session.flush()
        self.append_event(row, EventRecord("task.created", "user", {"title": row.title}))
        self.session.commit()
        self.session.refresh(row)
        return row

    def owned_task(self, user_id: int, task_id: str, *, lock: bool = False) -> MaintenanceTask:
        statement = select(MaintenanceTask).where(
            MaintenanceTask.task_id == task_id, MaintenanceTask.user_id == user_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = self.session.exec(statement).first()
        if not row:
            raise MaintenanceNotFound("maintenance task not found")
        return row

    def append_event(self, task: MaintenanceTask, record: EventRecord) -> MaintenanceEvent:
        stable_id = str(record.event_id or f"evt_{uuid.uuid4().hex}")[:200]
        duplicate = self.session.exec(select(MaintenanceEvent).where(
            MaintenanceEvent.run_id == task.run_id, MaintenanceEvent.event_id == stable_id,
        )).first()
        if duplicate:
            return duplicate
        expected = int(task.last_sequence) + 1
        seq = expected if record.sequence is None else int(record.sequence)
        if seq != expected:
            raise MaintenanceConflict(f"event sequence must be {expected}, received {seq}")
        row = MaintenanceEvent(
            task_id=task.task_id, run_id=task.run_id, event_id=stable_id, sequence=seq,
            event_type=record.event_type[:100], actor_type=record.actor_type[:40], actor_id=record.actor_id[:200],
            phase=record.phase[:40], status=record.status[:40], payload_json=_json(record.payload or {}),
        )
        task.last_sequence = seq
        task.updated_at = time.time()
        self.session.add(row)
        self.session.add(task)
        self.session.flush()
        return row

    def apply_state(self, task: MaintenanceTask, *, status: str = "", phase: str = "") -> None:
        try:
            if status:
                validate_status_transition(task.status, status)
            if phase:
                validate_phase_transition(task.phase, phase)
        except ValueError as exc:
            raise MaintenanceConflict(str(exc)) from exc
        now = time.time()
        if status and status != task.status:
            task.status = status
            if status == "running" and task.started_at is None:
                task.started_at = now
            if status in TERMINAL_STATUSES:
                task.finished_at = now
                task.lease_expires_at = None
        if phase:
            task.phase = phase
        task.updated_at = now

    def device_event(self, user_id: int, record: DeviceEventRecord) -> tuple[MaintenanceTask, MaintenanceEvent]:
        task = self.session.exec(select(MaintenanceTask).where(
            MaintenanceTask.run_id == record.run_id, MaintenanceTask.user_id == user_id,
        ).with_for_update()).first()
        if not task or task.device_id != record.device_id:
            raise MaintenanceNotFound("maintenance run not assigned to this device")
        existing = self.session.exec(select(MaintenanceEvent).where(
            MaintenanceEvent.run_id == record.run_id, MaintenanceEvent.event_id == record.event_id,
        )).first()
        if existing:
            return task, existing
        expected_device = int(task.last_device_sequence) + 1
        if int(record.sequence) != expected_device:
            raise MaintenanceConflict(
                f"device event sequence must be {expected_device}, received {int(record.sequence)}"
            )
        self.apply_state(task, status=record.status, phase=record.phase)
        _apply_device_result_fields(task, record)
        if task.status not in TERMINAL_STATUSES:
            task.owner = record.device_id
            task.lease_expires_at = time.time() + max(30, min(int(record.lease_seconds), 1800))
        device_payload = _sanitized_device_payload(record)
        task.last_device_sequence = int(record.sequence)
        event = self.append_event(
            task, EventRecord(record.event_type, "device", device_payload,
                              event_id=record.event_id, actor_id=record.device_id,
                              phase=record.phase, status=record.status),
        )
        self.session.commit()
        self.session.refresh(task)
        self.session.refresh(event)
        return task, event

    def request_approval(self, task: MaintenanceTask, record: ApprovalRequestRecord) -> MaintenanceApproval:
        existing = self.session.get(MaintenanceApproval, record.approval_id)
        if existing:
            if existing.task_id != task.task_id:
                raise MaintenanceConflict("approval_id is already used by another maintenance task")
            return existing
        expected_device = int(task.last_device_sequence) + 1
        if int(record.sequence) != expected_device:
            raise MaintenanceConflict(
                f"device event sequence must be {expected_device}, received {int(record.sequence)}"
            )
        self.apply_state(task, status="waiting_user")
        approval = MaintenanceApproval(
            approval_id=record.approval_id, task_id=task.task_id, run_id=task.run_id,
            user_id=task.user_id, request_event_id=record.event_id, approval_type=record.approval_type[:80],
            title=record.title[:500], detail_json=_json(record.detail), expires_at=record.expires_at,
        )
        self.session.add(approval)
        task.last_device_sequence = int(record.sequence)
        self.append_event(
            task, EventRecord("approval.requested", "device",
                              {"approval_id": record.approval_id, "type": approval.approval_type,
                               "title": record.title}, event_id=record.event_id,
                              actor_id=task.device_id, status="waiting_user"),
        )
        self.session.commit()
        self.session.refresh(task)
        self.session.refresh(approval)
        return approval

    def decide_approval(self, user_id: int, approval_id: str, decision: str,
                        comment: str = "", command_id: str = "") -> tuple[MaintenanceTask, MaintenanceApproval, MaintenanceEvent]:
        approval = self.session.exec(select(MaintenanceApproval).where(
            MaintenanceApproval.approval_id == approval_id,
            MaintenanceApproval.user_id == user_id,
        ).with_for_update()).first()
        if not approval:
            raise MaintenanceNotFound("maintenance approval not found")
        task = self.owned_task(user_id, approval.task_id, lock=True)
        stable_command_id = str(command_id or f"approval-{approval_id}")[:200]
        wanted = str(decision or "").lower()
        if wanted not in {"approved", "denied"}:
            raise MaintenanceConflict("decision must be approved or denied")
        if approval.status != "pending":
            if approval.decision == wanted:
                event = self.session.exec(select(MaintenanceEvent).where(
                    MaintenanceEvent.run_id == task.run_id,
                    MaintenanceEvent.event_type == "command.approval_decision",
                    MaintenanceEvent.event_id == f"cmd:{stable_command_id}",
                )).first()
                if event is None:
                    raise MaintenanceConflict("approval decision audit event is missing")
                return task, approval, event
            raise MaintenanceConflict("approval already decided")
        approval.status = "decided"
        approval.decision = wanted
        approval.decided_by_user_id = user_id
        approval.decided_at = time.time()
        # Declining a command/file approval rejects that operation, not the
        # maintenance work order. Codex continues and can choose another path.
        self.apply_state(task, status="running")
        event = self.append_event(
            task, EventRecord("command.approval_decision", "user",
                              {"command_id": stable_command_id, "command": "approval_decision",
                               "approval_id": approval_id,
                               "decision": "accept" if wanted == "approved" else "decline",
                               "approval_decision": wanted,
                               "comment": comment},
                              event_id=f"cmd:{stable_command_id}", actor_id=approval_id,
                              status=task.status),
        )
        self.session.add(approval)
        self.session.commit()
        self.session.refresh(task)
        self.session.refresh(approval)
        self.session.refresh(event)
        return task, approval, event

    @staticmethod
    def task_payload(row: MaintenanceTask) -> dict:
        return {name: getattr(row, name) for name in (
            "task_id", "run_id", "user_id", "maintainer_ai_config_id",
            "reporter_ai_config_id", "source_session_id", "device_id", "title",
            "description", "acceptance_criteria", "affected_repo", "branch_name", "base_sha", "severity",
            "dedupe_key", "status", "phase", "owner", "lease_expires_at",
            "deadline_at", "last_sequence", "last_device_sequence", "summary", "error_code", "created_at",
            "updated_at", "started_at", "finished_at",
        )}

    @staticmethod
    def event_payload(row: MaintenanceEvent) -> dict:
        try:
            payload = json.loads(row.payload_json or "{}")
        except Exception:
            payload = {}
        return {
            "id": row.id, "task_id": row.task_id, "run_id": row.run_id,
            "event_id": row.event_id, "sequence": row.sequence,
            "event_type": row.event_type, "actor_type": row.actor_type,
            "actor_id": row.actor_id, "phase": row.phase, "status": row.status,
            "payload": payload, "created_at": row.created_at,
        }
