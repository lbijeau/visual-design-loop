"""Tests for CLI parsing and the resume decision."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orchestrator import decide_resume, parse_args
from run_state import RunState


def test_parse_defaults():
    print("  test_parse_defaults...", end=" ")
    args = parse_args([])
    assert args.intent is None
    assert args.max_iterations == config.MAX_ITERATIONS
    assert args.port == config.DEFAULT_PORT
    assert args.resume is False and args.fresh is False
    args = parse_args(["a coffee shop", "--max-iterations", "9", "--port", "9123", "--resume"])
    assert args.intent == "a coffee shop"
    assert args.max_iterations == 9
    assert args.port == 9123
    assert args.resume is True
    print("✅")


def test_resume_fresh_mutually_exclusive():
    print("  test_resume_fresh_mutually_exclusive...", end=" ")
    try:
        parse_args(["--resume", "--fresh"])
        assert False, "Should have raised SystemExit"
    except SystemExit:
        pass
    print("✅")


def test_reference_parsing_and_resume_conflict():
    print("  test_reference_parsing_and_resume_conflict...", end=" ")
    assert parse_args([]).reference is None
    assert parse_args(["--reference", "shot.png"]).reference == "shot.png"
    assert parse_args(["--reference", "https://example.com"]).reference == "https://example.com"
    try:
        parse_args(["--reference", "shot.png", "--resume"])
        assert False, "Should have raised SystemExit"
    except SystemExit:
        pass
    print("✅")


def test_reconfigure_flag():
    print("  test_reconfigure_flag...", end=" ")
    assert parse_args([]).reconfigure is False
    assert parse_args(["--reconfigure"]).reconfigure is True
    print("✅")


def test_reference_skips_resume_prompt():
    print("  test_reference_skips_resume_prompt...", end=" ")
    rs = RunState(Path(tempfile.mkdtemp()) / "run_state")
    rs.save_run({"intent": "seeded", "iteration": 2, "max_iterations": 5, "finished": False})

    def refuse(_q):
        raise AssertionError("the resume prompt must not appear when --reference is given")

    assert decide_resume(parse_args(["--reference", "https://example.com"]), rs, ask=refuse) is False
    # The unfinished state is left intact — only --fresh clears it.
    assert rs.has_unfinished() is True
    print("✅")


def test_decide_resume():
    print("  test_decide_resume...", end=" ")
    rs = RunState(Path(tempfile.mkdtemp()) / "run_state")

    # No state, no flags -> fresh
    assert decide_resume(parse_args([]), rs) is False
    # --resume with no state -> fresh (with a note)
    assert decide_resume(parse_args(["--resume"]), rs) is False

    rs.save_run({"intent": "seeded", "iteration": 2, "max_iterations": 5, "finished": False})
    # --resume with state -> resume, no prompt
    assert decide_resume(parse_args(["--resume"]), rs) is True
    # --fresh clears the state
    assert decide_resume(parse_args(["--fresh"]), rs) is False
    assert rs.has_unfinished() is False

    rs.save_run({"intent": "seeded", "iteration": 2, "max_iterations": 5, "finished": False})
    # No flags + state -> prompt decides
    assert decide_resume(parse_args([]), rs, ask=lambda q: "y") is True
    assert decide_resume(parse_args([]), rs, ask=lambda q: "") is True  # default yes
    assert decide_resume(parse_args([]), rs, ask=lambda q: "n") is False
    print("✅")


if __name__ == "__main__":
    print("\n=== CLI Tests ===")
    test_parse_defaults()
    test_resume_fresh_mutually_exclusive()
    test_decide_resume()
    test_reference_parsing_and_resume_conflict()
    test_reconfigure_flag()
    test_reference_skips_resume_prompt()
    print("\nAll tests passed ✅")
