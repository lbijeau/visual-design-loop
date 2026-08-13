"""Tests for loop control: render_preview, keyword dispatch, accept/undo, snapshots."""

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
from loop import (
    FrontendDesignLoop,
    aggregate_missing_focus_bug,
    cell_key,
    close_failed_bug,
    composite_audit,
    match_cells,
    match_pseudo,
    match_states,
    match_views,
    missing_focus_bug,
    record_cell,
    split_cell,
    synthetic_failure_audit,
)
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


def test_render_preview_no_capture():
    print("  test_render_preview_no_capture...", end=" ")
    loop_obj = FrontendDesignLoop("Test")
    loop_obj.theme_json = THEME
    loop_obj.current_code = "<html><head><title>t</title></head><body>PREVIEW-MARKER</body></html>"
    before_version = open(config.VERSION_PATH).read() if os.path.exists(config.VERSION_PATH) else ""
    loop_obj.render_preview()
    rendered = open(config.RENDER_PATH).read()
    assert "PREVIEW-MARKER" in rendered
    assert "tailwind.config" in rendered  # theme injected
    assert "checkVersion" in rendered  # reload script injected
    assert open(config.VERSION_PATH).read() != before_version
    print("✅")


def _make_loop(codes, audits, intent="Test intent", max_iterations=5, auto=False):
    """Loop with stubbed LLM + audit and isolated run state.

    codes: list of code strings the 'brain' returns for successive
           generate/refine calls (theme calls return THEME json automatically).
    audits: list of audit dicts returned by successive audits.
    Returns (loop_obj, prompts_seen, restore_fn).
    """
    prompts = []
    code_iter = iter(codes)
    audit_iter = iter(audits)

    def fake_call_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        prompts.append(prompt)
        if "design theme" in prompt:
            return json.dumps(THEME)
        return next(code_iter)

    def fake_audit(image_path, original_intent, view_context=None):
        return next(audit_iter)

    orig_llm, orig_audit = llm_client.call_llm, visual_audit.perform_visual_audit
    llm_client.call_llm = fake_call_llm
    visual_audit.perform_visual_audit = fake_audit

    undo_export = _force_finish_stubs()

    loop_obj = FrontendDesignLoop(intent, max_iterations=max_iterations, auto=auto)
    loop_obj.run_state = RunState(Path(tempfile.mkdtemp()) / "run_state")
    loop_obj.status.signal_provider_config()  # skip wizard wait
    # Avoid node/Playwright: render the preview for real, skip the screenshot
    loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [{"id": "default", "screenshot": "dummy.png"}])[1]

    def restore():
        llm_client.call_llm = orig_llm
        visual_audit.perform_visual_audit = orig_audit
        undo_export()

    return loop_obj, prompts, restore


BAD_AUDIT = {"overall_score": 50, "bugs": [{"severity": "minor", "issue": "meh"}], "summary": "needs work"}
GOOD_AUDIT = {"overall_score": 96, "bugs": [], "summary": "ship it"}


def test_auto_refines_without_waiting_for_feedback():
    """No action or feedback is queued: an interactive run would block at the
    first gate forever. --auto must drive itself to the iteration budget."""
    print("  test_auto_refines_without_waiting_for_feedback...", end=" ")
    codes = [f"<html><body>V{n}</body></html>" for n in range(1, 4)]
    loop_obj, prompts, restore = _make_loop(codes, [BAD_AUDIT] * 3, max_iterations=3, auto=True)
    try:
        outcome = loop_obj.run(port=0)
    finally:
        restore()
    assert outcome == "exhausted", f"got {outcome}"
    assert loop_obj.iteration == 3, f"expected the budget to be spent, got {loop_obj.iteration}"
    assert loop_obj.status.get_status()["feedback_count"] == 0, "auto mode must not record human feedback"
    print("✅")


def test_auto_stops_at_convergence():
    print("  test_auto_stops_at_convergence...", end=" ")
    codes = [f"<html><body>V{n}</body></html>" for n in range(1, 4)]
    loop_obj, _, restore = _make_loop(codes, [BAD_AUDIT, GOOD_AUDIT], max_iterations=5, auto=True)
    try:
        outcome = loop_obj.run(port=0)
    finally:
        restore()
    assert outcome == "converged", f"got {outcome}"
    assert loop_obj.iteration == 2, f"should stop the moment it converges, got {loop_obj.iteration}"
    print("✅")


def test_interactive_run_still_reports_its_outcome():
    """run() returning an outcome is what lets main() set an exit code; the
    interactive paths must report it too, not just --auto."""
    print("  test_interactive_run_still_reports_its_outcome...", end=" ")
    loop_obj, _, restore = _make_loop(["<html><body>V1</body></html>"], [BAD_AUDIT])
    try:
        loop_obj.status.put_action("accept")
        outcome = loop_obj.run(port=0)
    finally:
        restore()
    assert outcome == "accepted", f"got {outcome}"
    print("✅")


def test_worst_cell_picks_the_lowest_scoring_view():
    """The composite score IS the worst cell, so an unattended refine must
    attach that cell's screenshot rather than the default view's."""
    print("  test_worst_cell_picks_the_lowest_scoring_view...", end=" ")
    loop_obj, _, restore = _make_loop([], [])
    try:
        loop_obj.view_audits = {
            "dashboard@desktop": {"audit": {"overall_score": 80, "bugs": []}, "iteration": 1},
            "settings@mobile": {"audit": {"overall_score": 41, "bugs": []}, "iteration": 1},
            "settings@desktop": {"audit": {"overall_score": 77, "bugs": []}, "iteration": 1},
        }
        loop_obj._capture_records = [
            {"id": "dashboard", "breakpoint": "desktop", "screenshot": "dash.png"},
            {"id": "settings", "breakpoint": "mobile", "screenshot": "settings-mobile.png"},
            {"id": "settings", "breakpoint": "desktop", "screenshot": "settings-desktop.png"},
        ]
        assert loop_obj._worst_cell() == "settings@mobile"
    finally:
        restore()
    print("✅")


def test_keyword_translation_and_count():
    print("  test_keyword_translation_and_count...", end=" ")
    loop_obj, _, restore = _make_loop([], [])
    try:
        s = loop_obj.status
        s.put_feedback("DONE")
        s.put_feedback(" keep ")
        s.put_feedback("undo")
        s.put_feedback("undo the header change")  # prose — NOT a keyword
        s.put_action("accept")
        assert loop_obj._next_action() == {"type": "accept"}
        assert loop_obj._next_action() == {"type": "accept"}
        assert loop_obj._next_action() == {"type": "undo"}
        assert loop_obj._next_action() == {"type": "feedback", "text": "undo the header change"}
        assert loop_obj._next_action() == {"type": "accept"}
        assert s.get_status()["feedback_count"] == 1  # only the prose counted
    finally:
        restore()
    print("✅")


def test_accept_exits_and_finishes():
    print("  test_accept_exits_and_finishes...", end=" ")
    loop_obj, _, restore = _make_loop(["<html><body>V1</body></html>"], [BAD_AUDIT])
    try:
        loop_obj.status.put_action("accept")  # consumed at the first wait
        loop_obj.run(port=0)
    finally:
        restore()
    final = open(config.FINAL_PATH).read()
    assert "V1" in final and "tailwind.config" in final
    assert loop_obj.status.get_status()["phase"] == "converged"
    meta = loop_obj.run_state.load_run()
    assert meta["finished"] is True
    assert loop_obj.run_state.latest_iteration() == 1
    print("✅")


