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
    print("\nAll tests passed ✅")
