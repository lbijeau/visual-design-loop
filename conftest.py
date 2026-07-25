"""Pytest fixtures for the visual-design-loop suite.

Isolates provider-config resolution from the real, gitignored `providers.json`
so no test ever reads or writes the user's live provider configuration.
"""

import pytest

import provider_config


@pytest.fixture
def tmp_config_path(tmp_path):
    """A per-test `providers.json` path under pytest's `tmp_path`.

    The file does not exist until a test writes it, which is what the
    fallback/save tests rely on.
    """
    return str(tmp_path / "providers.json")


@pytest.fixture(autouse=True)
def isolate_provider_config(tmp_path, monkeypatch):
    """Redirect `ProviderConfig`'s default path away from the real
    `providers.json` for every test.

    `ProviderConfig(None)` resolves its path via `provider_config._config_path()`.
    We point that at a per-test path that does not exist, so a no-argument
    `ProviderConfig()` falls back to `BUILTIN_PROVIDERS` (ollama-only) — the
    documented defaults the no-arg tests assert against — while `save()` calls
    (e.g. via the `/save-providers` server route) write into the isolated
    tmp path instead of the user's live config.
    """
    isolated = tmp_path / "isolated_providers.json"
    monkeypatch.setattr(provider_config, "_config_path", lambda: str(isolated))
