"""Tests for server status tracking, /status, and /audit-latest endpoints."""

import os
import socket
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests as req

from server import LivePreviewServer


def test_set_phase_and_get_status():
    print("  test_set_phase_and_get_status...", end=" ")
    server = LivePreviewServer()
    server.status.set_max_iterations(5)
    server.status.set_phase("generating-code", "Writing HTML...")
    status = server.status.get_status()
    assert status["phase"] == "generating-code"
    assert status["message"] == "Writing HTML..."
    assert status["max_iterations"] == 5
    assert status["iteration"] == 0
    assert status["feedback_count"] == 0
    print("✅")


def test_increment_feedback():
    print("  test_increment_feedback...", end=" ")
    server = LivePreviewServer()
    server.status.increment_feedback()
    server.status.increment_feedback()
    assert server.status.get_status()["feedback_count"] == 2
    print("✅")


def test_set_iteration():
    print("  test_set_iteration...", end=" ")
    server = LivePreviewServer()
    server.status.set_iteration(3)
    assert server.status.get_status()["iteration"] == 3
    print("✅")


def test_status_endpoint():
    print("  test_status_endpoint...", end=" ")
    server = LivePreviewServer()
    server.status.set_max_iterations(5)
    server.status.set_phase("auditing", "Analyzing screenshot...")
    server.status.set_iteration(2)
    server.start()
    try:
        time.sleep(0.3)
        resp = req.get(f"http://localhost:{server.port}/status", timeout=5)
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"
        data = resp.json()
        assert data["phase"] == "auditing"
        assert data["iteration"] == 2
        assert data["max_iterations"] == 5
        assert data["message"] == "Analyzing screenshot..."
    finally:
        server.stop()
    print("✅")


def test_audit_latest_endpoint():
    print("  test_audit_latest_endpoint...", end=" ")
    server = LivePreviewServer()
    server.status.set_audit({"overall_score": 85, "summary": "Looks good", "bugs": []})
    server.start()
    try:
        time.sleep(0.3)
        resp = req.get(f"http://localhost:{server.port}/audit-latest", timeout=5)
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"
        data = resp.json()
        assert data["overall_score"] == 85
        assert data["summary"] == "Looks good"
    finally:
        server.stop()
    print("✅")


def test_audit_latest_empty():
    print("  test_audit_latest_empty...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        resp = req.get(f"http://localhost:{server.port}/audit-latest", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data == {}
    finally:
        server.stop()
    print("✅")


def test_start_endpoint():
    print("  test_start_endpoint...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/start"
        resp = req.post(url, json={"intent": "A coffee shop landing page"}, timeout=5)
        assert resp.status_code == 200
        assert server.status.intent == "A coffee shop landing page"
        assert server.status.intent_event.is_set()
    finally:
        server.stop()
    print("✅")


def test_start_origin_rejected():
    print("  test_start_origin_rejected...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/start"
        resp = req.post(url, json={"intent": "test"}, headers={"Origin": "http://evil.com"}, timeout=5)
        assert resp.status_code == 403
    finally:
        server.stop()
    print("✅")


def test_start_empty_intent():
    print("  test_start_empty_intent...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/start"
        resp = req.post(url, json={"intent": ""}, timeout=5)
        assert resp.status_code == 400
    finally:
        server.stop()
    print("✅")


def test_submit_feedback_missing_content_length():
    """Regression: missing Content-Length used to raise KeyError -> 500."""
    print("  test_submit_feedback_missing_content_length...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        s.sendall(b"POST /submit-feedback HTTP/1.1\r\nHost: localhost\r\n\r\n")
        first_line = s.recv(4096).decode().split("\r\n")[0]
        s.close()
        assert " 400 " in first_line, f"Expected 400, got: {first_line}"
    finally:
        server.stop()
    print("✅")


def test_submit_feedback_ok():
    print("  test_submit_feedback_ok...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/submit-feedback"
        resp = req.post(url, json={"feedback": "more contrast"}, timeout=5)
        assert resp.status_code == 200
        assert server.status.get_message(timeout=1) == {"type": "feedback", "text": "more contrast"}
    finally:
        server.stop()
    print("✅")


def test_action_endpoint():
    print("  test_action_endpoint...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/action"
        resp = req.post(url, json={"action": "accept"}, timeout=5)
        assert resp.status_code == 200
        assert server.status.get_message(timeout=1) == {"type": "accept"}
        resp = req.post(url, json={"action": "undo"}, timeout=5)
        assert resp.status_code == 200
        assert server.status.get_message(timeout=1) == {"type": "undo"}
    finally:
        server.stop()
    print("✅")


def test_action_unknown_and_bad_json():
    print("  test_action_unknown_and_bad_json...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/action"
        resp = req.post(url, json={"action": "explode"}, timeout=5)
        assert resp.status_code == 400
        assert "Unknown action" in resp.json()["error"]
        resp = req.post(url, data="{not json", headers={"Content-Type": "application/json"}, timeout=5)
        assert resp.status_code == 400
        assert "Invalid JSON" in resp.json()["error"]
    finally:
        server.stop()
    print("✅")


def test_action_and_feedback_origin_rejected():
    print("  test_action_and_feedback_origin_rejected...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        for path, payload in (("/action", {"action": "accept"}), ("/submit-feedback", {"feedback": "x"})):
            resp = req.post(f"http://localhost:{server.port}{path}", json=payload, headers={"Origin": "http://evil.com"}, timeout=5)
            assert resp.status_code == 403, f"{path}: expected 403, got {resp.status_code}"
    finally:
        server.stop()
    print("✅")


def test_port_parameter_and_hunt():
    print("  test_port_parameter_and_hunt...", end=" ")
    import config

    base = config.DEFAULT_PORT + 37
    s1 = LivePreviewServer(port=base)
    s1.start()
    s2 = LivePreviewServer(port=base)  # same base -> must hunt to base+1
    try:
        s2.start()
        assert s1.port == base, f"s1 got {s1.port}"
        assert s2.port == base + 1, f"s2 got {s2.port}"
    finally:
        s1.stop()
        s2.stop()
    print("✅")


if __name__ == "__main__":
    print("\n=== Status & Audit Endpoint Tests ===")
    test_set_phase_and_get_status()
    test_increment_feedback()
    test_set_iteration()
    test_status_endpoint()
    test_audit_latest_endpoint()
    test_audit_latest_empty()
    test_start_endpoint()
    test_start_origin_rejected()
    test_start_empty_intent()
    test_submit_feedback_missing_content_length()
    test_submit_feedback_ok()
    test_action_endpoint()
    test_action_unknown_and_bad_json()
    test_action_and_feedback_origin_rejected()
    test_port_parameter_and_hunt()
    print("\nAll tests passed ✅")
