"""诊断/测试路由：供管理员控制台「系统测试」栏目调用，对各模块做逐项、具体的测试。

- ``GET  /api/diagnostics/selftest`` —— 一键自检：进程 / 数据库 / MCP / 文件存储，逐点返回结果
- ``POST /api/diagnostics/models``   —— 逐个测试已配置模型（主脑 + 各 preset）的连通性

MCP 单工具的手动测试仍复用既有的 ``/api/mcp/tools`` 与 ``/api/mcp/call``。
所有接口仅限房主/管理员调用。
"""

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, inspect, text
from sqlmodel import Session, select

from api.core.config import user_shared_knowledge_dir
from api.core.settings import settings
from api.database import engine
from api.http_client import ai_http_post
from api.models import AssistantAIConfig, ChatMessage, ChatSession, User
from api.runtime.internal_http import internal_headers
from api.services.model_presets import normalize_model_presets, resolve_model_preset
from .admin import require_admin_user

router = APIRouter()
PREFIX = "/api/diagnostics"


def _check(id: str, label: str, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """运行单个检查，捕获异常并附上耗时。``fn`` 返回 {ok, detail, [skipped], [extra...]}。"""
    started = time.perf_counter()
    try:
        result = fn() or {}
    except Exception as exc:  # 任何检查失败都不应让整个自检 500
        result = {"ok": False, "detail": f"检查异常：{exc}"}
    result.setdefault("ok", False)
    result.setdefault("detail", "")
    result["id"] = id
    result["label"] = label
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


# ---------------------------------------------------------------------------
# 进程
# ---------------------------------------------------------------------------

def _runtime_check(base_url: str, *, in_process_note: str) -> Dict[str, Any]:
    if not base_url:
        return {"ok": True, "skipped": True, "detail": in_process_note}
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=8.0) as client:
        resp = client.get("/internal/health", headers=internal_headers())
    if resp.status_code != 200:
        return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    return {"ok": bool(body.get("ok", True)), "detail": "运行正常", "info": body}


def _process_checks() -> List[Dict[str, Any]]:
    def mcp_runtime() -> Dict[str, Any]:
        if settings.mcp_runtime_url:
            r = _runtime_check(settings.mcp_runtime_url, in_process_note="")
            info = r.get("info") or {}
            if r["ok"] and info.get("tools") is not None:
                r["detail"] = f"运行正常，已注册 {info.get('tools')} 个工具"
            return r
        from mcp_runtime.mcp import registry
        return {"ok": True, "detail": f"同进程运行，已注册 {len(registry.list_tools())} 个工具"}

    def connector_runtime() -> Dict[str, Any]:
        if settings.connector_runtime_url:
            r = _runtime_check(settings.connector_runtime_url, in_process_note="")
            info = r.get("info") or {}
            if r["ok"] and info.get("agents") is not None:
                r["detail"] = f"运行正常，在线端侧 Agent {info.get('agents')} 个"
            return r
        return {"ok": True, "detail": "与网关同进程运行（单体部署）"}

    def ai_runtime() -> Dict[str, Any]:
        if not settings.ai_runtime_url:
            return {"ok": True, "skipped": True, "detail": "未配置独立 AI 运行时地址，跳过探测"}
        return _runtime_check(settings.ai_runtime_url, in_process_note="")

    return [
        _check("gateway", "网关进程 (gateway)", lambda: {"ok": True, "detail": "当前进程，能响应即正常"}),
        _check("mcp_runtime", "MCP 运行时", mcp_runtime),
        _check("connector_runtime", "连接器运行时", connector_runtime),
        _check("ai_runtime", "AI 运行时", ai_runtime),
    ]


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

_REQUIRED_TABLES = ("user", "assistantaiconfig", "chatmessage", "chatsession")


