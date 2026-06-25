# HeySure Server

This is the **HeySure-Server** repository.

`server/` contains the backend for HeySure AI 2.0. The backend is split into
several runtime processes:

- `gateway` for public REST APIs, Socket.IO, and static assets
- `mcp_runtime` for tool registration, loading, and permission checks
- `connector_runtime` for QQ / Feishu connectors and external dispatch
- `ai_runtime` for queue consumption, inference, and chat persistence

## Quick Start

Run the full backend on Windows:

```bat
server\run.bat
```

That script uses `tile_windows.ps1` to open the following service windows:

| Service | Startup script | Default URL / port | Description |
| --- | --- | --- | --- |
| API Gateway | `run_gateway.bat` | `http://127.0.0.1:3000` | Public REST API, Socket.IO, static assets |
| MCP Runtime | `run_mcp.bat` | `http://127.0.0.1:3001` | MCP tool registration, loading, and internal tool calls |
| Connector Runtime | `run_connector.bat` | `http://127.0.0.1:3002` | QQ / Feishu connectors and agent dispatch |
| AI Runtime | `run_ai.bat` | `http://127.0.0.1:3003` | Remote chat queue consumption and AI inference |

You can also run the individual `run_*.bat` scripts for debugging a specific
service.

## Environment

The startup scripts read `.env` from the **workspace root** (the directory containing docker-compose.yml and init-env.*).

If developing HeySure-Server in isolation, you can also place a `.env` directly inside the server/ directory (it will be picked up).

Common values:

```env
DATABASE_URL=postgresql+psycopg://heysure:heysure@127.0.0.1:5432/heysure
HEYSURE_INTERNAL_TOKEN=heysure-dev-internal-token-change-me
MCP_RUNTIME_URL=http://127.0.0.1:3001
CONNECTOR_RUNTIME_URL=http://127.0.0.1:3002
AI_RUNTIME_URL=http://127.0.0.1:3003
AI_DISPATCH_MODE=remote
```

Important variables:

- `DATABASE_URL`: required PostgreSQL connection string. Startup fails if it is
  missing or points to another database.
- `HEYSURE_INTERNAL_TOKEN`: bearer token used when the gateway calls internal
  `/internal/*` endpoints.
- `MCP_RUNTIME_URL`: MCP runtime address, usually `http://127.0.0.1:3001`.
- `CONNECTOR_RUNTIME_URL`: connector runtime address, usually
  `http://127.0.0.1:3002`.
- `AI_RUNTIME_URL`: AI runtime status service address, usually
  `http://127.0.0.1:3003`.
- `AI_DISPATCH_MODE`: set to `remote` to route chat jobs into the queue
  consumed by `ai_runtime`.

Full configuration lives in `main/api/core/settings.py`.

## Windows launcher

On Windows, `run.bat` opens a modern single-window launcher (built with customtkinter).
It provides live log tabs for gateway / MCP / connector / AI runtime + Web console,
per-service start/restart/stop, global controls, status overview pills, and quick "open web" button.

## Directory Layout

| Path | Description |
| --- | --- |
| `main/gateway/` | FastAPI + Socket.IO gateway with HTTP routes and realtime events. |
| `main/api/` | Shared models, database access, authentication, services, and business logic. |
| `main/ai_runtime/` | AI worker entrypoint, queue consumer, inference pipeline, and internal app. |
| `main/mcp_runtime/` | MCP plugin loader, registry, permissions, and built-in tools. |
| `main/connector_runtime/` | External connectors, device dispatch, and bot implementations. |
| `other/migrations/` | Alembic database migrations. |
| `other/scripts/` | Helper scripts such as data migration and version snapshotting. |
| `other/tests/` | pytest suite. |
| `library/` | Internal library (知识工坊) code. |
| `data/` | Local runtime data and workspace storage. |
| `logs/` | Service logs. |
| `static/` | Backend static assets. |

## Development

Install backend dependencies:

```bat
cd server
install-deps.bat
```

Run the gateway manually:

```bat
cd server
set PYTHONPATH=main;.
venv\Scripts\activate
python -m gateway.main
```

Run the split services directly:

```bat
python -m mcp_runtime.main
python -m connector_runtime.main
python -m ai_runtime.main
```

Run tests:

```bat
cd server
pytest
```

## Health Check

After the gateway starts, visit:

```text
http://127.0.0.1:3000/
```

The service is healthy when it returns:

```json
{"message":"HeySure Server is running"}
```

Internal runtime endpoints under `/internal/*` require the matching
`HEYSURE_INTERNAL_TOKEN` bearer token.
