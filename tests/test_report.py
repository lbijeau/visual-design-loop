"""Tests for report.py: synthetic-fixture rendering + a real mini-run integration."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import export as export_mod
import llm_client
import report
import visual_audit
from loop import FrontendDesignLoop
from run_state import RunState

INTENT = "A portfolio site <script>alert(1)</script>"
FAKE_PNG = b"\x89PNG-not-really-but-bytes-suffice"


def _fixture_state():
    """RunState with 3 iterations, an error, an undo, and a finished event."""
    tmp = Path(tempfile.mkdtemp())
    rs = RunState(tmp / "run_state")
    shots = {}
    for n in (1, 2, 3):
        shot = tmp / f"shot{n}.png"
        shot.write_bytes(FAKE_PNG)
        shots[n] = str(shot)
        rs.snapshot(
            n,
            f"<html><body>V{n}</body></html>",
            {"tailwind_config": {}},
            {
                "overall_score": 40 + 20 * n,
                "summary": f"summary {n}",
                "bugs": [{"severity": "critical", "issue": f"bug {n} <b>bold</b>", "location": "B4"}],
            },
        )
    rs.save_run(
        {"intent": INTENT, "iteration": 3, "undo_pointer": 3, "max_iterations": 5, "theme_json": {}, "phase": "converged", "finished": True}
    )
    rs.append_event({"event": "run_started", "intent": INTENT, "max_iterations": 5})
    rs.append_event(
        {"event": "iteration_produced", "n": 1, "score": 60, "bugs": 1, "summary": "summary 1", "screenshot": shots[1], "feedback": None}
    )
    rs.append_event({"event": "error", "iteration": 1, "message": "brain melted <badly>"})
    rs.append_event(
        {
            "event": "iteration_produced",
            "n": 2,
            "score": 80,
            "bugs": 1,
            "summary": "summary 2",
            "screenshot": shots[2],
            "feedback": "make it <bluer>",
        }
    )
    rs.append_event({"event": "undo", "restored": 1, "from": 2, "error_wait": False})
    rs.append_event(
        {
            "event": "iteration_produced",
            "n": 3,
            "score": 100,
            "bugs": 1,
            "summary": "summary 3",
            "screenshot": str(tmp / "deleted.png"),
            "feedback": "final polish",
        }
    )
    rs.append_event({"event": "finished", "outcome": "accepted", "final_score": 100})
    return rs


def test_build_report_content():
    print("  test_build_report_content...", end=" ")
    out = report.build_report(_fixture_state())
    assert "<script" not in out  # zero JS, escaped intent
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out  # intent escaped, present
    assert out.count("iteration-card") >= 3
    assert "data:image/png;base64," in out  # embedded screenshot
    assert "screenshot unavailable" in out  # deleted.png placeholder
    assert "make it &lt;bluer&gt;" in out  # feedback quoted + escaped
    assert "brain melted &lt;badly&gt;" in out  # error row escaped
    assert "Restored iteration 1" in out  # undo row
    assert "accepted" in out  # outcome badge
    assert "bug 1 &lt;b&gt;bold&lt;/b&gt;" in out  # bugs list from snapshot, escaped
    print("✅")


def test_build_report_no_events():
    print("  test_build_report_no_events...", end=" ")
    rs = RunState(Path(tempfile.mkdtemp()) / "run_state")
    rs.save_run({"intent": "x", "finished": False})
    try:
        report.build_report(rs)
        assert False, "Should have raised ReportError"
    except report.ReportError:
        pass
    print("✅")


def test_write_report_filename_and_dir():
    print("  test_write_report_filename_and_dir...", end=" ")
    original = config.REPORTS_DIR
    config.REPORTS_DIR = Path(tempfile.mkdtemp()) / "reports"
    try:
        path = report.write_report(_fixture_state())
        assert path.exists()
        assert re.fullmatch(r"\d{8}-\d{6}-[a-z0-9-]+\.html", path.name), path.name
        assert "a-portfolio-site" in path.name  # slug from intent
    finally:
        config.REPORTS_DIR = original
    print("✅")


def test_write_report_empty_intent_slug():
    print("  test_write_report_empty_intent_slug...", end=" ")
    rs = RunState(Path(tempfile.mkdtemp()) / "run_state")
    rs.save_run({"intent": "!!!", "finished": True})
    rs.append_event({"event": "finished", "outcome": "converged", "final_score": 0})
    original = config.REPORTS_DIR
    config.REPORTS_DIR = Path(tempfile.mkdtemp()) / "reports"
    try:
        path = report.write_report(rs)
        assert path.name.endswith("-run.html"), path.name  # fallback slug
    finally:
        config.REPORTS_DIR = original
    print("✅")


def test_report_generated_by_real_run():
    print("  test_report_generated_by_real_run...", end=" ")

    theme = {"tailwind_config": {"theme": {}}}

    def fake_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        if "design theme" in prompt:
            return json.dumps(theme)
        return "<html><head><title>t</title></head><body>MINI</body></html>"

    orig_llm, orig_audit = llm_client.call_llm, visual_audit.perform_visual_audit
    orig_export = export_mod.build_standalone
    orig_reports_dir = config.REPORTS_DIR
    llm_client.call_llm = fake_llm
    visual_audit.perform_visual_audit = lambda p, i, view_context=None: {
        "overall_score": 50,
        "bugs": [{"severity": "minor", "issue": "x"}],
        "summary": "mini",
    }
    export_mod.build_standalone = lambda c, t: (_ for _ in ()).throw(export_mod.ExportError("forced"))
    config.REPORTS_DIR = Path(tempfile.mkdtemp()) / "reports"
    try:
        loop_obj = FrontendDesignLoop("mini integration run", max_iterations=3)
        loop_obj.run_state = RunState(Path(tempfile.mkdtemp()) / "run_state")
        loop_obj.status.signal_provider_config()
        loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [{"id": "default", "screenshot": "dummy.png"}])[1]
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)  # NO report stub — real write_report
        files = list(config.REPORTS_DIR.glob("*.html"))
        assert len(files) == 1, f"expected one report, got {files}"
        out = files[0].read_text()
        assert "iteration-card" in out
        assert "accepted" in out
        assert "mini integration run" in out
        assert "screenshot unavailable" in out  # dummy.png does not exist
    finally:
        llm_client.call_llm = orig_llm
        visual_audit.perform_visual_audit = orig_audit
        export_mod.build_standalone = orig_export
        config.REPORTS_DIR = orig_reports_dir
    print("✅")


def test_iteration_card_views_grid():
    print("  test_iteration_card_views_grid...", end=" ")
    rs = _fixture_state()
    tmp = Path(tempfile.mkdtemp())
    shot = tmp / "v.png"
    shot.write_bytes(FAKE_PNG)
    rs.append_event(
        {
            "event": "iteration_produced",
            "n": 4,
            "score": 70,
            "bugs": 0,
            "summary": "multi",
            "screenshot": str(shot),
            "feedback": "split views",
            "views": {
                "dashboard@desktop": {"score": 90, "screenshot": str(shot)},
                "settings@mobile": {"score": 70, "screenshot": None},
                "cart@mobile+checkout-modal": {"score": 80, "screenshot": None},
                "cart@mobile#hover": {"score": 88, "screenshot": None},
            },
        }
    )
    out = report.build_report(rs)
    assert "view-grid" in out
    assert "dashboard @ desktop — 90/100" in out
    assert "settings @ mobile — 70/100" in out
    assert "cart @ mobile + checkout-modal — 80/100" in out
    assert "cart @ mobile # hover — 88/100" in out
    assert out.count("screenshot unavailable") >= 2  # fixture's deleted.png + settings None
    print("✅")


if __name__ == "__main__":
    print("\n=== Report Tests ===")
    test_build_report_content()
    test_build_report_no_events()
    test_write_report_filename_and_dir()
    test_write_report_empty_intent_slug()
    test_report_generated_by_real_run()
    test_iteration_card_views_grid()
    print("\nAll tests passed ✅")
