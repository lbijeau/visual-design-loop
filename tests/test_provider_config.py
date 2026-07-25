"""Tests for provider_config module."""

import os
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from provider_config import GLOBAL_MODEL_ID, ProviderConfig, is_key_required, mask_key

CONFIG_PATH = None  # set per test


def test_mask_key():
    print("  test_mask_key...", end=" ")
    assert mask_key("") == ""
    assert mask_key("short") == "****"
    assert mask_key("sk-or-v1-abcdef1234567890") == "sk-o...7890"
    assert mask_key("sk-1234567890ab") == "sk-1...90ab"
    print("✅")


def test_is_key_required():
    print("  test_is_key_required...", end=" ")
    assert not is_key_required({"id": "ollama"})
    assert is_key_required({"id": "openrouter"})
    assert is_key_required({"id": "openai"})
    assert is_key_required({"id": "anthropic"})
    assert is_key_required({"id": "deepseek"})
    assert is_key_required({"id": "custom-provider"})
    print("✅")


def test_resolve_builtin():
    print("  test_resolve_builtin...", end=" ")
    cfg = ProviderConfig(CONFIG_PATH)
    ollama = cfg.resolve("ollama")
    assert ollama["id"] == "ollama"
    assert ollama["baseUrl"] == "http://localhost:11434"
    assert ollama["type"] == "builtin"
    print("✅")


def test_resolve_missing_raises():
    print("  test_resolve_missing_raises...", end=" ")
    cfg = ProviderConfig(CONFIG_PATH)
    try:
        cfg.resolve("nonexistent")
        assert False, "Should have raised KeyError"
    except KeyError:
        pass
    print("✅")


def test_get_role():
    print("  test_get_role...", end=" ")
    cfg = ProviderConfig(CONFIG_PATH)
    role = cfg.get_role("brain")
    assert role["providerId"] == "ollama"
    assert role["fallbackOrder"] == []
    print("✅")


def test_enabled_providers():
    print("  test_enabled_providers...", end=" ")
    cfg = ProviderConfig(CONFIG_PATH)
    enabled = cfg.enabled_providers()
    assert len(enabled) == 1  # only Ollama enabled by default
    assert enabled[0]["id"] == "ollama"
    print("✅")


def test_get_model_id_provider_override():
    print("  test_get_model_id_provider_override...", end=" ")
    cfg = ProviderConfig(CONFIG_PATH)
    p = cfg.resolve("ollama")
    p["modelId"] = "my-custom-model"
    assert cfg.get_model_id(p) == "my-custom-model"
    print("✅")


def test_get_model_id_fallback_to_global():
    print("  test_get_model_id_fallback_to_global...", end=" ")
    cfg = ProviderConfig(CONFIG_PATH)
    p = cfg.resolve("ollama")
    p["modelId"] = None
    assert cfg.get_model_id(p) == GLOBAL_MODEL_ID
    print("✅")


def test_save_and_reload(tmp_config_path):
    print("  test_save_and_reload...", end=" ")
    cfg = ProviderConfig(tmp_config_path)
    new_cfg = cfg.default_config()
    new_cfg["providers"][1]["enabled"] = True  # enable openrouter
    cfg.save(new_cfg)
    # Reload
    cfg2 = ProviderConfig(tmp_config_path)
    assert cfg2.resolve("openrouter")["enabled"] is True
    print("✅")


def test_save_permissions(tmp_config_path):
    print("  test_save_permissions...", end=" ")
    cfg = ProviderConfig(tmp_config_path)
    cfg.save(cfg.default_config())
    # Check mode
    mode = os.stat(tmp_config_path).st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
    print("✅")


def test_fallback_on_missing_file(tmp_config_path):
    print("  test_fallback_on_missing_file...", end=" ")
    # Ensure no file exists
    if os.path.exists(tmp_config_path):
        os.remove(tmp_config_path)
    cfg = ProviderConfig(tmp_config_path)
    assert cfg.get_role("brain")["providerId"] == "ollama"
    print("✅")


def test_fallback_on_invalid_json(tmp_config_path):
    print("  test_fallback_on_invalid_json...", end=" ")
    with open(tmp_config_path, "w") as f:
        f.write("NOT JSON {{{")
    cfg = ProviderConfig(tmp_config_path)
    assert cfg.get_role("brain")["providerId"] == "ollama"
    print("✅")


if __name__ == "__main__":
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        CONFIG_PATH = tf.name

    try:
        print("\n=== ProviderConfig Tests ===")
        test_mask_key()
        test_is_key_required()
        test_resolve_builtin()
        test_resolve_missing_raises()
        test_get_role()
        test_enabled_providers()
        test_get_model_id_provider_override()
        test_get_model_id_fallback_to_global()
        test_save_and_reload(CONFIG_PATH)
        test_save_permissions(CONFIG_PATH)
        test_fallback_on_missing_file(CONFIG_PATH)
        test_fallback_on_invalid_json(CONFIG_PATH)
        print("\nAll tests passed ✅")
    finally:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
