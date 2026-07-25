"""End-to-end smoke: bootstrap -> theme -> code -> render -> capture -> audit -> converge.

LLM and audit are stubbed; the render/capture step runs REAL Playwright and is
skipped (with the rest still asserted where possible) when chromium is missing.
Overwrites gitignored artifacts (theme.json, current_render.html, final_result.html).
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import export as export_mod
import llm_client
import report as report_mod
import visual_audit
from loop import FrontendDesignLoop


def _force_finish_stubs():
    """Deterministic finish path for loop-driving tests: export takes the CDN
    fallback (real export needs chromium+network) and the report is a no-op
    (keeps test artifacts out of reports/)."""
    orig_export = export_mod.build_standalone
    orig_report = report_mod.write_report

    def raiser(code, theme):
        raise export_mod.ExportError("forced by test")

    export_mod.build_standalone = raiser
    report_mod.write_report = lambda rs: "stub-report.html"

    def undo():
        export_mod.build_standalone = orig_export
        report_mod.write_report = orig_report

    return undo


CANNED_THEME = {
    "hard_tokens": {"brand_primary": "#0f0f23", "brand_secondary": "#1a1a3e", "primary_font": "Inter, sans-serif"},
    "soft_tokens": {"accent_color": "#00d4ff", "border_radius": "8px", "spacing_unit": "8px"},
    "tailwind_config": {"theme": {"extend": {"colors": {"brand": {"primary": "#0f0f23", "secondary": "#1a1a3e"}, "accent": "#00d4ff"}}}},
}
CANNED_HTML = (
    "<!DOCTYPE html><html><head><title>Smoke</title>"
    '<script src="https://cdn.tailwindcss.com"></script></head>'
    '<body class="bg-brand-primary"><h1>Smoke Test Page</h1></body></html>'
)


def chromium_available() -> bool:
    """True only if node + playwright + an actual chromium binary can launch."""
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(
        ["node", "-e", "require('playwright').chromium.launch().then(b => b.close())"],
        cwd=str(config.PROJECT_DIR),
        capture_output=True,
        timeout=60,
    )
    return probe.returncode == 0


def test_smoke_loop():
    print("  test_smoke_loop...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return

    def fake_call_llm(role, prompt, system_prompt=None, image_path=None, **kwargs):
        if "design theme" in prompt:
            return json.dumps(CANNED_THEME)
        return CANNED_HTML

    def fake_audit(image_path, original_intent, view_context=None):
        assert os.path.exists(image_path), f"audit got a nonexistent screenshot: {image_path}"
        return {"overall_score": 100, "bugs": [], "summary": "flawless"}

    before_shots = set(os.listdir(config.SCREENSHOT_DIR)) if config.SCREENSHOT_DIR.exists() else set()
    original_call, original_audit = llm_client.call_llm, visual_audit.perform_visual_audit
    llm_client.call_llm = fake_call_llm
    visual_audit.perform_visual_audit = fake_audit
    undo_export = _force_finish_stubs()
    try:
        loop = FrontendDesignLoop("A smoke-test landing page", max_iterations=2)
        loop.status.signal_provider_config()  # skip the wizard wait
        # Anti-hang guard: if render/capture unexpectedly fails, the recovery
        # path blocks on the feedback queue — this pre-queued KEEP makes it
        # exit immediately so the test FAILS on assertions instead of hanging.
        loop.status.put_feedback("KEEP")
        loop.run(port=0)
    finally:
        llm_client.call_llm = original_call
        visual_audit.perform_visual_audit = original_audit
        undo_export()

    render = open(config.RENDER_PATH).read()
    assert "tailwind.config" in render, "current_render.html missing theme injection"
    assert "checkVersion" in render, "current_render.html missing reload script"

    final = open(config.FINAL_PATH).read()
    assert "tailwind.config" in final, "final_result.html missing theme injection (export/preview mismatch)"
    assert "checkVersion" not in final, "final_result.html must not contain the reload script"

    theme = json.load(open(config.THEME_PATH))
    assert theme == CANNED_THEME

    after_shots = set(os.listdir(config.SCREENSHOT_DIR))
    assert len(after_shots - before_shots) >= 1, "no screenshot was captured"

    assert loop.status.get_status()["phase"] == "converged"
    assert loop.status.get_audit()["overall_score"] == 100
    print("✅")


if __name__ == "__main__":
    print("\n=== Smoke Test ===")
    test_smoke_loop()
    print("\nAll tests passed ✅")
