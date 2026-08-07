"""Access-controlled filesystem storage for oversized projected step results."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Tuple

from api.core.settings import DATA_DIR, settings


ROOT = Path(DATA_DIR) / "workflow_results"
PREFIX = "workflow-result:"


def save_result(user_id: int, run_id: str, value: Any, *, max_bytes: int | None = None) -> Tuple[str, int]:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    allowed = min(int(settings.workflow_max_result_bytes), int(max_bytes or settings.workflow_max_result_bytes))
    if len(raw) > allowed:
        raise ValueError("STEP_RESULT_TOO_LARGE")
    folder = ROOT / str(int(user_id)) / str(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.json"
    path = folder / filename
    path.write_bytes(raw)
    return f"{PREFIX}{int(user_id)}/{run_id}/{filename}", len(raw)


def load_result(reference: str, user_id: int, run_id: str) -> Any:
    raw_ref = str(reference or "")
    expected_prefix = f"{PREFIX}{int(user_id)}/{run_id}/"
    if not raw_ref.startswith(expected_prefix):
        raise FileNotFoundError("workflow result reference is not owned by this run")
    relative = raw_ref[len(PREFIX):]
    candidate = (ROOT / relative).resolve(strict=True)
    root = ROOT.resolve()
    if root not in candidate.parents or candidate.suffix != ".json":
        raise FileNotFoundError("invalid workflow result reference")
    return json.loads(candidate.read_text(encoding="utf-8"))


def cleanup_expired_results(now: float | None = None) -> int:
    if not ROOT.exists():
        return 0
    cutoff = float(now or time.time()) - max(3600, int(settings.workflow_result_retention_seconds))
    removed = 0
    for path in ROOT.rglob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    # Remove only empty descendants, never the configured root itself.
    for folder in sorted((item for item in ROOT.rglob("*") if item.is_dir()), reverse=True):
        try:
            os.rmdir(folder)
        except OSError:
            pass
    return removed