def test_undo_restores_previous_iteration():
    print("  test_undo_restores_previous_iteration...", end=" ")
    loop_obj, prompts, restore = _make_loop(["<html><body>V1</body></html>", "<html><body>V2</body></html>"], [BAD_AUDIT, BAD_AUDIT])
    try:
        s = loop_obj.status
        s.put_feedback("try something else")  # -> iteration 2 (V2)
        s.put_feedback("UNDO")  # -> restore iteration 1 (V1)
        s.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    final = open(config.FINAL_PATH).read()
    assert "V1" in final and "V2" not in final
    assert loop_obj.run_state.latest_iteration() == 2  # snapshots append-only
    assert loop_obj.run_state.load_run()["undo_pointer"] == 1
    assert loop_obj.status.get_status()["feedback_count"] == 1
    print("✅")

    events = loop_obj.run_state.read_events()
    kinds = [e["event"] for e in events]
    assert "run_started" in kinds
    undo_events = [e for e in events if e["event"] == "undo"]
    assert undo_events and undo_events[0]["restored"] == 1 and undo_events[0]["from"] == 2
    produced = [e for e in events if e["event"] == "iteration_produced"]
    assert produced[0]["feedback"] is None  # initial draft
    assert produced[1]["feedback"] == "try something else"
    finished = [e for e in events if e["event"] == "finished"]
    assert finished and finished[-1]["outcome"] == "accepted"


def test_undo_at_first_iteration_noop():
    print("  test_undo_at_first_iteration_noop...", end=" ")
    loop_obj, _, restore = _make_loop(["<html><body>V1</body></html>"], [BAD_AUDIT])
    try:
        loop_obj.status.put_action("undo")  # nothing to undo yet
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    assert "V1" in open(config.FINAL_PATH).read()
    assert loop_obj.run_state.load_run()["undo_pointer"] == 1
    print("✅")


def test_error_wait_retry_and_keep():
    print("  test_error_wait_retry_and_keep...", end=" ")
    calls = {"n": 0}

    def flaky_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        if "design theme" in prompt:
            return json.dumps(THEME)
        calls["n"] += 1
        if calls["n"] == 2:  # the first refine call fails
            raise Exception("brain melted")
        return f"<html><body>V{calls['n']}</body></html>"

    orig_llm, orig_audit = llm_client.call_llm, visual_audit.perform_visual_audit
    llm_client.call_llm = flaky_llm
    visual_audit.perform_visual_audit = lambda p, i, view_context=None: dict(BAD_AUDIT)
    undo_export = _force_finish_stubs()
    loop_obj = FrontendDesignLoop("Test intent", max_iterations=5)
    loop_obj.run_state = RunState(Path(tempfile.mkdtemp()) / "run_state")
    loop_obj.status.signal_provider_config()
    loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [{"id": "default", "screenshot": "dummy.png"}])[1]
    try:
        s = loop_obj.status
        s.put_feedback("refine please")  # refine #1 fails -> error wait
        s.put_feedback("try again")  # retry succeeds -> iteration 2 (V3)
        s.put_feedback("KEEP")  # accept at the next wait
        loop_obj.run(port=0)
    finally:
        llm_client.call_llm = orig_llm
        visual_audit.perform_visual_audit = orig_audit
        undo_export()
    assert "V3" in open(config.FINAL_PATH).read()
    assert loop_obj.run_state.latest_iteration() == 2
    print("✅")

    events = loop_obj.run_state.read_events()
    errors = [e for e in events if e["event"] == "error"]
    assert errors and "brain melted" in errors[0]["message"]
    produced = [e for e in events if e["event"] == "iteration_produced"]
    assert produced[-1]["feedback"] == "try again"  # retry feedback attributed
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted"  # KEEP == accepted


def test_synthetic_failure_audit():
    print("  test_synthetic_failure_audit...", end=" ")
    a = synthetic_failure_audit("panel not visible after activation", "view 'x'")
    assert a["overall_score"] == 0
    assert a["bugs"][0]["severity"] == "critical"
    assert "view 'x' failed to activate" in a["bugs"][0]["issue"]
    print("✅")


def test_composite_audit():
    print("  test_composite_audit...", end=" ")
    va = {
        "dashboard": {"audit": {"overall_score": 90, "bugs": [], "summary": "fine"}, "iteration": 2},
        "settings": {
            "audit": {"overall_score": 55, "bugs": [{"severity": "critical", "issue": "x"}], "summary": "cramped"},
            "iteration": 2,
        },
    }
    c = composite_audit(va, ["dashboard", "settings"])
    assert c["overall_score"] == 55
    assert c["summary"] == "[settings] cramped"
    assert len(c["bugs"]) == 1
    assert c["views"] == {"dashboard": {"score": 90, "bugs": 0}, "settings": {"score": 55, "bugs": 1}}
    single = composite_audit({"default": va["dashboard"]}, ["default"])
    assert single["summary"] == "fine"  # no prefix for single view
    assert list(single["views"]) == ["default"]
    # A close_failed cell is flagged in the views map so the shell chip can
    # refuse the green 'pass' state even at score 100:
    va["cart@desktop+checkout-modal"] = {
        "audit": {"overall_score": 100, "bugs": [close_failed_bug("checkout-modal")], "summary": "clean but stuck"},
        "iteration": 2,
    }
    c2 = composite_audit(va, ["dashboard", "settings", "cart@desktop+checkout-modal"])
    assert c2["views"]["cart@desktop+checkout-modal"].get("close_failed") is True
    assert "close_failed" not in c2["views"]["dashboard"]
    print("✅")


def test_match_views():
    print("  test_match_views...", end=" ")
    ids = ["dashboard", "user-settings"]
    assert match_views("tighten the settings table", ids) == ["user-settings"]
    assert match_views("the DASHBOARD feels empty", ids) == ["dashboard"]
    assert match_views("user-settings needs padding", ids) == ["user-settings"]
    assert match_views("make it darker", ids) == []
    assert match_views("reset it please", ids) == []  # no substring matches
    assert match_views("", ids) == []
    assert match_views(None, ids) == []
    print("✅")


def test_cell_helpers():
    print("  test_cell_helpers...", end=" ")
    assert cell_key("settings", "mobile") == "settings@mobile"
    assert cell_key("cart", "mobile", "checkout-modal") == "cart@mobile+checkout-modal"
    assert cell_key("cart", "mobile", None, "hover") == "cart@mobile#hover"
    assert split_cell("settings@mobile") == ("settings", "mobile", None, None)
    assert split_cell("cart@mobile+checkout-modal") == ("cart", "mobile", "checkout-modal", None)
    assert split_cell("cart@mobile#hover") == ("cart", "mobile", None, "hover")
    assert split_cell("settings") == ("settings", "desktop", None, None)  # legacy key
    assert record_cell({"id": "cart", "breakpoint": "mobile", "pseudo": "focus"}) == "cart@mobile#focus"
    print("✅")


def test_match_cells():
    print("  test_match_cells...", end=" ")
    cells = [
        "dashboard@desktop",
        "user-settings@desktop",
        "user-settings@desktop+filters-drawer",
        "dashboard@mobile",
        "user-settings@mobile",
        "user-settings@mobile+filters-drawer",
    ]
    # nothing -> [] (global convention)
    assert match_cells("make it darker", cells) == []
    # view only -> that view's base AND state cells, all breakpoints
    assert match_cells("the settings table", cells) == [
        "user-settings@desktop",
        "user-settings@desktop+filters-drawer",
        "user-settings@mobile",
        "user-settings@mobile+filters-drawer",
    ]
    # breakpoint only -> everything at that breakpoint, states included
    assert match_cells("the mobile menu overlaps", cells) == [
        "dashboard@mobile",
        "user-settings@mobile",
        "user-settings@mobile+filters-drawer",
    ]
    # state only -> matched-state cells across breakpoints
    assert match_cells("the filters drawer is cramped", cells) == [
        "user-settings@desktop+filters-drawer",
        "user-settings@mobile+filters-drawer",
    ]
    # state + breakpoint -> one cell
    assert match_cells("the drawer on mobile", cells) == ["user-settings@mobile+filters-drawer"]
    # breakpoint synonym -> that breakpoint ('phone' maps to mobile)
    assert match_cells("settings on my phone is cramped", cells) == ["user-settings@mobile", "user-settings@mobile+filters-drawer"]
    # breakpoint-adjacent unmapped word -> global
    assert match_cells("make it responsive", cells) == []
    print("✅")


