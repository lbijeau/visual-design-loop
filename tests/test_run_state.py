"""Tests for RunState: snapshots, run.json, unfinished detection, corruption."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_state import RunState


def _fresh():
    return RunState(Path(tempfile.mkdtemp()) / "run_state")


def test_no_state():
    print("  test_no_state...", end=" ")
    rs = _fresh()
    assert rs.has_unfinished() is False
    assert rs.latest_iteration() == 0
    rs.clear()  # clearing nothing must not raise
    print("✅")


def test_save_and_load_run():
    print("  test_save_and_load_run...", end=" ")
    rs = _fresh()
    meta = {
        "intent": "a blog",
        "iteration": 2,
        "max_iterations": 5,
        "undo_pointer": 2,
        "theme_json": {"soft_tokens": {}},
        "phase": "awaiting-feedback",
        "finished": False,
    }
    rs.save_run(meta)
    assert rs.has_unfinished() is True
    assert rs.load_run() == meta
    meta["finished"] = True
    rs.save_run(meta)
    assert rs.has_unfinished() is False
    print("✅")


def test_snapshot_roundtrip_and_latest():
    print("  test_snapshot_roundtrip_and_latest...", end=" ")
    rs = _fresh()
    theme = {"tailwind_config": {"theme": {}}}
    audit = {"overall_score": 70, "bugs": [], "summary": "ok"}
    rs.snapshot(1, "<html>v1</html>", theme, audit)
    rs.snapshot(2, "<html>v2</html>", theme, audit)
    assert rs.latest_iteration() == 2
    code, t, a = rs.load_snapshot(1)
    assert code == "<html>v1</html>"
    assert t == theme
    assert a == audit
    print("✅")


def test_corrupt_run_json():
    print("  test_corrupt_run_json...", end=" ")
    rs = _fresh()
    os.makedirs(rs.state_dir, exist_ok=True)
    with open(rs.run_path, "w") as f:
        f.write("{not json")
    assert rs.has_unfinished() is False  # corrupt state degrades, never raises
    print("✅")


def test_clear():
    print("  test_clear...", end=" ")
    rs = _fresh()
    rs.save_run({"intent": "x", "finished": False})
    rs.snapshot(1, "<html></html>", {}, {})
    rs.clear()
    assert rs.has_unfinished() is False
    assert rs.latest_iteration() == 0
    print("✅")


def test_events_roundtrip():
    print("  test_events_roundtrip...", end=" ")
    rs = _fresh()
    rs.append_event({"event": "run_started", "intent": "x"})
    rs.append_event({"event": "iteration_produced", "n": 1})
    events = rs.read_events()
    assert len(events) == 2
    assert events[0]["event"] == "run_started"
    assert isinstance(events[0]["ts"], float)
    assert events[1]["n"] == 1
    print("✅")


def test_events_corrupt_line_skipped():
    print("  test_events_corrupt_line_skipped...", end=" ")
    rs = _fresh()
    rs.append_event({"event": "a"})
    with open(rs.events_path, "a") as f:
        f.write("{torn line\n")
    rs.append_event({"event": "b"})
    assert [e["event"] for e in rs.read_events()] == ["a", "b"]
    print("✅")


def test_events_missing_file():
    print("  test_events_missing_file...", end=" ")
    rs = _fresh()
    assert rs.read_events() == []
    rs.save_run({"intent": "x", "finished": False})  # state exists, no events yet
    assert rs.read_events() == []
    print("✅")


def test_reference_survives_repeated_save():
    print("  test_reference_survives_repeated_save...", end=" ")
    from loop import FrontendDesignLoop

    loop = FrontendDesignLoop("test intent", reference="https://example.com/x")
    loop.reference_png = "/tmp/reference_1.png"
    meta = loop._run_meta()
    # run.json is rewritten wholesale from _run_meta() at six call sites, so a
    # one-off save_run would be erased by the next one.
    assert meta["reference"] == "https://example.com/x"
    assert meta["reference_png"] == "/tmp/reference_1.png"
    print("✅")


def test_relative_reference_resolves_against_the_shell_cwd():
    """The capture subprocess runs with cwd=PROJECT_DIR, so a relative --reference
    must be made absolute first or it resolves against the project instead."""
    print("  test_relative_reference_resolves_against_the_shell_cwd...", end=" ")
    import subprocess

    import config
    import loop as loop_mod
    from loop import FrontendDesignLoop

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "ref.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")  # existence is all _acquire_reference checks

    seen = {}

    def fake_run(argv, **kwargs):
        seen["source"] = argv[2]
        with open(argv[3], "wb") as out:
            out.write(b"\x89PNG\r\n\x1a\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    cwd = os.getcwd()
    original = loop_mod.subprocess.run
    loop_mod.subprocess.run = fake_run
    try:
        os.chdir(d)
        FrontendDesignLoop("x", reference="ref.png")._acquire_reference()
    finally:
        loop_mod.subprocess.run = original
        os.chdir(cwd)

    assert os.path.isabs(seen["source"]), seen
    assert not seen["source"].startswith(str(config.PROJECT_DIR)), f"resolved against the project: {seen['source']}"
    assert os.path.exists(seen["source"])
    print("✅")


def test_missing_reference_file_fails_before_launching_a_browser():
    print("  test_missing_reference_file_fails_before_launching_a_browser...", end=" ")
    import loop as loop_mod
    from loop import FrontendDesignLoop

    def forbidden(*a, **kw):
        raise AssertionError("must not spend a browser launch on a path that does not exist")

    original = loop_mod.subprocess.run
    loop_mod.subprocess.run = forbidden
    try:
        FrontendDesignLoop("x", reference="/nowhere/at/all.png")._acquire_reference()
        assert False, "a missing reference file must raise"
    except Exception as e:
        assert "not found" in str(e), e
    finally:
        loop_mod.subprocess.run = original
    print("✅")


if __name__ == "__main__":
    print("\n=== RunState Tests ===")
    test_no_state()
    test_save_and_load_run()
    test_snapshot_roundtrip_and_latest()
    test_corrupt_run_json()
    test_clear()
    test_events_roundtrip()
    test_events_corrupt_line_skipped()
    test_events_missing_file()
    test_reference_survives_repeated_save()
    test_relative_reference_resolves_against_the_shell_cwd()
    test_missing_reference_file_fails_before_launching_a_browser()
    print("\nAll tests passed ✅")
