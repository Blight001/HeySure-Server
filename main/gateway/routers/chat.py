# chat 域路由聚合入口：chat_base 提供共享 router/PREFIX 与运行态，下面两个子模块在
# import 时通过 @router 装饰器把端点注册到同一个 router 上（副作用导入，勿删）。
from .chat_base import PREFIX, _RUN_LIVE_STATE, _RUN_STATE_LOCK, router
from api.chat_runtime.chat_scheduler import process_task_scheduler
from . import chat_action_routes as _chat_action_routes  # noqa: F401 (副作用导入：注册路由)
from . import chat_history_routes as _chat_history_routes  # noqa: F401 (副作用导入：注册路由)

__all__ = [
    "router",
    "PREFIX",
    "process_task_scheduler",
    "_RUN_LIVE_STATE",
    "_RUN_STATE_LOCK",
]