def test_match_states_ignores_breakpoint_words():
    """A state token that is also a breakpoint keyword must not hijack
    breakpoint feedback: 'the mobile layout' targets the mobile breakpoint,
    not a 'mobile-menu' state. Naming the state still works via its other
    tokens or the full id."""
    print("  test_match_states_ignores_breakpoint_words...", end=" ")
    assert match_states("the mobile layout is cramped", ["mobile-menu"]) == []
    assert match_states("the menu overlaps", ["mobile-menu"]) == ["mobile-menu"]
    assert match_states("fix the mobile-menu", ["mobile-menu"]) == ["mobile-menu"]
    cells = ["home@desktop", "home@desktop+mobile-menu", "home@mobile", "home@mobile+mobile-menu"]
    # bp word alone -> base AND state cells at that breakpoint, not state-only
    assert match_cells("the mobile layout is cramped", cells) == ["home@mobile", "home@mobile+mobile-menu"]
    # non-bp token still narrows to the state (at the matched breakpoint)
    assert match_cells("the mobile menu is broken", cells) == ["home@mobile+mobile-menu"]
    print("✅")


def test_match_pseudo():
    print("  test_match_pseudo...", end=" ")
    names = ["hover", "focus"]
    assert match_pseudo("the hover states look flat", names) == ["hover"]
    assert match_pseudo("the focus rings are invisible", names) == ["focus"]
    assert match_pseudo("focus indicator contrast is weak", names) == ["focus"]
    assert match_pseudo("the focus outline clips", names) == ["focus"]
    # bare 'focus' is the imperative verb — must NOT match:
    assert match_pseudo("focus on the header first", names) == []
    assert match_pseudo("make it darker", names) == []
    assert match_pseudo("", names) == []
    print("✅")


def test_reserved_words_excluded_from_views_and_states():
    print("  test_reserved_words_excluded_from_views_and_states...", end=" ")
    # split tokens lose reserved words; the full id always matches
    assert match_views("the hover states look flat", ["focus-mode", "hover-cards"]) == []
    assert match_views("open focus-mode please", ["focus-mode"]) == ["focus-mode"]
    assert match_states("the focus rings are weak", ["focus-trap-modal"]) == []
    assert match_states("the focus-trap-modal is stuck", ["focus-trap-modal"]) == ["focus-trap-modal"]
    print("✅")


def test_match_cells_pseudo_axis():
    print("  test_match_cells_pseudo_axis...", end=" ")
    cells = [
        "cart@desktop",
        "cart@desktop+checkout-modal",
        "cart@desktop#hover",
        "cart@desktop#focus",
        "cart@mobile",
        "cart@mobile#hover",
        "cart@mobile#focus",
    ]
    # pseudo only -> sheet cells across breakpoints
    assert match_cells("the hover states look flat", cells) == ["cart@desktop#hover", "cart@mobile#hover"]
    # pseudo + breakpoint -> one sheet cell
    assert match_cells("the focus rings on mobile are invisible", cells) == ["cart@mobile#focus"]
    # view only -> everything of that view, sheets included
    assert match_cells("the cart page", cells) == cells
    # no axis -> global
    assert match_cells("focus on making it darker", cells) == []
    print("✅")


def test_missing_focus_bug_shapes():
    print("  test_missing_focus_bug_shapes...", end=" ")
    b = missing_focus_bug('a "Learn more" [v27]')
    assert b["severity"] == "important" and b["synthetic"] == "missing_focus"
    assert 'a "Learn more" [v27]' in b["issue"] and "focus indicator" in b["issue"]
    agg = aggregate_missing_focus_bug(['a "x" [v1]', 'a "y" [v2]', 'a "z" [v3]', 'a "w" [v4]'])
    assert agg["synthetic"] == "missing_focus"
    assert agg["issue"].startswith("4 interactive element(s) lack a visible focus indicator")
    assert "(+1 more)" in agg["issue"] and 'a "w" [v4]' not in agg["issue"]
    short = aggregate_missing_focus_bug(['a "x" [v1]'])
    assert "(+" not in short["issue"]
    print("✅")


def test_audit_passes_synthetic_allowlist():
    print("  test_audit_passes_synthetic_allowlist...", end=" ")
    loop_obj = FrontendDesignLoop("x")
    blocked = {"overall_score": 100, "bugs": [missing_focus_bug('a "x" [v1]')]}
    assert loop_obj._audit_passes(blocked) is False
    unknown = {"overall_score": 100, "bugs": [{"severity": "minor", "issue": "m", "synthetic": "made-up-by-vision"}]}
    assert loop_obj._audit_passes(unknown) is True  # unknown tags never hold the gate
    print("✅")


def test_match_cells_combined_state_pseudo():
    print("  test_match_cells_combined_state_pseudo...", end=" ")
    cells = [
        "cart@desktop",
        "cart@desktop+checkout-modal",
        "cart@desktop+checkout-modal#hover",
        "cart@desktop+checkout-modal#focus",
        "cart@desktop#hover",
        "cart@desktop#focus",
        "cart@mobile+checkout-modal",
        "cart@mobile+checkout-modal#focus",
    ]
    # state + pseudo -> exactly that overlay's matching sheets, all breakpoints
    assert match_cells("the checkout modal's focus rings are invisible", cells) == [
        "cart@desktop+checkout-modal#focus",
        "cart@mobile+checkout-modal#focus",
    ]
    # pseudo only -> base AND overlay sheets of that pseudo
    assert match_cells("the hover states look flat", cells) == ["cart@desktop+checkout-modal#hover", "cart@desktop#hover"]
    # state only -> the state's base cell AND its sheets
    assert match_cells("the checkout modal is cramped", cells) == [
        "cart@desktop+checkout-modal",
        "cart@desktop+checkout-modal#hover",
        "cart@desktop+checkout-modal#focus",
        "cart@mobile+checkout-modal",
        "cart@mobile+checkout-modal#focus",
    ]
    print("\u2705")


def test_close_failed_guard_ignores_sheet_records():
    print("  test_close_failed_guard_ignores_sheet_records...", end=" ")
    audit = {"overall_score": 100, "bugs": []}
    poisoned = {"id": "cart", "breakpoint": "desktop", "state": "checkout-modal", "pseudo": "hover", "close_failed": True}
    out = FrontendDesignLoop._maybe_close_failed(audit, poisoned)
    assert out["bugs"] == [], f"sheet record attached a toggle bug: {out}"
    state_rec = {"id": "cart", "breakpoint": "desktop", "state": "checkout-modal", "close_failed": True}
    out2 = FrontendDesignLoop._maybe_close_failed(audit, state_rec)
    assert any(b.get("synthetic") == "close_failed" for b in out2["bugs"])
    print("\u2705")


def test_match_cells_union_on_collision():
    """View 'checkout' owns state 'checkout-modal': 'fix checkout' matches
    both axes; the union rule must include the BASE cells the user meant."""
    print("  test_match_cells_union_on_collision...", end=" ")
    cells = ["checkout@desktop", "checkout@desktop+checkout-modal", "cart@desktop"]
    assert match_cells("fix checkout", cells) == ["checkout@desktop", "checkout@desktop+checkout-modal"]
    print("✅")


