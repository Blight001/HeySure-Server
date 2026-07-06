"""``/api/agent-modes`` routes: list a specific AI's work modes and edit a mode's
prompt / device-MCP exposure toggle from the web console (知识库固有人格 → 模式栏目).

工作模式按 AI 隔离（``ai_config_id``）；省略时操作旧版用户级模板桶。存储与规则的
唯一权威在 ``api.services.mcp.agent_mode_store``（与 ``mode.manage`` MCP 工具、
chat_runtime 工具门禁共用同一实现）。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from api.database import get_session
from api.models import AssistantAIConfig, User
from api.services.mcp import agent_mode_store as store
from .auth import get_current_user

router = APIRouter()
PREFIX = "/api/agent-modes"


class AgentModeUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    allow_device_mcp: Optional[bool] = None


def _validated_ai_config_id(
    session: Session, user: User, ai_config_id: Optional[int]
) -> Optional[int]:
    if ai_config_id is None:
        return None
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.id == int(ai_config_id),
            AssistantAIConfig.user_id == user.id,
        )
    ).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="AI config not found")
    return int(ai_config_id)


@router.get("")
def list_agent_modes(
    ai_config_id: Optional[int] = Query(None),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    scope = _validated_ai_config_id(session, user, ai_config_id)
    return {"modes": store.list_modes(user.id, scope), "ai_config_id": scope}


@router.put("/{mode_key}")
def update_agent_mode(
    mode_key: str,
    req: AgentModeUpdateRequest,
    ai_config_id: Optional[int] = Query(None),
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    user = get_current_user(authorization, session)
    scope = _validated_ai_config_id(session, user, ai_config_id)
    return store.update_mode(
        user.id,
        mode_key,
        name=req.name,
        prompt=req.prompt,
        description=req.description,
        allow_device_mcp=req.allow_device_mcp,
        ai_config_id=scope,
    )