def _database_checks() -> List[Dict[str, Any]]:
    def connect() -> Dict[str, Any]:
        with Session(engine) as session:
            session.execute(text("SELECT 1"))
        return {"ok": True, "detail": "连接正常"}

    def tables() -> Dict[str, Any]:
        present = set(inspect(engine).get_table_names())
        missing = [t for t in _REQUIRED_TABLES if t not in present]
        if missing:
            return {"ok": False, "detail": f"缺少关键表：{'、'.join(missing)}"}
        return {"ok": True, "detail": f"关键表齐全（共 {len(present)} 张表）"}

    def counts() -> Dict[str, Any]:
        with Session(engine) as session:
            users = session.exec(select(func.count()).select_from(User)).one()
            ais = session.exec(select(func.count()).select_from(AssistantAIConfig)).one()
            sessions = session.exec(select(func.count()).select_from(ChatSession)).one()
            messages = session.exec(select(func.count()).select_from(ChatMessage)).one()
        return {
            "ok": True,
            "detail": f"用户 {users} · AI 配置 {ais} · 会话 {sessions} · 消息 {messages}",
        }

    return [
        _check("db_connect", "数据库连接", connect),
        _check("db_tables", "关键数据表", tables),
        _check("db_counts", "数据量统计", counts),
    ]


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

async def _call_mcp(tool: str, user_id: int, args: Dict[str, Any]) -> Any:
    runtime_url = settings.mcp_runtime_url
    if runtime_url:
        async with httpx.AsyncClient(base_url=runtime_url.rstrip("/"), timeout=30.0) as client:
            resp = await client.post(
                "/internal/mcp/call",
                headers=internal_headers(),
                json={"tool": tool, "user_id": user_id, "ai_config_id": None, "arguments": args},
            )
            resp.raise_for_status()
            return resp.json()
    from mcp_runtime.mcp import registry
    return await registry.call(tool, user_id, args, None)


async def _mcp_checks(user_id: int) -> List[Dict[str, Any]]:
    from mcp_runtime.mcp import registry

    checks: List[Dict[str, Any]] = []

    # 1) 注册表已加载
    started = time.perf_counter()
    try:
        tools = registry.list_tools()
        ok = len(tools) > 0
        checks.append({
            "id": "mcp_registry", "label": "MCP 工具注册表", "ok": ok,
            "detail": f"已注册 {len(tools)} 个内置工具" if ok else "注册表为空",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        })
    except Exception as exc:
        checks.append({"id": "mcp_registry", "label": "MCP 工具注册表", "ok": False, "detail": f"读取失败：{exc}", "latency_ms": 0.0})
        tools = []

    # 2) 工具说明完整性
    missing_desc = [str(t.get("name") or "") for t in tools if not str(t.get("description") or "").strip()]
    checks.append({
        "id": "mcp_descriptions", "label": "工具说明完整性",
        "ok": len(missing_desc) == 0,
        "detail": "全部工具均有说明" if not missing_desc else f"{len(missing_desc)} 个工具缺少说明：{'、'.join(missing_desc[:5])}",
        "latency_ms": 0.0,
    })

    # 3) 调用通道端到端自检（describe_tool 无副作用）
    started = time.perf_counter()
    try:
        res = await _call_mcp("mcp.describe_tool", user_id, {"tool": "mcp.describe_tool"})
        payload = res.get("result", res) if isinstance(res, dict) else res
        name = (payload or {}).get("name") if isinstance(payload, dict) else None
        ok = name == "mcp.describe_tool"
        checks.append({
            "id": "mcp_call", "label": "MCP 调用通道", "ok": ok,
            "detail": "调用链路正常（已成功读取工具 schema）" if ok else "调用返回异常",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        })
    except Exception as exc:
        checks.append({"id": "mcp_call", "label": "MCP 调用通道", "ok": False, "detail": f"调用失败：{exc}", "latency_ms": round((time.perf_counter() - started) * 1000, 1)})

    return checks


# ---------------------------------------------------------------------------
# 连接器（端侧 Agent / 机器人）
# ---------------------------------------------------------------------------