def test_close_failed_bug_and_synthetic_wording():
    print("  test_close_failed_bug_and_synthetic_wording...", end=" ")
    b = close_failed_bug("checkout-modal")
    assert b["severity"] == "important" and b["synthetic"] == "close_failed"
    assert "does not close the overlay" in b["issue"]
    a = synthetic_failure_audit("panel not visible after trigger click", "state 'checkout-modal'")
    assert "state 'checkout-modal' failed to activate" in a["bugs"][0]["issue"]
    a2 = synthetic_failure_audit("panel not visible after activation", "view 'settings'")
    assert "view 'settings' failed to activate" in a2["bugs"][0]["issue"]
    print("✅")


def test_mv_normal_path_refine_routing():
    """Regression: view-named feedback on the NORMAL path must attach the
    routed view's screenshot and name it in the refine prompt (PR #7 review:
    routing had landed only in the error-retry path)."""
    print("  test_mv_normal_path_refine_routing...", end=" ")
    refine_calls = []

    def fake_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        if "design theme" in prompt:
            return json.dumps(THEME)
        if image_path is not None:
            refine_calls.append({"image_path": image_path, "prompt": prompt})
        return "<html><body>MV</body></html>"

    audit_script = {"dash.png": [dict(FAIL_AUDIT)], "set.png": [dict(FAIL_AUDIT), dict(FAIL_AUDIT)]}

    def fake_visual_audit(image_path, original_intent, view_context=None):
        return dict(audit_script[image_path].pop(0))

    orig_llm, orig_audit = llm_client.call_llm, visual_audit.perform_visual_audit
    llm_client.call_llm = fake_llm
    visual_audit.perform_visual_audit = fake_visual_audit
    undo_stubs = _force_finish_stubs()
    loop_obj = FrontendDesignLoop("an admin app", max_iterations=6)
    loop_obj.run_state = RunState(Path(tempfile.mkdtemp()) / "run_state")
    loop_obj.status.signal_provider_config()
    loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [dict(r) for r in MV_RECORDS])[1]
    try:
        loop_obj.status.put_feedback("tighten the settings table")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        llm_client.call_llm = orig_llm
        visual_audit.perform_visual_audit = orig_audit
        undo_stubs()

    assert len(refine_calls) == 1, f"expected one refine call, got {len(refine_calls)}"
    assert refine_calls[0]["image_path"] == "set.png", f"refine attached the wrong view's screenshot: {refine_calls[0]['image_path']}"
    assert "The attached screenshot shows the 'settings' view at the desktop viewport." in refine_calls[0]["prompt"]
    print("✅")


def test_mv_single_view_regression():
    print("  test_mv_single_view_regression...", end=" ")
    records = [{"id": "default", "screenshot": "dummy.png"}]
    script = {"dummy.png": [dict(FAIL_AUDIT), dict(PASS_AUDIT)]}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_feedback("more contrast")
        loop_obj.run(port=0)
    finally:
        restore()
    assert len(calls) == 2
    assert all(c["context"] is None for c in calls)  # single view: today's prompt
    composite = loop_obj.status.get_audit()
    assert not composite["summary"].startswith("[")  # no prefix
    print("✅")


MX_RECORDS = [
    {"id": "dashboard", "breakpoint": "desktop", "screenshot": "dash_desktop.png"},
    {"id": "settings", "breakpoint": "desktop", "screenshot": "set_desktop.png"},
    {"id": "dashboard", "breakpoint": "mobile", "screenshot": "dash_mobile.png"},
    {"id": "settings", "breakpoint": "mobile", "screenshot": "set_mobile.png"},
]


def test_matrix_targeting():
    print("  test_matrix_targeting...", end=" ")
    # set_mobile.png is audited 4x: full sweep, view-named, bp-named, both-named
    script = {p: [dict(FAIL_AUDIT)] * 4 for p in ("dash_desktop.png", "set_desktop.png", "dash_mobile.png", "set_mobile.png")}
    loop_obj, calls, restore = _make_mv_loop(script, records=MX_RECORDS)
    try:
        s = loop_obj.status
        s.put_feedback("the settings table")  # view-named -> settings x all bps
        s.put_feedback("the mobile menu overlaps")  # bp-named -> all views @ mobile
        s.put_feedback("settings on mobile")  # both -> one cell
        s.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    paths = [c["path"] for c in calls]
    assert paths[:1] == ["dash_desktop.png"]  # iter1: default cell only
    assert set(paths[1:3]) == {"set_desktop.png", "set_mobile.png"}  # view-named (tablet absent from shots)
    assert set(paths[3:5]) == {"dash_mobile.png", "set_mobile.png"}  # bp-named
    assert paths[5:] == ["set_mobile.png"]  # both -> single cell
    print("✅")


def test_matrix_topup_convergence():
    print("  test_matrix_topup_convergence...", end=" ")
    script = {
        "dash_desktop.png": [dict(FAIL_AUDIT), dict(PASS_AUDIT)],
        "set_desktop.png": [dict(PASS_AUDIT)],
        "dash_mobile.png": [dict(PASS_AUDIT)],
        "set_mobile.png": [dict(PASS_AUDIT)],
    }
    loop_obj, calls, restore = _make_mv_loop(script, records=MX_RECORDS)
    try:
        loop_obj.status.put_feedback("settings on mobile")  # 1 fresh cell passes
        loop_obj.run(port=0)  # top-up sweeps the other 3
    finally:
        restore()
    assert len(calls) == 1 + 1 + 3  # default cell + targeted + top-up
    events = loop_obj.run_state.read_events()
    assert [e for e in events if e["event"] == "finished"][-1]["outcome"] == "converged"
    produced = [e for e in events if e["event"] == "iteration_produced"]
    assert "settings@mobile" in produced[-1]["views"]  # cell-keyed event views
    print("✅")


def test_matrix_context_mentions_viewport():
    print("  test_matrix_context_mentions_viewport...", end=" ")
    script = {p: [dict(PASS_AUDIT)] for p in ("dash_desktop.png", "set_desktop.png", "dash_mobile.png", "set_mobile.png")}
    loop_obj, calls, restore = _make_mv_loop(script, records=MX_RECORDS)
    try:
        loop_obj.status.put_action("accept")  # converges at iter1 anyway
        loop_obj.run(port=0)
    finally:
        restore()
    mobile_calls = [c for c in calls if c["path"].endswith("_mobile.png")]
    assert all("mobile viewport (375\u00d7812)" in c["context"] for c in mobile_calls)
    assert all("stacked single-column layout is correct" in c["context"] for c in calls)
    print("✅")


def test_single_view_multi_bp_regression():
    print("  test_single_view_multi_bp_regression...", end=" ")
    records = [
        {"id": "default", "breakpoint": "desktop", "screenshot": "d_desktop.png"},
        {"id": "default", "breakpoint": "mobile", "screenshot": "d_mobile.png"},
    ]
    script = {"d_desktop.png": [dict(FAIL_AUDIT), dict(PASS_AUDIT)], "d_mobile.png": [dict(PASS_AUDIT)]}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_feedback("more contrast")  # global -> both cells
        loop_obj.run(port=0)
    finally:
        restore()
    assert len(calls) == 3
    composite = loop_obj.status.get_audit()
    assert set(composite["views"]) == {"default@desktop", "default@mobile"}
    print("✅")


