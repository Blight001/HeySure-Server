from ai_runtime.inference.ai_service import _default_ai_specs
from gateway.routers.ai_base import _normalize_ai_role


def test_default_members_only_use_digital_member_role():
    specs = _default_ai_specs()

    assert {spec["name"] for spec in specs} == {"阿尔法", "贝塔", "德尔塔"}
    assert {spec["ai_role"] for spec in specs} == {"digital_member"}
    assert "assistant_worker_file" not in {spec["switch_key"] for spec in specs}


def test_retired_role_is_normalized_to_digital_member():
    assert _normalize_ai_role("assistant_admin") == "digital_member"
    assert _normalize_ai_role("digital_member") == "digital_member"
