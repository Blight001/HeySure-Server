"""Legacy one-time database adoption shim (pre-Alembic).

⚠️ This module is no longer part of the steady-state boot path. Alembic is the
single source of schema truth now (see ``api.db`` / ``migrations/`` /
``doc/db-migrations.md``). It is invoked **at most once per database**, by
``api.db._legacy_adopt``, to bring a pre-Alembic database fully current the
exact way the old boot did — adding missing columns and consolidating legacy
data — after which the database is stamped at Alembic head and this code never
runs again for it.

It is kept (rather than deleted) so existing deployments adopt Alembic without
manual intervention. Once every deployment has been adopted, this module can be
removed (tracked in doc/db-migrations.md). New schema/data changes must be
authored as Alembic revisions, never added here.

Each migration is idempotent: it inspects ``PRAGMA table_info`` first and only
runs when the target column is missing.
"""

import json
import logging
import os
import sqlite3
import time
from typing import Dict

from .config import SQLITE_FILE, database_dialect


logger = logging.getLogger(__name__)

# Imported lazily inside ``run_pending_migrations`` to avoid a hard dependency
# on the models package at import time (the package itself is loaded before
# ``SQLModel.metadata.create_all``).


def run_data_consolidations(engine) -> None:
    """Dialect-agnostic data migrations executed via SQLAlchemy engine.

    Unlike ``run_pending_migrations`` (legacy SQLite-only ALTER TABLE
    patches) these run on every backend — copying rows out of deprecated
    per-bot tables into ``botsessionroute`` etc.
    """
    _consolidate_bot_session_routes(engine)
    _consolidate_assistantaiconfig_bot_configs(engine)
    _consolidate_prompts_to_files(engine)
    _cleanup_dead_workspace_folders(engine)
    _backfill_toolbox_bindings(engine)


