from pathlib import Path
import re

import pytest


@pytest.mark.deployment
def test_public_gateway_base_reaches_gateway_and_mcp_runtime():
    workspace_compose = Path(__file__).resolve().parents[5] / "docker-compose.yml"
    if not workspace_compose.is_file():
        pytest.skip("workspace Compose is not present in a standalone server checkout")

    source = workspace_compose.read_text(encoding="utf-8")
    def service_block(name: str) -> str:
        match = re.search(rf"(?ms)^  {re.escape(name)}:\n.*?(?=^  [a-z][\w-]*:\n|\Z)", source)
        assert match is not None
        return match.group(0)

    gateway = service_block("api-gateway")
    mcp_runtime = service_block("mcp-runtime")
    expected = "HEYSURE_PUBLIC_BASE_URL: ${HEYSURE_PUBLIC_BASE_URL:-${PUBLIC_BASE_URL:-}}"

    assert expected in gateway
    assert expected in mcp_runtime


@pytest.mark.deployment
def test_bot_encryption_secret_is_required_by_all_runtimes():
    workspace_compose = Path(__file__).resolve().parents[5] / "docker-compose.yml"
    if not workspace_compose.is_file():
        pytest.skip("workspace Compose is not present in a standalone server checkout")

    source = workspace_compose.read_text(encoding="utf-8")

    def service_block(name: str) -> str:
        match = re.search(rf"(?ms)^  {re.escape(name)}:\n.*?(?=^  [a-z][\w-]*:\n|\Z)", source)
        assert match is not None
        return match.group(0)

    expected = "HEYSURE_BOT_ENCRYPTION_SECRET: ${HEYSURE_BOT_ENCRYPTION_SECRET:?required}"
    for service in ("api-gateway", "ai-runtime", "mcp-runtime", "connector-runtime"):
        assert expected in service_block(service)


@pytest.mark.deployment
def test_web_homepage_does_not_depend_on_gateway_readiness():
    workspace_compose = Path(__file__).resolve().parents[5] / "docker-compose.yml"
    if not workspace_compose.is_file():
        pytest.skip("workspace Compose is not present in a standalone server checkout")

    source = workspace_compose.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^  web:\n.*?(?=^  [a-z][\w-]*:\n|\Z)", source)
    assert match is not None
    assert "depends_on:" not in match.group(0)
