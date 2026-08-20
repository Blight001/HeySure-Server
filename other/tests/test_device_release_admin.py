import hashlib
import io
import json

import pytest

from api.services.device_release_admin import admin_catalog, publish_release, withdraw_release
from api.services.device_releases import DeviceReleaseError, resolve_artifact


def _catalog(root):
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "products": [{
            "id": "windows-desktop",
            "name": "Windows",
            "targets": [{
                "id": "windows-x86_64-stable",
                "platform": "windows",
                "version": "0.1.0",
                "artifact": "windows/placeholder.exe",
            }],
        }],
    }
    (root / "catalog.json").write_text(json.dumps(payload), encoding="utf-8")


def test_publish_streams_artifact_and_atomically_updates_catalog(tmp_path):
    _catalog(tmp_path)
    content = b"signed-installer"
    result = publish_release(
        product_id="windows-desktop", target_id="windows-x86_64-stable",
        version="1.2.0", filename="HeySure Setup.exe", stream=io.BytesIO(content),
        release_notes="new", root=tmp_path,
    )
    target = admin_catalog(tmp_path)["products"][0]["targets"][0]
    assert result["release"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert target["version"] == "1.2.0"
    assert target["filename"].endswith("1.2.0.exe")
    assert resolve_artifact("windows-desktop", "windows-x86_64-stable", tmp_path).read_bytes() == content
    assert list((tmp_path / "backups").glob("catalog.*.json"))


def test_same_version_cannot_be_overwritten(tmp_path):
    _catalog(tmp_path)
    kwargs = dict(product_id="windows-desktop", target_id="windows-x86_64-stable",
                  version="1.2.0", filename="setup.exe", root=tmp_path)
    publish_release(stream=io.BytesIO(b"first"), **kwargs)
    with pytest.raises(DeviceReleaseError, match="already exists"):
        publish_release(stream=io.BytesIO(b"second"), **kwargs)


def test_rejects_extension_and_oversized_upload(tmp_path):
    _catalog(tmp_path)
    common = dict(product_id="windows-desktop", target_id="windows-x86_64-stable",
                  version="1.2.0", root=tmp_path)
    with pytest.raises(DeviceReleaseError, match="file type"):
        publish_release(filename="payload.apk", stream=io.BytesIO(b"x"), **common)
    with pytest.raises(DeviceReleaseError, match="size limit"):
        publish_release(filename="setup.exe", stream=io.BytesIO(b"too-large"), max_bytes=3, **common)
    assert not list(tmp_path.glob(".upload.*"))


def test_withdraw_current_falls_back_to_previous_release(tmp_path):
    _catalog(tmp_path)
    base = dict(product_id="windows-desktop", target_id="windows-x86_64-stable",
                filename="setup.exe", root=tmp_path)
    publish_release(version="1.0.0", stream=io.BytesIO(b"one"), **base)
    publish_release(version="2.0.0", stream=io.BytesIO(b"two"), **base)
    result = withdraw_release("windows-desktop", "windows-x86_64-stable", root=tmp_path)
    target = admin_catalog(tmp_path)["products"][0]["targets"][0]
    assert result["version"] == "2.0.0"
    assert target["version"] == "1.0.0"


def test_filename_path_traversal_is_rejected(tmp_path):
    _catalog(tmp_path)
    with pytest.raises(DeviceReleaseError, match="filename"):
        publish_release(
            product_id="windows-desktop", target_id="windows-x86_64-stable",
            version="1.0.0", filename="../setup.exe", stream=io.BytesIO(b"x"), root=tmp_path,
        )
