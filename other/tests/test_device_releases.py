import json

import pytest

from api.services.device_releases import (
    DeviceReleaseError,
    public_catalog,
    resolve_artifact,
    update_info,
    version_key,
)


def _write_catalog(root, artifact="windows/setup.exe", version="1.2.0"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "catalog.json").write_text(json.dumps({
        "schema_version": 1,
        "products": [{
            "id": "windows-desktop",
            "name": "Windows",
            "targets": [{
                "id": "windows-x86_64-stable",
                "version": version,
                "artifact": artifact,
                "sha256": "abc",
            }],
        }],
    }), encoding="utf-8")


def test_catalog_only_exposes_existing_artifact(tmp_path):
    _write_catalog(tmp_path)
    target = public_catalog("https://example.test", tmp_path)["products"][0]["targets"][0]
    assert target["available"] is False
    assert target["download_url"] is None

    artifact = tmp_path / "artifacts" / "windows" / "setup.exe"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"installer")
    target = public_catalog("https://example.test", tmp_path)["products"][0]["targets"][0]
    assert target["available"] is True
    assert target["size_bytes"] == 9
    assert target["download_url"].endswith("/windows-desktop/windows-x86_64-stable")


def test_update_info_compares_versions_and_resolves_download(tmp_path):
    _write_catalog(tmp_path)
    artifact = tmp_path / "artifacts" / "windows" / "setup.exe"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"installer")
    result = update_info("https://example.test", "windows-desktop", "windows-x86_64-stable", "1.1.9", tmp_path)
    assert result["update_available"] is True
    assert result["latest_version"] == "1.2.0"
    assert "device-hall=1" in result["release_page_url"]
    assert "product=windows-desktop" in result["release_page_url"]
    assert resolve_artifact("windows-desktop", "windows-x86_64-stable", tmp_path) == artifact
    assert update_info("https://example.test", "windows-desktop", "windows-x86_64-stable", "1.2.0", tmp_path)["update_available"] is False


def test_artifact_path_cannot_escape_release_root(tmp_path):
    _write_catalog(tmp_path, "../../secret.txt")
    with pytest.raises(DeviceReleaseError):
        public_catalog("https://example.test", tmp_path)


def test_version_key_accepts_semver_and_build_suffixes():
    assert version_key("v1.10.2-beta.1") > version_key("1.9.9")


def test_external_release_url_rejects_non_http_scheme(tmp_path):
    _write_catalog(tmp_path)
    catalog = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))
    catalog["products"][0]["targets"][0]["external_url"] = "javascript:alert(1)"
    (tmp_path / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(DeviceReleaseError):
        public_catalog("https://example.test", tmp_path)
