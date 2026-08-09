"""Check split-runtime dependency and startup-side-effect boundaries."""

from __future__ import annotations

import ast
import json
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).with_name("architecture_baseline.json")
RUNTIME_PACKAGES = {"gateway", "ai_runtime", "mcp_runtime", "connector_runtime"}
DDL_CALLS = {"create_all", "drop_all", "run_pending_migrations", "ensure_schema"}


def _module(imported: str | None) -> str:
    return (imported or "").split(".", 1)[0]


def _scan_api_imports() -> list[str]:
    violations: list[str] = []
    for path in sorted((SERVER_ROOT / "main" / "api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(SERVER_ROOT).as_posix()
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                if _module(name) in RUNTIME_PACKAGES:
                    violations.append(f"runtime-import:{relative}:{node.lineno}:{name}")
    return violations


def _scan_import_time_calls() -> list[str]:
    violations: list[str] = []
    for path in sorted((SERVER_ROOT / "main" / "api").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(SERVER_ROOT).as_posix()
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in DDL_CALLS or name.startswith("start_") or name.startswith("reset_"):
                violations.append(f"import-side-effect:{relative}:{node.lineno}:{name}")
    return violations


def main() -> int:
    current = sorted([*_scan_api_imports(), *_scan_import_time_calls()])
    if not BASELINE_PATH.exists():
        BASELINE_PATH.write_text(json.dumps({"version": 1, "violations": current}, indent=2) + "\n", encoding="utf-8")
        print(f"wrote architecture baseline with {len(current)} violations")
        return 0
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8")).get("violations", [])
    if len(current) > len(baseline):
        print(f"architecture regressions: {len(current)} violations exceeds baseline {len(baseline)}")
        for item in current:
            print(f"  {item}")
        return 1
    print(
        f"architecture passed: {len(current)} violations "
        f"(baseline {len(baseline)}; debt may move only while total decreases)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