def _connector_checks(user_id: int) -> List[Dict[str, Any]]:
    def agents() -> Dict[str, Any]:
        from api.device_presence import online_tool_defs_for_user
        defs = online_tool_defs_for_user(user_id) or {}
        n = len(defs)
        return {"ok": True, "detail": f"在线端侧工具 {n} 个" if n else "当前无在线端侧 Agent"}

    def bots() -> Dict[str, Any]:
        enabled: List[str] = []
        with Session(engine) as session:
            rows = session.exec(
                select(AssistantAIConfig).where(AssistantAIConfig.user_id == user_id)
            ).all()
        for cfg in rows:
            try:
                configs = json.loads(cfg.bot_configs or "{}")
            except Exception:
                configs = {}
            if not isinstance(configs, dict):
                continue
            for channel, conf in configs.items():
                if isinstance(conf, dict) and conf.get("enabled"):
                    enabled.append(f"{cfg.name}/{channel}")
        if not enabled:
            return {"ok": True, "skipped": True, "detail": "未配置已启用的机器人渠道"}
        return {"ok": True, "detail": f"已启用机器人 {len(enabled)} 个：{'、'.join(enabled[:6])}"}

    return [
        _check("connector_agents", "在线端侧 Agent", agents),
        _check("connector_bots", "机器人渠道配置", bots),
    ]


# ---------------------------------------------------------------------------
# 文件存储（KnowledgeBase）
# ---------------------------------------------------------------------------

def _storage_checks(user_id: int) -> List[Dict[str, Any]]:
    root = user_shared_knowledge_dir(user_id)

    def writable() -> Dict[str, Any]:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".diagnostics_selftest.tmp")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        try:
            with open(probe, "r", encoding="utf-8") as f:
                content = f.read()
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass
        if content != "ok":
            return {"ok": False, "detail": "写入后回读不一致"}
        return {"ok": True, "detail": f"可读写：{root}"}

    def prompt_files() -> Dict[str, Any]:
        def _count_md(sub: str) -> int:
            d = os.path.join(root, sub)
            if not os.path.isdir(d):
                return 0
            return len([n for n in os.listdir(d) if n.endswith(".md")])
        return {
            "ok": True,
            "detail": f"系统提示词 {_count_md('system')} 个 · 人格 {_count_md('personas')} 个 · MCP 说明目录{'存在' if os.path.isdir(os.path.join(root, 'mcp')) else '未生成'}",
        }

    return [
        _check("kb_writable", "KnowledgeBase 读写", writable),
        _check("kb_prompts", "提示词文件", prompt_files),
    ]


@router.get("/selftest")
async def diagnostics_selftest(user: User = Depends(require_admin_user)) -> Dict[str, Any]:
    """一键自检：分模块逐点检查并返回结构化结果。"""
    groups = [
        {"module": "process", "label": "进程", "checks": _process_checks()},
        {"module": "database", "label": "数据库", "checks": _database_checks()},
        {"module": "mcp", "label": "MCP", "checks": await _mcp_checks(user.id)},
        {"module": "connector", "label": "连接器（端侧 / 机器人）", "checks": _connector_checks(user.id)},
        {"module": "storage", "label": "文件存储 (KnowledgeBase)", "checks": _storage_checks(user.id)},
    ]
    total = 0
    passed = 0
    failed = 0
    for group in groups:
        for c in group["checks"]:
            if c.get("skipped"):
                continue
            total += 1
            if c.get("ok"):
                passed += 1
            else:
                failed += 1
    return {
        "ok": failed == 0,
        "summary": {"total": total, "passed": passed, "failed": failed},
        "groups": groups,
        "ran_at": time.time(),
    }


# ---------------------------------------------------------------------------
# 模型连通性
# ---------------------------------------------------------------------------

class ModelsTestRequest(BaseModel):
    prompt: Optional[str] = None
    ai_config_id: Optional[int] = None


