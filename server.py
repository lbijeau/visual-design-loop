"""Live preview server: serves the design shell, status/audit/feedback endpoints.

All mutable state lives in ``LoopStatus`` (imported from ``status``), so the
server and the design loop communicate through a single shared object.
"""

import copy
import http.server
import json
import logging
import os
import socketserver
import sys
import threading
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import llm_client
from provider_config import BUILTIN_PROVIDERS, ProviderConfig
from status import LoopStatus

logging.getLogger("http.server").setLevel(logging.WARNING)


class LivePreviewServer:
    """HTTP server for the design loop's live preview and HITL endpoints.

    Parameters
    ----------
    directory : str or None
        Static-file root.  Defaults to ``config.PROJECT_DIR``.
    status : LoopStatus or None
        Shared state object.  Defaults to a fresh ``LoopStatus``, exposed as
        ``self.status``.
    """

    def __init__(self, directory: Optional[str] = None, status: Optional[LoopStatus] = None, port: Optional[int] = None):
        self.directory = os.path.abspath(directory or config.PROJECT_DIR)
        self.status = status or LoopStatus()
        self.server = None
        self.server_thread = None
        self.port = port or config.DEFAULT_PORT

    def start(self):  # noqa: C901  (legacy HTTP handler wiring; refactor deferred)
        outer_self = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=outer_self.directory, **kwargs)

            def do_GET(self):
                if self.path == "/status":
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(json.dumps(outer_self.status.get_status()).encode())
                elif self.path == "/audit-latest":
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    audit = outer_self.status.get_audit() or {}
                    self.wfile.write(json.dumps(audit).encode())
                elif self.path == "/providers":
                    origin = self.headers.get("Origin", "")
                    if origin and not origin.startswith(("http://localhost:", "http://127.0.0.1:")):
                        self.send_response(403)
                        self.end_headers()
                        return
                    cfg = ProviderConfig()
                    enriched = copy.deepcopy(cfg.raw)
                    for p in enriched.get("providers", []):
                        p["resolvedModelId"] = cfg.get_model_id(p)
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(json.dumps(enriched).encode())
                else:
                    super().do_GET()

            def do_POST(self):  # noqa: C901  (legacy HTTP handler wiring; refactor deferred)
                # Sensitive endpoints: reject non-localhost origins
                if self.path in ("/save-providers", "/test-provider", "/start", "/submit-feedback", "/action"):
                    origin = self.headers.get("Origin", "")
                    if origin and not origin.startswith(("http://localhost:", "http://127.0.0.1:")):
                        self.send_response(403)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Forbidden"}).encode())
                        return

                if self.path == "/start":
                    content_length = int(self.headers.get("Content-Length", 0))
                    post_data = self.rfile.read(content_length)
                    try:
                        data = json.loads(post_data)
                        intent = data.get("intent", "").strip()
                        if not intent:
                            self.send_response(400)
                            self.send_header("Content-type", "application/json")
                            self.end_headers()
                            self.wfile.write(json.dumps({"error": "Intent required"}).encode())
                            return
                        outer_self.status.set_intent(intent)
                        self.send_response(200)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"status": "ok"}).encode())
                    except Exception:
                        self.send_response(400)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Invalid request"}).encode())

                elif self.path == "/save-providers":
                    content_length = int(self.headers.get("Content-Length", 0))
                    post_data = self.rfile.read(content_length)
                    try:
                        data = json.loads(post_data)
                    except json.JSONDecodeError:
                        self.send_response(400)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Invalid JSON in request body"}).encode())
                        return
                    try:
                        cfg = ProviderConfig()
                        for p in data.get("providers", []):
                            p.pop("resolvedModelId", None)
                            if p.get("type") == "builtin":
                                for builtin in BUILTIN_PROVIDERS:
                                    if builtin["id"] == p["id"]:
                                        p["baseUrl"] = builtin["baseUrl"]
                        cfg.save(data)
                        outer_self.status.signal_provider_config()
                        self.send_response(200)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"status": "ok"}).encode())
                    except OSError:
                        self.send_response(500)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Failed to write provider config to disk"}).encode())
                    except Exception:
                        self.send_response(400)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Invalid provider config"}).encode())

                elif self.path == "/test-provider":
                    content_length = int(self.headers.get("Content-Length", 0))
                    post_data = self.rfile.read(content_length)
                    try:
                        data = json.loads(post_data)
                    except json.JSONDecodeError:
                        self.send_response(400)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(
                            json.dumps({"ok": False, "errorType": "bad-request", "message": "Invalid JSON in request body"}).encode()
                        )
                        return
                    provider = data.get("provider", {})
                    result = llm_client.probe_provider(provider, timeout=10)
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(result).encode())

                elif self.path == "/action":
                    content_length = int(self.headers.get("Content-Length", 0))
                    post_data = self.rfile.read(content_length)
                    try:
                        data = json.loads(post_data)
                    except json.JSONDecodeError:
                        self.send_response(400)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Invalid JSON in request body"}).encode())
                        return
                    action = data.get("action", "")
                    if action not in ("accept", "undo", "save"):
                        self.send_response(400)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Unknown action"}).encode())
                        return
                    outer_self.status.put_action(action)
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok"}).encode())

                elif self.path == "/submit-feedback":
                    content_length = int(self.headers.get("Content-Length", 0))
                    if not content_length:
                        self.send_response(400)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Missing Content-Length"}).encode())
                        return
                    post_data = self.rfile.read(content_length)
                    try:
                        data = json.loads(post_data)
                        feedback = data.get("feedback", "")
                        outer_self.status.put_feedback(feedback)
                        self.send_response(200)
                        self.send_header("Content-type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"status": "ok"}).encode())
                    except Exception as e:
                        self.send_error(400, str(e))
                else:
                    super().do_POST()

        base_port = self.port
        max_port = base_port + 100
        while self.port <= max_port:
            try:
                self.server = socketserver.TCPServer(("127.0.0.1", self.port), Handler)
                break
            except OSError:
                self.port += 1
        if self.server is None:
            raise OSError(f"No free port found in range {base_port}-{max_port}")

        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        print(f"🚀 Live Preview Server started at http://localhost:{self.port}/design_shell.html")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
