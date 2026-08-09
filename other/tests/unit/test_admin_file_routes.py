import os

import pytest
from fastapi import HTTPException

from gateway.routers import admin_file_routes


def test_admin_file_router_exposes_expected_paths():
    paths = {route.path for route in admin_file_routes.router.routes}
    assert paths == {
        "/files",
        "/files/batch-delete",
        "/files/mkdir",
        "/files/raw",
        "/files/read",
        "/files/rename",
    }


def test_safe_data_path_rejects_escape(monkeypatch, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(admin_file_routes, "DATA_ROOT", os.path.realpath(root))

    with pytest.raises(HTTPException) as caught:
        admin_file_routes.safe_data_path("../outside.txt")

    assert caught.value.status_code == 400


def test_file_kind_preserves_viewer_contract():
    assert admin_file_routes.file_kind("image.webp") == "image"
    assert admin_file_routes.file_kind("config.json") == "text"
    assert admin_file_routes.file_kind("archive.zip") == "binary"
    assert admin_file_routes.file_kind("README") == "text"
