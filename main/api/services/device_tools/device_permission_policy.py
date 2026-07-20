"""Read/write the per-user, device-type permission policy for runtime tools.

The policy maps each permission tag to a decision (allow / deny). It is
shipped to devices inside the ``device:tool-config`` push and applied by the
device permission guard. Unknown tags / decisions are dropped on write so a
malformed policy can never weaken the device's safe defaults.
"""

import json
import time
from typing import Any, Dict

from sqlmodel import Session, select

from api.database import engine
from api.models import DevicePermissionPolicy
from api.services.device_tools.device_dynamic_tools import KNOWN_PERMISSION_TAGS, normalize_device_type

VALID_DECISIONS = ("allow", "deny")


def _sanitize(policy: Any) -> Dict[str, str]:
    if not isinstance(policy, dict):
        return {}
    clean: Dict[str, str] = {}
    for tag, decision in policy.items():
        key = str(tag or "").strip()
        value = str(decision or "").strip().lower()
        # 旧版的 confirm 策略迁移为直接允许，系统不再向用户请求 MCP 确认。
        if value == "confirm":
            value = "allow"
        if key in KNOWN_PERMISSION_TAGS and value in VALID_DECISIONS:
            clean[key] = value
    return clean


def get_policy(user_id: int, device_type: str) -> Dict[str, str]:
    dtype = normalize_device_type(device_type)
    with Session(engine) as session:
        row = session.exec(
            select(DevicePermissionPolicy).where(
                DevicePermissionPolicy.user_id == user_id,
                DevicePermissionPolicy.device_type == dtype,
            )
        ).first()
        if not row:
            return {}
        try:
            parsed = json.loads(row.policy_json or "{}")
        except Exception:
            return {}
        return _sanitize(parsed)


def set_policy(user_id: int, device_type: str, policy: Any) -> Dict[str, str]:
    dtype = normalize_device_type(device_type)
    clean = _sanitize(policy)
    now = time.time()
    with Session(engine) as session:
        row = session.exec(
            select(DevicePermissionPolicy).where(
                DevicePermissionPolicy.user_id == user_id,
                DevicePermissionPolicy.device_type == dtype,
            )
        ).first()
        if not row:
            row = DevicePermissionPolicy(user_id=user_id, device_type=dtype)
            session.add(row)
        row.policy_json = json.dumps(clean, ensure_ascii=False)
        row.updated_at = now
        session.commit()
    return clean


def known_tags() -> list:
    return list(KNOWN_PERMISSION_TAGS)
