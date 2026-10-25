from pathlib import Path

from api.services.storage.workspace_visibility import list_workspace_entries
# Initialise the MCP package before importing a handler module; the package
# registry owns the builtin catalog and intentionally imports that handler.
import mcp_runtime.mcp  # noqa: F401
from tools import workspace


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_curated_listing_hides_runtime_and_sensitive_paths(tmp_path):
    _write(tmp_path / "src" / "main.py")
    _write(tmp_path / "dist" / "bundle.js")
    _write(tmp_path / "logs" / "server.log")
    _write(tmp_path / ".env", "SECRET=hidden")
    _write(tmp_path / "notes.txt")

    paths = {item["path"] for item in list_workspace_entries(str(tmp_path))["entries"]}

    assert "src" in paths
    assert "src/main.py" in paths
    assert "notes.txt" in paths
    assert "dist" not in paths
    assert "dist/bundle.js" not in paths
    assert "logs" not in paths
    assert ".env" not in paths


def test_listing_can_explicitly_include_generated_and_hidden_files(tmp_path):
    _write(tmp_path / "dist" / "bundle.js")
    _write(tmp_path / ".env", "SECRET=hidden")

    listing = list_workspace_entries(
        str(tmp_path),
        include_generated=True,
        include_hidden=True,
    )
    paths = {item["path"] for item in listing["entries"]}

    assert "dist/bundle.js" in paths
    assert ".env" in paths


def test_workspace_list_tool_uses_current_ai_workspace(monkeypatch, tmp_path):
    _write(tmp_path / "src" / "main.py")
    _write(tmp_path / "dist" / "bundle.js")
    monkeypatch.setattr(workspace, "get_project_root", lambda _user, _config: str(tmp_path))

    result = workspace._list_workspace_files(7, {"query": "main"}, 42)

    assert result["workspace_root"] == "."
    assert result["count"] == 1
    assert result["entries"][0]["path"] == "src/main.py"

