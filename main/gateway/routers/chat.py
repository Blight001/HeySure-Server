# chat 域路由聚合入口：chat_base 提供共享 router/PREFIX 与运行态，下面两个子模块在
# import 时通过 @router 装饰器把端点注册到同一个 router 上（副作用导入，勿删）。
from fastapi import Depends, Header, HTTPException, Request
from sqlmodel import Session, select

from api.database import get_session
from api.models import AssistantAIConfig
from .chat_base import PREFIX, _RUN_LIVE_STATE, _RUN_STATE_LOCK, router
from .auth import get_current_user


async def _reject_external_controller_chat(
    request: Request,
    session: Session = Depends(get_session),
    authorization: str = Header(None),
):
    if request.method != "POST" or request.url.path not in {"/api/chat/run/start", "/api/chat/stream"}:
        return
    body = await request.json()
    config_id = body.get("ai_config_id") if isinstance(body, dict) else None
    if config_id is None:
        return
    user = get_current_user(authorization, session)
    cfg = session.exec(
        select(AssistantAIConfig).where(
            AssistantAIConfig.id == config_id,
            AssistantAIConfig.user_id == user.id,
        )
    ).first()
    if cfg and cfg.execution_mode == "external_mcp":
        raise HTTPException(
            status_code=409,
            detail="This AI is controlled by an external MCP client; user chat is read-only",
        )


router.dependencies.append(Depends(_reject_external_controller_chat))
from api.chat_runtime.chat_scheduler import process_task_scheduler
from . import chat_action_routes as _chat_action_routes  # noqa: F401 (副作用导入：注册路由)
from . import chat_attachment_routes as _chat_attachment_routes  # noqa: F401 (副作用导入：注册路由)
from . import chat_run_start_routes as _chat_run_start_routes  # noqa: F401 (副作用导入：注册路由)
from . import chat_history_routes as _chat_history_routes  # noqa: F401 (副作用导入：注册路由)

__all__ = [
    "router",
    "PREFIX",
    "process_task_scheduler",
    "_RUN_LIVE_STATE",
    "_RUN_STATE_LOCK",
]
