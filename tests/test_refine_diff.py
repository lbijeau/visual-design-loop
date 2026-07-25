"""Branch tests for refine_code's diff/fallback routing (call_llm stubbed)."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client
from loop import FrontendDesignLoop
from run_state import RunState

BASE = "<!DOCTYPE html>\n<html>\n<body>\n<h1>Old Title</h1>\n</body>\n</html>"


def _loop(current_code=BASE):
    lo = FrontendDesignLoop("an app", max_iterations=3)
    lo.run_state = RunState(Path(tempfile.mkdtemp()) / "run_state")
    lo.current_code = current_code
    lo.theme_json = {"hard_tokens": {}, "soft_tokens": {}}
    lo._capture_records = [{"id": "main", "breakpoint": "desktop", "screenshot": None}]
    lo.view_audits = {}  # read by the theme-change branch (loop.py:505)
    return lo


def _audit(summary="fix the title"):
    return {"overall_score": 50, "summary": summary, "bugs": []}


def _stub(fn):
    orig = llm_client.call_llm
    llm_client.call_llm = fn
    return orig


def test_clean_diff_applied_no_fallback():
    print("  test_clean_diff_applied_no_fallback...", end=" ")
    seen = {"calls": 0}

    def fake(role, prompt, **kw):
        seen["calls"] += 1
        return "<<<<<<< SEARCH\n<h1>Old Title</h1>\n=======\n<h1>New Title</h1>\n>>>>>>> REPLACE"

    orig = _stub(fake)
    try:
        lo = _loop()
        lo.refine_code(_audit(), image_path=None, human_feedback=None)
        assert "<h1>New Title</h1>" in lo.current_code
        assert seen["calls"] == 1  # diff only, no fallback
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_unmatched_block_triggers_fallback():
    print("  test_unmatched_block_triggers_fallback...", end=" ")
    kinds = []

    def fake(role, prompt, **kw):
        kinds.append(kw.get("timeout"))
        if len(kinds) == 1:
            return "<<<<<<< SEARCH\nDOES NOT EXIST\n=======\nx\n>>>>>>> REPLACE"
        return "<!DOCTYPE html><html><body>REBUILT</body></html>"

    orig = _stub(fake)
    try:
        lo = _loop()
        lo.refine_code(_audit(), image_path=None, human_feedback=None)
        assert "REBUILT" in lo.current_code
        assert kinds == [600, 900]  # diff at 600, fallback at 900
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_blockless_document_is_salvaged():
    print("  test_blockless_document_is_salvaged...", end=" ")
    calls = {"n": 0}

    def fake(role, prompt, **kw):
        calls["n"] += 1
        return "```html\n<!DOCTYPE html><html><body>SALVAGED</body></html>\n```"

    orig = _stub(fake)
    try:
        lo = _loop()
        lo.refine_code(_audit(), image_path=None, human_feedback=None)
        assert "SALVAGED" in lo.current_code
        assert "```" not in lo.current_code  # fence-stripped
        assert calls["n"] == 1  # salvaged, no second call
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_theme_change_feedback_uses_diff():
    print("  test_theme_change_feedback_uses_diff...", end=" ")
    timeouts = []

    def fake(role, prompt, **kw):
        timeouts.append(kw.get("timeout"))
        return "<<<<<<< SEARCH\n<h1>Old Title</h1>\n=======\n<h1>Blue Title</h1>\n>>>>>>> REPLACE"

    orig = _stub(fake)
    try:
        lo = _loop()
        # Theme sub-steps do real rendering/audit; stub them so only the
        # diff-vs-full-file routing is exercised.
        lo.update_theme = lambda *a, **k: None
        lo.validate_theme = lambda *a, **k: {"overall_score": 90, "bugs": []}
        # feedback contains a theme keyword ("blue") -> theme path is now diff-first
        lo.refine_code(_audit(), image_path=None, human_feedback="make the title blue")
        assert "<h1>Blue Title</h1>" in lo.current_code
        assert timeouts == [600]  # diff path taken (600); no full-file (900)
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_theme_change_blockless_document_is_salvaged():
    print("  test_theme_change_blockless_document_is_salvaged...", end=" ")
    calls = {"n": 0}

    def fake(role, prompt, **kw):
        calls["n"] += 1
        return "```html\n<!DOCTYPE html><html><body>THEMED SALVAGE</body></html>\n```"

    orig = _stub(fake)
    try:
        lo = _loop()
        lo.update_theme = lambda *a, **k: None
        lo.validate_theme = lambda *a, **k: {"overall_score": 90, "bugs": []}
        lo.refine_code(_audit(), image_path=None, human_feedback="make it blue")
        assert "THEMED SALVAGE" in lo.current_code
        assert "```" not in lo.current_code  # fence-stripped
        assert calls["n"] == 1  # salvaged on the diff call; no full-file second call
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_diff_call_timeout_routes_to_fallback():
    print("  test_diff_call_timeout_routes_to_fallback...", end=" ")
    import requests

    timeouts = []

    def fake(role, prompt, **kw):
        timeouts.append(kw.get("timeout"))
        if len(timeouts) == 1:
            raise requests.exceptions.ReadTimeout("simulated")  # diff call times out
        return "<!DOCTYPE html><html><body>RECOVERED</body></html>"

    orig = _stub(fake)
    try:
        lo = _loop()
        lo.refine_code(_audit(), image_path=None, human_feedback=None)
        assert "RECOVERED" in lo.current_code
        assert timeouts == [600, 900]  # diff attempted (600), then full-file fallback (900)
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_pivot_routes_to_full_file():
    print("  test_pivot_routes_to_full_file...", end=" ")
    timeouts = []

    def fake(role, prompt, **kw):
        timeouts.append(kw.get("timeout"))
        return "<!DOCTYPE html><html><body>PIVOTED</body></html>"

    orig = _stub(fake)
    try:
        lo = _loop()
        lo.history = ["fix the title"]  # visual_summary already seen -> oscillation -> pivot
        lo.refine_code(_audit(summary="fix the title"), image_path=None, human_feedback=None)
        assert "PIVOTED" in lo.current_code
        assert 900 in timeouts and 600 not in timeouts  # full-file path, diffs skipped
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_blockless_non_document_routes_to_fallback():
    print("  test_blockless_non_document_routes_to_fallback...", end=" ")
    timeouts = []

    def fake(role, prompt, **kw):
        timeouts.append(kw.get("timeout"))
        if len(timeouts) == 1:
            return "Sorry, I cannot help with that."  # no blocks, not a document
        return "<!DOCTYPE html><html><body>REGEN</body></html>"

    orig = _stub(fake)
    try:
        lo = _loop()
        lo.refine_code(_audit(), image_path=None, human_feedback=None)
        assert "REGEN" in lo.current_code
        assert timeouts == [600, 900]  # diff (600) parsed 0 blocks, non-doc -> fallback (900)
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_theme_validation_sentinel_skips_correction():
    print("  test_theme_validation_sentinel_skips_correction...", end=" ")
    updates = {"n": 0}

    def fake(role, prompt, **kw):
        # Final diff call after the validation loop; matches BASE uniquely.
        return "<<<<<<< SEARCH\n<h1>Old Title</h1>\n=======\n<h1>New Title</h1>\n>>>>>>> REPLACE"

    orig = _stub(fake)
    try:
        lo = _loop()

        def counting_update(*a, **k):
            updates["n"] += 1  # counts both the initial token update and any correction

        lo.update_theme = counting_update
        # validate_theme returns the failed-audit sentinel (score 0, "unavailable").
        sentinel = {"overall_score": 0, "bugs": [], "summary": "Automated audit unavailable — eyes provider error"}
        lo.validate_theme = lambda *a, **k: sentinel
        lo.refine_code(_audit(), image_path=None, human_feedback="make it blue")
        # Guard: sentinel is NOT a regression -> no correction update_theme.
        # Only the initial update_theme(human_feedback) ran, so exactly 1 call.
        assert updates["n"] == 1, f"expected 1 update_theme call, got {updates['n']}"
        assert "<h1>New Title</h1>" in lo.current_code  # loop still proceeded to the diff
    finally:
        llm_client.call_llm = orig
    print("✅")


def test_theme_validation_genuine_zero_triggers_correction():
    """Guard is not over-broad: a real score-0 audit (summary without 'unavailable')
    is still a regression and fires a correction — unlike the failed-audit sentinel."""
    print("  test_theme_validation_genuine_zero_triggers_correction...", end=" ")
    updates = {"n": 0}

    def fake(role, prompt, **kw):
        return "<<<<<<< SEARCH\n<h1>Old Title</h1>\n=======\n<h1>New Title</h1>\n>>>>>>> REPLACE"

    orig = _stub(fake)
    try:
        lo = _loop()

        def counting_update(*a, **k):
            updates["n"] += 1  # initial token update + any correction

        lo.update_theme = counting_update
        # Genuine regression: score 0 but NO "unavailable" marker -> not the sentinel.
        regressed = {"overall_score": 0, "bugs": [], "summary": "layout collapsed"}
        lo.validate_theme = lambda *a, **k: regressed
        lo.refine_code(_audit(), image_path=None, human_feedback="make it blue")
        # score_drop = 50 - 0 = 50 > 15 -> correction fires, so more than the 1 initial call.
        assert updates["n"] > 1, f"expected corrections to fire, got {updates['n']}"
        assert "<h1>New Title</h1>" in lo.current_code  # loop still proceeds to the diff
    finally:
        llm_client.call_llm = orig
    print("✅")


if __name__ == "__main__":
    print("\n=== refine diff-routing Tests ===")
    test_clean_diff_applied_no_fallback()
    test_unmatched_block_triggers_fallback()
    test_blockless_document_is_salvaged()
    test_theme_change_feedback_uses_diff()
    test_theme_change_blockless_document_is_salvaged()
    test_theme_validation_sentinel_skips_correction()
    test_theme_validation_genuine_zero_triggers_correction()
    test_diff_call_timeout_routes_to_fallback()
    test_pivot_routes_to_full_file()
    test_blockless_non_document_routes_to_fallback()
    print("\nAll tests passed ✅")
