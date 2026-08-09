"""One local/CI verification entrypoint with stable semantics."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[2]


def run(label: str, command: list[str], env: dict[str, str]) -> int:
    print(f"\n== {label} ==", flush=True)
    return subprocess.run(command, cwd=SERVER_ROOT, env=env, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--integration", action="store_true")
    args = parser.parse_args()
    env = os.environ.copy()
    env.setdefault("HEYSURE_DB_AUTO_MIGRATE", "0")
    checks = [
        ("complexity guardrails", [sys.executable, "other/scripts/check_guardrails.py"]),
        ("architecture", [sys.executable, "other/scripts/check_architecture.py"]),
        ("syntax", [sys.executable, "-m", "compileall", "-q", "main", "other/scripts"]),
    ]
    if not args.skip_tests:
        checks.append(("unit and contract tests", [sys.executable, "-m", "pytest", "-q", "-m", "not integration and not e2e and not deployment"]))
    if args.integration:
        checks.append(("integration tests", [sys.executable, "-m", "pytest", "-q", "-m", "integration or e2e or deployment"]))
    for label, command in checks:
        code = run(label, command, env)
        if code:
            print(f"verification stopped at {label} (exit {code})")
            return code
    print("\nserver verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
