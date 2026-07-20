"""Workspace-file-backed device tools (replaces the DB store).

The source of truth for a user's device tools is now plain files under their
workspace, NOT the database (设备端MCP代码下放长期方案；用户要求弃用 DB、改存工作区文件)：

    <workspace>/<user_id>/device_tools/<device_type>/
        <name>.ps1       # runtime=powershell body（出厂默认；AI 可直接经工作区 MCP 读写）
        <name>.py        # runtime=python body
        <name>.js        # code_kind=js body
        <name>.json      # 元数据：description / input_schema / code_kind / runtime /
                         #         permissions / enabled / status（program 的 code 也在此）
        .history/<name>/<version_id>.json   # 快照，供回滚

Public interface mirrors the old ``device_dynamic_tools`` so REST / AI manager /
push only swap the import. ``validate_definition`` and friends are reused from
that module (pure logic). Default desktop PowerShell tools are seeded from the
``device_runtime_tools`` package on first use; existing DB rows are migrated to
files once so nothing is lost.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from api.core.config import user_workspace_dir
from api.services.device_tools.device_dynamic_tools import (
    _revision,
    normalize_device_type,
    validate_definition,
    KNOWN_PERMISSION_TAGS,  # re-exported for callers
    VALID_STATUSES,
)

__all__ = [
    "normalize_device_type", "validate_definition", "KNOWN_PERMISSION_TAGS",
    "list_tools", "get_tool", "upsert_tool", "set_enabled", "set_status",
    "delete_tool", "list_versions", "get_version", "restore_version",
    "device_payload", "seed_defaults", "seed_from_tool_defs",
]

MAX_VERSIONS_PER_TOOL = 50


def _tools_dir(user_id: int, device_type: str) -> str:
    return os.path.join(user_workspace_dir(int(user_id)), "device_tools", normalize_device_type(device_type))


def _body_ext(code_kind: str, runtime: str = "") -> Optional[str]:
    if code_kind == "js":
        return ".js"
    if code_kind == "runtime":
        return {"shell": ".sh", "powershell": ".ps1"}.get(runtime, ".py")
    return None


def _meta_path(d: str, name: str) -> str:
    return os.path.join(d, f"{name}.json")


def _definition_of(clean: Dict[str, Any]) -> Dict[str, Any]:
    """The identity dict used for the revision hash (matches the old store)."""
    return {
        "name": clean["name"],
        "description": clean["description"],
        "input_schema": clean["input_schema"],
        "code_kind": clean["code_kind"],
        "code": clean.get("code") or [],
        "js": clean.get("js") or "",
        "runtime": clean.get("runtime") or "",
        "source": clean.get("source") or "",
        "permissions": clean.get("permissions") or [],
    }


def _write_files(d: str, clean: Dict[str, Any], enabled: bool, status: str) -> None:
    os.makedirs(d, exist_ok=True)
    name = clean["name"]
    meta = {
        "name": name,
        "description": clean["description"],
        "input_schema": clean["input_schema"],
        "code_kind": clean["code_kind"],
        "runtime": clean.get("runtime") or "",
        "permissions": clean.get("permissions") or [],
        "enabled": bool(enabled),
        "status": status,
        "updated_at": time.time(),
    }
    if clean["code_kind"] == "program":
        meta["code"] = clean.get("code") or []
    _atomic_write(_meta_path(d, name), json.dumps(meta, ensure_ascii=False, indent=2))
    ext = _body_ext(clean["code_kind"], clean.get("runtime") or "")
    if ext:
        body = clean.get("source") if clean["code_kind"] == "runtime" else clean.get("js")
        _atomic_write(os.path.join(d, f"{name}{ext}"), str(body or ""))


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_tool(d: str, name: str) -> Optional[Dict[str, Any]]:
    meta_path = _meta_path(d, name)
    if not os.path.isfile(meta_path):
        return None
    try:
        meta = json.loads(open(meta_path, encoding="utf-8").read() or "{}")
    except Exception:
        return None
    code_kind = str(meta.get("code_kind") or "program")
    source = ""
    js = ""
    ext = _body_ext(code_kind, str(meta.get("runtime") or ""))
    if ext:
        body_path = os.path.join(d, f"{name}{ext}")
        body = open(body_path, encoding="utf-8").read() if os.path.isfile(body_path) else ""
        if code_kind == "runtime":
            source = body
        else:
            js = body
    definition = {
        "name": name,
        "description": str(meta.get("description") or ""),
        "input_schema": meta.get("input_schema") if isinstance(meta.get("input_schema"), dict) else {},
        "code_kind": code_kind,
        "code": meta.get("code") if isinstance(meta.get("code"), list) else [],
        "js": js,
        "runtime": str(meta.get("runtime") or ""),
        "source": source,
        "permissions": meta.get("permissions") if isinstance(meta.get("permissions"), list) else [],
    }
    return {
        **definition,
        "enabled": bool(meta.get("enabled", True)),
        "status": str(meta.get("status") or "active"),
        "revision": _revision(definition),
        "updated_at": float(meta.get("updated_at") or 0),
    }


def _tool_names(d: str) -> List[str]:
    if not os.path.isdir(d):
        return []
    return sorted(
        fn[:-5] for fn in os.listdir(d)
        if fn.endswith(".json") and os.path.isfile(os.path.join(d, fn))
    )


# ---- tombstones: remember explicitly-deleted tool names --------------------
#
# ``seed_defaults`` re-creates any factory-default tool whose file is missing, so
# without a tombstone a deleted default reappears on the next device connect
# (seed runs on every register). A tombstone marks a name as intentionally gone
# so seeding skips it; re-creating the tool (upsert/restore) clears the marker.

def _deleted_dir(d: str) -> str:
    return os.path.join(d, ".deleted")


def _tombstone_add(d: str, name: str) -> None:
    dd = _deleted_dir(d)
    os.makedirs(dd, exist_ok=True)
    _atomic_write(os.path.join(dd, name), "")


def _tombstone_remove(d: str, name: str) -> None:
    try:
        os.remove(os.path.join(_deleted_dir(d), name))
    except OSError:
        pass


def _is_tombstoned(d: str, name: str) -> bool:
    return os.path.isfile(os.path.join(_deleted_dir(d), name))


# ---- history (file-based, replaces the version table) ---------------------

def _history_dir(d: str, name: str) -> str:
    return os.path.join(d, ".history", name)


def _record_version(d: str, name: str, serialized: Dict[str, Any], action: str, actor: str, ai_config_id: Optional[int]) -> None:
    hist = _history_dir(d, name)
    os.makedirs(hist, exist_ok=True)
    version_id = int(time.time() * 1000)
    snap = {
        "version_id": version_id,
        "name": name,
        "revision": serialized.get("revision", ""),
        "action": action if action in ("upsert", "delete", "restore") else "upsert",
        "actor": actor if actor in ("web", "ai") else "web",
        "ai_config_id": ai_config_id,
        "description": serialized.get("description", ""),
        "input_schema": serialized.get("input_schema", {}),
        "code_kind": serialized.get("code_kind", "program"),
        "code": serialized.get("code", []),
        "js": serialized.get("js", ""),
        "runtime": serialized.get("runtime", ""),
        "source": serialized.get("source", ""),
        "permissions": serialized.get("permissions", []),
        "created_at": time.time(),
    }
    _atomic_write(os.path.join(hist, f"{version_id}.json"), json.dumps(snap, ensure_ascii=False, indent=2))
    # prune
    files = sorted(os.listdir(hist), reverse=True)
    for stale in files[MAX_VERSIONS_PER_TOOL:]:
        try:
            os.remove(os.path.join(hist, stale))
        except OSError:
            pass


# ---- public API (mirrors device_dynamic_tools) ----------------------------

def list_tools(user_id: int, device_type: str) -> List[Dict[str, Any]]:
    d = _tools_dir(user_id, device_type)
    _migrate_db_once(user_id, device_type, d)
    return [t for t in (_read_tool(d, n) for n in _tool_names(d)) if t]


def get_tool(user_id: int, device_type: str, name: str) -> Optional[Dict[str, Any]]:
    d = _tools_dir(user_id, device_type)
    return _read_tool(d, str(name or "").strip())


def upsert_tool(user_id: int, device_type: str, definition: Any, enabled: bool = True,
                actor: str = "web", ai_config_id: Optional[int] = None, action: str = "upsert") -> Dict[str, Any]:
    dtype = normalize_device_type(device_type)
    clean = validate_definition(definition)
    if clean["code_kind"] == "runtime" and dtype != "desktop":
        raise ValueError("runtime tools are only supported on desktop devices")
    if dtype == "android" and clean["code_kind"] != "program":
        raise ValueError("android dynamic MCP tools only support program steps")
    d = _tools_dir(user_id, dtype)
    status = "draft" if actor == "ai" else "active"
    _write_files(d, clean, enabled, status)
    # Re-creating a name clears any prior tombstone so it seeds normally again.
    _tombstone_remove(d, clean["name"])
    serialized = _read_tool(d, clean["name"]) or {}
    _record_version(d, clean["name"], serialized, action, actor, ai_config_id)
    return serialized


def set_enabled(user_id: int, device_type: str, name: str, enabled: bool) -> Optional[Dict[str, Any]]:
    d = _tools_dir(user_id, device_type)
    name = str(name or "").strip()
    meta_path = _meta_path(d, name)
    if not os.path.isfile(meta_path):
        return None
    meta = json.loads(open(meta_path, encoding="utf-8").read() or "{}")
    meta["enabled"] = bool(enabled)
    meta["updated_at"] = time.time()
    _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
    return _read_tool(d, name)


def set_status(user_id: int, device_type: str, name: str, status: str) -> Optional[Dict[str, Any]]:
    new_status = str(status or "").strip().lower()
    if new_status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    d = _tools_dir(user_id, device_type)
    name = str(name or "").strip()
    meta_path = _meta_path(d, name)
    if not os.path.isfile(meta_path):
        return None
    meta = json.loads(open(meta_path, encoding="utf-8").read() or "{}")
    meta["status"] = new_status
    meta["updated_at"] = time.time()
    _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))
    return _read_tool(d, name)


def delete_tool(user_id: int, device_type: str, name: str, actor: str = "web", ai_config_id: Optional[int] = None) -> bool:
    d = _tools_dir(user_id, device_type)
    name = str(name or "").strip()
    current = _read_tool(d, name)
    if not current:
        return False
    _record_version(d, name, current, "delete", actor, ai_config_id)
    for path in (
        _meta_path(d, name),
        os.path.join(d, f"{name}.py"),
        os.path.join(d, f"{name}.js"),
        os.path.join(d, f"{name}.ps1"),
        os.path.join(d, f"{name}.sh"),
    ):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    # Tombstone so seed_defaults won't resurrect a deleted factory-default tool.
    _tombstone_add(d, name)
    return True


def _serialize_version(snap: Dict[str, Any], full: bool = False) -> Dict[str, Any]:
    out = {
        "version_id": snap.get("version_id"),
        "name": snap.get("name"),
        "revision": snap.get("revision"),
        "action": snap.get("action"),
        "actor": snap.get("actor"),
        "ai_config_id": snap.get("ai_config_id"),
        "description": snap.get("description"),
        "code_kind": snap.get("code_kind"),
        "created_at": float(snap.get("created_at") or 0),
    }
    if full:
        for k in ("input_schema", "code", "js", "runtime", "source", "permissions"):
            out[k] = snap.get(k)
    return out


def list_versions(user_id: int, device_type: str, name: str, limit: int = MAX_VERSIONS_PER_TOOL) -> List[Dict[str, Any]]:
    d = _tools_dir(user_id, device_type)
    hist = _history_dir(d, str(name or "").strip())
    if not os.path.isdir(hist):
        return []
    snaps = []
    for fn in os.listdir(hist):
        if not fn.endswith(".json"):
            continue
        try:
            snaps.append(json.loads(open(os.path.join(hist, fn), encoding="utf-8").read()))
        except Exception:
            continue
    snaps.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    return [_serialize_version(s) for s in snaps[:max(1, min(int(limit or 1), MAX_VERSIONS_PER_TOOL))]]


def get_version(user_id: int, device_type: str, version_id: int) -> Optional[Dict[str, Any]]:
    d = _tools_dir(user_id, device_type)
    root = os.path.join(d, ".history")
    if not os.path.isdir(root):
        return None
    target = f"{int(version_id)}.json"
    for name in os.listdir(root):
        path = os.path.join(root, name, target)
        if os.path.isfile(path):
            try:
                return _serialize_version(json.loads(open(path, encoding="utf-8").read()), full=True)
            except Exception:
                return None
    return None


def restore_version(user_id: int, device_type: str, version_id: int, actor: str = "web", ai_config_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    snap = get_version(user_id, device_type, version_id)
    if not snap:
        return None
    definition = {
        "name": snap["name"],
        "description": snap.get("description") or "",
        "input_schema": snap.get("input_schema") or {},
        "code_kind": snap.get("code_kind") or "program",
        "code": snap.get("code") or [],
        "js": snap.get("js") or "",
        "runtime": snap.get("runtime") or "",
        "source": snap.get("source") or "",
        "permissions": snap.get("permissions") or [],
    }
    return upsert_tool(user_id, device_type, definition, actor=actor, ai_config_id=ai_config_id, action="restore")


def device_payload(user_id: int, device_type: str) -> Dict[str, Any]:
    dtype = normalize_device_type(device_type)
    tools = [
        {
            "name": t["name"], "description": t["description"], "input_schema": t["input_schema"],
            "code_kind": t.get("code_kind") or "program", "code": t["code"], "js": t.get("js") or "",
            "runtime": t.get("runtime") or "", "source": t.get("source") or "", "permissions": t.get("permissions") or [],
        }
        for t in list_tools(user_id, dtype)
        if t.get("enabled") and t.get("status", "active") == "active"
    ]
    try:
        from api.services.device_tools import device_permission_policy as policy_svc
        permission_policy = policy_svc.get_policy(user_id, dtype)
    except Exception:
        permission_policy = {}
    return {
        "version": 1, "deviceType": dtype, "tools": tools,
        "revision": _revision(tools), "permissionPolicy": permission_policy,
    }


def seed_defaults(user_id: int, device_type: str = "desktop") -> int:
    """Seed factory-default tools into the user's workspace (idempotent: never
    clobbers an existing user-edited file). Desktop → powershell/shell runtime
    tools; browser → program wrappers for the plugin-advertised browser tools.

    Untouched Python factory defaults from the pre-PowerShell release are
    upgraded in place. Their exact revisions are known, so a custom Python tool
    with the same name remains untouched.
    """
    dtype = normalize_device_type(device_type)
    if dtype == "desktop":
        from api.services.device_tools.device_runtime_tools import (
            LEGACY_PYTHON_DEFAULT_REVISIONS as _legacy_python_revisions,
            load_default_tools as _load,
        )
    elif dtype == "browser":
        from api.services.device_tools.device_browser_runtime_tools import (
            load_default_tools as _load,
            sync_workspace_after_catalog_change,
        )
        _legacy_python_revisions = {}
    else:
        return 0
    d = _tools_dir(user_id, dtype)
    _migrate_db_once(user_id, dtype, d)
    created = 0
    for spec in _load():
        name = spec["name"]
        if os.path.isfile(_meta_path(d, name)):
            current = _read_tool(d, name)
            legacy_revision = _legacy_python_revisions.get(name)
            if not current or not legacy_revision or current.get("revision") != legacy_revision:
                continue
            # This is byte-for-byte an old factory default, not a user edit.
            # Preserve governance state while replacing the obsolete runtime.
            clean = validate_definition(spec)
            status = str(current.get("status") or "active")
            if status not in VALID_STATUSES:
                status = "active"
            _write_files(d, clean, enabled=bool(current.get("enabled", True)), status=status)
            try:
                os.remove(os.path.join(d, f"{name}.py"))
            except OSError:
                pass
            created += 1
            continue
        # Respect an explicit deletion: don't resurrect a tombstoned default.
        if _is_tombstoned(d, name):
            continue
        clean = validate_definition(spec)
        _write_files(d, clean, enabled=True, status="active")
        created += 1
    if dtype == "browser":
        sync_workspace_after_catalog_change(user_id)
    return created


def seed_from_tool_defs(user_id: int, device_type: str, tool_defs: Any) -> int:
    """No-op in the workspace model: devices no longer report fixed builtins to
    mirror. Kept for call-site compatibility with the old store."""
    return 0


# ---- one-time migration: existing DB rows -> workspace files ---------------

def _migrate_db_once(user_id: int, device_type: str, d: str) -> None:
    """If the workspace dir has no tools yet but the legacy DB store has rows
    for this user/type, copy them into files once. Best-effort; never writes DB."""
    marker = os.path.join(d, ".migrated")
    if os.path.exists(marker) or _tool_names(d):
        return
    try:
        from sqlmodel import Session, select
        from api.database import engine
        from api.models import DeviceDynamicTool
        dtype = normalize_device_type(device_type)
        with Session(engine) as session:
            rows = session.exec(
                select(DeviceDynamicTool).where(
                    DeviceDynamicTool.user_id == user_id,
                    DeviceDynamicTool.device_type == dtype,
                )
            ).all()
        for row in rows:
            try:
                clean = validate_definition({
                    "name": row.name, "description": row.description,
                    "input_schema": json.loads(row.input_schema_json or "{}"),
                    "code_kind": getattr(row, "code_kind", "program"),
                    "code": json.loads(row.code_json or "[]"),
                    "js": getattr(row, "js_source", "") or "",
                    "runtime": getattr(row, "runtime", "") or "",
                    "source": getattr(row, "source", "") or "",
                    "permissions": json.loads(getattr(row, "permissions_json", "") or "[]"),
                })
                _write_files(d, clean, bool(row.enabled), getattr(row, "status", "active") or "active")
            except Exception:
                continue
        os.makedirs(d, exist_ok=True)
        _atomic_write(marker, str(time.time()))
    except Exception:
        # DB unavailable / model changed — skip migration silently.
        pass
