"""Tests for export: fetch guard, asset inlining, HTML surgery (offline);
harvest + end-to-end fidelity (guarded, needs chromium + network)."""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import export

THEME = {"tailwind_config": {"theme": {"extend": {"colors": {"accent": "#059669"}}}}}


class FakeResponse:
    def __init__(self, content=b"", status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    @property
    def text(self):
        return self.content.decode("utf-8")


def make_fetch(mapping):
    """fetch stub: url -> bytes (or FakeResponse); missing url -> 404."""

    def fetch(url, timeout=10):
        if url in mapping:
            value = mapping[url]
            return value if isinstance(value, FakeResponse) else FakeResponse(value)
        return FakeResponse(b"not found", status_code=404)

    return fetch


def test_inject_config_three_branches():
    print("  test_inject_config_three_branches...", end=" ")
    with_head = "<html><head><title>t</title></head><body>x</body></html>"
    out = export._inject_config(with_head, THEME)
    assert "tailwind.config" in out and out.index("tailwind.config") < out.index("</head>")
    no_head = "<html><body>x</body></html>"
    out = export._inject_config(no_head, THEME)
    assert out.index("tailwind.config") < out.index("<body>")
    bare = "<div>x</div>"
    out = export._inject_config(bare, THEME)
    assert out.strip().startswith("\n".join([]) or "<script>") or out.index("tailwind.config") < out.index("<div>")
    print("✅")


def test_is_fetch_allowed():
    print("  test_is_fetch_allowed...", end=" ")
    assert export._is_fetch_allowed("https://fonts.gstatic.com/s/x.woff2") is True
    assert export._is_fetch_allowed("http://fonts.gstatic.com/x.woff2") is False  # https only
    assert export._is_fetch_allowed("https://127.0.0.1/steal") is False
    assert export._is_fetch_allowed("https://10.0.0.5/internal.css") is False
    assert export._is_fetch_allowed("https://192.168.1.1/x") is False
    assert export._is_fetch_allowed("https://169.254.169.254/meta") is False
    assert export._is_fetch_allowed("https://[::1]/x") is False
    assert export._is_fetch_allowed("https://localhost/x") is False
    assert export._is_fetch_allowed("file:///etc/passwd") is False
    print("✅")


def test_fetch_stylesheet_guard_and_failure():
    print("  test_fetch_stylesheet_guard_and_failure...", end=" ")
    fetch = make_fetch({"https://fonts.example.com/a.css": b"body{}"})
    assert export._fetch_stylesheet("https://fonts.example.com/a.css", fetch=fetch) == "body{}"
    assert export._fetch_stylesheet("https://127.0.0.1/a.css", fetch=fetch) is None  # guarded
    assert export._fetch_stylesheet("https://fonts.example.com/missing.css", fetch=fetch) is None  # 404
    print("✅")


def test_inline_stylesheet_assets():
    print("  test_inline_stylesheet_assets...", end=" ")
    font = b"\x00\x01fontbytes"
    fetch = make_fetch(
        {
            "https://fonts.example.com/dir/rel.woff2": font,
            "https://cdn.example.com/abs.png": b"pngbytes",
        }
    )
    css = (
        "@font-face{src:url(./rel.woff2) format('woff2');}"
        ".x{background:url('https://cdn.example.com/abs.png');}"
        ".y{background:url(data:image/gif;base64,R0lGOD);}"
        ".z{src:url(https://fonts.example.com/dir/missing.woff2);}"
    )
    out = export._inline_stylesheet_assets(css, "https://fonts.example.com/dir/style.css", fetch=fetch)
    b64font = base64.b64encode(font).decode()
    assert f"url(data:font/woff2;base64,{b64font})" in out
    assert "data:image/png;base64," in out
    assert "url(data:image/gif;base64,R0lGOD)" in out  # pre-existing data URI untouched
    assert "https://fonts.example.com/dir/missing.woff2" in out  # failed ref left remote
    print("✅")


def test_import_one_level():
    print("  test_import_one_level...", end=" ")
    fetch = make_fetch(
        {
            "https://fonts.example.com/import.css": b".imported{color:red;url-marker:url(f.woff)}",
            "https://fonts.example.com/f.woff": b"fontdata",
        }
    )
    css = "@import url('https://fonts.example.com/import.css');\n.local{}"
    out = export._inline_stylesheet_assets(css, "https://fonts.example.com/base.css", fetch=fetch)
    assert ".imported{color:red" in out
    assert "@import" not in out
    assert "data:font/woff;base64," in out  # url() inside the imported sheet also embedded
    assert ".local{}" in out
    print("✅")


def test_remove_cdn_script():
    print("  test_remove_cdn_script...", end=" ")
    variants = [
        '<script src="https://cdn.tailwindcss.com"></script>',
        "<script src='https://cdn.tailwindcss.com'></script>",
        '<script defer src="https://cdn.tailwindcss.com?plugins=forms"></script>',
    ]
    for v in variants:
        code = f"<html><head>{v}<script>keep()</script></head><body></body></html>"
        out = export._remove_cdn_script(code)
        assert "cdn.tailwindcss.com" not in out, f"failed on: {v}"
        assert "keep()" in out
    print("✅")


def test_remove_links_including_escaped():
    print("  test_remove_links_including_escaped...", end=" ")
    href = "https://fonts.googleapis.com/css2?family=Inter&display=swap"
    escaped = href.replace("&", "&amp;")
    code = (
        f'<html><head><link rel="stylesheet" href="{escaped}">'
        f'<link href="https://keep.example.com/k.css" rel="stylesheet"></head><body></body></html>'
    )
    out = export._remove_links(code, [href])
    assert "fonts.googleapis.com" not in out
    assert "keep.example.com" in out  # not in the inlined list -> stays
    print("✅")


def test_browser_ua_and_content_type_mime():
    print("  test_browser_ua_and_content_type_mime...", end=" ")
    # Default fetch must send a browser UA (Google Fonts serves woff2 CSS only
    # to recognized browsers) — assert on the wrapper, no network needed.
    seen = {}

    class _Grab:
        def get(self, url, timeout=10, headers=None):
            seen.update(headers or {})
            return FakeResponse(b"x")

    original = export.requests
    export.requests = _Grab()
    try:
        export._default_fetch("https://fonts.example.com/a.css")
        assert "Mozilla/5.0" in seen.get("User-Agent", ""), f"UA sent: {seen}"
    finally:
        export.requests = original

    # Extensionless asset URL -> MIME from the response Content-Type header.
    fetch = make_fetch(
        {
            "https://fonts.example.com/nofile": FakeResponse(b"fontdata", headers={"Content-Type": "font/woff2"}),
        }
    )
    out = export._inline_stylesheet_assets(".a{src:url(https://fonts.example.com/nofile);}", "https://fonts.example.com/s.css", fetch=fetch)
    assert "data:font/woff2;base64," in out
    print("✅")


def test_insert_style():
    print("  test_insert_style...", end=" ")
    code = "<html><head><title>t</title></head><body></body></html>"
    out = export._insert_style(code, ".a{color:red}")
    assert "<style data-stylesentry-export>" in out
    assert out.index("data-stylesentry-export") < out.index("</head>")
    bare = "<div>x</div>"
    out = export._insert_style(bare, ".a{}")
    assert out.index("data-stylesentry-export") < out.index("<div>")
    print("✅")


def chromium_and_network_available() -> bool:
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(
        [
            "node",
            "-e",
            "const{chromium}=require('playwright');(async()=>{const b=await chromium.launch();"
            "const p=await b.newPage();"
            "const r=await p.goto('https://cdn.tailwindcss.com').catch(()=>null);"
            "await b.close();process.exit(r&&r.ok()?0:3);})()",
        ],
        cwd=str(config.PROJECT_DIR),
        capture_output=True,
        timeout=60,
    )
    return probe.returncode == 0


def test_extract_harvests_cdn_css_only():
    print("  test_extract_harvests_cdn_css_only...", end=" ")
    if not chromium_and_network_available():
        print("SKIPPED (chromium or network unavailable)")
        return
    page = (
        "<html><head>"
        "<style>.author-marker{color:pink}</style>"
        '<script src="https://cdn.tailwindcss.com"></script>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter&display=swap">'
        '</head><body><div class="bg-emerald-600 p-4">hi</div></body></html>'
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", dir=str(config.PROJECT_DIR))
    tmp.write(page)
    tmp.close()
    try:
        result = subprocess.run(
            ["node", str(config.PROJECT_DIR / "export_extract.js"), tmp.name],
            capture_output=True,
            text=True,
            cwd=str(config.PROJECT_DIR),
            timeout=90,
        )
        assert result.returncode == 0, f"extract failed: {result.stderr}"
        data = json.loads(result.stdout.strip().splitlines()[-1])
        assert "--tw-" in data["tailwindCss"]
        assert "bg-emerald-600" in data["tailwindCss"]
        assert "author-marker" not in data["tailwindCss"]
        hrefs = data["stylesheetHrefs"]
        assert len(hrefs) == 1
        assert hrefs[0]["url"].startswith("https://fonts.googleapis.com/")
        assert "family=Inter" in hrefs[0]["attr"]
    finally:
        os.remove(tmp.name)
    print("✅")


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_build_standalone_harvest_failures():
    print("  test_build_standalone_harvest_failures...", end=" ")
    original = export.subprocess.run
    cases = [
        FakeProc(returncode=2, stderr="No compiled Tailwind CSS found"),
        FakeProc(returncode=0, stdout="this is not json"),
        FakeProc(returncode=0, stdout=json.dumps({"tailwindCss": "  ", "stylesheetHrefs": []})),
    ]
    try:
        for proc in cases:
            export.subprocess.run = lambda *a, **k: proc
            try:
                export.build_standalone("<html><head></head><body></body></html>", THEME)
                assert False, f"Should have raised ExportError for {proc.__dict__}"
            except export.ExportError:
                pass
    finally:
        export.subprocess.run = original
    print("✅")


def test_build_standalone_assembly_with_stubbed_harvest():
    print("  test_build_standalone_assembly_with_stubbed_harvest...", end=" ")
    href_attr = "https://fonts.googleapis.com/css2?family=Inter&display=swap"
    harvest = {"tailwindCss": ".p-4{padding:1rem}/*--tw-*/", "stylesheetHrefs": [{"attr": href_attr, "url": href_attr}]}
    code = (
        "<html><head>"
        '<script src="https://cdn.tailwindcss.com"></script>'
        f'<link rel="stylesheet" href="{href_attr.replace("&", "&amp;")}">'
        "<script>keepMe()</script>"
        '</head><body><div class="p-4">hi</div></body></html>'
    )

    original_run = export.subprocess.run
    original_fetch = export._default_fetch
    export.subprocess.run = lambda *a, **k: FakeProc(returncode=0, stdout=json.dumps(harvest))
    export._default_fetch = make_fetch(
        {
            href_attr: b"@font-face{src:url(f.woff2)}",
            "https://fonts.googleapis.com/f.woff2": b"F",
        }
    )
    try:
        out = export.build_standalone(code, THEME)
    finally:
        export.subprocess.run = original_run
        export._default_fetch = original_fetch

    assert "cdn.tailwindcss.com" not in out
    assert "fonts.googleapis.com/css2" not in out  # link removed (escaped form)
    assert "data-stylesentry-export" in out
    assert ".p-4{padding:1rem}" in out
    assert "data:font/woff2;base64," in out
    assert "keepMe()" in out  # other scripts untouched
    assert "tailwind.config" not in out  # no config script in output
    print("✅")


def test_end_to_end_offline_fidelity():
    print("  test_end_to_end_offline_fidelity...", end=" ")
    if not chromium_and_network_available():
        print("SKIPPED (chromium or network unavailable)")
        return
    code = (
        "<html><head>"
        '<script src="https://cdn.tailwindcss.com"></script>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter&display=swap">'
        '</head><body><div id="probe" class="bg-emerald-600 p-4">hi</div></body></html>'
    )
    out = export.build_standalone(code, THEME)
    assert "cdn.tailwindcss.com" not in out
    assert "fonts.googleapis.com" not in out or "data:" in out
    assert "bg-emerald-600" in out
    assert "data:font/woff2;base64," in out

    # Render it with ALL network blocked; the emerald background must survive.
    out_path = config.PROJECT_DIR / "run_state" / "export_e2e.html"
    os.makedirs(out_path.parent, exist_ok=True)
    with open(out_path, "w") as f:
        f.write(out)
    node_script = """
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext();
  await context.route('**/*', route =>
    route.request().url().startsWith('file://') ? route.continue() : route.abort());
  const page = await context.newPage();
  await page.goto(`file://${process.argv[2]}`);
  await page.waitForTimeout(500);
  const bg = await page.$eval('#probe', el => getComputedStyle(el).backgroundColor);
  console.log(JSON.stringify({ bg }));
  await browser.close();
})();
"""
    runner = tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode="w", dir=str(config.PROJECT_DIR))
    runner.write(node_script)
    runner.close()
    try:
        result = subprocess.run(
            ["node", runner.name, str(out_path)], capture_output=True, text=True, cwd=str(config.PROJECT_DIR), timeout=60
        )
        assert result.returncode == 0, f"render failed: {result.stderr}"
        bg = json.loads(result.stdout.strip().splitlines()[-1])["bg"]
        assert bg == "rgb(5, 150, 105)", f"emerald-600 lost offline: got {bg}"
    finally:
        os.remove(runner.name)
        os.remove(out_path)
    print("✅")


if __name__ == "__main__":
    print("\n=== Export Tests ===")
    test_inject_config_three_branches()
    test_is_fetch_allowed()
    test_fetch_stylesheet_guard_and_failure()
    test_inline_stylesheet_assets()
    test_import_one_level()
    test_remove_cdn_script()
    test_remove_links_including_escaped()
    test_browser_ua_and_content_type_mime()
    test_insert_style()
    test_extract_harvests_cdn_css_only()
    test_build_standalone_harvest_failures()
    test_build_standalone_assembly_with_stubbed_harvest()
    test_end_to_end_offline_fidelity()
    print("\nAll tests passed ✅")