def test_matrix_refine_routing_normal_path():
    print("  test_matrix_refine_routing_normal_path...", end=" ")
    refine_calls = []

    def fake_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        if "design theme" in prompt:
            return json.dumps(THEME)
        if image_path is not None:
            refine_calls.append({"image_path": image_path, "prompt": prompt})
        return "<html><body>MX</body></html>"

    script = {p: [dict(FAIL_AUDIT)] * 2 for p in ("dash_desktop.png", "set_desktop.png", "dash_mobile.png", "set_mobile.png")}

    def fake_visual_audit(image_path, original_intent, view_context=None):
        return dict(script[image_path].pop(0))

    orig_llm, orig_audit = llm_client.call_llm, visual_audit.perform_visual_audit
    llm_client.call_llm = fake_llm
    visual_audit.perform_visual_audit = fake_visual_audit
    undo_stubs = _force_finish_stubs()
    loop_obj = FrontendDesignLoop("an admin app", max_iterations=6)
    loop_obj.run_state = RunState(Path(tempfile.mkdtemp()) / "run_state")
    loop_obj.status.signal_provider_config()
    loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [dict(r) for r in MX_RECORDS])[1]
    try:
        loop_obj.status.put_feedback("settings on mobile is cramped")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        llm_client.call_llm = orig_llm
        visual_audit.perform_visual_audit = orig_audit
        undo_stubs()

    assert len(refine_calls) == 1
    assert refine_calls[0]["image_path"] == "set_mobile.png"
    assert "The attached screenshot shows the 'settings' view at the mobile viewport." in refine_calls[0]["prompt"]
    print("✅")


def test_mv_activation_error_blocks_convergence():
    print("  test_mv_activation_error_blocks_convergence...", end=" ")
    records = [{"id": "dashboard", "screenshot": "dash.png"}, {"id": "settings", "error": "panel not visible after activation"}]
    script = {"dash.png": [dict(PASS_AUDIT)]}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_action("accept")  # must NOT converge on its own
        loop_obj.run(port=0)
    finally:
        restore()
    assert [c["path"] for c in calls] == ["dash.png"]  # no vision call for errored view
    composite = loop_obj.status.get_audit()
    assert composite["overall_score"] == 0
    assert "failed to activate" in composite["bugs"][0]["issue"]
    events = loop_obj.run_state.read_events()
    assert [e for e in events if e["event"] == "finished"][-1]["outcome"] == "accepted"
    print("✅")


def test_mv_topup_sweep_gates_convergence():
    print("  test_mv_topup_sweep_gates_convergence...", end=" ")
    script = {
        "dash.png": [dict(FAIL_AUDIT), dict(PASS_AUDIT)],  # iter1 fail, top-up pass
        "set.png": [dict(PASS_AUDIT)],
    }  # iter2 pass
    loop_obj, calls, restore = _make_mv_loop(script, records=MV_RECORDS)
    try:
        loop_obj.status.put_feedback("fix the settings list")  # iter2: settings only
        loop_obj.run(port=0)  # converges via top-up
    finally:
        restore()
    assert [c["path"] for c in calls] == ["dash.png", "set.png", "dash.png"]
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "converged"
    produced = [e for e in events if e["event"] == "iteration_produced"]
    assert set(produced[-1]["views"]) == {"dashboard@desktop", "settings@desktop"}  # cell-keyed event views
    print("✅")


def test_mv_global_feedback_audits_all():
    print("  test_mv_global_feedback_audits_all...", end=" ")
    script = {"dash.png": [dict(FAIL_AUDIT)] * 3, "set.png": [dict(FAIL_AUDIT)] * 2}
    loop_obj, calls, restore = _make_mv_loop(script, records=MV_RECORDS)
    try:
        s = loop_obj.status
        s.put_feedback("make everything darker")
        s.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    assert len(calls) == 4  # 1 (default cell) + 1 (theme validation) + 2
    print("✅")


def test_mv_targeted_audit_and_context():
    print("  test_mv_targeted_audit_and_context...", end=" ")
    script = {"dash.png": [dict(FAIL_AUDIT)], "set.png": [dict(FAIL_AUDIT), dict(FAIL_AUDIT)]}
    loop_obj, calls, restore = _make_mv_loop(script, records=MV_RECORDS)
    try:
        s = loop_obj.status
        s.put_feedback("tighten the settings table")  # iteration 2: settings only
        s.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    iter1 = calls[:1]
    assert {c["path"] for c in iter1} == {"dash.png"}  # first iteration: default cell only
    assert all("multiple views" in c["context"] for c in iter1)
    iter2 = calls[1:]
    assert [c["path"] for c in iter2] == ["set.png"]  # targeted
    assert "'settings' view" in iter2[0]["context"]
    print("✅")


MV_RECORDS = [{"id": "dashboard", "screenshot": "dash.png"}, {"id": "settings", "screenshot": "set.png"}]

PASS_AUDIT = {"overall_score": 100, "bugs": [], "summary": "clean"}
FAIL_AUDIT = {"overall_score": 50, "bugs": [{"severity": "minor", "issue": "meh"}], "summary": "rough"}


def _make_mv_loop(audit_script, records=None):
    """Multi-view loop harness. audit_script: dict path -> list of audits
    popped per call. Returns (loop_obj, calls, restore)."""
    calls = []

    def fake_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        if "design theme" in prompt:
            return json.dumps(THEME)
        return "<html><body>MV</body></html>"

    def fake_visual_audit(image_path, original_intent, view_context=None):
        calls.append({"path": image_path, "context": view_context})
        return dict(audit_script[image_path].pop(0))

    orig_llm, orig_audit = llm_client.call_llm, visual_audit.perform_visual_audit
    llm_client.call_llm = fake_llm
    visual_audit.perform_visual_audit = fake_visual_audit
    undo_stubs = _force_finish_stubs()

    loop_obj = FrontendDesignLoop("an admin app", max_iterations=6)
    loop_obj.run_state = RunState(Path(tempfile.mkdtemp()) / "run_state")
    loop_obj.status.signal_provider_config()
    recs = records or MV_RECORDS
    loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [dict(r) for r in recs])[1]

    def restore():
        llm_client.call_llm = orig_llm
        visual_audit.perform_visual_audit = orig_audit
        undo_stubs()

    return loop_obj, calls, restore


ST_RECORDS = [
    {"id": "cart", "breakpoint": "desktop", "screenshot": "cart_d.png"},
    {"id": "cart", "breakpoint": "desktop", "state": "checkout-modal", "screenshot": "cart_d_cm.png"},
    {"id": "cart", "breakpoint": "mobile", "screenshot": "cart_m.png"},
    {"id": "cart", "breakpoint": "mobile", "state": "checkout-modal", "screenshot": "cart_m_cm.png"},
]


def test_state_cells_swept_and_context():
    print("  test_state_cells_swept_and_context...", end=" ")
    script = {p: [dict(FAIL_AUDIT)] for p in ("cart_d.png", "cart_d_cm.png", "cart_m.png", "cart_m_cm.png")}
    loop_obj, calls, restore = _make_mv_loop(script, records=ST_RECORDS)
    try:
        # First iteration only audits base cells; target state on iteration 2.
        loop_obj.status.put_feedback("the checkout modal is cramped")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    modal_calls = [c for c in calls if c["path"].endswith("_cm.png")]
    assert [c["path"] for c in modal_calls] == ["cart_d_cm.png", "cart_m_cm.png"], (
        f"expected both modal cells audited on iteration 2, got {[c['path'] for c in modal_calls]}"
    )
    assert all("'checkout-modal' overlay is open" in c["context"] for c in modal_calls)
    events = loop_obj.run_state.read_events()
    produced = [e for e in events if e["event"] == "iteration_produced"]
    assert "cart@desktop+checkout-modal" in produced[-1]["views"]
    print("✅")


