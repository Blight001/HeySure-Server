import sys
sys.path.insert(0, ".")
from tk_launcher import classify_line

tests = [
    "21:35:10 INFO    gateway.app — loaded router",
    "21:40:56 INFO    uvicorn.error — Started server process",
    "21:40:57 INFO    alembic.runtime.migration — Context impl",
    "21:35:10 WARNING api.foo — something bad happened",
    "21:35:10 ERROR   api.bar — critical failure",
    "Traceback (most recent call last):",
    "uvicorn.error — connection closed",
    'uvicorn.access — 127.0.0.1 - "GET /foo HTTP/1.1" 200',
    'uvicorn.access — 127.0.0.1 - "GET /bad HTTP/1.1" 404',
    'uvicorn.access — 127.0.0.1 - "GET /boom HTTP/1.1" 500',
    "some log with [ERROR] inside",
    "[WARN] deprecated thing",
    "ERROR: module failed to start",
    "21:41:00 ERROR   mcp_runtime — plugin load failed",
    "21:35:10 WARNING gateway.app — watchdog reaped stale runs",
]

for t in tests:
    print(f"{classify_line(t):8} | {t[:75]}")
