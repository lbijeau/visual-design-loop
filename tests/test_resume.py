"""Tests for resume: restore from snapshots, skip generation, degrade to fresh."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import export as export_mod
import llm_client
import report as report_mod
import visual_audit
from loop import FrontendDesignLoop
from run_state import RunState


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


THEME = {
    "hard_tokens": {"brand_primary": "#000", "brand_secondary": "#fff", "primary_font": "sans-serif"},
    "soft_tokens": {"accent_color": "#3b82f6", "border_radius": "4px", "spacing_unit": "4px"},
    "tailwind_config": {"theme": {"extend": {"colors": {"accent": "#3b82f6"}}}},
}
AUDIT = {"overall_score": 60, "bugs": [{"severity": "minor", "issue": "x"}], "summary": "restored"}


def _seed_state(rs, iteration=2, **extra):
    meta = {
        "intent": "seeded intent",
        "theme_json": THEME,
        "iteration": iteration,
        "undo_pointer": iteration,
        "phase": "awaiting-feedback",
        "max_iterations": 5,
        "finished": False,
    }
    meta.update(extra)
    rs.save_run(meta)
    for n in range(1, iteration + 1):
        rs.snapshot(n, f"<html><body>SEED-V{n}</body></html>", THEME, AUDIT)


def test_resume_restores_and_skips_generation():
    print("  test_resume_restores_and_skips_generation...", end=" ")
    rs = RunState(Path(tempfile.mkdtemp()) / "run_state")
    _seed_state(rs, iteration=2)

    def forbidden_generation(role, prompt, system_prompt=None, image_path=None, **kw):
        raise AssertionError(f"resume must not call the LLM for generation: {prompt[:60]}")

    orig_llm = llm_client.call_llm
    llm_client.call_llm = forbidden_generation
    loop_obj = FrontendDesignLoop(None, max_iterations=3)  # no CLI intent
    loop_obj.run_state = rs
    # Anti-hang guard: if providers.json is absent on this machine, the resume
    # path waits for the wizard — pre-signal so the test never blocks.
    loop_obj.status.signal_provider_config()
    loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [{"id": "default", "screenshot": "dummy.png"}])[1]
    undo_export = _force_finish_stubs()
    try:
        loop_obj.status.put_action("accept")  # finish at the first wait
        loop_obj.run(port=0, resume=True)
    finally:
        llm_client.call_llm = orig_llm
        undo_export()

    assert loop_obj.intent == "seeded intent"
    assert loop_obj.iteration == 2
    final = open(config.FINAL_PATH).read()
    assert "SEED-V2" in final
    assert json.load(open(config.THEME_PATH)) == THEME  # theme.json re-written
    assert loop_obj.status.get_status()["model_label"] != ""  # model badge restored
    assert loop_obj.status.get_audit() == AUDIT
    assert rs.load_run()["finished"] is True
    print("✅")


def test_resume_preserves_the_reference_record():
    """run.json is rewritten wholesale from _run_meta(), so any field _restore_run
    forgets is erased by the first save after a resume."""
    print("  test_resume_preserves_the_reference_record...", end=" ")
    rs = RunState(Path(tempfile.mkdtemp()) / "run_state")
    _seed_state(rs, iteration=2, reference="https://example.com/x", reference_png="/tmp/reference_1.png")

    loop_obj = FrontendDesignLoop(None, max_iterations=3)
    loop_obj.run_state = rs
    loop_obj.status.signal_provider_config()  # anti-hang guard, as above
    assert loop_obj._restore_run(port=0) is not None

    assert loop_obj.reference == "https://example.com/x"
    assert loop_obj.reference_png == "/tmp/reference_1.png"

    rs.save_run(loop_obj._run_meta())  # the rewrite that used to null them
    assert rs.load_run()["reference"] == "https://example.com/x"
    assert rs.load_run()["reference_png"] == "/tmp/reference_1.png"
    print("✅")


def test_resume_without_snapshots_degrades_to_fresh():
    print("  test_resume_without_snapshots_degrades_to_fresh...", end=" ")
    rs = RunState(Path(tempfile.mkdtemp()) / "run_state")
    rs.save_run(
        {
            "intent": "died early",
            "theme_json": {},
            "iteration": 0,
            "undo_pointer": 0,
            "phase": "generating-theme",
            "max_iterations": 5,
            "finished": False,
        }
    )  # no snapshots

    calls = []

    def fake_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        calls.append(prompt)
        if "design theme" in prompt:
            return json.dumps(THEME)
        return "<html><body>FRESH</body></html>"

    orig_llm = llm_client.call_llm
    orig_audit = visual_audit.perform_visual_audit
    llm_client.call_llm = fake_llm
    visual_audit.perform_visual_audit = lambda p, i, view_context=None: {"overall_score": 100, "bugs": [], "summary": "ok"}
    loop_obj = FrontendDesignLoop("cli intent", max_iterations=2)
    loop_obj.run_state = rs
    loop_obj.status.signal_provider_config()
    loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [{"id": "default", "screenshot": "dummy.png"}])[1]
    undo_export = _force_finish_stubs()
    try:
        loop_obj.status.put_action("accept")  # iter1 waits for feedback; accept to finish
        loop_obj.run(port=0, resume=True)  # degrades to fresh, then accepts at first wait
    finally:
        llm_client.call_llm = orig_llm
        visual_audit.perform_visual_audit = orig_audit
        undo_export()

    assert len(calls) >= 2, "fresh path must have generated theme + code"
    assert "FRESH" in open(config.FINAL_PATH).read()
    print("✅")


if __name__ == "__main__":
    print("\n=== Resume Tests ===")
    test_resume_restores_and_skips_generation()
    test_resume_preserves_the_reference_record()
    test_resume_without_snapshots_degrades_to_fresh()
    print("\nAll tests passed ✅")
