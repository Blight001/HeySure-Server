# ai 域路由聚合入口：ai_base 提供共享 router/PREFIX，下面三个子模块在 import 时
# 通过各自的 @router 装饰器把端点注册到同一个 router 上（副作用导入，勿删）。
from .ai_base import PREFIX, router
from . import ai_config_routes as _ai_config_routes  # noqa: F401 (副作用导入：注册路由)
from . import ai_misc_routes as _ai_misc_routes  # noqa: F401 (副作用导入：注册路由)
from . import ai_task_routes as _ai_task_routes  # noqa: F401 (副作用导入：注册路由)

__all__ = ["router", "PREFIX"]