def _backfill_toolbox_bindings(engine) -> None:
    """给存量 AI 补绑工具箱（``toolbox_builtin_<user>``）。

    工具箱改为绑定制门禁后，未绑定的 AI 会被挡在工具箱工具之外。新 AI 在创建时
    自动绑定；这里覆盖"旧库首次纳管（stamp 跳过 Alembic 迁移）"的路径。幂等：仅为
    尚无工具箱绑定的 (user, ai) 新增行，不动用户手动解绑的状态。
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if "assistantaiconfig" not in tables or "workshopaibinding" not in tables:
        return
    now = time.time()
    with engine.begin() as conn:
        configs = conn.execute(text("SELECT id, user_id FROM assistantaiconfig")).fetchall()
        existing = conn.execute(
            text(
                "SELECT user_id, ai_config_id FROM workshopaibinding "
                "WHERE device_id LIKE 'toolbox_builtin_%'"
            )
        ).fetchall()
        already = {(row[0], row[1]) for row in existing}
        for cfg_id, user_id in configs:
            if user_id is None or cfg_id is None or (user_id, cfg_id) in already:
                continue
            conn.execute(
                text(
                    "INSERT INTO workshopaibinding "
                    "(user_id, device_id, ai_config_id, created_at, updated_at) "
                    "VALUES (:user_id, :device_id, :ai_config_id, :created_at, :updated_at)"
                ),
                {
                    "user_id": user_id,
                    "device_id": f"toolbox_builtin_{user_id}",
                    "ai_config_id": cfg_id,
                    "created_at": now,
                    "updated_at": now,
                },
            )


# 已迁出数据库、真相源改为 KnowledgeBase 文件的文本列。
_USER_PROMPT_COLS = (
    "admin_prompt",
    "mcp_call_method",
    "mcp_namespace_hints",
    "mcp_dynamic_rule",
    "mcp_format_error_hint",
    "default_start_task_prompt",
    "default_resume_task_prompt",
    "default_supervision_prompt",
    "default_compression_prompt",
    "prompt_ai_message_notify",
    "prompt_ai_message_inquiry",
    "prompt_ai_message_inquiry_reminder",
    "prompt_ai_message_reply",
    "prompt_ai_message_chitchat",
    "prompt_ai_message_reply_success",
    "prompt_user_message_notice",
)


def _consolidate_prompts_to_files(engine) -> None:
    """把人格 / 系统提示从数据库迁到 KnowledgeBase 文件，然后物理删除冗余列。

    顺序严格"先迁库再删"：对每个用户/AI 先把现有列值导出成 ``system/*.md`` /
    ``personas/*.md``（文件已存在则跳过，绝不覆盖用户已编辑的文件），全部导出
    完成后才 ``DROP COLUMN``。幂等：列已删后整段跳过；导出失败则不删该用户的列。
    """
    from sqlalchemy import inspect, text

    from api.services import kb_store

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if "user" not in tables or "assistantaiconfig" not in tables:
        return

    user_cols = {c["name"] for c in insp.get_columns("user")}
    cfg_cols = {c["name"] for c in insp.get_columns("assistantaiconfig")}
    present_user_prompt_cols = [c for c in _USER_PROMPT_COLS if c in user_cols]
    cfg_has_prompt = "prompt" in cfg_cols

    # 已经没有任何待迁列 → 迁移早已完成。
    if not present_user_prompt_cols and not cfg_has_prompt:
        return

    exported_ok = True

    # 1) 导出用户系统提示 → system/*.md
    if present_user_prompt_cols:
        select_cols = ", ".join(["id", *present_user_prompt_cols])
        try:
            with engine.begin() as conn:
                rows = conn.execute(text(f"SELECT {select_cols} FROM \"user\"")).mappings().all()
            for row in rows:
                uid = int(row["id"])
                try:
                    kb_store._ensure_layout(uid)
                    for col in present_user_prompt_cols:
                        value = row.get(col)
                        if value is None or str(value) == "":
                            continue
                        if kb_store.read_system_prompt(uid, col) is None:
                            kb_store.write_system_prompt(uid, col, value)
                except Exception as exc:
                    exported_ok = False
                    logger.exception(f"export system prompts for user {uid} failed: {exc}")
        except Exception as exc:
            exported_ok = False
            logger.exception(f"read user prompt columns failed: {exc}")

    # 2) 导出 AI 人格 prompt → personas/*.md。任务流程 prompt 由
    #    system/default_*.md 统一控制，不再导出 AI 级覆盖。
    if cfg_has_prompt:
        sac = "system_auto_control" if "system_auto_control" in cfg_cols else None
        sel = ["id", "user_id", "name", "ai_role", "prompt"] + ([sac] if sac else [])
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(f"SELECT {', '.join(sel)} FROM assistantaiconfig")
                ).mappings().all()
            for row in rows:
                try:
                    kb_store.seed_persona_raw(
                        int(row["user_id"]),
                        row["id"],
                        str(row.get("name") or ""),
                        str(row.get("ai_role") or ""),
                        str(row.get("prompt") or ""),
                        str(row.get(sac) or "{}") if sac else "{}",
                    )
                except Exception as exc:
                    exported_ok = False
                    logger.exception(f"export persona for ai {row.get('id')} failed: {exc}")
        except Exception as exc:
            exported_ok = False
            logger.exception(f"read assistantaiconfig.prompt failed: {exc}")

    if not exported_ok:
        logger.warning("prompt->file export had failures; skipping DROP COLUMN this boot")
        return

    # 3) 全部导出成功后，物理删除冗余列（best-effort，逐列）。
    for col in present_user_prompt_cols:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "user" DROP COLUMN {col}'))
        except Exception as exc:
            logger.exception(f"drop user.{col} failed: {exc}")
    if cfg_has_prompt:
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE assistantaiconfig DROP COLUMN prompt"))
        except Exception as exc:
            logger.exception(f"drop assistantaiconfig.prompt failed: {exc}")


_LEGACY_FEISHU_COLS = (
    "feishu_enabled",
    "feishu_webhook_url",
    "feishu_app_id",
    "feishu_app_secret",
    "feishu_verification_token",
    "feishu_default_receive_id",
    "feishu_default_receive_id_type",
)
_LEGACY_QQ_COLS = (
    "qq_enabled",
    "qq_app_id",
    "qq_app_secret",
    "qq_sandbox",
    "qq_default_target_id",
    "qq_default_target_type",
)


def _consolidate_assistantaiconfig_bot_configs(engine) -> None:
    """Backfill ``assistantaiconfig.bot_configs`` from the deprecated flat
    columns, then drop those columns.

    The flat columns (``feishu_*`` / ``qq_*``) were the original storage
    for per-bot credentials. We're moving them into a single ``bot_configs``
    JSON column keyed by channel so adding a new bot doesn't require a
    schema migration each time.

    The migration is idempotent:
    - If ``bot_configs`` is missing it's added.
    - For rows whose ``bot_configs`` is empty (``{}`` / NULL) we build the
      JSON from whatever flat columns still exist and write it.
    - Flat columns are dropped at the end via ``ALTER TABLE DROP COLUMN``
      (works on Postgres + SQLite ≥ 3.35). Drops that fail are logged but
      do not abort the migration.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "assistantaiconfig" not in set(insp.get_table_names()):
        return
    columns = {col["name"] for col in insp.get_columns("assistantaiconfig")}

    # 1. Ensure bot_configs exists.
    if "bot_configs" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE assistantaiconfig ADD COLUMN bot_configs TEXT NOT NULL DEFAULT '{}'"
                )
            )
        columns.add("bot_configs")

    # 2. Backfill any row whose bot_configs is empty.
    flat_present = [c for c in (*_LEGACY_FEISHU_COLS, *_LEGACY_QQ_COLS) if c in columns]
    if flat_present:
        select_cols = ", ".join(["id", "bot_configs", *flat_present])
        with engine.begin() as conn:
            rows = conn.execute(
                text(f"SELECT {select_cols} FROM assistantaiconfig")
            ).mappings().all()
            for row in rows:
                current = str(row.get("bot_configs") or "").strip() or "{}"
                try:
                    parsed = json.loads(current)
                except Exception:
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {}
                if parsed:
                    continue  # already migrated for this row
                feishu_slice = {
                    "enabled": bool(row.get("feishu_enabled")) if "feishu_enabled" in columns else False,
                    "webhook_url": row.get("feishu_webhook_url") or "" if "feishu_webhook_url" in columns else "",
                    "app_id": row.get("feishu_app_id") or "" if "feishu_app_id" in columns else "",
                    "app_secret": row.get("feishu_app_secret") or "" if "feishu_app_secret" in columns else "",
                    "verification_token": row.get("feishu_verification_token") or "" if "feishu_verification_token" in columns else "",
                    "default_receive_id": row.get("feishu_default_receive_id") or "" if "feishu_default_receive_id" in columns else "",
                    "default_receive_id_type": row.get("feishu_default_receive_id_type") or "chat_id" if "feishu_default_receive_id_type" in columns else "chat_id",
                }
                raw_target_type = (
                    row.get("qq_default_target_type") or ""
                    if "qq_default_target_type" in columns
                    else ""
                )
                if raw_target_type not in {"c2c", "group", "channel", "dm"}:
                    raw_target_type = "c2c"
                qq_slice = {
                    "enabled": bool(row.get("qq_enabled")) if "qq_enabled" in columns else False,
                    "app_id": row.get("qq_app_id") or "" if "qq_app_id" in columns else "",
                    "app_secret": row.get("qq_app_secret") or "" if "qq_app_secret" in columns else "",
                    "sandbox": bool(row.get("qq_sandbox")) if "qq_sandbox" in columns else False,
                    "default_target_id": row.get("qq_default_target_id") or "" if "qq_default_target_id" in columns else "",
                    "default_target_type": raw_target_type,
                }
                payload = json.dumps(
                    {"feishu": feishu_slice, "qq": qq_slice}, ensure_ascii=False
                )
                conn.execute(
                    text("UPDATE assistantaiconfig SET bot_configs = :p WHERE id = :id"),
                    {"p": payload, "id": row["id"]},
                )

    # 3. Drop the legacy columns. Best-effort per-column so a partial
    #    failure (older SQLite, missing column) doesn't strand the rest.
    for col in (*_LEGACY_FEISHU_COLS, *_LEGACY_QQ_COLS):
        if col not in columns:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE assistantaiconfig DROP COLUMN {col}"))
        except Exception as exc:
            # Surface but do not raise: production may need a manual rebuild
            # on very old SQLite that predates DROP COLUMN.
            logger.exception(f"drop column {col} failed: {exc}")


def _cleanup_dead_workspace_folders(engine) -> None:
    """Remove workspace subfolders that no longer back any feature.

    ``BrainCore`` / ``EvolutionArena`` / ``SystemSetting`` / ``Valhalla``:
    never (or no longer) file-backed — evolution and settings live in the
    database, and the legacy generational-handoff (Valhalla) feature has been
    removed in favour of automatic conversation compression.

    Best-effort: failures are logged and never abort startup.
    """
    import os

    from .config import WORKSPACE_DIR, user_workspace_dir

    if not os.path.isdir(WORKSPACE_DIR):
        return

    for name in os.listdir(WORKSPACE_DIR):
        if not name.isdigit():
            continue
        user_id = int(name)
        user_dir = user_workspace_dir(user_id)
        for dead in ("BrainCore", "EvolutionArena", "SystemSetting", "Valhalla"):
            _rmtree_quiet(os.path.join(user_dir, dead))


def _rmtree_quiet(path: str) -> None:
    import os
    import shutil

    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:
        logger.exception(f"failed to remove legacy workspace folder {path}: {exc}")


def _read_text_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _read_json_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _consolidate_bot_session_routes(engine) -> None:
    """Copy legacy ``feishusessionroute`` + ``qqsessionroute`` rows into the
    unified ``botsessionroute`` table.

    Idempotent: rows whose (channel, user, ai_config, ai_kind, session_id)
    already exist in the new table are skipped, so repeat boots do not
    duplicate. The legacy tables are NOT dropped — they stay as a safety
    net until a separate cleanup pass removes them.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    if "botsessionroute" not in existing_tables:
        return  # create_all hasn't materialized the new table yet

    def _copy(channel: str, src_table: str, build_target: callable) -> None:
        if src_table not in existing_tables:
            return
        with engine.begin() as conn:
            rows = conn.execute(text(f"SELECT * FROM {src_table}")).mappings().all()
            for row in rows:
                exists = conn.execute(
                    text(
                        "SELECT id FROM botsessionroute "
                        "WHERE channel = :channel AND user_id = :uid "
                        "AND ai_config_id = :cid AND ai_kind = :kind "
                        "AND session_id = :sid LIMIT 1"
                    ),
                    {
                        "channel": channel,
                        "uid": row["user_id"],
                        "cid": row["ai_config_id"],
                        "kind": row["ai_kind"],
                        "sid": row["session_id"],
                    },
                ).first()
                if exists:
                    continue
                target_json, extras = build_target(row)
                conn.execute(
                    text(
                        "INSERT INTO botsessionroute "
                        "(channel, user_id, ai_config_id, ai_kind, session_id, target_json, "
                        " source_message_id, source_event_id, next_msg_seq, created_at, updated_at) "
                        "VALUES (:channel, :uid, :cid, :kind, :sid, :tj, "
                        " :smid, :seid, :seq, :ca, :ua)"
                    ),
                    {
                        "channel": channel,
                        "uid": row["user_id"],
                        "cid": row["ai_config_id"],
                        "kind": row["ai_kind"],
                        "sid": row["session_id"],
                        "tj": target_json,
                        "smid": extras.get("source_message_id", ""),
                        "seid": extras.get("source_event_id", ""),
                        "seq": extras.get("next_msg_seq", 1),
                        "ca": row.get("created_at"),
                        "ua": row.get("updated_at"),
                    },
                )

    def _build_feishu(row) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "receive_id": row.get("receive_id", "") or "",
                    "receive_id_type": row.get("receive_id_type", "chat_id") or "chat_id",
                },
                ensure_ascii=False,
            ),
            {},
        )

    def _build_qq(row) -> tuple[str, dict]:
        return (
            json.dumps(
                {
                    "target_id": row.get("target_id", "") or "",
                    "target_type": row.get("target_type", "c2c") or "c2c",
                },
                ensure_ascii=False,
            ),
            {
                "source_message_id": row.get("source_message_id", "") or "",
                "source_event_id": row.get("source_event_id", "") or "",
                "next_msg_seq": int(row.get("next_msg_seq", 1) or 1),
            },
        )

    _copy("feishu", "feishusessionroute", _build_feishu)
    _copy("qq", "qqsessionroute", _build_qq)


def _migrate_rename_device_tables() -> None:
    """端侧设备表/列 agent->device 改名（与 Alembic 同名 revision 对齐）。

    供 legacy-adopt 路径（pre-Alembic 老库）使用：renames 端侧设备相关表与列
    endpointagentpresence/agentaibinding/agenttypemcppermission ->
    devicepresence/deviceaibinding/devicetypemcppermission，列 agent_id->device_id、
    agent_type->device_type。幂等：仅当旧表存在且新表不存在时执行。
    """
    from ..database import engine
    from sqlalchemy import inspect

    renames = [
        ("endpointagentpresence", "devicepresence",
         [("agent_id", "device_id"), ("agent_type", "device_type")],
         [("ix_endpointagentpresence_agent_id", "ix_devicepresence_device_id"),
          ("ix_endpointagentpresence_ai_config_id", "ix_devicepresence_ai_config_id"),
          ("ix_endpointagentpresence_online", "ix_devicepresence_online"),
          ("ix_endpointagentpresence_user_id", "ix_devicepresence_user_id")]),
        ("agentaibinding", "deviceaibinding",
         [("agent_id", "device_id")],
         [("ix_agentaibinding_agent_id", "ix_deviceaibinding_device_id"),
          ("ix_agentaibinding_ai_config_id", "ix_deviceaibinding_ai_config_id"),
          ("ix_agentaibinding_user_id", "ix_deviceaibinding_user_id")]),
        ("agenttypemcppermission", "devicetypemcppermission",
         [("agent_id", "device_id"), ("agent_type", "device_type")],
         [("ix_agenttypemcppermission_agent_id", "ix_devicetypemcppermission_device_id"),
          ("ix_agenttypemcppermission_agent_type", "ix_devicetypemcppermission_device_type"),
          ("ix_agenttypemcppermission_ai_config_id", "ix_devicetypemcppermission_ai_config_id"),
          ("ix_agenttypemcppermission_user_id", "ix_devicetypemcppermission_user_id")]),
    ]
    is_pg = database_dialect() == "postgresql"
    existing = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for old_t, new_t, cols, idxs in renames:
            if old_t not in existing or new_t in existing:
                continue
            conn.exec_driver_sql(f'ALTER TABLE "{old_t}" RENAME TO "{new_t}"')
            for old_c, new_c in cols:
                conn.exec_driver_sql(f'ALTER TABLE "{new_t}" RENAME COLUMN "{old_c}" TO "{new_c}"')
            if is_pg:
                for old_i, new_i in idxs:
                    conn.exec_driver_sql(f'ALTER INDEX IF EXISTS "{old_i}" RENAME TO "{new_i}"')

    # 表名不变、仅列 agent_id->device_id 的两张表（工坊绑定 / 端侧调度任务）。
    # 按列存在性幂等：旧列在且新列不在才改。
    col_only = [
        ("workshopaibinding", "agent_id", "device_id",
         "ix_workshopaibinding_agent_id", "ix_workshopaibinding_device_id"),
        ("agentdispatchtask", "agent_id", "device_id", None, None),
    ]
    refreshed = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for table, old_c, new_c, old_i, new_i in col_only:
            if table not in refreshed:
                continue
            try:
                cols = {c["name"] for c in inspect(engine).get_columns(table)}
            except Exception:
                continue
            if old_c not in cols or new_c in cols:
                continue
            conn.exec_driver_sql(f'ALTER TABLE "{table}" RENAME COLUMN "{old_c}" TO "{new_c}"')
            if is_pg and old_i:
                conn.exec_driver_sql(f'ALTER INDEX IF EXISTS "{old_i}" RENAME TO "{new_i}"')


def run_pending_migrations() -> None:
    _migrate_assistantaiconfig_strip_markdown_symbols()
    _migrate_user_email()
    _migrate_user_role()
    _migrate_endpointagentpresence_tool_defs()
    _migrate_agenttypemcppermission_agent_id()
    _migrate_agenttypemcppermission_nullable_ai_config()
    _migrate_assistantaiconfig_strip_endpoint_mcp_tools()
    _migrate_assistantaiconfig_prune_unknown_mcp_tools()
    _migrate_user_role_mcp_permissions_rename()
    _migrate_chatmessagemedia_message_cascade()
    _migrate_rename_device_tables()
    # Only run for SQLite. Postgres deployments either start fresh or are
    # seeded by the migration script, both of which produce a current schema.
    if database_dialect() != "sqlite":
        return
    if not SQLITE_FILE.endswith(".db") or not os.path.exists(SQLITE_FILE):
        return
    from ..models.defaults import (
        DEFAULT_AI_MESSAGE_CHITCHAT_TEMPLATE,
        DEFAULT_AI_MESSAGE_INQUIRY_TEMPLATE,
        DEFAULT_AI_MESSAGE_INQUIRY_REMINDER,
        DEFAULT_AI_MESSAGE_NOTIFY_TEMPLATE,
        DEFAULT_AI_MESSAGE_REPLY_SUCCESS,
        DEFAULT_AI_MESSAGE_REPLY_TEMPLATE,
        DEFAULT_COMPRESSION_PROMPT,
        DEFAULT_MCP_CALL_METHOD,
        DEFAULT_MCP_DYNAMIC_RULE,
        DEFAULT_MCP_FORMAT_ERROR_HINT,
        DEFAULT_MCP_NAMESPACE_HINTS,
        DEFAULT_MODEL_PRESETS,
        DEFAULT_RESUME_TASK_PROMPT,
        DEFAULT_START_TASK_PROMPT,
        DEFAULT_SUPERVISION_PROMPT,
        DEFAULT_UI_BRAIN_VIEW_MODE,
        DEFAULT_UI_FONT_SIZE,
        DEFAULT_UI_THEME_MODE,
        DEFAULT_USER_MESSAGE_NOTICE,
    )

    conn = sqlite3.connect(SQLITE_FILE)
    try:
        cursor = conn.cursor()

        _migrate_chatmessage(cursor)
        _migrate_chatrun(cursor)
        _migrate_user(
            cursor,
            mcp_call_method=DEFAULT_MCP_CALL_METHOD,
            mcp_namespace_hints=DEFAULT_MCP_NAMESPACE_HINTS,
            mcp_dynamic_rule=DEFAULT_MCP_DYNAMIC_RULE,
            mcp_format_error_hint=DEFAULT_MCP_FORMAT_ERROR_HINT,
            start_task_prompt=DEFAULT_START_TASK_PROMPT,
            resume_task_prompt=DEFAULT_RESUME_TASK_PROMPT,
            supervision_prompt=DEFAULT_SUPERVISION_PROMPT,
            compression_prompt=DEFAULT_COMPRESSION_PROMPT,
            ai_message_notify=DEFAULT_AI_MESSAGE_NOTIFY_TEMPLATE,
            ai_message_inquiry=DEFAULT_AI_MESSAGE_INQUIRY_TEMPLATE,
            ai_message_inquiry_reminder=DEFAULT_AI_MESSAGE_INQUIRY_REMINDER,
            ai_message_reply=DEFAULT_AI_MESSAGE_REPLY_TEMPLATE,
            ai_message_chitchat=DEFAULT_AI_MESSAGE_CHITCHAT_TEMPLATE,
            ai_message_reply_success=DEFAULT_AI_MESSAGE_REPLY_SUCCESS,
            user_message_notice=DEFAULT_USER_MESSAGE_NOTICE,
            ui_theme_mode=DEFAULT_UI_THEME_MODE,
            ui_font_size=DEFAULT_UI_FONT_SIZE,
            ui_brain_view_mode=DEFAULT_UI_BRAIN_VIEW_MODE,
            model_presets=DEFAULT_MODEL_PRESETS,
        )
        _migrate_assistantaiconfig(cursor)
        _migrate_qqsessionroute(cursor)
        _migrate_aitaskjob(cursor)
        _migrate_aimessage(cursor)

        conn.commit()
    finally:
        conn.close()


def _migrate_assistantaiconfig_strip_markdown_symbols() -> None:
    # Add the column on every supported backend so existing databases keep
    # working after the schema bump.
    from ..database import engine

    if database_dialect() == "sqlite":
        with engine.begin() as conn:
            result = conn.exec_driver_sql("PRAGMA table_info(assistantaiconfig)")
            existing = {row[1] for row in result.fetchall()}
            if "strip_markdown_symbols" not in existing:
                conn.exec_driver_sql(
                    "ALTER TABLE assistantaiconfig ADD COLUMN strip_markdown_symbols BOOLEAN DEFAULT 0"
                )
            conn.exec_driver_sql(
                "UPDATE assistantaiconfig SET strip_markdown_symbols = 0 "
                "WHERE strip_markdown_symbols IS NULL"
            )
        return

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE assistantaiconfig "
            "ADD COLUMN IF NOT EXISTS strip_markdown_symbols BOOLEAN DEFAULT FALSE"
        )
        conn.exec_driver_sql(
            "UPDATE assistantaiconfig SET strip_markdown_symbols = FALSE "
            "WHERE strip_markdown_symbols IS NULL"
        )


def _migrate_chatmessagemedia_message_cascade() -> None:
    """Ensure deleting a chat message also deletes DB-backed screenshot media."""
    from ..database import engine
    from sqlalchemy import inspect

    insp = inspect(engine)
    if "chatmessagemedia" not in set(insp.get_table_names()):
        return

    if database_dialect() != "postgresql":
        return

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE chatmessagemedia "
            "DROP CONSTRAINT IF EXISTS chatmessagemedia_message_id_fkey"
        )
        conn.exec_driver_sql(
            "ALTER TABLE chatmessagemedia "
            "ADD CONSTRAINT chatmessagemedia_message_id_fkey "
            "FOREIGN KEY (message_id) REFERENCES chatmessage(id) ON DELETE CASCADE"
        )


def _migrate_user_email() -> None:
    """Add the optional ``email`` column used by email-code register/login.

    先用 inspector 探测列是否存在再 ALTER，不依赖 ``IF NOT EXISTS``：
    部分 Postgres 兼容数据库（openGauss / Kingbase 等）对该语法支持不全，
    且列已存在时重复 ALTER 会让所有进程启动失败。
    """
    from sqlalchemy import inspect as sa_inspect

    from ..database import engine

    insp = sa_inspect(engine)
    if "user" not in insp.get_table_names():
        return
    if any(col["name"] == "email" for col in insp.get_columns("user")):
        return

    statement = (
        "ALTER TABLE user ADD COLUMN email VARCHAR"
        if database_dialect() == "sqlite"
        else 'ALTER TABLE "user" ADD COLUMN email VARCHAR'
    )
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(statement)
    except Exception:
        # 并发启动等场景下列可能刚被其它进程加上：复查后列已存在则放行。
        insp = sa_inspect(engine)
        if not any(col["name"] == "email" for col in insp.get_columns("user")):
            raise
        logger.info("user.email already present; skipping duplicate ALTER")


def _migrate_endpointagentpresence_tool_defs() -> None:
    """Add ``tool_defs_json`` to the endpoint presence snapshot.

    Endpoint agents now ship their own tool schemas at register time; the
    server stores them here so it never hardcodes per-tool schemas. Existing
    databases that predate this column get it back-filled to ``{}`` (legacy
    rows simply fall back to a generic schema until the agent reconnects).
    Runs on every backend.
    """
    from ..database import engine
    from sqlalchemy import inspect

    insp = inspect(engine)
    if "endpointagentpresence" not in set(insp.get_table_names()):
        return
    columns = {col["name"] for col in insp.get_columns("endpointagentpresence")}
    if "tool_defs_json" in columns:
        return
    if database_dialect() == "sqlite":
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE endpointagentpresence ADD COLUMN tool_defs_json TEXT DEFAULT '{}'"
            )
    else:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE endpointagentpresence "
                "ADD COLUMN IF NOT EXISTS tool_defs_json TEXT DEFAULT '{}'"
            )


def _migrate_agenttypemcppermission_agent_id() -> None:
    """Add ``agent_id`` to the endpoint MCP permission table.

    Scope moved from per-(AI, agent-type) to per-individual-agent. Existing rows
    keyed only by type can't be safely remapped to a specific agent id, so they
    back-fill ``agent_id=''`` and are simply ignored by the new per-agent lookup
    (the agent starts closed until rescoped in the Workshop). Runs on every
    backend.
    """
    from ..database import engine
    from sqlalchemy import inspect

    insp = inspect(engine)
    if "agenttypemcppermission" not in set(insp.get_table_names()):
        return
    columns = {col["name"] for col in insp.get_columns("agenttypemcppermission")}
    if "agent_id" in columns:
        return
    if database_dialect() == "sqlite":
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE agenttypemcppermission ADD COLUMN agent_id TEXT DEFAULT ''"
            )
    else:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE agenttypemcppermission ADD COLUMN IF NOT EXISTS agent_id TEXT DEFAULT ''"
            )


def _migrate_agenttypemcppermission_nullable_ai_config() -> None:
    """Allow endpoint MCP scopes to be saved before an agent is assigned.

    The permission scope is keyed by ``(user_id, agent_id)`` now; ``ai_config_id``
    is only informational. Older Postgres databases still have a NOT NULL
    constraint from the previous per-(AI, type) schema, which breaks saving MCP
    permissions for an unassigned Workshop agent.
    """
    from ..database import engine
    from sqlalchemy import inspect

    insp = inspect(engine)
    if "agenttypemcppermission" not in set(insp.get_table_names()):
        return
    columns = {
        col["name"]: col
        for col in insp.get_columns("agenttypemcppermission")
    }
    column = columns.get("ai_config_id")
    if not column or column.get("nullable", True):
        return

    if database_dialect() == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE agenttypemcppermission ALTER COLUMN ai_config_id DROP NOT NULL"
            )


def _migrate_assistantaiconfig_strip_endpoint_mcp_tools() -> None:
    """Remove legacy endpoint-agent tools from AI ``mcp_tools`` JSON.

    Desktop/browser tools used to be stored directly on the AI config in some
    old rows. They are now controlled only by per-agent Workshop scope; leaving
    stale names here makes ``mcp.list_tools`` expose them even after the AI's
    visible MCP list is unchecked.
    """
    from ..database import engine
    from sqlalchemy import inspect, text
    from connector_runtime.dispatch.desktop_agent_tools import is_endpoint_tool_config_name

    insp = inspect(engine)
    if "assistantaiconfig" not in set(insp.get_table_names()):
        return
    columns = {col["name"] for col in insp.get_columns("assistantaiconfig")}
    if "mcp_tools" not in columns:
        return

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, mcp_tools FROM assistantaiconfig")).mappings().all()
        for row in rows:
            try:
                parsed = json.loads(row.get("mcp_tools") or "[]")
            except Exception:
                continue
            if not isinstance(parsed, list):
                continue
            next_tools = []
            seen = set()
            changed = False
            for item in parsed:
                tool = str(item or "").strip() if isinstance(item, str) else ""
                if not tool:
                    changed = True
                    continue
                if is_endpoint_tool_config_name(tool):
                    changed = True
                    continue
                if tool in seen:
                    changed = True
                    continue
                next_tools.append(tool)
                seen.add(tool)
            if not changed:
                continue
            conn.execute(
                text("UPDATE assistantaiconfig SET mcp_tools = :tools WHERE id = :id"),
                {"tools": json.dumps(next_tools, ensure_ascii=False), "id": row["id"]},
            )


def _migrate_user_role_mcp_permissions_rename() -> None:
    """Rename legacy granular tool names inside ``User.role_mcp_permissions``.

    The admin-configured per-role allow-list stores tool names as JSON. After the
    consolidation refactor those granular names no longer exist, so map them onto
    the unified ``*.manage`` tools (de-duplicated) to preserve each role's grants.
    """
    from ..database import engine
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "user" not in set(insp.get_table_names()):
        return
    columns = {col["name"] for col in insp.get_columns("user")}
    if "role_mcp_permissions" not in columns:
        return

    with engine.begin() as conn:
        rows = conn.execute(
            text('SELECT id, role_mcp_permissions FROM "user"')
        ).mappings().all()
        for row in rows:
            raw = row.get("role_mcp_permissions") or ""
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            changed = False
            for role, tools in list(data.items()):
                if not isinstance(tools, list):
                    continue
                next_tools = []
                seen = set()
                for item in tools:
                    tool = str(item or "").strip() if isinstance(item, str) else ""
                    if not tool:
                        changed = True
                        continue
                    renamed = _LEGACY_TOOL_RENAMES.get(tool)
                    if renamed:
                        tool = renamed
                        changed = True
                    if tool in seen:
                        changed = True
                        continue
                    next_tools.append(tool)
                    seen.add(tool)
                data[role] = next_tools
            if not changed:
                continue
            conn.execute(
                text('UPDATE "user" SET role_mcp_permissions = :perms WHERE id = :id'),
                {"perms": json.dumps(data, ensure_ascii=False), "id": row["id"]},
            )


def _live_registered_tool_names() -> set:
    """Authoritative set of currently-valid MCP tool names.

    Builtin registry tools plus the self-inspection tools.
    Returns an empty set if the registry cannot be loaded so callers can refuse
    to prune rather than risk mass-deleting a still-valid config.
    """
    try:
        from mcp_runtime.mcp.registry import registry as _registry
        from mcp_runtime.mcp.core import MCP_INTROSPECTION_TOOLS

        names = {str(t.get("name") or "").strip() for t in _registry.list_tools() if t.get("name")}
        names |= set(MCP_INTROSPECTION_TOOLS)
        return names
    except Exception:
        return set()


# Legacy granular MCP tool name -> unified ``*.manage`` tool. Applied when
# pruning stored allow-lists so operators keep an equivalent grant after the
# consolidation refactor instead of silently losing the permission. The table is
# the single source shared with runtime allow-list normalization
# (``api.mcp_tool_aliases``) so migration-time and runtime stay in lock-step.
from api.mcp_tool_aliases import LEGACY_TOOL_RENAMES as _LEGACY_TOOL_RENAMES  # noqa: E402


def _migrate_assistantaiconfig_prune_unknown_mcp_tools() -> None:
    """Drop stale tool names from AI ``mcp_tools`` that no longer map to any
    registered MCP tool.

    Tools removed in earlier refactors (e.g. ``admin.dispatch_task``,
    ``feishu.send_message``, ``human.ask``) used to linger in persisted config
    allow-lists. They can never be called, but they polluted the prompt tool
    catalog and ``mcp.list_tools``. This prunes them once so they stop being
    retained. Endpoint-config names stay handled by the dedicated strip above.
    """
    from ..database import engine
    from sqlalchemy import inspect, text
    from connector_runtime.dispatch.desktop_agent_tools import is_endpoint_tool_config_name

    valid = _live_registered_tool_names()
    # Safety guard: a degraded / empty registry must never wipe every config.
    if len(valid) < 10:
        return

    insp = inspect(engine)
    if "assistantaiconfig" not in set(insp.get_table_names()):
        return
    columns = {col["name"] for col in insp.get_columns("assistantaiconfig")}
    if "mcp_tools" not in columns:
        return

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, mcp_tools FROM assistantaiconfig")).mappings().all()
        for row in rows:
            try:
                parsed = json.loads(row.get("mcp_tools") or "[]")
            except Exception:
                continue
            if not isinstance(parsed, list):
                continue
            next_tools = []
            seen = set()
            changed = False
            for item in parsed:
                tool = str(item or "").strip() if isinstance(item, str) else ""
                if not tool:
                    changed = True
                    continue
                renamed = _LEGACY_TOOL_RENAMES.get(tool)
                if renamed:
                    tool = renamed
                    changed = True
                # Endpoint tools are governed by per-agent scope, not stored here.
                if is_endpoint_tool_config_name(tool):
                    changed = True
                    continue
                if tool not in valid:
                    changed = True
                    continue
                if tool in seen:
                    changed = True
                    continue
                next_tools.append(tool)
                seen.add(tool)
            if not changed:
                continue
            conn.execute(
                text("UPDATE assistantaiconfig SET mcp_tools = :tools WHERE id = :id"),
                {"tools": json.dumps(next_tools, ensure_ascii=False), "id": row["id"]},
            )


def _migrate_user_role() -> None:
    """Add the platform-level ``role`` (+ ``created_at``) columns and make
    sure exactly one ``owner`` exists.

    Runs on every backend. Existing rows backfill to ``member``; the
    lowest-id user is promoted to ``owner`` when no owner is present yet so
    pre-existing single-user installs keep full admin access after the
    upgrade. New installs get their owner assigned at registration time.
    """
    import time as _time

    from ..database import engine

    if database_dialect() == "sqlite":
        table = "user"
        with engine.begin() as conn:
            existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user)").fetchall()}
            if "role" not in existing:
                conn.exec_driver_sql("ALTER TABLE user ADD COLUMN role TEXT DEFAULT 'member'")
            if "created_at" not in existing:
                conn.exec_driver_sql("ALTER TABLE user ADD COLUMN created_at REAL")
            conn.exec_driver_sql(
                "UPDATE user SET role = 'member' "
                "WHERE role IS NULL OR role = '' OR role NOT IN ('owner', 'admin', 'member')"
            )
            conn.exec_driver_sql(
                f"UPDATE user SET created_at = {_time.time()} WHERE created_at IS NULL"
            )
    else:
        table = '"user"'
        with engine.begin() as conn:
            conn.exec_driver_sql('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS role TEXT DEFAULT \'member\'')
            conn.exec_driver_sql('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS created_at DOUBLE PRECISION')
            conn.exec_driver_sql(
                'UPDATE "user" SET role = \'member\' '
                "WHERE role IS NULL OR role = '' OR role NOT IN ('owner', 'admin', 'member')"
            )
            conn.exec_driver_sql(
                f'UPDATE "user" SET created_at = {_time.time()} WHERE created_at IS NULL'
            )

    # Bootstrap an owner from the oldest account when none exists yet.
    with engine.begin() as conn:
        has_owner = conn.exec_driver_sql(
            f"SELECT 1 FROM {table} WHERE role = 'owner' LIMIT 1"
        ).first()
        if not has_owner:
            min_id = conn.exec_driver_sql(f"SELECT MIN(id) FROM {table}").scalar()
            if min_id is not None:
                conn.exec_driver_sql(
                    f"UPDATE {table} SET role = 'owner' WHERE id = {int(min_id)}"
                )


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _existing_columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _add_column(cursor: sqlite3.Cursor, table: str, name: str, definition: str, existing: set[str]) -> None:
    if name in existing:
        return
    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    existing.add(name)


def _migrate_chatmessage(cursor: sqlite3.Cursor) -> None:
    existing = _existing_columns(cursor, "chatmessage")
    _add_column(cursor, "chatmessage", "ai_config_id", "INTEGER", existing)
    _add_column(cursor, "chatmessage", "ai_kind", "TEXT DEFAULT 'assistant'", existing)
    _add_column(cursor, "chatmessage", "cache_read_tokens", "INTEGER", existing)


def _migrate_chatrun(cursor: sqlite3.Cursor) -> None:
    # ``heartbeat_at`` was added when ai-runtime was split out; older SQLite
    # files predate it. The watchdog checks this column to reap dead runs.
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chatrun'"
    )
    if not cursor.fetchone():
        return
    existing = _existing_columns(cursor, "chatrun")
    _add_column(cursor, "chatrun", "heartbeat_at", "REAL", existing)
    _add_column(cursor, "chatrun", "worker_kwargs_json", "TEXT", existing)


def _migrate_user(
    cursor: sqlite3.Cursor,
    *,
    mcp_call_method: str,
    mcp_namespace_hints: str,
    mcp_dynamic_rule: str,
    mcp_format_error_hint: str,
    start_task_prompt: str,
    resume_task_prompt: str,
    supervision_prompt: str,
    compression_prompt: str,
    ai_message_notify: str,
    ai_message_inquiry: str,
    ai_message_inquiry_reminder: str,
    ai_message_reply: str,
    ai_message_chitchat: str,
    ai_message_reply_success: str,
    user_message_notice: str,
    ui_theme_mode: str,
    ui_font_size: str,
    ui_brain_view_mode: str,
    model_presets: str,
) -> None:
    existing = _existing_columns(cursor, "user")
    _add_column(cursor, "user", "mcp_call_method", f"TEXT DEFAULT '{_quote(mcp_call_method)}'", existing)
    _add_column(cursor, "user", "mcp_namespace_hints", f"TEXT DEFAULT '{_quote(mcp_namespace_hints)}'", existing)
    _add_column(cursor, "user", "mcp_dynamic_rule", f"TEXT DEFAULT '{_quote(mcp_dynamic_rule)}'", existing)
    _add_column(cursor, "user", "mcp_format_error_hint", f"TEXT DEFAULT '{_quote(mcp_format_error_hint)}'", existing)
    _add_column(cursor, "user", "mcp_max_steps", "INTEGER DEFAULT 48", existing)
    _add_column(cursor, "user", "role_mcp_permissions", "TEXT DEFAULT ''", existing)
    _add_column(cursor, "user", "tavily_api_key", "TEXT DEFAULT ''", existing)
    _add_column(cursor, "user", "model_presets", f"TEXT DEFAULT '{_quote(model_presets)}'", existing)
    _add_column(cursor, "user", "default_start_task_prompt", f"TEXT DEFAULT '{_quote(start_task_prompt)}'", existing)
    _add_column(cursor, "user", "default_resume_task_prompt", f"TEXT DEFAULT '{_quote(resume_task_prompt)}'", existing)
    _add_column(cursor, "user", "default_supervision_prompt", f"TEXT DEFAULT '{_quote(supervision_prompt)}'", existing)
    _add_column(cursor, "user", "default_supervision_idle_seconds", "INTEGER DEFAULT 25", existing)
    _add_column(cursor, "user", "default_compression_prompt", f"TEXT DEFAULT '{_quote(compression_prompt)}'", existing)
    _add_column(cursor, "user", "prompt_ai_message_notify", f"TEXT DEFAULT '{_quote(ai_message_notify)}'", existing)
    _add_column(cursor, "user", "prompt_ai_message_inquiry", f"TEXT DEFAULT '{_quote(ai_message_inquiry)}'", existing)
    _add_column(cursor, "user", "ai_message_inquiry_reminder_seconds", "INTEGER DEFAULT 3", existing)
    _add_column(cursor, "user", "prompt_ai_message_inquiry_reminder", f"TEXT DEFAULT '{_quote(ai_message_inquiry_reminder)}'", existing)
    _add_column(cursor, "user", "prompt_ai_message_reply", f"TEXT DEFAULT '{_quote(ai_message_reply)}'", existing)
    _add_column(cursor, "user", "prompt_ai_message_chitchat", f"TEXT DEFAULT '{_quote(ai_message_chitchat)}'", existing)
    _add_column(cursor, "user", "prompt_ai_message_reply_success", f"TEXT DEFAULT '{_quote(ai_message_reply_success)}'", existing)
    _add_column(cursor, "user", "prompt_user_message_notice", f"TEXT DEFAULT '{_quote(user_message_notice)}'", existing)
    _add_column(cursor, "user", "ui_theme_mode", f"TEXT DEFAULT '{ui_theme_mode}'", existing)
    _add_column(cursor, "user", "ui_font_size", f"TEXT DEFAULT '{ui_font_size}'", existing)
    _add_column(cursor, "user", "ui_brain_view_mode", f"TEXT DEFAULT '{ui_brain_view_mode}'", existing)

    cursor.execute(
        f"UPDATE user SET ui_theme_mode = '{ui_theme_mode}' "
        "WHERE ui_theme_mode IS NULL OR ui_theme_mode = '' "
        "OR ui_theme_mode NOT IN ('light', 'dark')"
    )
    cursor.execute(
        f"UPDATE user SET ui_font_size = '{ui_font_size}' "
        "WHERE ui_font_size IS NULL OR ui_font_size = '' "
        "OR ui_font_size NOT IN ('sm', 'md', 'lg')"
    )
    cursor.execute(
        f"UPDATE user SET ui_brain_view_mode = '{ui_brain_view_mode}' "
        "WHERE ui_brain_view_mode IS NULL OR ui_brain_view_mode = '' "
        "OR ui_brain_view_mode NOT IN ('sections', 'all')"
    )
    _collapse_role_permission_tool_items(
        cursor,
        [
            "task.create_immediate",
            "task.create_scheduled",
            "task.create_recurring",
        ],
        "task.create",
    )
    _append_role_permission_tool_items_after_anchor(
        cursor,
        "task.create",
        ["task.update", "task.delete"],
    )
    _append_role_permission_tool_items_after_anchor(
        cursor,
        "workspace.run_command",
        ["web.search"],
    )
    _rename_role_permission_tool_item(cursor, "conversation.forget_before_current", "conversation.edit")
    _rename_role_permission_tool_item(cursor, "conversation.find", "conversation.list")
    _append_role_permission_tool_items_after_anchor(cursor, "conversation.list", ["conversation.detail"])
    _append_role_permission_tool_items_after_anchor(
        cursor,
        "user.send_message",
        [
            "conversation.list",
            "conversation.detail",
            "conversation.create",
            "conversation.delete",
            "conversation.edit",
        ],
    )
    _append_role_permission_tool_items_after_anchor(
        cursor,
        "message.send_to_user",
        [
            "conversation.list",
            "conversation.detail",
            "conversation.create",
            "conversation.delete",
            "conversation.edit",
        ],
    )
    _remove_role_permission_tool_item(cursor, "task.get_current")
    _remove_role_permission_tool_item(cursor, "admin.dispatch_flow")
    # Rename the send-message tools out of the ambiguous user./ai. prefixes into
    # a dedicated message.* namespace (UI now groups them under 「发消息」).
    _rename_role_permission_tool_item(cursor, "user.send_message", "message.send_to_user")
    _rename_role_permission_tool_item(cursor, "ai.send_message", "message.send_to_ai")
    # web.search 并入 workspace 命名空间；project/memory MCP 工具组已下线。
    _rename_role_permission_tool_item(cursor, "web.search", "workspace.search")
    for _tool in (
        "project.list_projects",
        "project.create_project",
        "project.update_project",
        "project.delete_project",
        "memory.write",
        "memory.search",
        "memory.list",
        "memory.update",
        "memory.archive",
        "librarian.propose",
        "librarian.consult",
        "librarian.list_topics",
        "librarian.read",
        "librarian.archive",
        "evolution.input",
        "evolution.list",
        "evolution.review",
        "debug.ping",
    ):
        _remove_role_permission_tool_item(cursor, _tool)


def _migrate_assistantaiconfig(cursor: sqlite3.Cursor) -> None:
    existing = _existing_columns(cursor, "assistantaiconfig")
    _add_column(cursor, "assistantaiconfig", "ai_role", "TEXT DEFAULT 'digital_member'", existing)
    _add_column(cursor, "assistantaiconfig", "digital_member_role", "TEXT DEFAULT 'member'", existing)
    _add_column(cursor, "assistantaiconfig", "is_librarian", "BOOLEAN DEFAULT 0", existing)
    _add_column(cursor, "assistantaiconfig", "platform", "TEXT DEFAULT 'Server-Core'", existing)
    _add_column(cursor, "assistantaiconfig", "generation", "INTEGER DEFAULT 1", existing)
    _add_column(cursor, "assistantaiconfig", "token_limit", "INTEGER DEFAULT 10000", existing)
    _add_column(cursor, "assistantaiconfig", "lifecycle_status", "TEXT DEFAULT 'working'", existing)
    _add_column(cursor, "assistantaiconfig", "current_behavior", "TEXT DEFAULT '等待指令...'", existing)
    _add_column(cursor, "assistantaiconfig", "workspace_root", "TEXT", existing)
    _add_column(cursor, "assistantaiconfig", "database_uri", "TEXT", existing)
    _add_column(cursor, "assistantaiconfig", "bot_channel", "TEXT DEFAULT 'feishu'", existing)
    # The legacy ``feishu_*`` / ``qq_*`` flat columns are consolidated into
    # ``bot_configs`` JSON by ``_consolidate_assistantaiconfig_bot_configs``.
    # The add-column calls used to live here; they're intentionally gone so
    # we don't re-add columns we drop in the dialect-agnostic migration.
    _add_column(cursor, "assistantaiconfig", "project_id", "TEXT", existing)
    _add_column(cursor, "assistantaiconfig", "project_name", "TEXT", existing)
    _add_column(cursor, "assistantaiconfig", "sort_order", "INTEGER DEFAULT 100", existing)
    _add_column(cursor, "assistantaiconfig", "model_preset_id", "TEXT DEFAULT ''", existing)
    _add_column(cursor, "assistantaiconfig", "strip_markdown_symbols", "BOOLEAN DEFAULT 0", existing)
    _backfill_model_presets_from_ai_configs(cursor)

    if "system_auto_control" not in existing:
        default_auto = (
            '{"enabled":true,'
            '"tasks":[]}'
        )
        cursor.execute(
            f"ALTER TABLE assistantaiconfig ADD COLUMN system_auto_control TEXT DEFAULT '{_quote(default_auto)}'"
        )
        existing.add("system_auto_control")

    _add_column(cursor, "assistantaiconfig", "parent_ai_config_id", "INTEGER", existing)
    _add_column(cursor, "assistantaiconfig", "root_manager_ai_config_id", "INTEGER", existing)
    _add_column(cursor, "assistantaiconfig", "management_scope", "TEXT DEFAULT 'self'", existing)

    # Backfill legacy enum values to current vocabulary.
    cursor.execute(
        "UPDATE assistantaiconfig SET ai_role = 'digital_member' "
        "WHERE ai_role IS NULL OR ai_role = '' OR ai_role = 'admin' OR ai_role = 'worker'"
    )
    cursor.execute(
        "UPDATE assistantaiconfig SET digital_member_role = 'manager' "
        "WHERE ai_role = 'digital_member' AND switch_key = 'assistant_default'"
    )
    cursor.execute(
        "UPDATE assistantaiconfig SET digital_member_role = 'member' "
        "WHERE digital_member_role IS NULL OR digital_member_role = '' "
        "OR digital_member_role NOT IN ('manager', 'member')"
    )
    cursor.execute(
        "UPDATE assistantaiconfig SET strip_markdown_symbols = 0 "
        "WHERE strip_markdown_symbols IS NULL"
    )
    _remove_json_array_item(cursor, "assistantaiconfig", "mcp_tools", "ai.reply_message")
    _remove_json_array_item(cursor, "assistantaiconfig", "mcp_tools", "task.get_current")
    _remove_json_array_item(cursor, "assistantaiconfig", "mcp_tools", "admin.dispatch_flow")
    _collapse_json_array_items(
        cursor,
        "assistantaiconfig",
        "mcp_tools",
        [
            "task.create_immediate",
            "task.create_scheduled",
            "task.create_recurring",
        ],
        "task.create",
    )
    _append_json_array_items_after_anchor(
        cursor,
        "assistantaiconfig",
        "mcp_tools",
        "task.create",
        ["task.update", "task.delete"],
    )
    _append_json_array_items_after_anchor(
        cursor,
        "assistantaiconfig",
        "mcp_tools",
        "workspace.run_command",
        ["web.search"],
    )
    _collapse_json_array_items(
        cursor, "assistantaiconfig", "mcp_tools", ["conversation.forget_before_current"], "conversation.edit"
    )
    _collapse_json_array_items(
        cursor, "assistantaiconfig", "mcp_tools", ["conversation.find"], "conversation.list"
    )
    _append_json_array_items_after_anchor(
        cursor, "assistantaiconfig", "mcp_tools", "conversation.list", ["conversation.detail"]
    )
    _append_json_array_items_after_anchor(
        cursor,
        "assistantaiconfig",
        "mcp_tools",
        "user.send_message",
        [
            "conversation.list",
            "conversation.detail",
            "conversation.create",
            "conversation.delete",
            "conversation.edit",
        ],
    )
    _append_json_array_items_after_anchor(
        cursor,
        "assistantaiconfig",
        "mcp_tools",
        "message.send_to_user",
        [
            "conversation.list",
            "conversation.detail",
            "conversation.create",
            "conversation.delete",
            "conversation.edit",
        ],
    )
    # Rename the send-message tools out of the ambiguous user./ai. prefixes into
    # a dedicated message.* namespace (UI now groups them under 「发消息」). Runs
    # after the anchor-based appends above so existing scopes migrate cleanly.
    _collapse_json_array_items(
        cursor, "assistantaiconfig", "mcp_tools", ["user.send_message"], "message.send_to_user"
    )
    _collapse_json_array_items(
        cursor, "assistantaiconfig", "mcp_tools", ["ai.send_message"], "message.send_to_ai"
    )
    # web.search 并入 workspace 命名空间；project/memory MCP 工具组已下线。
    _collapse_json_array_items(
        cursor, "assistantaiconfig", "mcp_tools", ["web.search"], "workspace.search"
    )
    for _tool in (
        "project.list_projects",
        "project.create_project",
        "project.update_project",
        "project.delete_project",
        "memory.write",
        "memory.search",
        "memory.list",
        "memory.update",
        "memory.archive",
        "librarian.propose",
        "librarian.consult",
        "librarian.list_topics",
        "librarian.read",
        "librarian.archive",
        "evolution.input",
        "evolution.list",
        "evolution.review",
        "debug.ping",
    ):
        _remove_json_array_item(cursor, "assistantaiconfig", "mcp_tools", _tool)
    cursor.execute(
        "UPDATE assistantaiconfig SET bot_channel = 'feishu' "
        "WHERE bot_channel IS NULL OR bot_channel = '' "
        "OR bot_channel NOT IN ('feishu', 'qq')"
    )
    # Normalize qq_default_target_type only while the legacy column still
    # exists; once the bot_configs consolidation drops it, this is a no-op.
    if "qq_default_target_type" in _existing_columns(cursor, "assistantaiconfig"):
        cursor.execute(
            "UPDATE assistantaiconfig SET qq_default_target_type = 'c2c' "
            "WHERE qq_default_target_type IS NULL OR qq_default_target_type = '' "
            "OR qq_default_target_type NOT IN ('c2c', 'group', 'channel', 'dm')"
        )


def _backfill_model_presets_from_ai_configs(cursor: sqlite3.Cursor) -> None:
    import json

    user_columns = _existing_columns(cursor, "user")
    config_columns = _existing_columns(cursor, "assistantaiconfig")
    required_user = {"id", "model_presets"}
    required_config = {"id", "user_id", "api_key", "base_url", "model", "model_preset_id"}
    if not required_user.issubset(user_columns) or not required_config.issubset(config_columns):
        return

    cursor.execute("SELECT id, model_presets FROM user")
    users = cursor.fetchall()
    for user_id, raw_presets in users:
        try:
            parsed = json.loads(raw_presets or "[]")
        except Exception:
            parsed = []
        presets = parsed if isinstance(parsed, list) else []
        normalized: list[dict] = []
        seen_ids: set[str] = set()
        seen_keys: dict[tuple[str, str, str], str] = {}
        for index, item in enumerate(presets):
            if not isinstance(item, dict):
                continue
            api_key = str(item.get("api_key") or "").strip()
            base_url = str(item.get("base_url") or "").strip()
            model = str(item.get("model") or "").strip()
            if not api_key or not base_url or not model:
                continue
            preset_id = str(item.get("id") or model or f"model_{index + 1}").strip()
            if not preset_id or preset_id in seen_ids:
                preset_id = f"{model}_{index + 1}"
            seen_ids.add(preset_id)
            seen_keys[(api_key, base_url, model)] = preset_id
            normalized.append(
                {
                    "id": preset_id,
                    "name": str(item.get("name") or model).strip() or model,
                    "api_key": api_key,
                    "base_url": base_url,
                    "model": model,
                }
            )

        cursor.execute(
            "SELECT id, api_key, base_url, model, model_preset_id FROM assistantaiconfig WHERE user_id = ?",
            (user_id,),
        )
        configs = cursor.fetchall()
        for cfg_id, api_key, base_url, model, model_preset_id in configs:
            api_key = str(api_key or "").strip()
            base_url = str(base_url or "").strip()
            model = str(model or "").strip()
            if not api_key or not base_url or not model:
                continue
            key = (api_key, base_url, model)
            preset_id = seen_keys.get(key)
            if not preset_id:
                base_id = model
                preset_id = base_id
                suffix = 2
                while preset_id in seen_ids:
                    preset_id = f"{base_id}_{suffix}"
                    suffix += 1
                seen_ids.add(preset_id)
                seen_keys[key] = preset_id
                normalized.append(
                    {
                        "id": preset_id,
                        "name": model,
                        "api_key": api_key,
                        "base_url": base_url,
                        "model": model,
                    }
                )
            if not str(model_preset_id or "").strip():
                cursor.execute(
                    "UPDATE assistantaiconfig SET model_preset_id = ? WHERE id = ?",
                    (preset_id, cfg_id),
                )

        cursor.execute(
            "UPDATE user SET model_presets = ? WHERE id = ?",
            (json.dumps(normalized, ensure_ascii=False), user_id),
        )


def _migrate_qqsessionroute(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS qqsessionroute (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            ai_config_id INTEGER NOT NULL,
            ai_kind TEXT NOT NULL DEFAULT 'core',
            session_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_type TEXT NOT NULL DEFAULT 'c2c',
            source_message_id TEXT NOT NULL DEFAULT '',
            source_event_id TEXT NOT NULL DEFAULT '',
            next_msg_seq INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    existing = _existing_columns(cursor, "qqsessionroute")
    _add_column(cursor, "qqsessionroute", "source_message_id", "TEXT DEFAULT ''", existing)
    _add_column(cursor, "qqsessionroute", "source_event_id", "TEXT DEFAULT ''", existing)
    _add_column(cursor, "qqsessionroute", "next_msg_seq", "INTEGER DEFAULT 1", existing)


def _migrate_aitaskjob(cursor: sqlite3.Cursor) -> None:
    existing = _existing_columns(cursor, "aitaskjob")
    _add_column(cursor, "aitaskjob", "task_payload", "TEXT", existing)
    _add_column(cursor, "aitaskjob", "created_by_ai_config_id", "INTEGER", existing)
    _add_column(cursor, "aitaskjob", "created_by_session_id", "TEXT", existing)
    _add_column(cursor, "aitaskjob", "completion_notified_at", "REAL", existing)


def _remove_json_array_item(cursor: sqlite3.Cursor, table: str, column: str, item: str) -> None:
    cursor.execute(f"SELECT id, {column} FROM {table} WHERE {column} LIKE ?", (f"%{item}%",))
    rows = cursor.fetchall()
    for row_id, raw_value in rows:
        try:
            import json
            parsed = json.loads(raw_value or "[]")
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        next_items = [value for value in parsed if value != item]
        if next_items == parsed:
            continue
        cursor.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?",
            (json.dumps(next_items, ensure_ascii=False), row_id),
        )


def _collapse_json_array_items(
    cursor: sqlite3.Cursor,
    table: str,
    column: str,
    old_items: list[str],
    new_item: str,
) -> None:
    old_set = set(old_items)
    if not old_set:
        return
    like = " OR ".join([f"{column} LIKE ?" for _ in old_items])
    cursor.execute(
        f"SELECT id, {column} FROM {table} WHERE {like}",
        tuple(f"%{item}%" for item in old_items),
    )
    rows = cursor.fetchall()
    for row_id, raw_value in rows:
        try:
            import json
            parsed = json.loads(raw_value or "[]")
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        next_items: list[str] = []
        seen: set[str] = set()
        needs_new_item = False
        changed = False
        for value in parsed:
            if value in old_set:
                needs_new_item = True
                changed = True
                continue
            if value not in seen:
                next_items.append(value)
                seen.add(value)
        if needs_new_item and new_item not in seen:
            next_items.append(new_item)
        if not changed:
            continue
        cursor.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?",
            (json.dumps(next_items, ensure_ascii=False), row_id),
        )


def _collapse_role_permission_tool_items(
    cursor: sqlite3.Cursor,
    old_items: list[str],
    new_item: str,
) -> None:
    old_set = set(old_items)
    cursor.execute("SELECT id, role_mcp_permissions FROM user WHERE role_mcp_permissions LIKE '%task.create_%'")
    rows = cursor.fetchall()
    for row_id, raw_value in rows:
        try:
            import json
            parsed = json.loads(raw_value or "{}")
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        changed = False
        next_permissions = {}
        for role, tools in parsed.items():
            if not isinstance(tools, list):
                next_permissions[role] = tools
                continue
            next_tools: list[str] = []
            seen: set[str] = set()
            needs_new_item = False
            for tool in tools:
                if tool in old_set:
                    needs_new_item = True
                    changed = True
                    continue
                if tool not in seen:
                    next_tools.append(tool)
                    seen.add(tool)
            if needs_new_item and new_item not in seen:
                next_tools.append(new_item)
            next_permissions[role] = next_tools
        if not changed:
            continue
        cursor.execute(
            "UPDATE user SET role_mcp_permissions = ? WHERE id = ?",
            (json.dumps(next_permissions, ensure_ascii=False), row_id),
        )


def _remove_role_permission_tool_item(cursor: sqlite3.Cursor, item: str) -> None:
    cursor.execute("SELECT id, role_mcp_permissions FROM user WHERE role_mcp_permissions LIKE ?", (f"%{item}%",))
    rows = cursor.fetchall()
    for row_id, raw_value in rows:
        try:
            import json
            parsed = json.loads(raw_value or "{}")
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        changed = False
        next_permissions = {}
        for role, tools in parsed.items():
            if not isinstance(tools, list):
                next_permissions[role] = tools
                continue
            next_tools = [tool for tool in tools if tool != item]
            if next_tools != tools:
                changed = True
            next_permissions[role] = next_tools
        if not changed:
            continue
        cursor.execute(
            "UPDATE user SET role_mcp_permissions = ? WHERE id = ?",
            (json.dumps(next_permissions, ensure_ascii=False), row_id),
        )


def _rename_role_permission_tool_item(cursor: sqlite3.Cursor, old_item: str, new_item: str) -> None:
    """Rename a single tool name inside every user's role_mcp_permissions map,
    preserving its position within each role's list and de-duplicating."""
    cursor.execute(
        "SELECT id, role_mcp_permissions FROM user WHERE role_mcp_permissions LIKE ?",
        (f"%{old_item}%",),
    )
    rows = cursor.fetchall()
    for row_id, raw_value in rows:
        try:
            import json
            parsed = json.loads(raw_value or "{}")
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        changed = False
        next_permissions = {}
        for role, tools in parsed.items():
            if not isinstance(tools, list):
                next_permissions[role] = tools
                continue
            next_tools: list[str] = []
            seen: set[str] = set()
            for tool in tools:
                mapped = new_item if tool == old_item else tool
                if mapped != tool:
                    changed = True
                if mapped not in seen:
                    next_tools.append(mapped)
                    seen.add(mapped)
            next_permissions[role] = next_tools
        if not changed:
            continue
        cursor.execute(
            "UPDATE user SET role_mcp_permissions = ? WHERE id = ?",
            (json.dumps(next_permissions, ensure_ascii=False), row_id),
        )


def _append_json_array_items_after_anchor(
    cursor: sqlite3.Cursor,
    table: str,
    column: str,
    anchor_item: str,
    new_items: list[str],
) -> None:
    cursor.execute(f"SELECT id, {column} FROM {table} WHERE {column} LIKE ?", (f"%{anchor_item}%",))
    rows = cursor.fetchall()
    for row_id, raw_value in rows:
        try:
            import json
            parsed = json.loads(raw_value or "[]")
        except Exception:
            continue
        if not isinstance(parsed, list) or anchor_item not in parsed:
            continue
        next_items = list(parsed)
        changed = False
        for item in new_items:
            if item not in next_items:
                next_items.append(item)
                changed = True
        if not changed:
            continue
        cursor.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?",
            (json.dumps(next_items, ensure_ascii=False), row_id),
        )


def _append_role_permission_tool_items_after_anchor(
    cursor: sqlite3.Cursor,
    anchor_item: str,
    new_items: list[str],
) -> None:
    cursor.execute("SELECT id, role_mcp_permissions FROM user WHERE role_mcp_permissions LIKE ?", (f"%{anchor_item}%",))
    rows = cursor.fetchall()
    for row_id, raw_value in rows:
        try:
            import json
            parsed = json.loads(raw_value or "{}")
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        changed = False
        next_permissions = {}
        for role, tools in parsed.items():
            if not isinstance(tools, list) or anchor_item not in tools:
                next_permissions[role] = tools
                continue
            next_tools = list(tools)
            for item in new_items:
                if item not in next_tools:
                    next_tools.append(item)
                    changed = True
            next_permissions[role] = next_tools
        if not changed:
            continue
        cursor.execute(
            "UPDATE user SET role_mcp_permissions = ? WHERE id = ?",
            (json.dumps(next_permissions, ensure_ascii=False), row_id),
        )


def _migrate_aimessage(cursor: sqlite3.Cursor) -> None:
    # Table may not exist yet if running on a brand new DB before
    # SQLModel.metadata.create_all — guard against that.
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='aimessage'"
    )
    if not cursor.fetchone():
        return
    existing = _existing_columns(cursor, "aimessage")
    _add_column(cursor, "aimessage", "target_session_id", "TEXT DEFAULT ''", existing)
    _add_column(cursor, "aimessage", "from_session_id", "TEXT DEFAULT ''", existing)
    _add_column(cursor, "aimessage", "message_type", "TEXT DEFAULT 'notify'", existing)
    _add_column(cursor, "aimessage", "cascade_depth", "INTEGER DEFAULT 0", existing)
    _add_column(cursor, "aimessage", "reply_reminded_at", "REAL", existing)
    # 旧的、缺乏 message_type 的行按当前 require_reply 推导出兼容值：
    # require_reply=1 → inquiry，期望对方答复；其余视为 notify。
    cursor.execute(
        "UPDATE aimessage SET message_type='inquiry' "
        "WHERE (message_type IS NULL OR message_type='' OR message_type='notify') "
        "AND require_reply=1"
    )
    # 旧的、缺乏 session 归属的 in-flight 消息无法被新的严格匹配 pop 出来，
    # 直接终结掉避免幽灵堆积。
    cursor.execute(
        "UPDATE aimessage SET status='failed', "
        "failure_reason='schema migration: target_session_id required' "
        "WHERE status IN ('pending','delivered') "
        "AND (target_session_id IS NULL OR target_session_id='')"
    )
