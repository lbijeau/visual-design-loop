"""Guarded contract tests for capture_reference.js (needs chromium)."""

import http.server
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

SHORT_PAGE = "<html><head><title>t</title></head><body style='margin:0'><h1>Short</h1></body></html>"
TALL_PAGE = "<html><head><title>t</title></head><body style='margin:0'><div style='height:5000px;background:linear-gradient(#fff,#000)'></div></body></html>"


def chromium_available() -> bool:
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(
        ["node", "-e", "require('playwright').chromium.launch().then(b => b.close())"],
        cwd=str(config.PROJECT_DIR),
        capture_output=True,
        timeout=60,
    )
    return probe.returncode == 0


def png_size(path):
    """Width and height straight out of the PNG IHDR — no Pillow needed."""
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"not a PNG: {path}"
    return struct.unpack(">II", head[16:24])


class _Serve:
    """Serve a temp dir over HTTP so the URL path is exercised for real."""

    def __init__(self, files: dict):
        self.dir = tempfile.mkdtemp()
        for name, body in files.items():
            with open(os.path.join(self.dir, name), "w") as f:
                f.write(body)

    def __enter__(self):
        directory = self.dir

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=directory, **kw)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        shutil.rmtree(self.dir, ignore_errors=True)


def _run(source, out):
    return subprocess.run(
        ["node", str(config.PROJECT_DIR / "capture_reference.js"), source, out],
        capture_output=True,
        text=True,
        cwd=str(config.PROJECT_DIR),
        timeout=120,
    )


def test_url_capture_is_1280_wide():
    print("  test_url_capture_is_1280_wide...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    out = os.path.join(tempfile.mkdtemp(), "ref.png")
    with _Serve({"short.html": SHORT_PAGE}) as base:
        result = _run(f"{base}/short.html", out)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    w, h = png_size(out)
    assert w == 1280, f"expected 1280 wide, got {w}"
    assert 0 < h <= 2400, f"height out of range: {h}"
    print("✅")


def test_tall_page_is_clamped():
    print("  test_tall_page_is_clamped...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    out = os.path.join(tempfile.mkdtemp(), "tall.png")
    with _Serve({"tall.html": TALL_PAGE}) as base:
        result = _run(f"{base}/tall.html", out)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    w, h = png_size(out)
    assert (w, h) == (1280, 2400), f"expected a 1280x2400 clamp, got {w}x{h}"
    print("✅")


def test_unreachable_url_exits_nonzero():
    print("  test_unreachable_url_exits_nonzero...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    out = os.path.join(tempfile.mkdtemp(), "nope.png")
    result = _run("http://127.0.0.1:1/nothing", out)
    assert result.returncode != 0, "unreachable URL must fail loudly"
    assert not os.path.exists(out)
    print("✅")


if __name__ == "__main__":
    print("\n=== Capture Reference Tests ===")
    test_url_capture_is_1280_wide()
    test_tall_page_is_clamped()
    test_unreachable_url_exits_nonzero()
    print("\nAll tests passed ✅")
