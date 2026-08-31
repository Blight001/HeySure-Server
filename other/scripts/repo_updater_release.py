"""Safety-gated backup and rolling release helpers for the host updater."""

from __future__ import annotations

import os
import json
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional


DEFAULT_DATA_ROOT = Path("/www/wwwroot/heysureai2/deploy/server/data")
RUNTIME_SERVICES = ("api-gateway", "mcp-runtime", "connector-runtime", "ai-runtime")

RunCommand = Callable[[list[str], float], subprocess.CompletedProcess[str]]
RunStreaming = Callable[[list[str], float, Optional[dict[str, str]]], None]
LogLine = Callable[[str], None]
SetPhase = Callable[[str, str], None]


def _proxy_configured(root: Path) -> bool:
    """Return whether the checkout .env declares a Docker proxy."""
    try:
        lines = (root / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    names = {"DOCKER_HTTP_PROXY", "DOCKER_HTTPS_PROXY", "DOCKER_ALL_PROXY"}
    return any(
        "=" in line
        and not line.lstrip().startswith("#")
        and line.split("=", 1)[0].strip() in names
        and bool(line.split("=", 1)[1].strip().strip("\"'"))
        for line in lines
    )


def _docker_build_environment(root: Path) -> dict[str, str]:
    """Use host networking and the loopback proxy for Docker builds when available."""
    environment = dict(os.environ)
    environment["DOCKER_BUILD_NETWORK"] = "host"
    if not _proxy_configured(root):
        return environment
    try:
        with socket.create_connection(("127.0.0.1", 7890), timeout=1):
            pass
    except OSError:
        # An unusable proxy in .env must not leak into the build.  Empty
        # process environment values take precedence over Compose's .env.
        for name in (
            "DOCKER_HTTP_PROXY",
            "DOCKER_HTTPS_PROXY",
            "DOCKER_ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            environment[name] = ""
        return environment
    proxy = "http://127.0.0.1:7890"
    for name in ("DOCKER_HTTP_PROXY", "DOCKER_HTTPS_PROXY", "DOCKER_ALL_PROXY"):
        environment[name] = proxy
    return environment


class ReleaseSafetyError(RuntimeError):
    pass


def _compose(compose_cmd: list[str], *args: str) -> list[str]:
    return [*compose_cmd, *args]


def _container_id(run: RunCommand, compose_cmd: list[str], service: str) -> str:
    result = run(_compose(compose_cmd, "ps", "-q", service), 30)
    container_id = result.stdout.strip()
    if not container_id:
        raise ReleaseSafetyError(f"required compose service is missing: {service}")
    return container_id


def _inspect_value(run: RunCommand, container_id: str, template: str) -> str:
    return run(["docker", "inspect", "-f", template, container_id], 30).stdout.strip()


def _mounts(run: RunCommand, container_id: str) -> list[tuple[str, Path]]:
    template = '{{range .Mounts}}{{println .Destination "|" .Source}}{{end}}'
    mounts: list[tuple[str, Path]] = []
    for line in _inspect_value(run, container_id, template).splitlines():
        mounted_at, separator, source = line.partition("|")
        if separator:
            mounts.append((mounted_at.strip(), Path(source.strip()).resolve()))
    return mounts


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def validate_deployment_layout(
    root: Path,
    data_root: Path,
    compose_cmd: list[str],
    run: RunCommand,
) -> None:
    run(_compose(compose_cmd, "config", "--quiet"), 60)
    expected_root = root.resolve()
    expected_data = data_root.resolve()
    expected_mounts: dict[str, tuple[str, Path]] = {
        "db": ("/var/lib/postgresql/data", expected_data),
        **{
            service: ("/app/data", expected_data / "app")
            for service in RUNTIME_SERVICES
        },
    }
    label_template = '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
    config_template = '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
    containers = {
        service: _container_id(run, compose_cmd, service)
        for service in ("db", *RUNTIME_SERVICES, "web")
    }
    for service, container_id in containers.items():
        working_dir = Path(_inspect_value(run, container_id, label_template)).resolve()
        if working_dir != expected_root:
            raise ReleaseSafetyError(f"compose working directory mismatch for {service}")
        config_files = _inspect_value(run, container_id, config_template).split(",")
        if expected_root / "docker-compose.yml" not in {
            Path(path.strip()).resolve() for path in config_files if path.strip()
        }:
            raise ReleaseSafetyError(f"compose config file mismatch for {service}")
    for service, (destination, expected_source) in expected_mounts.items():
        mounts = _mounts(run, containers[service])
        actual_source = next((source for mounted_at, source in mounts if mounted_at == destination), None)
        if actual_source != expected_source:
            raise ReleaseSafetyError(f"persistent mount source mismatch for {service}")
        if service != "db" and any(
            source != expected_data / "app"
            and _overlaps(source, expected_data)
            for _mounted_at, source in mounts
        ):
            raise ReleaseSafetyError(f"runtime has unsafe PostgreSQL data access: {service}")
    db_mounts = _mounts(run, containers["db"])
    if any(
        _overlaps(source, expected_data)
        and (target, source) != ("/var/lib/postgresql/data", expected_data)
        for target, source in db_mounts
    ):
        raise ReleaseSafetyError("PostgreSQL has an unexpected persistent-data mount")
    if any(_overlaps(source, expected_data) for _target, source in _mounts(run, containers["web"])):
        raise ReleaseSafetyError("Web service has unsafe persistent-data access")
    db_environment = _inspect_value(
        run,
        containers["db"],
        "{{range .Config.Env}}{{println .}}{{end}}",
    ).splitlines()
    if "PGDATA=/var/lib/postgresql/data/postgres" not in db_environment:
        raise ReleaseSafetyError("PostgreSQL PGDATA is outside the reserved postgres directory")


def validate_rendered_compose(
    root: Path,
    data_root: Path,
    compose_cmd: list[str],
    run: RunCommand,
) -> None:
    raw = run(_compose(compose_cmd, "config", "--format", "json"), 60).stdout
    try:
        services = json.loads(raw)["services"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseSafetyError("rendered compose configuration is invalid") from exc
    expected_data = data_root.resolve()
    required = {"db", *RUNTIME_SERVICES, "web"}
    if not isinstance(services, dict) or not required.issubset(services):
        raise ReleaseSafetyError("rendered compose is missing required services")

    def mounts(service: str) -> list[tuple[str, Path, str]]:
        volumes = services[service].get("volumes") or []
        if not isinstance(volumes, list):
            raise ReleaseSafetyError(f"rendered compose volumes are invalid for {service}")
        parsed = []
        for volume in volumes:
            if not isinstance(volume, dict):
                raise ReleaseSafetyError(f"rendered compose volume is invalid for {service}")
            parsed.append((str(volume.get("target") or ""), Path(str(volume.get("source") or "")).resolve(), str(volume.get("type") or "")))
        return parsed

    db_mounts = mounts("db")
    if ("/var/lib/postgresql/data", expected_data, "bind") not in db_mounts:
        raise ReleaseSafetyError("rendered compose PostgreSQL mount is unsafe")
    environment = services["db"].get("environment") or {}
    if not isinstance(environment, dict) or environment.get("PGDATA") != "/var/lib/postgresql/data/postgres":
        raise ReleaseSafetyError("rendered compose PostgreSQL PGDATA is unsafe")
    if any(
        _overlaps(source, expected_data)
        and (target, source, kind) != ("/var/lib/postgresql/data", expected_data, "bind")
        for target, source, kind in db_mounts
    ):
        raise ReleaseSafetyError("rendered compose PostgreSQL has an unexpected data mount")
    for service in RUNTIME_SERVICES:
        runtime_mounts = mounts(service)
        if ("/app/data", expected_data / "app", "bind") not in runtime_mounts:
            raise ReleaseSafetyError(f"rendered compose data mount is unsafe for {service}")
        for _target, source, _kind in runtime_mounts:
            if source != expected_data / "app" and _overlaps(source, expected_data):
                raise ReleaseSafetyError(f"rendered compose exposes PostgreSQL data to {service}")
    if any(_overlaps(source, expected_data) for _target, source, _kind in mounts("web")):
        raise ReleaseSafetyError("rendered compose exposes persistent data to Web")


def create_postgres_backup(
    root: Path,
    data_root: Path,
    compose_cmd: list[str],
    run: RunCommand,
    target_sha: str,
) -> tuple[Path, int]:
    run(_compose(compose_cmd, "up", "-d", "db"), 120)
    backup_dir = data_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    unique = f"{time.time_ns()}-{secrets.token_hex(4)}"
    backup = backup_dir / f"repo-rollback-{stamp}-{target_sha[:8]}-{unique}.dump"
    partial = backup.with_suffix(".dump.partial")
    command = _compose(
        compose_cmd,
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        'exec pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
    )
    try:
        descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            result = subprocess.run(
                command,
                cwd=root,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=600,
                check=False,
            )
        size = partial.stat().st_size
        with partial.open("rb") as source:
            header = source.read(5)
        if result.returncode or size <= 5 or header != b"PGDMP":
            raise ReleaseSafetyError("PostgreSQL custom-format backup validation failed")
        partial.rename(backup)
        os.chmod(backup, 0o600)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, ReleaseSafetyError):
            raise
        raise ReleaseSafetyError("PostgreSQL custom-format backup failed") from exc
    return backup, size


def prepare_release(
    root: Path,
    data_root: Path,
    compose_cmd: list[str],
    run: RunCommand,
    target_sha: str,
) -> tuple[Path, int]:
    validate_deployment_layout(root, data_root, compose_cmd, run)
    return create_postgres_backup(root, data_root, compose_cmd, run, target_sha)


def deploy_release(
    root: Path,
    data_root: Path,
    compose_cmd: list[str],
    run: RunCommand,
    run_streaming: RunStreaming,
    log: LogLine,
    set_phase: SetPhase,
) -> None:
    validate_rendered_compose(root, data_root, compose_cmd, run)
    release_script = root / "deploy" / "server" / "other" / "scripts" / "rolling_release.py"
    if not release_script.is_file():
        raise ReleaseSafetyError("target version does not contain rolling_release.py")
    environment = _docker_build_environment(root)
    environment["HEYSURE_COMPOSE_DIR"] = str(root.resolve())
    set_phase("rebuilding", "running readiness-gated server release")
    log("开始执行数据库迁移与四 Runtime 滚动发布...")
    run_streaming(
        [sys.executable, str(release_script), "--timeout", "180"],
        2400,
        environment,
    )
    set_phase("restarting", "building and replacing web service")
    log("Runtime 已通过 readiness，开始构建 Web...")
    run_streaming(_compose(compose_cmd, "build", "web"), 1800, None)
    run(_compose(compose_cmd, "up", "-d", "--no-deps", "web"), 300)
    _container_id(run, compose_cmd, "web")
    run(["curl", "-fsS", "http://127.0.0.1:3000/"], 30)
    run(["curl", "-fsS", "http://127.0.0.1:58150/"], 30)
    validate_deployment_layout(root, data_root, compose_cmd, run)
