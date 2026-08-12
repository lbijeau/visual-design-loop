"""Tests for the startup provider gate: when the browser wizard is actually needed.

A saved config whose roles already resolve to live models needs no browser
round-trip. Blocking for one turns every unattended CLI run into a ten-minute
stall at a URL nobody is watching.
"""

import os
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client
import loop as loop_mod
from loop import FrontendDesignLoop
from provider_config import ProviderConfig


class _FakeConfig:
    """Stands in for ProviderConfig: only from_disk and the label path matter here."""

    def __init__(self, from_disk=True):
        self.from_disk = from_disk

    def get_role(self, role):
        return {"providerId": "ollama", "fallbackOrder": []}

    def resolve(self, pid):
        return {"id": pid, "baseUrl": "http://localhost:11434"}

    def get_model_id(self, provider):
        return "some-model"


def _gate(loop_obj, *, from_disk, preflight, reconfigure=False):
    """Run the gate with collaborators stubbed. Returns (result, waited)."""
    waited = []

    def record_wait(timeout=600):
        waited.append(timeout)
        return True

    loop_obj.status.await_provider_config = record_wait
    orig_cfg, orig_pre = loop_mod.ProviderConfig, llm_client.preflight_roles
    loop_mod.ProviderConfig = lambda *a, **kw: _FakeConfig(from_disk=from_disk)
    llm_client.preflight_roles = preflight
    try:
        result = loop_obj._resolve_providers(port=0, reconfigure=reconfigure)
    finally:
        loop_mod.ProviderConfig, llm_client.preflight_roles = orig_cfg, orig_pre
    return result, bool(waited)


def test_provider_config_records_whether_it_loaded_from_disk():
    print("  test_provider_config_records_whether_it_loaded_from_disk...", end=" ")
    missing = os.path.join(tempfile.mkdtemp(), "nope.json")
    assert ProviderConfig(config_path=missing).from_disk is False

    present = os.path.join(tempfile.mkdtemp(), "providers.json")
    with open(present, "w") as f:
        f.write('{"providers": [], "roles": {}}')
    assert ProviderConfig(config_path=present).from_disk is True
    print("✅")


def test_live_saved_config_skips_the_wizard():
    print("  test_live_saved_config_skips_the_wizard...", end=" ")
    ok, waited = _gate(FrontendDesignLoop("x"), from_disk=True, preflight=lambda cfg, **kw: (True, ""))
    assert ok is True
    assert waited is False, "a live saved config must not block on the browser wizard"
    print("✅")


def test_missing_config_still_waits_for_the_wizard():
    """A first run has nothing saved: the wizard is the only way to configure."""
    print("  test_missing_config_still_waits_for_the_wizard...", end=" ")
    ok, waited = _gate(FrontendDesignLoop("x"), from_disk=False, preflight=lambda cfg, **kw: (True, ""))
    assert ok is True
    assert waited is True, "with no saved config the wizard must still be offered"
    print("✅")


def test_dead_saved_config_falls_back_to_the_wizard():
    """Saved but unusable is the case the wizard exists for."""
    print("  test_dead_saved_config_falls_back_to_the_wizard...", end=" ")
    calls = []

    def preflight(cfg, **kw):
        calls.append(1)
        return (True, "") if len(calls) > 1 else (False, "Model 'x' is unavailable")

    ok, waited = _gate(FrontendDesignLoop("x"), from_disk=True, preflight=preflight)
    assert waited is True, "a dead saved config must fall back to the wizard"
    assert ok is True
    print("✅")


def test_still_dead_after_the_wizard_aborts_the_run():
    print("  test_still_dead_after_the_wizard_aborts_the_run...", end=" ")
    ok, waited = _gate(FrontendDesignLoop("x"), from_disk=False, preflight=lambda cfg, **kw: (False, "Model 'x' is unavailable"))
    assert waited is True
    assert ok is False, "an unusable config after the wizard must stop the run"
    print("✅")


def test_reconfigure_forces_the_wizard():
    """Skipping the wizard must not remove the ability to change models."""
    print("  test_reconfigure_forces_the_wizard...", end=" ")
    ok, waited = _gate(FrontendDesignLoop("x"), from_disk=True, preflight=lambda cfg, **kw: (True, ""), reconfigure=True)
    assert ok is True
    assert waited is True, "--reconfigure must offer the wizard even when the saved config is live"
    print("✅")


if __name__ == "__main__":
    print("\n=== Provider Gate Tests ===")
    test_provider_config_records_whether_it_loaded_from_disk()
    test_live_saved_config_skips_the_wizard()
    test_missing_config_still_waits_for_the_wizard()
    test_dead_saved_config_falls_back_to_the_wizard()
    test_still_dead_after_the_wizard_aborts_the_run()
    test_reconfigure_forces_the_wizard()
    print("\nAll tests passed ✅")