def test_state_targeting():
    print("  test_state_targeting...", end=" ")
    script = {p: [dict(FAIL_AUDIT)] * 2 for p in ("cart_d.png", "cart_d_cm.png", "cart_m.png", "cart_m_cm.png")}
    loop_obj, calls, restore = _make_mv_loop(script, records=ST_RECORDS)
    try:
        loop_obj.status.put_feedback("the checkout modal is cramped")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    # Iteration 1 audits base cells; iteration 2 targets state cells.
    state_calls = [c for c in calls if c["path"].endswith("_cm.png")]
    assert [c["path"] for c in state_calls] == ["cart_d_cm.png", "cart_m_cm.png"]
    print("✅")


def test_close_failed_stale_cell_topup_keeps_gate():
    """Regression: a close_failed cell that goes STALE (feedback targets other
    cells) is re-audited by _check_convergence's top-up — the top-up must
    re-attach the toggle bug or the run converges with a broken toggle."""
    print("  test_close_failed_stale_cell_topup_keeps_gate...", end=" ")
    records = [dict(r) for r in ST_RECORDS]
    records[1]["close_failed"] = True  # desktop modal won't close
    script = {
        p: [dict(PASS_AUDIT)] * 3
        for p in ("cart_d.png", "cart_d_cm.png", "cart_m.png", "cart_m_cm.png")  # 3rd: theme validation on cart_d
    }
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_feedback("the mobile layout needs more spacing")
        loop_obj.status.put_action("accept")  # must be needed: no auto-converge
        loop_obj.run(port=0)
    finally:
        restore()
    # Iteration 2 targeted mobile only; the desktop modal cell went stale and
    # was re-audited by the convergence top-up — the bug must survive it:
    topped = loop_obj.view_audits["cart@desktop+checkout-modal"]["audit"]
    assert any(b.get("synthetic") == "close_failed" for b in topped.get("bugs", [])), f"top-up dropped the toggle bug: {topped}"
    # The convergence sweep's set_audit runs last and overwrites produce's
    # composite — the toggle bug must survive into the displayed audit:
    composite = loop_obj.status.get_audit()
    assert any(b.get("synthetic") == "close_failed" for b in composite.get("bugs", [])), (
        f"convergence sweep hid the toggle bug from the composite: {composite['bugs']}"
    )
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted", f"converged with a broken toggle: {finished[-1]}"
    print("✅")


def test_close_failed_blocks_convergence_and_reaches_composite():
    print("  test_close_failed_blocks_convergence...", end=" ")
    records = [dict(r) for r in ST_RECORDS]
    records[1]["close_failed"] = True  # desktop modal won't close
    script = {p: [dict(PASS_AUDIT)] * 2 for p in ("cart_d.png", "cart_d_cm.png", "cart_m.png", "cart_m_cm.png")}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_action("accept")  # must be needed: no auto-converge
        loop_obj.run(port=0)
    finally:
        restore()
    composite = loop_obj.status.get_audit()
    tog = [b for b in composite["bugs"] if b.get("synthetic") == "close_failed"]
    assert len(tog) == 1, f"toggle bug missing from composite: {composite['bugs']}"
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted"  # NOT converged despite all-100 scores
    print("\u2705")


def test_state_open_failure_blocks():
    print("  test_state_open_failure_blocks...", end=" ")
    records = [
        dict(ST_RECORDS[0]),
        {"id": "cart", "breakpoint": "desktop", "state": "checkout-modal", "error": "state panel not visible after trigger click"},
    ]
    script = {"cart_d.png": [dict(PASS_AUDIT)]}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    composite = loop_obj.status.get_audit()
    assert composite["overall_score"] == 0
    assert "state 'checkout-modal' failed to activate" in composite["bugs"][0]["issue"]
    print("\u2705")


def test_state_refine_routing_normal_path():
    print("  test_state_refine_routing_normal_path...", end=" ")
    refine_calls = []

    def fake_llm(role, prompt, system_prompt=None, image_path=None, **kw):
        if "design theme" in prompt:
            return json.dumps(THEME)
        if image_path is not None:
            refine_calls.append({"image_path": image_path, "prompt": prompt})
        return "<html><body>ST</body></html>"

    script = {p: [dict(FAIL_AUDIT)] * 2 for p in ("cart_d.png", "cart_d_cm.png", "cart_m.png", "cart_m_cm.png")}

    def fake_visual_audit(image_path, original_intent, view_context=None):
        return dict(script[image_path].pop(0))

    orig_llm, orig_audit = llm_client.call_llm, visual_audit.perform_visual_audit
    llm_client.call_llm = fake_llm
    visual_audit.perform_visual_audit = fake_visual_audit
    undo_stubs = _force_finish_stubs()
    loop_obj = FrontendDesignLoop("a shop", max_iterations=6)
    loop_obj.run_state = RunState(Path(tempfile.mkdtemp()) / "run_state")
    loop_obj.status.signal_provider_config()
    loop_obj.render_and_capture = lambda: (loop_obj.render_preview(), [dict(r) for r in ST_RECORDS])[1]
    try:
        loop_obj.status.put_feedback("the checkout modal on mobile is cramped")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        llm_client.call_llm = orig_llm
        visual_audit.perform_visual_audit = orig_audit
        undo_stubs()

    assert len(refine_calls) == 1
    assert refine_calls[0]["image_path"] == "cart_m_cm.png"
    assert "the 'cart' view at the mobile viewport with 'checkout-modal' open" in refine_calls[0]["prompt"]
    print("\u2705")


PS_RECORDS = [
    {"id": "cart", "breakpoint": "desktop", "screenshot": "cart_d.png"},
    {"id": "cart", "breakpoint": "desktop", "pseudo": "hover", "screenshot": "cart_d_h.png"},
    {"id": "cart", "breakpoint": "desktop", "pseudo": "focus", "screenshot": "cart_d_f.png"},
    {"id": "cart", "breakpoint": "mobile", "screenshot": "cart_m.png"},
    {"id": "cart", "breakpoint": "mobile", "pseudo": "hover", "screenshot": "cart_m_h.png"},
    {"id": "cart", "breakpoint": "mobile", "pseudo": "focus", "screenshot": "cart_m_f.png"},
]
PS_PATHS = ("cart_d.png", "cart_d_h.png", "cart_d_f.png", "cart_m.png", "cart_m_h.png", "cart_m_f.png")


def test_sheet_cells_swept_and_context():
    print("  test_sheet_cells_swept_and_context...", end=" ")
    script = {p: [dict(FAIL_AUDIT)] for p in PS_PATHS}
    loop_obj, calls, restore = _make_mv_loop(script, records=PS_RECORDS)
    try:
        # First iteration only audits base cells; target hover on iteration 2
        # to verify sheet context is still attached when sheets are audited.
        loop_obj.status.put_feedback("the hover states look flat")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    hover_calls = [c for c in calls if c["path"].endswith("_h.png")]
    assert [c["path"] for c in hover_calls] == ["cart_d_h.png", "cart_m_h.png"], (
        f"expected both hover sheets audited on iteration 2, got {[c['path'] for c in hover_calls]}"
    )
    assert all("hover state forced" in c["context"] for c in hover_calls)
    focus_calls = [c for c in calls if c["path"].endswith("_f.png")]
    assert all("focus indicator" in c["context"] for c in focus_calls)
    events = loop_obj.run_state.read_events()
    produced = [e for e in events if e["event"] == "iteration_produced"]
    assert "cart@desktop#hover" in produced[-1]["views"]
    print("✅")


