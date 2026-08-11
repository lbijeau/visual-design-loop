import json
import os
import sys
from unittest.mock import MagicMock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import llm_client
import visual_audit
from loop import FrontendDesignLoop


def test_theme_validation_loop():
    # Setup
    loop = FrontendDesignLoop("Test Intent")
    loop.theme_json = {
        "hard_tokens": {"brand_primary": "#000", "brand_secondary": "#fff", "primary_font": "sans-serif"},
        "soft_tokens": {"accent_color": "#3b82f6", "border_radius": "4px", "spacing_unit": "4px"},
        "tailwind_config": {"theme": {"extend": {"colors": {"brand": {"primary": "#000", "secondary": "#fff"}, "accent": "#3b82f6"}}}},
    }
    loop.current_code = "<html><body>Hello</body></html>"
    loop._capture_records = [{"id": "default", "screenshot": "dummy.png"}]

    # Mock LLM to simulate a bad update then a good one
    def mock_llm(role, prompt, system_prompt=None, image_path=None, **kwargs):
        if "adjust the tokens to fix" in prompt.lower():
            return json.dumps(
                {
                    "hard_tokens": {"brand_primary": "#000", "brand_secondary": "#fff", "primary_font": "sans-serif"},
                    "soft_tokens": {"accent_color": "#00FF00", "border_radius": "4px", "spacing_unit": "4px"},
                    "tailwind_config": {
                        "theme": {"extend": {"colors": {"brand": {"primary": "#000", "secondary": "#fff"}, "accent": "#00FF00"}}}
                    },
                }
            )
        return json.dumps(
            {
                "hard_tokens": {"brand_primary": "#000", "brand_secondary": "#fff", "primary_font": "sans-serif"},
                "soft_tokens": {"accent_color": "#FFFFFF", "border_radius": "4px", "spacing_unit": "4px"},
                "tailwind_config": {
                    "theme": {"extend": {"colors": {"brand": {"primary": "#000", "secondary": "#fff"}, "accent": "#FFFFFF"}}}
                },
            }
        )

    original_call_llm = llm_client.call_llm
    llm_client.call_llm = mock_llm

    audit_results = [
        {"overall_score": 40, "summary": "Contrast is terrible", "bugs": [{"severity": "critical", "issue": "White on White"}]},
        {"overall_score": 95, "summary": "Looks great", "bugs": []},
    ]

    def mock_audit(image_path, original_intent, view_context=None):
        return audit_results.pop(0) if audit_results else {"overall_score": 100, "summary": "Perfect", "bugs": []}

    original_audit = visual_audit.perform_visual_audit
    visual_audit.perform_visual_audit = mock_audit
    loop.render_and_capture = MagicMock(return_value=[{"id": "default", "screenshot": "dummy.png"}])

    initial_audit = {"overall_score": 90, "summary": "Good baseline", "bugs": []}
    feedback = "Make the accent color a bright, glowy white"

    print("\n--- Testing Theme Validation Loop ---")
    try:
        loop.refine_code(initial_audit, human_feedback=feedback)
    finally:
        llm_client.call_llm = original_call_llm
        visual_audit.perform_visual_audit = original_audit

    final_accent = loop.theme_json["soft_tokens"]["accent_color"]
    assert final_accent == "#00FF00", f"Expected corrected accent #00FF00, got {final_accent}"
    assert not audit_results, "Validation loop did not consume the expected audits"

    print("\nTest Complete. ✅ Theme validation loop corrected the regression.")


def test_theme_gate_like_for_like_baseline_and_no_status_write():
    print("  test_theme_gate_like_for_like...", end=" ")
    loop = FrontendDesignLoop("Test Intent")
    loop.theme_json = {"tailwind_config": {}}
    loop.current_code = "<html><body>x</body></html>"
    loop._capture_records = [
        {"id": "dashboard", "breakpoint": "desktop", "screenshot": "dash.png"},
        {"id": "settings", "breakpoint": "desktop", "screenshot": "set.png"},
        {"id": "dashboard", "breakpoint": "mobile", "screenshot": "dash_m.png"},
        {"id": "settings", "breakpoint": "mobile", "screenshot": "set_m.png"},
    ]
    loop.view_audits = {
        "dashboard@desktop": {"audit": {"overall_score": 60, "bugs": [], "summary": ""}, "iteration": 1},
        "settings@desktop": {"audit": {"overall_score": 90, "bugs": [], "summary": ""}, "iteration": 1},
    }
    calls = []

    def fake_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        return json.dumps({"tailwind_config": {}, "soft_tokens": {}, "hard_tokens": {}})

    def mock_audit(image_path, original_intent, view_context=None):
        calls.append(image_path)
        return {"overall_score": 70, "bugs": [], "summary": "dropped"}  # 90 -> 70: drop 20 > 15

    original_llm = llm_client.call_llm
    original_audit = visual_audit.perform_visual_audit
    llm_client.call_llm = fake_llm
    visual_audit.perform_visual_audit = mock_audit
    loop.render_and_capture = MagicMock(return_value=list(loop._capture_records))
    composite_before = {"overall_score": 60, "bugs": [], "summary": "baseline", "views": {}}
    loop.status.set_audit(composite_before)
    try:
        # theme keywords + view name: gate must target settings and compare 90 vs 70
        loop.refine_code({"overall_score": 60, "bugs": [], "summary": ""}, human_feedback="make the settings view darker")
    finally:
        llm_client.call_llm = original_llm
        visual_audit.perform_visual_audit = original_audit
    assert all(p == "set.png" for p in calls), f"gate audited wrong view: {calls}"
    assert len(calls) >= 2, "drop of 20 must trip the gate and re-validate"
    assert loop.status.get_audit() == composite_before, "gate must not write status"
    print("✅")


