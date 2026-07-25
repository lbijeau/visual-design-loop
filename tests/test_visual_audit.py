"""Tests for visual_audit: prompt goes through llm_client, JSON parsing, AuditParseError."""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client
import visual_audit


def _patch_call_llm(response_text, seen):
    def fake(role=None, prompt=None, system_prompt=None, image_path=None, response_format="text", timeout=300, provider_config=None):
        seen.update(role=role, prompt=prompt, image_path=image_path, response_format=response_format)
        return response_text

    return fake


def test_parses_markdown_wrapped_json():
    print("  test_parses_markdown_wrapped_json...", end=" ")
    seen = {}
    original = llm_client.call_llm
    llm_client.call_llm = _patch_call_llm('Sure!\n```json\n{"overall_score": 90, "bugs": [], "summary": "ok"}\n```', seen)
    try:
        result = visual_audit.perform_visual_audit("shot.png", "a portfolio site")
        assert result == {"overall_score": 90, "bugs": [], "summary": "ok"}
        assert seen["role"] == "eyes"
        assert seen["image_path"] == "shot.png"
        assert seen["response_format"] == "json"
        assert "a portfolio site" in seen["prompt"]
        print("✅")
    finally:
        llm_client.call_llm = original


def test_unparseable_raises_audit_parse_error():
    print("  test_unparseable_raises_audit_parse_error...", end=" ")
    original = llm_client.call_llm
    llm_client.call_llm = _patch_call_llm("I could not look at the image, sorry.", {})
    try:
        try:
            visual_audit.perform_visual_audit("shot.png", "intent")
            assert False, "Should have raised AuditParseError"
        except visual_audit.AuditParseError:
            pass
        print("✅")
    finally:
        llm_client.call_llm = original


def test_no_view_context_prompt_unchanged():
    print("  test_no_view_context_prompt_unchanged...", end=" ")
    seen = {}
    original = llm_client.call_llm
    llm_client.call_llm = _patch_call_llm('{"overall_score": 90, "bugs": [], "summary": "ok"}', seen)
    try:
        visual_audit.perform_visual_audit("shot.png", "an admin app")
        assert "VIEW CONTEXT" not in seen["prompt"]
        print("✅")
    finally:
        llm_client.call_llm = original


def test_view_context_in_prompt():
    print("  test_view_context_in_prompt...", end=" ")
    seen = {}
    original = llm_client.call_llm
    llm_client.call_llm = _patch_call_llm('{"overall_score": 90, "bugs": [], "summary": "ok"}', seen)
    try:
        ctx = "This screenshot shows only the 'settings' view of a multi-view app (views: dashboard, settings)."
        visual_audit.perform_visual_audit("shot.png", "an admin app", view_context=ctx)
        assert "VIEW CONTEXT:" in seen["prompt"]
        assert "'settings' view" in seen["prompt"]
        assert seen["prompt"].index("USER INTENT") < seen["prompt"].index("VIEW CONTEXT:") < seen["prompt"].index("STEP 1")
        print("✅")
    finally:
        llm_client.call_llm = original


def test_build_audit_prompt_grounded_and_calibrated():
    print("  test_build_audit_prompt_grounded_and_calibrated...", end=" ")
    p = visual_audit._build_audit_prompt("a fire-lookout dashboard")
    assert "a fire-lookout dashboard" in p  # intent embedded
    assert "components_present" in p  # inventory step (D2)
    assert (
        "absent from components_present" in p.lower()
    )  # D2 flag-missing-only-if-absent rule (pinned exactly, not via the generic word "only")
    # score-band anchors (D4) — all four bands present
    for band in ("90-100", "70-89", "40-69", "0-39"):
        assert band in p, band
    # generic cleanup: catastrophizing/grid trigger phrases removed (D5)
    assert "identify visual discrepancies" not in p
    assert "Use the red A-J/1-10 grid overlay" not in p
    print("✅")


def test_perform_visual_audit_strips_components_present():
    print("  test_perform_visual_audit_strips_components_present...", end=" ")
    seen = {}
    original = llm_client.call_llm
    resp = '{"components_present": ["header", "footer"], "overall_score": 82, "bugs": [], "summary": "ok"}'
    llm_client.call_llm = _patch_call_llm(resp, seen)
    try:
        result = visual_audit.perform_visual_audit("shot.png", "a site")
        assert "components_present" not in result  # D6 strip
        assert result == {"overall_score": 82, "bugs": [], "summary": "ok"}
        print("✅")
    finally:
        llm_client.call_llm = original


if __name__ == "__main__":
    print("\n=== visual_audit Tests ===")
    test_parses_markdown_wrapped_json()
    test_unparseable_raises_audit_parse_error()
    test_no_view_context_prompt_unchanged()
    test_view_context_in_prompt()
    test_build_audit_prompt_grounded_and_calibrated()
    test_perform_visual_audit_strips_components_present()
    print("\nAll tests passed ✅")