def test_missing_focus_blocks_and_aggregates():
    print("  test_missing_focus_blocks_and_aggregates...", end=" ")
    records = [dict(r) for r in PS_RECORDS]
    records[2]["missing_focus"] = ['a "Learn more" [v3]', 'a "Learn more" [v9]']
    script = {p: [dict(PASS_AUDIT)] * 3 for p in PS_PATHS}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        # Base cells pass on iteration 1; missing_focus synthetic bug blocks convergence.
        # Target the focus sheet on iteration 2 to verify per-cell aggregation.
        loop_obj.status.put_feedback("the focus rings are invisible")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    cell_audit = loop_obj.view_audits["cart@desktop#focus"]["audit"]
    per_cell = [b for b in cell_audit["bugs"] if b.get("synthetic") == "missing_focus"]
    assert len(per_cell) == 2  # per-offender on the cell
    composite = loop_obj.status.get_audit()
    agg = [b for b in composite["bugs"] if b.get("synthetic") == "missing_focus"]
    assert len(agg) == 1, f"composite must aggregate: {composite['bugs']}"
    assert agg[0]["issue"].startswith("2 interactive element(s)")
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted"  # NOT converged
    assert finished[-1].get("synthetic_blocks") == ["missing_focus"]
    print("✅")


def test_first_iteration_only_audits_base_cells():
    """Regression: first iteration must not audit pseudo-state sheets or state
    overlays, keeping initial screenshot analysis fast."""
    print("  test_first_iteration_only_audits_base_cells...", end=" ")
    script = {
        "cart_d.png": [dict(PASS_AUDIT)],
        "cart_d_h.png": [dict(FAIL_AUDIT)],
        "cart_d_f.png": [dict(FAIL_AUDIT)],
        "cart_m_h.png": [dict(FAIL_AUDIT)],
        "cart_m_f.png": [dict(FAIL_AUDIT)],
    }
    loop_obj, calls, restore = _make_mv_loop(script, records=PS_RECORDS)
    try:
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    assert {c["path"] for c in calls} == {"cart_d.png"}, f"audited non-default cells on first iteration: {set(c['path'] for c in calls)}"
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted"
    print("✅")


def test_convergence_gate_not_masked_by_bugfree_low_score_cell():
    """Regression: the convergence gate must be per-cell, not composite.
    composite_audit keeps only the worst-by-score cell's bugs, so a bug-free
    low-scoring cell would otherwise mask a bugged higher-scoring cell and let
    the loop converge with a real bug still present."""
    print("  test_convergence_gate_not_masked_by_bugfree_low_score_cell...", end=" ")
    low_no_bugs = {"overall_score": 90, "bugs": [], "summary": "low but clean"}
    mid_with_bug = {"overall_score": 94, "bugs": [{"severity": "minor", "issue": "real"}], "summary": "has bug"}
    # dashboard: iter1 audit, then iter2 top-up surfaces a real bug at score 94.
    # settings: iter2 targeted audit has no bugs but a lower score (90) -> it is
    # the composite's "worst" cell, which would mask dashboard's bug under a
    # composite gate.
    script = {"dash.png": [dict(PASS_AUDIT), dict(mid_with_bug)], "set.png": [dict(low_no_bugs)]}
    loop_obj, calls, restore = _make_mv_loop(script, records=MV_RECORDS)
    try:
        loop_obj.status.put_feedback("the settings list")  # iter2 targets settings only
        loop_obj.status.put_action("accept")  # only reached if NOT converged
        loop_obj.run(port=0)
    finally:
        restore()
    assert [c["path"] for c in calls] == ["dash.png", "set.png", "dash.png"], f"unexpected audit sequence: {[c['path'] for c in calls]}"
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted", f"gate falsely converged while a bugged cell remained: {finished[-1]['outcome']}"
    print("✅")


def test_missing_focus_stale_topup_keeps_gate():
    print("  test_missing_focus_stale_topup_keeps_gate...", end=" ")
    records = [dict(r) for r in PS_RECORDS]
    records[2]["missing_focus"] = ['a "Learn more" [v3]']
    script = {p: [dict(PASS_AUDIT)] * 4 for p in PS_PATHS}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_feedback("the mobile layout needs more spacing")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    topped = loop_obj.view_audits["cart@desktop#focus"]["audit"]
    assert any(b.get("synthetic") == "missing_focus" for b in topped.get("bugs", [])), f"top-up dropped the focus bug: {topped}"
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted"
    print("✅")


def test_pseudo_targeting():
    print("  test_pseudo_targeting...", end=" ")
    script = {p: [dict(FAIL_AUDIT)] * 2 for p in PS_PATHS}
    loop_obj, calls, restore = _make_mv_loop(script, records=PS_RECORDS)
    try:
        loop_obj.status.put_feedback("the hover states look flat")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    # last-two, not calls[6:]: theme validation may add an audit mid-sequence,
    # and the convergence check exits on the fresh failures before topping up
    assert [c["path"] for c in calls[-2:]] == ["cart_d_h.png", "cart_m_h.png"]
    print("✅")


def test_bare_focus_not_rerouted():
    """'focus' as imperative verb must not swap the refine attachment to a
    forced-focus sheet; real focus vocabulary must."""
    print("  test_bare_focus_not_rerouted...", end=" ")
    loop_obj = FrontendDesignLoop("x")
    loop_obj._capture_records = [dict(r) for r in PS_RECORDS]
    loop_obj._last_img = "cart_d.png"
    img, label = loop_obj._route_feedback("focus on the header spacing")
    assert img == "cart_d.png" and label is None  # fell back to the default shot
    img, label = loop_obj._route_feedback("the focus rings are invisible")
    assert img == "cart_d_f.png"
    assert "focus states forced" in label
    print("✅")


def test_vision_synthetic_keys_stripped():
    print("  test_vision_synthetic_keys_stripped...", end=" ")
    poisoned = {
        "overall_score": 100,
        "bugs": [{"severity": "minor", "issue": "m", "synthetic": "close_failed"}],
        "summary": "hallucinated tag",
    }
    script = {p: [dict(poisoned)] for p in PS_PATHS}
    loop_obj, calls, restore = _make_mv_loop(script, records=PS_RECORDS)
    try:
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted", "a vision-authored synthetic tag held the gate"  # stripped -> accepted via DONE
    print("✅")


OS_RECORDS = [
    {"id": "cart", "breakpoint": "desktop", "screenshot": "cart_d.png"},
    {"id": "cart", "breakpoint": "desktop", "state": "checkout-modal", "screenshot": "cart_d_cm.png"},
    {"id": "cart", "breakpoint": "desktop", "state": "checkout-modal", "pseudo": "hover", "screenshot": "cart_d_cm_h.png"},
    {"id": "cart", "breakpoint": "desktop", "state": "checkout-modal", "pseudo": "focus", "screenshot": "cart_d_cm_f.png"},
    {"id": "cart", "breakpoint": "desktop", "pseudo": "hover", "screenshot": "cart_d_h.png"},
    {"id": "cart", "breakpoint": "desktop", "pseudo": "focus", "screenshot": "cart_d_f.png"},
]
OS_PATHS = ("cart_d.png", "cart_d_cm.png", "cart_d_cm_h.png", "cart_d_cm_f.png", "cart_d_h.png", "cart_d_f.png")


def test_overlay_sheet_context_scope_aware():
    print("  test_overlay_sheet_context_scope_aware...", end=" ")
    loop_obj = FrontendDesignLoop("x")
    combined = loop_obj._cell_context("cart@desktop+checkout-modal#hover", ["cart"], 6)
    assert "'checkout-modal' overlay is open" in combined  # state sentence
    assert "inside the open 'checkout-modal' panel" in combined  # scoped hover sentence
    assert "outside the panel are NOT forced" in combined  # background caveat
    base = loop_obj._cell_context("cart@desktop#hover", ["cart"], 6)
    assert "hover state forced" in base
    assert "outside the panel" not in base  # no caveat on base sheets
    cf = loop_obj._cell_context("cart@desktop+checkout-modal#focus", ["cart"], 6)
    assert "inside the open 'checkout-modal' panel" in cf and "focus indicator" in cf
    print("\u2705")


