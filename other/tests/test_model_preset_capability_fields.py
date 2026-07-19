"""Model presets carry optional explicit capability fields (provider /
tool_protocol) so OpenAI-shaped gateways that hide the real backend — e.g. the
grok-cli local gateway — can be configured instead of sniffed from base_url."""

import json

from api.services.model_presets import (
    model_presets_json,
    normalize_model_presets,
)


def _preset(**extra):
    return {
        "id": "p1",
        "name": "grok",
        "api_key": "k",
        "base_url": "http://127.0.0.1:8100/v1/chat/completions",
        "model": "grok-4.5",
        **extra,
    }


def test_defaults_to_auto_when_fields_absent():
    presets = normalize_model_presets([_preset()])
    assert presets[0]["provider"] == "auto"
    assert presets[0]["tool_protocol"] == "auto"


def test_explicit_fields_survive_normalize_roundtrip():
    raw = json.dumps([_preset(provider="openai", tool_protocol="text")])
    round_tripped = json.loads(model_presets_json(raw))
    assert round_tripped[0]["provider"] == "openai"
    assert round_tripped[0]["tool_protocol"] == "text"
    # A second normalize pass (as on every save) must not mutate the fields.
    assert normalize_model_presets(round_tripped)[0]["tool_protocol"] == "text"


def test_invalid_values_fall_back_to_auto():
    presets = normalize_model_presets([_preset(provider="grok???", tool_protocol="xml")])
    assert presets[0]["provider"] == "auto"
    assert presets[0]["tool_protocol"] == "auto"