def test_model_label_guards_unresolvable_provider():
    """_model_label is display-only: a stale provider id yields 'pid/?' not a KeyError crash."""
    print("  test_model_label_guards_unresolvable_provider...", end=" ")
    import tempfile

    from loop import _model_label
    from provider_config import ProviderConfig

    def _cfg(mutate):
        c = ProviderConfig.default_config()
        mutate(c)
        tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        json.dump(c, tf)
        tf.close()
        return ProviderConfig(tf.name), tf.name

    cfg, p = _cfg(lambda c: c)  # default brain -> ollama, resolvable
    try:
        label = _model_label(cfg)
        assert label.startswith("ollama/") and label != "ollama/?", label
    finally:
        os.remove(p)

    cfg, p = _cfg(lambda c: c["roles"]["brain"].update(providerId="ghost"))
    try:
        assert _model_label(cfg) == "ghost/?"  # resolve('ghost') raises -> guarded, no crash
    finally:
        os.remove(p)
    print("✅")


def _capture_call(loop_obj, method):
    """Run a seed method against a stubbed LLM, returning the recorded kwargs."""
    seen = {}

    def mock_llm(role, prompt, system_prompt=None, image_path=None, **kwargs):
        seen["prompt"] = prompt
        seen["image_path"] = image_path
        return json.dumps(
            {
                "hard_tokens": {"brand_primary": "#000", "brand_secondary": "#fff", "primary_font": "sans-serif"},
                "soft_tokens": {"accent_color": "#3b82f6", "border_radius": "4px", "spacing_unit": "4px"},
                "tailwind_config": {"theme": {"extend": {}}},
            }
        )

    original = llm_client.call_llm
    llm_client.call_llm = mock_llm
    try:
        method()
    finally:
        llm_client.call_llm = original
    return seen


def test_reference_paragraphs_absent_without_a_reference():
    print("  test_reference_paragraphs_absent_without_a_reference...", end=" ")
    loop = FrontendDesignLoop("a coffee shop")

    theme = _capture_call(loop, loop.generate_theme)
    assert theme["image_path"] is None
    assert "REFERENCE IMAGE" not in theme["prompt"]

    code = _capture_call(loop, loop.generate_initial_code)
    assert code["image_path"] is None
    assert "STRUCTURAL REFERENCE" not in code["prompt"]
    print("✅")


def test_reference_paragraphs_and_image_threaded():
    print("  test_reference_paragraphs_and_image_threaded...", end=" ")
    loop = FrontendDesignLoop("a coffee shop")
    loop.reference_png = "/tmp/reference_1.png"

    theme = _capture_call(loop, loop.generate_theme)
    assert theme["image_path"] == "/tmp/reference_1.png"
    assert "REFERENCE IMAGE" in theme["prompt"]
    assert "classify" in theme["prompt"], "the typeface limitation must be stated"

    code = _capture_call(loop, loop.generate_initial_code)
    assert code["image_path"] == "/tmp/reference_1.png"
    assert "STRUCTURAL REFERENCE" in code["prompt"]
    assert "the tokens win" in code["prompt"], "token precedence must be explicit"
    print("✅")


def test_unparseable_theme_falls_back_without_a_reference():
    print("  test_unparseable_theme_falls_back_without_a_reference...", end=" ")
    loop = FrontendDesignLoop("a coffee shop")

    original = llm_client.call_llm
    llm_client.call_llm = lambda *a, **kw: "not json at all"
    try:
        loop.generate_theme()  # must NOT raise
    finally:
        llm_client.call_llm = original

    assert loop.theme_json["hard_tokens"]["brand_primary"] == "#000000"
    print("✅")


def test_unparseable_theme_aborts_with_a_reference():
    print("  test_unparseable_theme_aborts_with_a_reference...", end=" ")
    loop = FrontendDesignLoop("a coffee shop")
    loop.reference_png = "/tmp/reference_1.png"

    original = llm_client.call_llm
    llm_client.call_llm = lambda *a, **kw: "not json at all"
    try:
        loop.generate_theme()
        assert False, "expected an abort on an image-seeded run"
    except Exception as e:
        assert "generic palette" in str(e)
    finally:
        llm_client.call_llm = original
    print("✅")


if __name__ == "__main__":
    test_theme_validation_loop()
    test_theme_gate_like_for_like_baseline_and_no_status_write()
    test_model_label_guards_unresolvable_provider()
    test_reference_paragraphs_absent_without_a_reference()
    test_reference_paragraphs_and_image_threaded()
    test_unparseable_theme_falls_back_without_a_reference()
    test_unparseable_theme_aborts_with_a_reference()