def test_state_only_routing_prefers_state_cell():
    print("  test_state_only_routing_prefers_state_cell...", end=" ")
    loop_obj = FrontendDesignLoop("x")
    loop_obj._capture_records = [dict(r) for r in OS_RECORDS]
    loop_obj._last_img = "cart_d.png"
    img, label = loop_obj._route_feedback("the checkout modal is cramped")
    assert img == "cart_d_cm.png", f"routed to a sheet instead of the state cell: {img}"
    assert "with 'checkout-modal' open" in label and "forced" not in label
    print("\u2705")


def test_combined_routing_label():
    print("  test_combined_routing_label...", end=" ")
    loop_obj = FrontendDesignLoop("x")
    loop_obj._capture_records = [dict(r) for r in OS_RECORDS]
    loop_obj._last_img = "cart_d.png"
    img, label = loop_obj._route_feedback("the checkout modal's focus rings are invisible")
    assert img == "cart_d_cm_f.png"
    assert "with 'checkout-modal' open with focus states forced" in label
    print("\u2705")


def test_overlay_missing_focus_blocks_and_annotates():
    print("  test_overlay_missing_focus_blocks_and_annotates...", end=" ")
    records = [dict(r) for r in OS_RECORDS]
    records[3]["missing_focus"] = ['button "Confirm" [v9]']  # overlay offender
    records[5]["missing_focus"] = ['a "Base link" [v2]']  # base offender
    script = {p: [dict(PASS_AUDIT)] * 3 for p in OS_PATHS}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        # Base cells are audited on iteration 1; target overlay focus on iteration 2.
        loop_obj.status.put_feedback("the checkout modal's focus rings are invisible")
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    composite = loop_obj.status.get_audit()
    agg = [b for b in composite["bugs"] if b.get("synthetic") == "missing_focus"]
    assert len(agg) == 1
    issue = agg[0]["issue"]
    assert "(in checkout-modal)" in issue, f"origin annotation missing: {issue}"
    assert issue.index('a "Base link"') < issue.index('button "Confirm"'), f"base offender must sort first: {issue}"
    cell_audit = loop_obj.view_audits["cart@desktop+checkout-modal#focus"]["audit"]
    assert any(b.get("synthetic") == "missing_focus" for b in cell_audit["bugs"])
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted"  # NOT converged
    print("\u2705")


def test_overlay_missing_focus_stale_topup_keeps_gate():
    print("  test_overlay_missing_focus_stale_topup_keeps_gate...", end=" ")
    records = [dict(r) for r in OS_RECORDS]
    records[3]["missing_focus"] = ['button "Confirm" [v9]']
    script = {p: [dict(PASS_AUDIT)] * 4 for p in OS_PATHS}
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_feedback("make the base page darker")  # targets nothing -> global
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    topped = loop_obj.view_audits["cart@desktop+checkout-modal#focus"]["audit"]
    assert any(b.get("synthetic") == "missing_focus" for b in topped.get("bugs", []))
    events = loop_obj.run_state.read_events()
    finished = [e for e in events if e["event"] == "finished"]
    assert finished[-1]["outcome"] == "accepted"
    print("\u2705")


def test_combined_error_label():
    print("  test_combined_error_label...", end=" ")
    records = [
        dict(OS_RECORDS[0]),
        {"id": "cart", "breakpoint": "desktop", "state": "checkout-modal", "pseudo": "hover", "error": "forcing failed"},
    ]
    script = {"cart_d.png": [dict(PASS_AUDIT)] * 2}  # 2nd: theme validation may consume one
    loop_obj, calls, restore = _make_mv_loop(script, records=records)
    try:
        loop_obj.status.put_action("accept")
        loop_obj.run(port=0)
    finally:
        restore()
    audit = loop_obj.view_audits["cart@desktop+checkout-modal#hover"]["audit"]
    assert "pseudo 'hover' sheet ('checkout-modal' open) failed to activate" in audit["bugs"][0]["issue"], f"label wrong: {audit}"
    print("\u2705")


def test_blocked_only_by_synthetic_helper():
    print("  test_blocked_only_by_synthetic_helper...", end=" ")
    loop_obj = FrontendDesignLoop("x")
    loop_obj.view_audits = {
        "a@desktop": {"audit": {"overall_score": 100, "bugs": []}, "iteration": 1},
        "a@desktop#focus": {"audit": {"overall_score": 100, "bugs": [missing_focus_bug('a "x" [v1]')]}, "iteration": 1},
    }
    assert loop_obj._blocked_only_by_synthetic() is True
    loop_obj.view_audits["a@desktop"]["audit"] = dict(FAIL_AUDIT)
    assert loop_obj._blocked_only_by_synthetic() is False  # a real vision failure too
    loop_obj.view_audits = {"a@desktop": {"audit": dict(PASS_AUDIT), "iteration": 1}}
    assert loop_obj._blocked_only_by_synthetic() is False  # nothing blocked at all
    print("✅")


if __name__ == "__main__":
    print("\n=== Loop Control Tests ===")
    test_render_preview_no_capture()
    test_keyword_translation_and_count()
    test_accept_exits_and_finishes()
    test_undo_restores_previous_iteration()
    test_undo_at_first_iteration_noop()
    test_error_wait_retry_and_keep()
    test_match_views()
    test_cell_helpers()
    test_match_cells()
    test_match_cells_union_on_collision()
    test_match_states_ignores_breakpoint_words()
    test_match_pseudo()
    test_reserved_words_excluded_from_views_and_states()
    test_match_cells_pseudo_axis()
    test_match_cells_combined_state_pseudo()
    test_close_failed_guard_ignores_sheet_records()
    test_missing_focus_bug_shapes()
    test_audit_passes_synthetic_allowlist()
    test_close_failed_bug_and_synthetic_wording()
    test_composite_audit()
    test_synthetic_failure_audit()
    test_mv_targeted_audit_and_context()
    test_mv_global_feedback_audits_all()
    test_mv_topup_sweep_gates_convergence()
    test_mv_activation_error_blocks_convergence()
    test_mv_normal_path_refine_routing()
    test_mv_single_view_regression()
    test_matrix_targeting()
    test_matrix_topup_convergence()
    test_matrix_context_mentions_viewport()
    test_single_view_multi_bp_regression()
    test_matrix_refine_routing_normal_path()
    test_state_cells_swept_and_context()
    test_state_targeting()
    test_close_failed_stale_cell_topup_keeps_gate()
    test_close_failed_blocks_convergence_and_reaches_composite()
    test_state_open_failure_blocks()
    test_state_refine_routing_normal_path()
    test_sheet_cells_swept_and_context()
    test_missing_focus_blocks_and_aggregates()
    test_missing_focus_stale_topup_keeps_gate()
    test_pseudo_targeting()
    test_bare_focus_not_rerouted()
    test_vision_synthetic_keys_stripped()
    test_first_iteration_only_audits_base_cells()
    test_convergence_gate_not_masked_by_bugfree_low_score_cell()
    test_blocked_only_by_synthetic_helper()
    test_overlay_sheet_context_scope_aware()
    test_state_only_routing_prefers_state_cell()
    test_combined_routing_label()
    test_overlay_missing_focus_blocks_and_annotates()
    test_overlay_missing_focus_stale_topup_keeps_gate()
    test_combined_error_label()
    test_auto_refines_without_waiting_for_feedback()
    test_auto_stops_at_convergence()
    test_interactive_run_still_reports_its_outcome()
    test_worst_cell_picks_the_lowest_scoring_view()
    print("\nAll tests passed ✅")
