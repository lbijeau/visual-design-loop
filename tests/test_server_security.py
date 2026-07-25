"""Security tests: loopback binding, origin validation, file permissions."""

import os
import sys
import time

import requests as req

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import provider_config
from provider_config import ProviderConfig
from server import LivePreviewServer


def test_server_binds_loopback():
    """Server must bind to 127.0.0.1, not 0.0.0.0."""
    print("  test_server_binds_loopback...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        sock = server.server.socket
        addr = sock.getsockname()
        assert addr[0] == "127.0.0.1", f"Expected 127.0.0.1, got {addr[0]}"
        print("✅")
    finally:
        server.stop()


def test_get_providers_origin_rejected():
    """GET /providers from non-localhost origin returns 403."""
    print("  test_get_providers_origin_rejected...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://127.0.0.1:{server.port}/providers"
        # With wrong Origin
        resp = req.get(url, headers={"Origin": "http://evil.com"}, timeout=5)
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"
        # With correct Origin
        resp = req.get(url, headers={"Origin": f"http://127.0.0.1:{server.port}"}, timeout=5)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert "Cache-Control" in resp.headers
        assert resp.headers["Cache-Control"] == "no-store"
        print("✅")
    finally:
        server.stop()


def test_save_providers_writes_0600():
    """POST /save-providers writes providers.json with mode 0600."""
    print("  test_save_providers_writes_0600...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        cfg = ProviderConfig.default_config()
        url = f"http://localhost:{server.port}/save-providers"
        resp = req.post(url, json=cfg, headers={"Origin": f"http://localhost:{server.port}"}, timeout=5)
        assert resp.status_code == 200
        # Resolve the path the app actually writes to: under pytest the conftest
        # isolation fixture redirects this to a tmp file; under the __main__
        # runner it is the real providers.json, as before.
        config_path = provider_config._config_path()
        assert os.path.exists(config_path)
        mode = os.stat(config_path).st_mode & 0o777
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"
        print("✅")
    finally:
        server.stop()


def test_post_save_origin_rejected():
    """POST /save-providers from non-localhost origin returns 403."""
    print("  test_post_save_origin_rejected...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        cfg = ProviderConfig.default_config()
        url = f"http://127.0.0.1:{server.port}/save-providers"
        resp = req.post(url, json=cfg, headers={"Origin": "http://evil.com"}, timeout=5)
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"
        print("✅")
    finally:
        server.stop()


def test_test_provider_no_key_leak():
    """POST /test-provider response doesn't contain API keys."""
    print("  test_test_provider_no_key_leak...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/test-provider"
        payload = {
            "provider": {
                "id": "openai",
                "baseUrl": "https://api.openai.com",
                "apiKey": "sk-super-secret-key-12345678",
            }
        }
        resp = req.post(url, json=payload, headers={"Origin": f"http://localhost:{server.port}"}, timeout=15)
        assert resp.status_code == 200
        body = resp.text
        assert "sk-super-secret-key" not in body, "API key leaked in response!"
        print("✅")
    finally:
        server.stop()


def test_builtin_baseurl_immutable():
    """Saving config cannot change builtin provider base URLs."""
    print("  test_builtin_baseurl_immutable...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        cfg = ProviderConfig.default_config()
        # Try to tamper with Ollama base URL
        for p in cfg["providers"]:
            if p["id"] == "ollama":
                p["baseUrl"] = "http://evil.com:9999"
                break
        url = f"http://localhost:{server.port}/save-providers"
        req.post(url, json=cfg, headers={"Origin": f"http://localhost:{server.port}"}, timeout=5)
        # Read back
        resp = req.get(f"http://localhost:{server.port}/providers", timeout=5)
        data = resp.json()
        ollama = next(p for p in data["providers"] if p["id"] == "ollama")
        assert ollama["baseUrl"] == "http://localhost:11434", f"Base URL was tampered: {ollama['baseUrl']}"
        print("✅")
    finally:
        server.stop()


def test_save_providers_invalid_json_message():
    """Regression: malformed body used to report 'provider unreachable'."""
    print("  test_save_providers_invalid_json_message...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/save-providers"
        resp = req.post(
            url, data="{not json", headers={"Origin": f"http://localhost:{server.port}", "Content-Type": "application/json"}, timeout=5
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
        assert "Invalid JSON" in resp.json()["error"], f"got: {resp.text}"
        print("✅")
    finally:
        server.stop()


def test_test_provider_invalid_json_message():
    """Malformed /test-provider body gets a bad-request error, not 'provider unreachable'."""
    print("  test_test_provider_invalid_json_message...", end=" ")
    server = LivePreviewServer()
    server.start()
    try:
        time.sleep(0.3)
        url = f"http://localhost:{server.port}/test-provider"
        resp = req.post(
            url, data="{not json", headers={"Origin": f"http://localhost:{server.port}", "Content-Type": "application/json"}, timeout=5
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
        body = resp.json()
        assert body["ok"] is False
        assert "Invalid JSON" in body["message"], f"got: {resp.text}"
        print("✅")
    finally:
        server.stop()


if __name__ == "__main__":
    print("\n=== Server Security Tests ===")
    test_server_binds_loopback()
    test_get_providers_origin_rejected()
    test_post_save_origin_rejected()
    test_save_providers_writes_0600()
    test_test_provider_no_key_leak()
    test_builtin_baseurl_immutable()
    test_save_providers_invalid_json_message()
    test_test_provider_invalid_json_message()
    print("\nAll security tests passed ✅")
