import json
import unittest

from api.services.model_presets import (
    CLI_BASE_URL_PREFIX,
    cli_command_from_base_url,
    is_cli_base_url,
    normalize_model_presets,
    resolve_model_preset,
)


class _FakeUser:
    def __init__(self, presets):
        self.model_presets = json.dumps(presets, ensure_ascii=False)
        self.admin_model = ""
        self.admin_api_key = ""
        self.admin_base_url = ""


class _FakeCfg:
    def __init__(self, model_preset_id="", model=""):
        self.model_preset_id = model_preset_id
        self.model = model
        self.api_key = ""
        self.base_url = ""


class NormalizeCliPresetTests(unittest.TestCase):
    def test_cli_preset_survives_without_api_key_and_base_url(self):
        presets = normalize_model_presets(
            json.dumps([
                {"id": "g1", "name": "Grok CLI", "model": "grok-4.5",
                 "provider": "cli", "cli_command": r"C:\Users\admin\.grok\bin\grok.exe"},
            ])
        )
        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0]["provider"], "cli")
        self.assertEqual(presets[0]["cli_command"], r"C:\Users\admin\.grok\bin\grok.exe")

    def test_cli_preset_without_command_is_dropped(self):
        presets = normalize_model_presets(
            json.dumps([{"id": "g1", "model": "grok-4.5", "provider": "cli"}])
        )
        self.assertEqual(presets, [])

    def test_api_preset_still_requires_all_three_fields(self):
        presets = normalize_model_presets(
            json.dumps([{"id": "a1", "model": "gpt-x", "api_key": "sk", "base_url": ""}])
        )
        self.assertEqual(presets, [])

    def test_legacy_preset_defaults_to_api_provider(self):
        presets = normalize_model_presets(
            json.dumps([{"id": "a1", "model": "gpt-x", "api_key": "sk",
                         "base_url": "https://x/v1/chat/completions"}])
        )
        self.assertEqual(presets[0]["provider"], "api")
        self.assertEqual(presets[0]["cli_command"], "")


class ResolveCliPresetTests(unittest.TestCase):
    def test_resolve_returns_cli_sentinel_base_url(self):
        user = _FakeUser([
            {"id": "g1", "model": "grok-4.5", "provider": "cli", "cli_command": "grok"},
        ])
        api_key, base_url, model = resolve_model_preset(user, _FakeCfg(model_preset_id="g1"))
        self.assertTrue(api_key)  # non-empty so "not configured" checks pass
        self.assertEqual(base_url, CLI_BASE_URL_PREFIX + "grok")
        self.assertEqual(model, "grok-4.5")

    def test_sentinel_helpers(self):
        self.assertTrue(is_cli_base_url("cli://grok"))
        self.assertTrue(is_cli_base_url("CLI://C:\\bin\\grok.exe"))
        self.assertFalse(is_cli_base_url("https://api.x.ai/v1/chat/completions"))
        self.assertFalse(is_cli_base_url(""))
        self.assertEqual(
            cli_command_from_base_url(r"cli://C:\Users\a\grok.exe"),
            r"C:\Users\a\grok.exe",
        )

    def test_api_preset_resolution_unchanged(self):
        user = _FakeUser([
            {"id": "a1", "model": "gpt-x", "api_key": "sk",
             "base_url": "https://x/v1/chat/completions"},
        ])
        api_key, base_url, model = resolve_model_preset(user, _FakeCfg(model_preset_id="a1"))
        self.assertEqual((api_key, base_url, model), ("sk", "https://x/v1/chat/completions", "gpt-x"))


if __name__ == "__main__":
    unittest.main()
