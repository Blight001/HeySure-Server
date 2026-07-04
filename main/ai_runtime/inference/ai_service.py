import json
import os
import time
from typing import Dict, Optional, Tuple

from sqlmodel import Session, select

from api.database import engine
from api.models import AIRuntimeStatus, AssistantAIConfig, ChatMessage, TokenUsageSnapshot
from api.core.config import DATA_DIR


GENERIC_ASSISTANT_PROMPT = "你是一个辅助管理员，帮助用户处理项目任务。"


def _legacy_switch_file_paths(user_id: int) -> list[str]:
    server_dir = os.path.dirname(DATA_DIR)
    return [
        os.path.join(DATA_DIR, "workspace", str(user_id), "SystemSetting", "ai_switches.json"),
        os.path.join(server_dir, "api", "data", "workspace", str(user_id), "SystemSetting", "ai_switches.json"),
    ]


def migrate_legacy_switch_files_to_db() -> dict:
    """Import old ai_switches.json files once, then remove them.

    Runtime state now lives in AssistantAIConfig and AIRuntimeStatus. This
    migration keeps existing deployments from losing a manual switch-file edit.
    """
    workspace_root = os.path.join(DATA_DIR, "workspace")
    api_workspace_root = os.path.join(os.path.dirname(DATA_DIR), "api", "data", "workspace")
    user_ids: set[int] = set()
    for root in (workspace_root, api_workspace_root):
        if not os.path.exists(root):
            continue
        for name in os.listdir(root):
            if name.isdigit():
                user_ids.add(int(name))

    imported = 0
    removed = 0
    with Session(engine) as session:
        for user_id in sorted(user_ids):
            configs = session.exec(
                select(AssistantAIConfig).where(AssistantAIConfig.user_id == user_id)
            ).all()
            cfg_map = {cfg.switch_key: cfg for cfg in configs}
            changed = False

            for switch_path in _legacy_switch_file_paths(user_id):
                if not os.path.exists(switch_path):
                    continue
                try:
                    with open(switch_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}

                if isinstance(data, dict):
                    for key, value in data.items():
                        if not isinstance(value, dict):
                            continue
                        cfg = cfg_map.get(str(key))
                        if not cfg:
                            continue
                        enabled = bool(value.get("enabled", cfg.enabled))
                        mcp_enabled = bool(value.get("mcp_enabled", cfg.mcp_enabled))
                        if cfg.enabled != enabled or cfg.mcp_enabled != mcp_enabled:
                            cfg.enabled = enabled
                            cfg.mcp_enabled = mcp_enabled
                            cfg.updated_at = time.time()
                            session.add(cfg)
                            changed = True
                            imported += 1

                        status = session.exec(
                            select(AIRuntimeStatus).where(
                                AIRuntimeStatus.user_id == user_id,
                                AIRuntimeStatus.ai_config_id == cfg.id,
                                AIRuntimeStatus.ai_kind == "assistant",
                            )
                        ).first()
                        if not status:
                            status = AIRuntimeStatus(
                                user_id=user_id,
                                ai_config_id=cfg.id,
                                ai_kind="assistant",
                            )
                        status.running = enabled
                        status.mcp_enabled = mcp_enabled
                        status.updated_at = time.time()
                        session.add(status)
                        changed = True

                try:
                    os.remove(switch_path)
                    removed += 1
                except FileNotFoundError:
                    pass

            if changed:
                session.commit()

    return {"imported": imported, "removed": removed}


