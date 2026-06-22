"""游戏世界事件直推（world:event）。

把数字社会的关键瞬间（任务启动/完成）实时推到用户房间，
让世界页的演出零延迟触发，不再依赖 8s 轮询比对。

- 事件只是**通知**，不携带权威状态：客户端收到后照常 refresh 拉取真相
  （设计方案 §0①：地图不是第二真相源）。
- 一律 best-effort：emit 失败绝不影响业务链路。
- 跨进程安全：worker 进程里的 ``api.sio.sio`` 是 _RemoteSio，
  会经 gateway 的 /internal/socket/emit 中继（同 librarian 事件机制）。
"""

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

from ..sio import sio

logger = logging.getLogger(__name__)

EVENT_NAME = "world:event"


def emit_world_event(user_id: int, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """从任意（同步/异步）上下文向 user 房间广播一条世界事件。"""
    data = {
        "userId": user_id,
        "type": event_type,
        "payload": payload or {},
        "timestamp": time.time(),
    }
    room = f"user_{user_id}"

    async def _do_emit():
        try:
            await sio.emit(EVENT_NAME, data, room=room)
        except Exception as exc:
            logger.info(f"world:event {event_type}: {exc}")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_emit())
    except RuntimeError:
        from api.runtime.async_bridge import run_async

        def _runner():
            try:
                run_async(_do_emit())
            except Exception:
                pass

        threading.Thread(target=_runner, daemon=True).start()
