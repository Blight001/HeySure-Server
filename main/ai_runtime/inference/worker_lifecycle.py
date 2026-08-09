"""Heartbeat and connector cleanup around an inference worker request."""

import logging
import threading
from typing import Callable

from api.runtime import heartbeat
from api.services.chat import chat_inject
from ai_runtime.inference.run_request import WorkerRequest


logger = logging.getLogger(__name__)


def run_worker(
    request: WorkerRequest,
    implementation: Callable[[WorkerRequest], None],
) -> None:
    stop_heartbeat = threading.Event()
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(request.run_id, stop_heartbeat),
        name=f"hb-{request.run_id}",
        daemon=True,
    )
    thread.start()
    _start_qq_stream(request)
    try:
        implementation(request)
    finally:
        _finish_qq_stream(request)
        _resume_orphaned_injects(request)
        stop_heartbeat.set()
        thread.join(timeout=1.0)


def _heartbeat_loop(run_id: str, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            heartbeat.tick(run_id)
        except Exception:
            pass
        if stop_event.wait(heartbeat.TICK_INTERVAL_SECONDS):
            return


def _start_qq_stream(request: WorkerRequest) -> None:
    try:
        from connector_runtime.bots.qq.stream_sender import maybe_start_qq_stream

        maybe_start_qq_stream(
            run_id=request.run_id,
            user_id=request.user_id,
            ai_config_id=request.ai_config_id,
            ai_kind=request.ai_kind,
            session_id=request.session_id,
        )
    except Exception:
        pass


def _finish_qq_stream(request: WorkerRequest) -> None:
    try:
        from connector_runtime.bots.qq.stream_sender import finish_qq_stream

        finish_qq_stream(request.run_id, session_id=request.session_id)
    except Exception:
        pass


def _resume_orphaned_injects(request: WorkerRequest) -> None:
    try:
        chat_inject.resume_orphaned_injects(
            user_id=request.user_id,
            ai_config_id=request.ai_config_id,
            ai_kind=request.ai_kind,
            session_id=request.session_id,
            session_name=request.session_name,
        )
    except Exception:
        logger.exception("resume orphaned user-injects failed")
