"""Tests for LoopStatus: status dict, audit under lock, feedback queue, events."""

import os
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from status import LoopStatus


def test_status_dict():
    print("  test_status_dict...", end=" ")
    s = LoopStatus()
    assert s.get_status() == {
        "phase": "wizard",
        "iteration": 0,
        "max_iterations": 5,
        "feedback_count": 0,
        "message": "",
        "model_label": "",
        "error": "",
        "can_undo": False,
    }
    s.set_phase("auditing", "Analyzing...")
    s.set_iteration(2)
    s.set_max_iterations(7)
    s.set_model_label("ollama/gemma4:9b")
    s.increment_feedback()
    st = s.get_status()
    assert st["phase"] == "auditing"
    assert st["message"] == "Analyzing..."
    assert st["iteration"] == 2
    assert st["max_iterations"] == 7
    assert st["model_label"] == "ollama/gemma4:9b"
    assert st["feedback_count"] == 1
    print("✅")


def test_audit_roundtrip_and_concurrency():
    print("  test_audit_roundtrip_and_concurrency...", end=" ")
    s = LoopStatus()
    assert s.get_audit() is None
    s.set_audit({"overall_score": 85})
    assert s.get_audit() == {"overall_score": 85}

    # Hammer from a writer thread while reading — must not raise or corrupt
    def writer():
        for i in range(500):
            s.set_audit({"overall_score": i})

    t = threading.Thread(target=writer)
    t.start()
    for _ in range(500):
        audit = s.get_audit()
        assert isinstance(audit, dict)
    t.join()
    assert s.get_audit() == {"overall_score": 499}
    print("✅")


def test_feedback_queue():
    print("  test_feedback_queue...", end=" ")
    s = LoopStatus()
    s.put_feedback("make it blue")
    assert s.get_message(timeout=1) == {"type": "feedback", "text": "make it blue"}
    assert s.get_message(timeout=0.01) is None
    print("✅")


def test_actions():
    print("  test_actions...", end=" ")
    s = LoopStatus()
    s.put_action("accept")
    s.put_action("undo")
    assert s.get_message(timeout=1) == {"type": "accept"}
    assert s.get_message(timeout=1) == {"type": "undo"}
    try:
        s.put_action("explode")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    print("✅")


def test_error_and_can_undo():
    print("  test_error_and_can_undo...", end=" ")
    s = LoopStatus()
    s.set_error("LLM exploded")
    assert s.get_status()["error"] == "LLM exploded"
    s.set_phase("rendering", "next")  # any phase change clears the error
    assert s.get_status()["error"] == ""
    s.set_can_undo(True)
    assert s.get_status()["can_undo"] is True
    print("✅")


def test_intent_and_provider_events():
    print("  test_intent_and_provider_events...", end=" ")
    s = LoopStatus()
    assert s.await_intent(timeout=0.01) is False
    s.set_intent("a coffee shop page")
    assert s.intent == "a coffee shop page"
    assert s.await_intent(timeout=0.01) is True
    assert s.await_provider_config(timeout=0.01) is False
    s.signal_provider_config()
    assert s.await_provider_config(timeout=0.01) is True
    print("✅")


if __name__ == "__main__":
    print("\n=== LoopStatus Tests ===")
    test_status_dict()
    test_audit_roundtrip_and_concurrency()
    test_feedback_queue()
    test_actions()
    test_error_and_can_undo()
    test_intent_and_provider_events()
    print("\nAll tests passed ✅")
