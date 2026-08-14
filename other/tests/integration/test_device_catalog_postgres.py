import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlmodel import Session, select

from api.database import engine
from api.devices.presence_catalog_store import PresenceCatalogUpdate, swap_presence_catalog
from api.models import DevicePresence


pytestmark = pytest.mark.integration


def _swap(device_id: str, tool_name: str, catalog_hash: str):
    return swap_presence_catalog(PresenceCatalogUpdate(
        user_id=987654321,
        device_id=device_id,
        ai_config_id=None,
        device_type="custom",
        capabilities=(tool_name,),
        tool_defs={tool_name: {"description": tool_name, "input_schema": {"type": "object"}}},
        catalog_hash=catalog_hash,
        catalog_protocol_version=2,
    ))


def test_postgres_serializes_catalog_generation_and_keeps_one_complete_row():
    device_id = f"catalog-test-{uuid.uuid4()}"
    try:
        first = _swap(device_id, "tool.initial", "1" * 64)
        assert first["catalog_generation"] == 1
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda values: _swap(device_id, *values),
                [("tool.alpha", "a" * 64), ("tool.beta", "b" * 64)],
            ))
        assert sorted(item["catalog_generation"] for item in results) == [2, 3]
        with Session(engine) as session:
            rows = session.exec(
                select(DevicePresence).where(DevicePresence.device_id == device_id)
            ).all()
            assert len(rows) == 1
            row = rows[0]
            capabilities = json.loads(row.capabilities_json)
            definitions = json.loads(row.tool_defs_json)
            assert row.catalog_generation == 3
            assert row.catalog_hash in {"a" * 64, "b" * 64}
            assert capabilities == list(definitions)
    finally:
        with Session(engine) as session:
            rows = session.exec(
                select(DevicePresence).where(DevicePresence.device_id == device_id)
            ).all()
            for row in rows:
                session.delete(row)
            session.commit()
