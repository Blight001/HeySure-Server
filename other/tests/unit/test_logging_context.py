import logging
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core.logging_config import RingBufferHandler, RuntimeContextFilter
from api.runtime.log_context import bind, install_http_request_context


def test_runtime_context_filter_adds_process_identity():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    context = RuntimeContextFilter("connector", "connector-test-1")

    assert context.filter(record)
    assert record.service_role == "connector"
    assert record.instance_id == "connector-test-1"


def test_ring_buffer_retains_structured_process_identity():
    handler = RingBufferHandler(capacity=2)
    handler.addFilter(RuntimeContextFilter("worker", "worker-test-1"))
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "safe", (), None)

    handler.handle(record)

    item = handler.snapshot()[0]
    assert item["service_role"] == "worker"
    assert item["instance_id"] == "worker-test-1"


def test_runtime_filter_includes_context_local_correlation_fields():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "run", (), None)
    context = RuntimeContextFilter("worker", "worker-test-1")

    with bind(request_id="request-1", run_id="run-1", stage="mcp"):
        context.filter(record)

    assert record.request_id == "request-1"
    assert record.run_id == "run-1"
    assert record.stage == "mcp"


def test_http_context_echoes_or_generates_request_id():
    app = FastAPI()
    install_http_request_context(app)

    @app.get("/probe")
    def probe():
        return {"ok": True}

    supplied = TestClient(app).get("/probe", headers={"X-Request-ID": "request-client"})
    generated = TestClient(app).get("/probe")

    assert supplied.headers["X-Request-ID"] == "request-client"
    assert generated.headers["X-Request-ID"]
