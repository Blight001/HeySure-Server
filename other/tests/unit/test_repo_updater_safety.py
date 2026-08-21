from __future__ import annotations

import copy
import importlib.util
import json
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from other.scripts import repo_updater_release
from other.scripts.repo_updater_commands import run_streaming
from other.scripts.repo_updater_release import (
    RUNTIME_SERVICES,
    ReleaseSafetyError,
    create_postgres_backup,
    validate_deployment_layout,
    validate_rendered_compose,
)
from other.scripts.repo_updater_versions import rollback_candidate, version_history


def _rendered_services(data_root: Path) -> dict[str, object]:
    bind = lambda source, target: {"type": "bind", "source": str(source), "target": target}
    return {
        "db": {
            "environment": {"PGDATA": "/var/lib/postgresql/data/postgres"},
            "volumes": [bind(data_root, "/var/lib/postgresql/data")],
        },
        **{
            service: {"volumes": [bind(data_root / "app", "/app/data")]}
            for service in RUNTIME_SERVICES
        },
        "web": {"volumes": []},
    }


def _rendered_runner(services: dict[str, object]):
    def run(_command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=json.dumps({"services": services}), stderr="")

    return run


def test_rendered_compose_accepts_only_canonical_persistent_mounts(tmp_path: Path):
    data_root = (tmp_path / "data").resolve()
    services = _rendered_services(data_root)

    validate_rendered_compose(tmp_path, data_root, ["docker", "compose"], _rendered_runner(services))


@pytest.mark.parametrize("service", ["api-gateway", "web"])
def test_rendered_compose_rejects_backup_mount_for_runtime_or_web(tmp_path: Path, service: str):
    data_root = (tmp_path / "data").resolve()
    services = copy.deepcopy(_rendered_services(data_root))
    services[service]["volumes"].append(
        {"type": "bind", "source": str(data_root / "backups"), "target": "/leaked"}
    )

    with pytest.raises(ReleaseSafetyError):
        validate_rendered_compose(tmp_path, data_root, ["docker", "compose"], _rendered_runner(services))


class _LiveLayoutRunner:
    def __init__(self, root: Path, data_root: Path, unsafe_service: str):
        self.root = root.resolve()
        self.data_root = data_root.resolve()
        self.unsafe_service = unsafe_service

    def __call__(self, command: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "compose", "config"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:4] == ["docker", "compose", "ps", "-q"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"cid-{command[4]}\n", stderr="")
        template, container_id = command[3], command[4]
        service = container_id.removeprefix("cid-")
        if "working_dir" in template:
            output = str(self.root)
        elif "config_files" in template:
            output = str(self.root / "docker-compose.yml")
        elif ".Mounts" in template:
            output = self._mount_output(service)
        else:
            output = "PGDATA=/var/lib/postgresql/data/postgres\n" if service == "db" else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def _mount_output(self, service: str) -> str:
        if service == "db":
            mounts = [("/var/lib/postgresql/data", self.data_root)]
        elif service in RUNTIME_SERVICES:
            mounts = [("/app/data", self.data_root / "app")]
        else:
            mounts = []
        if service == self.unsafe_service:
            mounts.append(("/leaked", self.data_root / "backups"))
        return "".join(f"{target} | {source}\n" for target, source in mounts)


@pytest.mark.parametrize("service", ["connector-runtime", "web"])
def test_live_layout_rejects_backup_mount_for_runtime_or_web(tmp_path: Path, service: str):
    data_root = tmp_path / "data"
    runner = _LiveLayoutRunner(tmp_path, data_root, service)

    with pytest.raises(ReleaseSafetyError):
        validate_deployment_layout(tmp_path, data_root, ["docker", "compose"], runner)


def test_streaming_command_times_out_without_output(tmp_path: Path):
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="timed out"):
        run_streaming(
            tmp_path,
            [sys.executable, "-c", "import time; time.sleep(5)"],
            0.1,
            None,
            lambda _line: None,
            RuntimeError,
        )

    assert time.monotonic() - started < 2


def test_backup_is_validated_and_never_world_readable(tmp_path: Path, monkeypatch):
    secure_create_modes: list[int] = []
    chmod_modes: list[int] = []
    real_open = repo_updater_release.os.open
    real_chmod = repo_updater_release.os.chmod

    def secure_open(path, flags, mode):
        secure_create_modes.append(mode)
        return real_open(path, flags, mode)

    def secure_chmod(path, mode):
        chmod_modes.append(mode)
        return real_chmod(path, mode)

    def fake_pg_dump(command, **kwargs):
        kwargs["stdout"].write(b"PGDMPsafe-backup")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(repo_updater_release.os, "open", secure_open)
    monkeypatch.setattr(repo_updater_release.os, "chmod", secure_chmod)
    monkeypatch.setattr(repo_updater_release.subprocess, "run", fake_pg_dump)
    run = lambda command, _timeout: subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    backup, size = create_postgres_backup(
        tmp_path,
        tmp_path / "data",
        ["docker", "compose"],
        run,
        "b" * 40,
    )

    assert backup.read_bytes() == b"PGDMPsafe-backup"
    assert size == len(b"PGDMPsafe-backup")
    assert secure_create_modes == [0o600]
    assert chmod_modes == [0o600]
    if repo_updater_release.os.name != "nt":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert not list(backup.parent.glob("*.partial"))


