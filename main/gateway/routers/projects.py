"""``/api/projects`` routes: CRUD for ``EvolutionProject`` rows and syncing their
linked AI member configs."""

import json
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import Session, select

from api.database import get_session
from api.models import (
    AssistantAIConfig,
    EvolutionProject,
    EvolutionProjectCreate,
    EvolutionProjectUpdate,
)
from .auth import get_current_user

router = APIRouter()
PREFIX = "/api/projects"

def _validate_status(status: str) -> str:
    if status not in {"running", "ended"}:
        raise HTTPException(status_code=400, detail="status must be one of: running, ended")
    return status


def _normalize_member_ids(value: object) -> List[int]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except Exception:
            continue
    return sorted(list(set(result)))


def _save_member_ids(row: EvolutionProject, member_ids: List[int]) -> None:
    row.ai_member_ids = json.dumps(member_ids, ensure_ascii=False)


def _sync_project_members(session: Session, user_id: int, row: EvolutionProject) -> List[int]:
    configs = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user_id,
            AssistantAIConfig.project_id == row.project_id,
        )
    ).all()
    linked_ids = [cfg.id for cfg in configs if cfg.id is not None]
    stored_ids = _normalize_member_ids(row.ai_member_ids)
    merged = sorted(list(set(linked_ids + stored_ids)))
    if merged != stored_ids:
        _save_member_ids(row, merged)
        row.updated_at = time.time()
        session.add(row)
        session.commit()
    return merged


def _apply_member_assignment(session: Session, user_id: int, project: EvolutionProject, member_ids: List[int]) -> None:
    if not member_ids:
        return
    rows = session.exec(
        select(AssistantAIConfig).where(AssistantAIConfig.user_id == user_id)
    ).all()
    changed = False
    for cfg in rows:
        if cfg.id is None:
            continue
        if cfg.id in member_ids:
            if cfg.project_id != project.project_id or cfg.project_name != project.name:
                cfg.project_id = project.project_id
                cfg.project_name = project.name
                cfg.updated_at = time.time()
                session.add(cfg)
                changed = True
    if changed:
        session.commit()


@router.get("")
async def list_projects(
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    rows = session.exec(
        select(EvolutionProject)
        .where(EvolutionProject.user_id == user.id)
        .order_by(EvolutionProject.created_at.asc())
    ).all()
    data = []
    for row in rows:
        members = _sync_project_members(session, user.id, row)
        data.append(
            {
                "id": row.project_id,
                "name": row.name,
                "description": row.description,
                "status": row.status,
                "ai_member_ids": members,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
    return data


@router.post("")
async def create_project(
    body: EvolutionProjectCreate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")

    project_id = f"p-{int(time.time() * 1000)}"
    status = _validate_status((body.status or "running").strip())
    member_ids = _normalize_member_ids(body.ai_member_ids)

    row = EvolutionProject(
        user_id=user.id,
        project_id=project_id,
        name=body.name.strip(),
        description=(body.description or "").strip(),
        status=status,
    )
    _save_member_ids(row, member_ids)
    session.add(row)
    session.commit()
    session.refresh(row)

    _apply_member_assignment(session, user.id, row, member_ids)

    return {
        "id": row.project_id,
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "ai_member_ids": member_ids,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.put("/{project_id}")
async def update_project(
    project_id: str,
    body: EvolutionProjectUpdate,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = session.exec(
        select(EvolutionProject).where(
            EvolutionProject.user_id == user.id,
            EvolutionProject.project_id == project_id,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    changed = False
    payload = body.model_dump(exclude_unset=True)

    if "name" in payload:
        next_name = (payload.get("name") or "").strip()
        if not next_name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        if next_name != row.name:
            row.name = next_name
            linked_configs = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.user_id == user.id,
                    AssistantAIConfig.project_id == row.project_id,
                )
            ).all()
            for cfg in linked_configs:
                cfg.project_name = next_name
                cfg.updated_at = time.time()
                session.add(cfg)
            changed = True

    if "description" in payload:
        row.description = (payload.get("description") or "").strip()
        changed = True

    if "status" in payload:
        row.status = _validate_status((payload.get("status") or "").strip())
        changed = True

    member_ids: Optional[List[int]] = None
    if "ai_member_ids" in payload:
        member_ids = _normalize_member_ids(payload.get("ai_member_ids"))
        _save_member_ids(row, member_ids)
        _apply_member_assignment(session, user.id, row, member_ids)
        changed = True

    if changed:
        row.updated_at = time.time()
        session.add(row)
        session.commit()
        session.refresh(row)

    members = _sync_project_members(session, user.id, row)
    return {
        "id": row.project_id,
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "ai_member_ids": members,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    row = session.exec(
        select(EvolutionProject).where(
            EvolutionProject.user_id == user.id,
            EvolutionProject.project_id == project_id,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    linked_configs = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.user_id == user.id,
            AssistantAIConfig.project_id == row.project_id,
        )
    ).all()
    for cfg in linked_configs:
        cfg.project_id = None
        cfg.project_name = None
        cfg.updated_at = time.time()
        session.add(cfg)

    session.delete(row)
    session.commit()
    return {"success": True}
