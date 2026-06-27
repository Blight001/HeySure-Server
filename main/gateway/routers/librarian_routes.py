"""图书管理员 / 知识库的用户侧接口。

- GET  /api/librarian/entries        → 列出 active 条目（按 status 过滤）
- GET  /api/librarian/entries/{memory_id}  → 读全文
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from api.services.knowledge import librarian_service
from api.database import get_session
from .auth import get_current_user


PREFIX = "/api/librarian"
router = APIRouter()


class IntrinsicPropertyToolBody(BaseModel):
    name: str
    description: Optional[str] = ""
    parameters: Optional[List[Dict[str, Any]]] = None


class IntrinsicPropertiesBody(BaseModel):
    tools: List[IntrinsicPropertyToolBody] = []


class SystemPromptBody(BaseModel):
    key: str
    content: Any = ""


class SystemPromptsBody(BaseModel):
    prompts: List[SystemPromptBody] = []


class ClawHubInstallBody(BaseModel):
    version: Optional[str] = None
    force: bool = False
    endpoint_kind: Optional[str] = None


class ClawHubInstalledUpdateBody(BaseModel):
    skill_card: str = ""


class InheritanceThoughtEndpointBody(BaseModel):
    endpoint_kind: str = "any"


@router.get("/inheritance-tools/clawhub/search")
async def search_clawhub_skills(
    q: str,
    limit: int = 20,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        return librarian_service.search_clawhub_skills(user_id=user.id, query=q, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/inheritance-tools/clawhub/installed/{slug:path}/endpoint")
async def set_installed_clawhub_skill_endpoint(
    slug: str,
    body: InheritanceThoughtEndpointBody,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        return librarian_service.set_inheritance_thought_endpoint(
            user_id=user.id,
            slug=slug,
            endpoint_kind=body.endpoint_kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/inheritance-tools/clawhub/installed/{slug:path}")
async def get_installed_clawhub_skill(
    slug: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        return librarian_service.clawhub_installed_skill_detail(user_id=user.id, slug=slug)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/inheritance-tools/clawhub/installed/{slug:path}")
async def update_installed_clawhub_skill(
    slug: str,
    body: ClawHubInstalledUpdateBody,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        return librarian_service.update_clawhub_installed_skill(
            user_id=user.id,
            slug=slug,
            skill_card=body.skill_card,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/inheritance-tools/clawhub/installed/{slug:path}")
async def delete_installed_clawhub_skill(
    slug: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        return librarian_service.delete_clawhub_installed_skill(user_id=user.id, slug=slug)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/inheritance-tools/clawhub/{slug:path}")
async def get_clawhub_skill_detail(
    slug: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        return librarian_service.clawhub_skill_detail(user_id=user.id, slug=slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/inheritance-tools/clawhub/{slug:path}/install")
async def install_clawhub_skill(
    slug: str,
    body: ClawHubInstallBody = ClawHubInstallBody(),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        return librarian_service.install_clawhub_skill(
            user_id=user.id,
            slug=slug,
            version=body.version,
            force=body.force,
            endpoint_kind=body.endpoint_kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/entries")
async def list_entries(
    scope: Optional[str] = None,
    status: Optional[str] = "active",
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        items = librarian_service.list_topics(user_id=user.id, scope=scope, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"items": items, "total": len(items)}


@router.get("/entries/{memory_id}")
async def read_entry(
    memory_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        return librarian_service.read(user_id=user.id, memory_id=memory_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/intrinsic-properties")
async def save_intrinsic_properties(
    body: IntrinsicPropertiesBody,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        tools = [item.model_dump() for item in body.tools]
        return librarian_service.save_intrinsic_properties_overrides(user_id=user.id, tools=tools)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/system-prompts")
async def save_system_prompts(
    body: SystemPromptsBody,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        prompts = [item.model_dump() for item in body.prompts]
        return librarian_service.save_system_prompts(user_id=user.id, prompts=prompts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/entries/{memory_id}/archive")
async def archive_entry(
    memory_id: str,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    try:
        entry = librarian_service.archive(user_id=user.id, memory_id=memory_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"archived": True, "entry": entry}
