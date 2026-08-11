"""Readiness-gated Compose release with per-service image rollback.

Run from any directory after CI passes. The script builds the migration and
runtime images first, migrates once, replaces one runtime at a time, and never
advances until the new container reports ready. An old image is restored only
when the database revision did not change during this release.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
COMPOSE_ROOT = Path(os.environ.get("HEYSURE_COMPOSE_DIR", str(WORKSPACE_ROOT))).resolve()
SERVICES = (
    ("api-gateway", 3000),
    ("mcp-runtime", 3001),
    ("connector-runtime", 3002),
    ("ai-runtime", 3003),
)


def command(*args: str, capture: bool = False, check: bool = True) -> str:
    result = subprocess.run(
        args,
        cwd=COMPOSE_ROOT,
        check=False,
        text=True,
        capture_output=capture,
    )
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}")
    return (result.stdout or "").strip()


def compose(*args: str, capture: bool = False, check: bool = True) -> str:
    return command("docker", "compose", *args, capture=capture, check=check)


@dataclass(frozen=True)
class PreviousImage:
    image_id: str
    image_name: str


def previous_image(service: str) -> Optional[PreviousImage]:
    container_id = compose("ps", "-q", service, capture=True, check=False)
    if not container_id:
        return None
    image_id = command("docker", "inspect", "-f", "{{.Image}}", container_id, capture=True)
    image_name = command(
        "docker", "inspect", "-f", "{{.Config.Image}}", container_id, capture=True
    )
    return PreviousImage(image_id=image_id, image_name=image_name)


def wait_ready(service: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    probe = (
        "curl -fsS -H \"Authorization: Bearer $HEYSURE_INTERNAL_TOKEN\" "
        f"http://127.0.0.1:{port}/internal/health/ready >/dev/null"
    )
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", service, "sh", "-c", probe],
            cwd=COMPOSE_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(2)
    raise RuntimeError(f"{service} readiness timed out after {timeout:.0f}s")


def rollback(service: str, previous: Optional[PreviousImage], timeout: float, port: int) -> None:
    if not previous:
        compose("stop", service, check=False)
        return
    command("docker", "tag", previous.image_id, previous.image_name)
    compose("up", "-d", "--no-deps", "--force-recreate", service)
    wait_ready(service, port, timeout)


def database_revision() -> str:
    query = 'SELECT version_num FROM alembic_version ORDER BY version_num'
    return compose(
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "{query}"',
        capture=True,
    )


def release(timeout: float) -> None:
    compose("up", "-d", "db")
    compose("build", "db-migrate", *(service for service, _port in SERVICES))
    revision_before = database_revision()
    compose("run", "--rm", "db-migrate")
    revision_changed = database_revision() != revision_before
    for service, port in SERVICES:
        previous = previous_image(service)
        try:
            compose("up", "-d", "--no-deps", service)
            wait_ready(service, port, timeout)
            print(f"ready: {service}")
        except Exception:
            if revision_changed:
                print(
                    f"release failed: {service}; database revision advanced, "
                    "so the schema-incompatible previous image was not restored"
                )
            else:
                print(f"release failed: {service}; restoring previous image")
                rollback(service, previous, timeout, port)
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    try:
        release(max(10.0, args.timeout))
    except Exception as exc:
        print(exc)
        return 1
    print("readiness-gated server release completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