def _probe_model(name: str, model: str, base_url: str, api_key: str, prompt: str) -> Dict[str, Any]:
    if not (api_key and base_url and model):
        return {"name": name, "model": model, "ok": False, "detail": "配置不完整（缺少 API Key / Base URL / 模型名）"}
    started = time.perf_counter()
    try:
        # 与真实聊天完全一致的流式请求：很多「端口代理 / 中转」对非流式支持不一致，
        # 用流式探测才能真实反映聊天能否跑通。
        resp = ai_http_post(
            base_url,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            stream=True,
            timeout=30,
        )
        if resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:200]
            except Exception:
                pass
            return {"name": name, "model": model, "base_url": base_url, "ok": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "detail": f"HTTP {resp.status_code}：{body}"}
        reply = ""
        lines_read = 0
        for raw in resp.iter_lines():
            lines_read += 1
            if lines_read > 300:
                break
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            choices = obj.get("choices") or []
            if choices:
                piece = str((choices[0].get("delta") or {}).get("content") or "")
                if piece:
                    reply += piece
                    if len(reply) >= 8:
                        break
        try:
            resp.close()
        except Exception:
            pass
        latency = round((time.perf_counter() - started) * 1000, 1)
        if reply:
            return {"name": name, "model": model, "base_url": base_url, "ok": True,
                    "latency_ms": latency, "reply": reply[:120], "detail": "流式响应正常"}
        return {"name": name, "model": model, "base_url": base_url, "ok": True,
                "latency_ms": latency, "detail": "连接已建立（流式返回成功，但未取到文本片段）"}
    except Exception as exc:
        return {"name": name, "model": model, "base_url": base_url, "ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "detail": f"请求失败：{exc}"}


@router.post("/models")
def diagnostics_models(req: ModelsTestRequest, user: User = Depends(require_admin_user)) -> Dict[str, Any]:
    """逐个测试已配置模型的连通性：指定 ai_config_id 时只测该 AI，否则测主脑 + 各 preset。"""
    prompt = (req.prompt or "回复一个字：好").strip() or "回复一个字：好"
    results: List[Dict[str, Any]] = []

    with Session(engine) as session:
        fresh_user = session.get(User, user.id) or user
        if req.ai_config_id is not None:
            cfg = session.exec(
                select(AssistantAIConfig).where(
                    AssistantAIConfig.id == req.ai_config_id,
                    AssistantAIConfig.user_id == user.id,
                )
            ).first()
            if not cfg:
                return {"ok": False, "models": [], "detail": "未找到该 AI 配置"}
            api_key, base_url, model = resolve_model_preset(fresh_user, cfg)
            results.append(_probe_model(cfg.name, model, base_url, api_key, prompt))
        else:
            presets = normalize_model_presets(getattr(fresh_user, "model_presets", ""), fresh_user)
            seen = set()
            for preset in presets[:8]:
                key = (preset["model"], preset["base_url"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(_probe_model(preset["name"], preset["model"], preset["base_url"], preset["api_key"], prompt))
            if not results:
                results.append(_probe_model("主脑模型", getattr(fresh_user, "admin_model", ""), getattr(fresh_user, "admin_base_url", ""), getattr(fresh_user, "admin_api_key", ""), prompt))

    return {"ok": all(r["ok"] for r in results) if results else False, "models": results}


# ---------------------------------------------------------------------------
# 维护操作：用注册表（中文）重新生成当前用户的 MCP 工具说明文件
# ---------------------------------------------------------------------------

@router.post("/reseed-mcp-docs")
def diagnostics_reseed_mcp_docs(user: User = Depends(require_admin_user)) -> Dict[str, Any]:
    """用注册表里的中文说明覆盖重写当前用户的 KnowledgeBase/mcp/*.md。

    工具说明的运行时真相源是每用户的 md 文件，由注册表首次播种、之后不再覆盖。
    当注册表里的内置说明更新（如改为中文）后，老用户的文件仍是旧内容；调用本接口
    可强制按当前注册表重新生成。注意：会覆盖对这些工具说明做过的手动修改。
    """
    from mcp_runtime.mcp import registry
    from api.services import kb_store
    from api.services.librarian_service import _mcp_schema_parameter_rows

    count = 0
    failed: List[str] = []
    for tool in registry.list_tools():
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        try:
            params = _mcp_schema_parameter_rows(name, schema, None)
            kb_store.write_mcp_tool(
                user.id,
                name,
                str(tool.get("description") or "").strip(),
                params or [],
                bool(tool.get("destructive")),
            )
            count += 1
        except Exception:
            failed.append(name)
    return {
        "ok": not failed,
        "regenerated": count,
        "failed": failed,
        "detail": f"已重新生成 {count} 个工具说明" + (f"，{len(failed)} 个失败" if failed else ""),
    }