def test_first_parent_target_with_same_migration_tree_is_eligible():
    current_sha, target_sha = "a" * 40, "b" * 40
    current_server, target_server, migration_tree = "c" * 40, "d" * 40, "e" * 40
    compose = "\n".join(
        [
            "- /www/wwwroot/heysureai2/deploy/server/data:/var/lib/postgresql/data",
            *["- /www/wwwroot/heysureai2/deploy/server/data/app:/app/data"] * 4,
            "PGDATA=/var/lib/postgresql/data/postgres",
        ]
    )

    def git_output(args: list[str], _timeout: float) -> str:
        if args == ["rev-parse", "HEAD"]:
            return current_sha
        if args[0] == "log":
            assert "--first-parent" in args
            return (
                f"{current_sha}\x1faaaaaaaa\x1fAdmin\x1f2\x1fCurrent\x1e"
                f"{target_sha}\x1fbbbbbbbb\x1fAdmin\x1f1\x1fPrevious\x1e"
            )
        if args[0] == "ls-tree":
            root_ref = args[1]
            server_sha = current_server if root_ref in {"HEAD", current_sha} else target_server
            return f"160000 commit {server_sha}\tdeploy/server"
        if args[0] == "show":
            return compose
        if args == ["rev-parse", "--verify", f"{target_sha}^{{commit}}"]:
            return target_sha
        raise AssertionError(args)

    def server_git_output(args: list[str], _timeout: float) -> str:
        if args[0] == "rev-parse":
            return migration_tree
        if args[0] == "cat-file":
            return ""
        raise AssertionError(args)

    history = version_history(git_output, server_git_output)
    from_sha, selected = rollback_candidate(git_output, server_git_output, target_sha)

    assert history["versions"][0]["disabled_reason"] == "当前版本"
    assert history["versions"][1]["rollback_eligible"] is True
    assert from_sha == current_sha
    assert selected["sha"] == target_sha


def _load_host_updater():
    script = Path(__file__).parents[2] / "scripts" / "repo-updater.py"
    spec = importlib.util.spec_from_file_location("repo_updater_under_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployed_marker_is_written_only_after_expected_checkout_revalidation(monkeypatch):
    updater = _load_host_updater()
    events: list[object] = []
    version = {"current": {"sha": "a" * 40}}
    monkeypatch.setattr(updater, "_verify_expected_checkout", lambda sha: events.append(("verify", sha)))
    monkeypatch.setattr(updater, "deploy_release", lambda *_args: events.append("deploy"))
    monkeypatch.setattr(updater, "_deployment_snapshot", lambda: events.append("snapshot") or version)
    monkeypatch.setattr(updater, "_write_version_file", lambda payload: events.append(("write", payload)))
    monkeypatch.setattr(updater, "_append_log", lambda _line: None)
    monkeypatch.setattr(updater, "_set_state", lambda **_fields: None)

    updater._publish_checked_out_version("a" * 40)

    assert events == [
        ("verify", "a" * 40),
        "deploy",
        ("verify", "a" * 40),
        "snapshot",
        ("write", version),
    ]


@pytest.mark.parametrize(
    ("checkout_sha", "deployed_sha", "expected_consistent"),
    [
        ("a" * 40, "a" * 40, True),
        ("b" * 40, "a" * 40, False),
    ],
)
def test_release_failure_consistency_uses_deployed_marker(
    monkeypatch, checkout_sha: str, deployed_sha: str, expected_consistent: bool
):
    updater = _load_host_updater()
    captured: dict[str, object] = {}
    monkeypatch.setattr(updater, "_read_version_file", lambda: {"current": {"sha": deployed_sha}})
    monkeypatch.setattr(updater, "_commit_info", lambda _ref: {"sha": checkout_sha})
    monkeypatch.setattr(updater, "verify_deployment_checkout", lambda *_args: None)
    monkeypatch.setattr(updater, "_append_log", lambda _line: None)
    monkeypatch.setattr(updater, "_set_state", lambda **fields: captured.update(fields))

    updater._mark_release_failed("update failed", subprocess.TimeoutExpired("git fetch", 180))

    assert captured["deployment_consistent"] is expected_consistent