def _default_ai_specs():
    return [
        {
            "switch_key": "assistant_default",
            "name": "阿尔法",
            "description": "数字社会核心管理员（兼图书管理员），负责总体调度、战略指引与知识传承。",
            "ai_role": "digital_member",
            "digital_member_role": "manager",
            "is_librarian": True,
            "platform": "服务器",
            "generation": 1,
            "token_limit": 50000,
            "lifecycle_status": "working",
            "current_behavior": "维护学习总结数据库与调度策略...",
            "project_id": "p-memory",
            "project_name": "学习总结数据库",
            "prompt": "",
            "sort_order": 1,
        },
        {
            "switch_key": "assistant_worker_file",
            "name": "辅助管理员·总督",
            "description": "辅助主脑进行项目治理、归档与流程巡检。",
            "ai_role": "assistant_admin",
            "digital_member_role": "member",
            "platform": "服务器",
            "generation": 1,
            "token_limit": 0,
            "lifecycle_status": "working",
            "current_behavior": "正在向主脑同步知识库...",
            "project_id": "p-files",
            "project_name": "文件项目管理系统",
            "prompt": "",
            "sort_order": 10,
        },
        {
            "switch_key": "assistant_beta_browser",
            "name": "贝塔",
            "description": "普通数字成员，负责浏览器端任务、网页操作与信息采集。",
            "ai_role": "digital_member",
            "digital_member_role": "member",
            "platform": "浏览器",
            "generation": 1,
            "token_limit": 10000,
            "lifecycle_status": "working",
            "current_behavior": "等待浏览器端任务...",
            "project_id": None,
            "project_name": "",
            "prompt": "",
            "sort_order": 20,
        },
        {
            "switch_key": "assistant_delta_windows",
            "name": "德尔塔",
            "description": "普通数字成员，负责 Windows 端任务、桌面操作与本机协作。",
            "ai_role": "digital_member",
            "digital_member_role": "member",
            "platform": "Windows端",
            "generation": 1,
            "token_limit": 10000,
            "lifecycle_status": "working",
            "current_behavior": "等待 Windows 端任务...",
            "project_id": None,
            "project_name": "",
            "prompt": "",
            "sort_order": 30,
        },
    ]


def ensure_default_configs(session: Session, user_id: int) -> list[AssistantAIConfig]:
    created = []
    _seed_persona_prompts: list = []
    existing = session.exec(
        select(AssistantAIConfig).where(AssistantAIConfig.user_id == user_id)
    ).all()

    # Existing users keep their current AI configs unchanged.
    if existing:
        pass
    else:
        # Seed defaults only for first-time bootstrap.
        for spec in _default_ai_specs():
            # 人格 Prompt 列已删除：先记录，建表后写入 personas 文件。
            row = AssistantAIConfig(
                user_id=user_id,
                switch_key=spec["switch_key"],
                name=spec["name"],
                description=spec["description"],
                avatar="ai_avatars1.png",
                ai_role=spec["ai_role"],
                digital_member_role=spec.get("digital_member_role", "member"),
                is_librarian=bool(spec.get("is_librarian", False)),
                platform=spec["platform"],
                generation=spec["generation"],
                token_limit=spec["token_limit"],
                lifecycle_status=spec["lifecycle_status"],
                current_behavior=spec["current_behavior"],
                project_id=spec["project_id"],
                project_name=spec["project_name"],
                workspace_root=None,
                sort_order=spec["sort_order"],
            )
            session.add(row)
            created.append(row)
            _seed_persona_prompts.append((row, spec.get("prompt", GENERIC_ASSISTANT_PROMPT)))

    if created:
        session.commit()
        for row in created:
            session.refresh(row)

        # 人格 Prompt 真相源为文件：写入 personas/*.md。
        try:
            from api.services.knowledge import kb_store

            for row, prompt in _seed_persona_prompts:
                kb_store.write_persona(user_id, row, prompt=prompt)
        except Exception:
            pass

    if created:
        # 工具箱默认自动绑定：新建的 AI 一律绑定工具箱（多绑），获得默认工具集。
        # 之后绑定关系完全由用户操作控制，不再做全量自愈。
        try:
            from tools.engine import bind_config_to_toolbox

            for row in created:
                if row.id:
                    bind_config_to_toolbox(user_id, row.id)
        except Exception:
            pass
    return session.exec(
        select(AssistantAIConfig).where(AssistantAIConfig.user_id == user_id)
    ).all()


