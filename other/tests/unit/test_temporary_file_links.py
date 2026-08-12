import hashlib
import time

import pytest
from fastapi import HTTPException

from api.services.storage import temporary_file_links as links


def _patch_grants(tmp_path, monkeypatch, source):
    monkeypatch.setattr(links, "GRANT_DIR", tmp_path / "grants")
    monkeypatch.setattr(
        links,
        "resolve_file_ref",
        lambda **kwargs: {
            "file_ref": kwargs["file_ref"],
            "file_name": source.name,
            "mime_type": "text/plain",
            "bytes": source.stat().st_size,
            "server_path": str(source),
        },
    )


def test_create_and_resolve_temporary_link_without_storing_plain_token(tmp_path, monkeypatch):
    source = tmp_path / "报告.txt"
    source.write_bytes(b"temporary-link")
    _patch_grants(tmp_path, monkeypatch, source)
    created = links.create_temporary_file_link(
        user_id=7,
        ai_config_id=19,
        file_ref="file_" + "a" * 32,
        public_base_url="https://heysure.example/",
        now=1000,
    )

    assert created["ttl_seconds"] == 300
    assert created["expires_at"] == 1300
    assert created["sha256"] == hashlib.sha256(b"temporary-link").hexdigest()
    token = created["url"].rsplit("/", 1)[1]
    stored = (links.GRANT_DIR / f"{created['grant_id']}.json").read_text(encoding="utf-8")
    assert token not in stored
    resolved = links.resolve_temporary_file_link(created["grant_id"], token, now=1299)
    assert resolved["server_path"] == str(source)


def test_expired_wrong_token_and_changed_source_are_rejected(tmp_path, monkeypatch):
    source = tmp_path / "file.txt"
    source.write_bytes(b"original")
    _patch_grants(tmp_path, monkeypatch, source)
    created = links.create_temporary_file_link(
        user_id=7,
        ai_config_id=19,
        file_ref="file_" + "b" * 32,
        public_base_url="https://heysure.example",
        ttl_seconds=60,
        now=2000,
    )
    token = created["url"].rsplit("/", 1)[1]
    with pytest.raises(HTTPException) as wrong:
        links.resolve_temporary_file_link(created["grant_id"], "x" * 43, now=2001)
    assert wrong.value.status_code == 404

    source.write_bytes(b"changed")
    with pytest.raises(HTTPException) as changed:
        links.resolve_temporary_file_link(created["grant_id"], token, now=2001)
    assert changed.value.status_code == 409
    with pytest.raises(HTTPException) as expired:
        links.resolve_temporary_file_link(created["grant_id"], token, now=2061)
    assert expired.value.status_code == 404


def test_revoke_is_member_scoped_and_cleanup_removes_expired(tmp_path, monkeypatch):
    source = tmp_path / "file.txt"
    source.write_bytes(b"data")
    _patch_grants(tmp_path, monkeypatch, source)
    created = links.create_temporary_file_link(
        user_id=7,
        ai_config_id=19,
        file_ref="file_" + "c" * 32,
        public_base_url="https://heysure.example",
        ttl_seconds=60,
        now=time.time() - 120,
    )
    assert links.cleanup_expired_grants() == 1
    with pytest.raises(HTTPException):
        links.revoke_temporary_file_link(user_id=7, ai_config_id=19, grant_id=created["grant_id"])

    active = links.create_temporary_file_link(
        user_id=7,
        ai_config_id=19,
        file_ref="file_" + "d" * 32,
        public_base_url="https://heysure.example",
    )
    with pytest.raises(HTTPException):
        links.revoke_temporary_file_link(user_id=8, ai_config_id=19, grant_id=active["grant_id"])
    assert links.revoke_temporary_file_link(user_id=7, ai_config_id=19, grant_id=active["grant_id"])["revoked"]