def ensure_default_ai_for_user(session: Session, user_id: int) -> None:
    cfgs = ensure_default_configs(session, user_id)
    changed = False
    for cfg in cfgs:
        status = session.exec(
            select(AIRuntimeStatus).where(
                AIRuntimeStatus.user_id == user_id,
                AIRuntimeStatus.ai_config_id == cfg.id,
                AIRuntimeStatus.ai_kind == "assistant",
            )
        ).first()
        if not status:
            status = AIRuntimeStatus(
                user_id=user_id,
                ai_config_id=cfg.id,
                ai_kind="assistant",
                running=cfg.enabled,
                mcp_enabled=cfg.mcp_enabled,
            )
            session.add(status)
            changed = True
    if changed:
        session.commit()
    from library import engine as workshop_engine

    workshop_engine.ensure_presence_for_user(user_id)


def align_token_snapshots_with_history() -> dict:
    """On server startup, reduce snapshot totals when they exceed chat-history usage.

    Uses DB-side aggregation instead of loading the entire ChatMessage table
    into memory. This was causing very slow gateway restarts ("卡死") when
    there is non-trivial chat history.
    """
    kinds = {"assistant", "core"}
    changed_rows = 0
    deleted_rows = 0

    with Session(engine) as session:
        # Aggregate directly in Postgres — avoids loading every ChatMessage row.
        # Bucket format must exactly match the legacy "%Y-%m-%d".
        from sqlalchemy import text

        kind_list = ",".join(f"'{k}'" for k in kinds)
        history_rows = session.exec(
            text(
                f"""
                SELECT user_id,
                       ai_kind,
                       ai_config_id,
                       to_char(to_timestamp(created_at), 'YYYY-MM-DD') AS bucket,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt,
                       COALESCE(SUM(completion_tokens), 0) AS completion,
                       COALESCE(SUM(total_tokens), 0) AS total
                FROM chatmessage
                WHERE ai_kind IN ({kind_list})
                GROUP BY user_id, ai_kind, ai_config_id, bucket
                """
            )
        ).all()

        history_usage: Dict[Tuple[int, str, Optional[int], str], Dict[str, int]] = {}
        for row in history_rows:
            # row is a Row object; access by index or attribute
            user_id = row[0]
            ai_kind = row[1]
            ai_config_id = row[2]
            bucket = row[3]
            prompt = int(row[4] or 0)
            completion = int(row[5] or 0)
            total = int(row[6] or 0)

            if prompt <= 0 and completion <= 0 and total <= 0:
                continue
            key = (user_id, ai_kind, ai_config_id, bucket)
            history_usage[key] = {"prompt": prompt, "completion": completion, "total": total}

        snapshots = session.exec(
            select(TokenUsageSnapshot).where(TokenUsageSnapshot.ai_kind.in_(list(kinds)))
        ).all()

        for row in snapshots:
            key = (row.user_id, row.ai_kind, row.ai_config_id, row.bucket)
            history = history_usage.get(key, {"prompt": 0, "completion": 0, "total": 0})

            next_prompt = min(int(row.prompt_tokens or 0), int(history["prompt"]))
            next_completion = min(int(row.completion_tokens or 0), int(history["completion"]))
            next_total = min(int(row.total_tokens or 0), int(history["total"]))

            if next_prompt <= 0 and next_completion <= 0 and next_total <= 0:
                if (row.prompt_tokens or 0) != 0 or (row.completion_tokens or 0) != 0 or (row.total_tokens or 0) != 0:
                    session.delete(row)
                    deleted_rows += 1
                continue

            if (
                next_prompt != int(row.prompt_tokens or 0)
                or next_completion != int(row.completion_tokens or 0)
                or next_total != int(row.total_tokens or 0)
            ):
                row.prompt_tokens = next_prompt
                row.completion_tokens = next_completion
                row.total_tokens = next_total
                row.updated_at = time.time()
                session.add(row)
                changed_rows += 1

        if changed_rows > 0 or deleted_rows > 0:
            session.commit()

    return {
        "changed_rows": changed_rows,
        "deleted_rows": deleted_rows,
    }
