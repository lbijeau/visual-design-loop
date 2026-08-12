# Image-Seeded Design Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--reference <path|url>` so a design run is seeded from a screenshot or a live page — its palette feeds `theme.json` and its layout feeds the first draft — instead of being invented from the text intent alone.

**Architecture:** One new Node script normalizes either input kind into a single bounded PNG. A new preflight check proves the brain model can actually see before anything is generated. The PNG is then attached to the two existing seed calls (`generate_theme`, `generate_initial_code`) using the image slot `llm_client` already supports, and dropped afterwards — the audit and refine loop are untouched.

**Tech Stack:** Python 3 (stdlib only — argparse, subprocess, zlib, struct), Playwright/Chromium via Node, pytest.

**Spec:** `docs/superpowers/specs/2026-08-11-image-seeded-runs-design.md`

## Global Constraints

- **No new Python dependencies.** No Pillow. Image work happens in Chromium; PNG inspection in tests uses `struct` on the IHDR header.
- **Reference-less runs must be byte-identical to today.** Every prompt addition is conditional on a reference being present. This is directly asserted by tests.
- **A reference that was asked for and cannot be used aborts the run.** Never warn-and-continue, at acquisition *or* consumption.
- **A vision-capable brain is required.** No transcription fallback, no text-only degradation path.
- **CLI only.** Do not touch `server.py`, `/start`, or `design_shell.html`.
- **`_build_messages` keeps its single image slot.** No multi-image calls.
- Reference image width: **1280px**. Max height: **2400px**. Probe answer: **`738`**.
- Test style in this repo: each test prints `"  test_name..."` then `"✅"`, and is registered in the file's `__main__` block. Match it.

---

### Task 1: `--reference` CLI argument and its resume interaction

Seeding only ever runs in the fresh branch of `run()`, and `_restore_run` refuses to resume unless a completed iteration exists (`loop.py:1262`) — which is only true long after seeding. So a resumed run can never consume a reference, and the CLI must say so rather than accept the flag and ignore it.

**Files:**
- Modify: `orchestrator.py:18-26` (`parse_args`), `orchestrator.py:29-50` (`decide_resume`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_args(argv).reference` — `str | None`, the raw path-or-URL. `decide_resume(args, run_state, ask=input) -> bool` keeps its signature and returns `False` whenever `args.reference` is set.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
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
```

Register both in the `__main__` block:

```python
    test_reference_parsing_and_resume_conflict()
    test_reference_skips_resume_prompt()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_cli.py -q`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'reference'`

- [ ] **Step 3: Add the argument and the guards**

In `orchestrator.py`, replace the body of `parse_args`:

```python
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="StyleSentry design loop")
    p.add_argument("intent", nargs="?", default=None, help="design intent (omit to enter it in the web shell)")
    p.add_argument("--max-iterations", type=int, default=config.MAX_ITERATIONS, help=f"iteration budget (default {config.MAX_ITERATIONS})")
    p.add_argument("--port", type=int, default=config.DEFAULT_PORT, help=f"base port; hunts base..base+100 (default {config.DEFAULT_PORT})")
    p.add_argument(
        "--reference",
        default=None,
        metavar="PATH_OR_URL",
        help="seed the theme and first draft from a screenshot or a live URL (fresh runs only)",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--resume", action="store_true", help="resume an unfinished run without prompting")
    g.add_argument("--fresh", action="store_true", help="discard any unfinished run state")
    args = p.parse_args(argv)
    # Seeding runs only in the fresh branch of run(), so a resumed run can never
    # consume a reference. Reject the combination rather than silently ignore it.
    if args.reference and args.resume:
        p.error("--reference cannot be combined with --resume (seeding only happens on a fresh run)")
    return args
```

Then, in `decide_resume`, insert this immediately after the `if args.fresh:` block:

```python
    if getattr(args, "reference", None):
        # --resume is already rejected at parse time; this covers the interactive
        # path, where answering "yes" would silently discard the reference.
        if run_state.has_unfinished():
            print("Starting fresh: --reference cannot seed a resumed run.")
        return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_cli.py -q`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add orchestrator.py tests/test_cli.py
git commit -m "feat: add --reference CLI flag, rejected on resumed runs"
```

---

### Task 2: `capture_reference.js` — URL capture

**Files:**
- Create: `capture_reference.js`
- Create: `tests/test_capture_reference.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `node capture_reference.js <source> <outPath>`. Exits 0 having written a PNG at `outPath`, or exits 1 with a message on stderr. In this task `<source>` must be an `http(s)` URL; anything else is a clean error.

**Why not `networkidle`:** analytics beacons, websockets and long-polling keep the network busy indefinitely on most real marketing pages. `networkidle` would time out and Task 5's abort rule would turn a perfectly renderable page into a hard failure.

**Why clamped `fullPage` rather than a viewport shot:** the structural prompt in Task 6 asks the model for *section order*, which lives below the fold — so a 1280×800 viewport shot cannot deliver what the feature promises. Unbounded `fullPage` on a long page would cost many thousands of image tokens per seed call. The clamp is the only way to hold both.

- [ ] **Step 1: Write the failing test**

Create `tests/test_capture_reference.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_capture_reference.py -q`
Expected: FAIL — node exits non-zero, `Cannot find module '.../capture_reference.js'`

- [ ] **Step 3: Write the script**

Create `capture_reference.js`:

```js
const { chromium } = require("playwright");

// Matches capture.js's desktop breakpoint, so the reference is read at the same
// width the generated page will first be judged at.
const WIDTH = 1280;
const VIEWPORT_HEIGHT = 800;
// Three viewports. The structural prompt asks the model for section order, which
// lives below the fold, so a viewport-only shot would not carry it — but an
// unbounded fullPage shot of a long marketing page costs thousands of image
// tokens on every seed call. This clamp holds both.
const MAX_HEIGHT = 2400;
const SETTLE_MS = 1500;

async function shootUrl(page, url) {
  // 'load', not 'networkidle': beacons, websockets and long-polling keep the
  // network busy indefinitely on most real pages, so networkidle would time out
  // and the caller would abort a page that renders perfectly well.
  await page.goto(url, { waitUntil: "load", timeout: 30000 });
  await page.waitForTimeout(SETTLE_MS);
}

async function capture(source, outPath) {
  if (!/^https?:\/\//i.test(source)) {
    throw new Error(`expected an http(s) URL, got: ${source}`);
  }
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({
      viewport: { width: WIDTH, height: VIEWPORT_HEIGHT },
      deviceScaleFactor: 1,
    });
    const page = await context.newPage();
    await shootUrl(page, source);
    const full = await page.evaluate(() => document.documentElement.scrollHeight);
    const height = Math.max(1, Math.min(full, MAX_HEIGHT));
    await page.screenshot({ path: outPath, fullPage: true, clip: { x: 0, y: 0, width: WIDTH, height } });
  } finally {
    await browser.close();
  }
}

const [source, outPath] = process.argv.slice(2);
if (!source || !outPath) {
  console.error("Usage: node capture_reference.js <path-or-url> <out.png>");
  process.exit(1);
}
capture(source, outPath)
  .then(() => console.log(outPath))
  .catch((err) => {
    console.error(String(err));
    process.exit(1);
  });
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_capture_reference.py -q`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add capture_reference.js tests/test_capture_reference.py
git commit -m "feat: capture_reference.js normalizes a URL to a bounded 1280px PNG"
```

---

### Task 3: `capture_reference.js` — local image normalization

**Files:**
- Modify: `capture_reference.js`
- Test: `tests/test_capture_reference.py`

**Interfaces:**
- Consumes: `capture(source, outPath)` from Task 2.
- Produces: the same CLI contract, now also accepting a local image path.

**Why not `page.goto("file://…png")`:** that renders Chromium's *image document*, which centers the image on a theme-dependent letterbox background, flattens transparency onto it, and would feed that letterbox straight to palette extraction in Task 6. An HTML shim controls the background, prevents upscaling, and keeps the height clamp.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_capture_reference.py` — first the PNG writer (stdlib only), then the tests:

```python
import zlib


def make_png(path, width, height, rgb=(200, 30, 30)):
    """Write a solid-color truecolor PNG without Pillow."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, color type 2 (RGB)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def test_large_image_is_scaled_down():
    print("  test_large_image_is_scaled_down...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    d = tempfile.mkdtemp()
    src, out = os.path.join(d, "big.png"), os.path.join(d, "out.png")
    make_png(src, 2560, 1400)
    result = _run(src, out)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    w, h = png_size(out)
    assert w == 1280, f"expected downscale to 1280, got {w}"
    assert h <= 2400
    print("✅")


def test_small_image_is_not_upscaled():
    print("  test_small_image_is_not_upscaled...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    d = tempfile.mkdtemp()
    src, out = os.path.join(d, "small.png"), os.path.join(d, "out.png")
    make_png(src, 400, 300)
    result = _run(src, out)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    w, h = png_size(out)
    # The canvas stays 1280 wide; what must not happen is the 400px image being
    # stretched across it and blurred. The shot is 1280 wide but the image within
    # it keeps its own height, so a scaled copy would be 300 * (1280/400) = 960 tall.
    assert h < 900, f"400px-wide image appears upscaled (height {h})"
    print("✅")


def test_undecodable_file_exits_nonzero():
    print("  test_undecodable_file_exits_nonzero...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    d = tempfile.mkdtemp()
    src, out = os.path.join(d, "junk.png"), os.path.join(d, "out.png")
    with open(src, "wb") as f:
        f.write(b"not an image at all")
    result = _run(src, out)
    assert result.returncode != 0, "an undecodable file must fail loudly"
    print("✅")
```

Register all three in the `__main__` block:

```python
    test_large_image_is_scaled_down()
    test_small_image_is_not_upscaled()
    test_undecodable_file_exits_nonzero()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_capture_reference.py -q`
Expected: FAIL on the two new size tests — `expected an http(s) URL, got: …`

- [ ] **Step 3: Add the shim path**

In `capture_reference.js`, add the imports at the top:

```js
const path = require("path");
const { pathToFileURL } = require("url");
```

Add `shootImage` immediately after `shootUrl`:

```js
// Not page.goto("file://…png"): that renders Chromium's image document, which
// centers the image on a theme-dependent letterbox, flattens transparency onto
// it, and would feed that letterbox to palette extraction. The shim fixes the
// background, and max-width:100% scales a large image down while leaving a small
// one at its own size rather than upscaling it into blur.
async function shootImage(page, filePath) {
  const href = pathToFileURL(path.resolve(filePath)).href;
  await page.setContent(
    `<!doctype html><html><body style="margin:0;background:#ffffff">` +
      `<img id="ref" src="${href}" style="display:block;max-width:100%;height:auto">` +
      `</body></html>`,
  );
  try {
    await page.waitForFunction(
      () => {
        const img = document.getElementById("ref");
        return img && img.complete && img.naturalWidth > 0;
      },
      null,
      { timeout: 15000 },
    );
  } catch (err) {
    throw new Error(`could not decode image: ${filePath}`);
  }
}
```

Then replace the guard and the single `shootUrl` call inside `capture` with the dispatch:

```js
async function capture(source, outPath) {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({
      viewport: { width: WIDTH, height: VIEWPORT_HEIGHT },
      deviceScaleFactor: 1,
    });
    const page = await context.newPage();
    if (/^https?:\/\//i.test(source)) {
      await shootUrl(page, source);
    } else {
      await shootImage(page, source);
    }
    const full = await page.evaluate(() => document.documentElement.scrollHeight);
    const height = Math.max(1, Math.min(full, MAX_HEIGHT));
    await page.screenshot({ path: outPath, fullPage: true, clip: { x: 0, y: 0, width: WIDTH, height } });
  } finally {
    await browser.close();
  }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_capture_reference.py -q`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add capture_reference.js tests/test_capture_reference.py
git commit -m "feat: normalize local reference images through an HTML shim"
```

---

### Task 4: `preflight_vision` — prove the brain can actually see

A text-only model handed an image is silent in the Ollama dialect — the image is dropped and the model answers from the prompt alone. So this must be an active capability probe, not error handling.

**Files:**
- Create: `assets/vision_probe.png` (generated once by the command below, then committed)
- Modify: `llm_client.py` (add after `check_model_liveness`, before `preflight_roles`)
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Consumes: `_build_messages(provider, prompt, system_prompt, image_b64)` and `_do_api_call(provider, messages, model_id, response_format, timeout, retries)`, both already in `llm_client.py`.
- Produces:
  - `probe_vision(provider: dict, model_id: str, timeout: int = 60) -> Tuple[bool, str]`
  - `preflight_vision(provider_config: ProviderConfig, role: str = "brain") -> Tuple[bool, str]`

  Both return `(ok, detail)` and never raise, matching `preflight_roles`.

- [ ] **Step 1: Generate and commit the probe image**

Run this once from the repo root. It uses Playwright, already a dependency — no Pillow, no hand-rolled zlib. `738` is deliberately not `123`, `000` or `111`: those are exactly what a blind model guesses.

```bash
mkdir -p assets && node -e "
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 480, height: 240 }, deviceScaleFactor: 1 });
  await p.setContent('<body style=\"margin:0;display:flex;align-items:center;justify-content:center;height:240px;background:#fff\"><div style=\"font:bold 160px monospace;color:#000\">738</div></body>');
  await p.screenshot({ path: 'assets/vision_probe.png' });
  await b.close();
})();
"
```

Verify it looks right before continuing — open it and confirm large black `738` on white:

```bash
python3 -c "import struct;d=open('assets/vision_probe.png','rb').read(24);print(struct.unpack('>II', d[16:24]))"
```
Expected: `(480, 240)`

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_llm_client.py`:

```python
def test_probe_vision_reads_the_digits():
    print("  test_probe_vision_reads_the_digits...", end=" ")
    provider = {"id": "ollama", "baseUrl": "http://localhost:11434"}
    calls = []

    def fake_call(prov, messages, model_id, **kwargs):
        calls.append((prov, messages, model_id, kwargs))
        return "The number is 738."

    original = llm_client._do_api_call
    llm_client._do_api_call = fake_call
    try:
        ok, detail = llm_client.probe_vision(provider, "qwen-vl")
    finally:
        llm_client._do_api_call = original

    assert ok is True, detail
    # Lenient matching: prose around the digits must still pass.
    assert calls[0][3]["retries"] == 1, "a blind model surfaces as a retriable error; do not burn 90s of backoff"
    assert "images" in calls[0][1][-1], "the probe must actually attach the image"
    print("✅")


def test_probe_vision_rejects_a_blind_model():
    print("  test_probe_vision_rejects_a_blind_model...", end=" ")
    provider = {"id": "ollama", "baseUrl": "http://localhost:11434"}

    original = llm_client._do_api_call
    llm_client._do_api_call = lambda *a, **kw: "I cannot see any image."
    try:
        ok, detail = llm_client.probe_vision(provider, "llama-text")
    finally:
        llm_client._do_api_call = original

    assert ok is False
    assert "738" in detail
    print("✅")


def test_probe_vision_treats_http_error_as_blind():
    print("  test_probe_vision_treats_http_error_as_blind...", end=" ")
    provider = {"id": "openai", "baseUrl": "https://api.openai.com/v1"}

    def boom(*a, **kw):
        raise requests.exceptions.HTTPError("400 Bad Request: image input not supported")

    original = llm_client._do_api_call
    llm_client._do_api_call = boom
    try:
        ok, detail = llm_client.probe_vision(provider, "gpt-text")
    finally:
        llm_client._do_api_call = original

    # OpenAI 400s, llama.cpp 500s. Either way the reference is unusable.
    assert ok is False
    assert "probe call failed" in detail
    print("✅")


def test_preflight_vision_probes_every_candidate():
    print("  test_preflight_vision_probes_every_candidate...", end=" ")

    class FakeConfig:
        def get_role(self, role):
            return {"providerId": "primary", "fallbackOrder": ["backup"]}

        def resolve(self, pid):
            return {"id": pid, "baseUrl": f"http://{pid}"}

        def get_model_id(self, provider):
            return f"model-{provider['id']}"

    seen = []

    def fake_probe(provider, model_id, timeout=60):
        seen.append(model_id)
        # The primary can see; the fallback cannot. call_llm would fall through to
        # it on any transient error, so this must fail the whole preflight.
        return (True, "") if model_id == "model-primary" else (False, "answered '12'")

    original = llm_client.probe_vision
    llm_client.probe_vision = fake_probe
    try:
        ok, detail = llm_client.preflight_vision(FakeConfig())
    finally:
        llm_client.probe_vision = original

    assert seen == ["model-primary", "model-backup"], f"probed {seen}"
    assert ok is False
    assert "model-backup" in detail and "cannot see images" in detail
    print("✅")
```

Register all four in the `__main__` block of `tests/test_llm_client.py`. If `requests` is not already imported there, add `import requests` at the top.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_llm_client.py -q`
Expected: FAIL — `AttributeError: module 'llm_client' has no attribute 'probe_vision'`

- [ ] **Step 4: Implement both functions**

In `llm_client.py`, insert after `check_model_liveness` and before `preflight_roles`:

```python
VISION_PROBE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "vision_probe.png")
VISION_PROBE_ANSWER = "738"
VISION_PROBE_PROMPT = "What three-digit number is shown in this image? Reply with the digits only."


def probe_vision(provider: dict, model_id: str, timeout: int = 60) -> Tuple[bool, str]:
    """Ask ``(provider, model_id)`` to read the committed probe image.

    Returns ``(can_see, detail)`` and never raises. A transport or HTTP failure counts
    as "cannot see": that is exactly how the OpenAI-compatible dialects report the
    condition (OpenAI 400s, llama.cpp 500s), and a run whose reference is unusable must
    stop either way. Only Ollama fails silently, which is why the probe exists at all.

    Calls the transport directly rather than ``call_llm`` — the fallback chain there
    could let the probe pass on a different provider than the seed calls later use.
    """
    try:
        with open(VISION_PROBE_PATH, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
    except OSError as e:
        return (False, f"vision probe image missing: {e}")
    messages = _build_messages(provider, VISION_PROBE_PROMPT, None, image_b64)
    try:
        # retries=1: a blind Ollama model can surface as an in-stream error normalized
        # to ConnectionError, and the default 3 retries would spend 90s of backoff to
        # learn nothing.
        reply = _do_api_call(provider, messages, model_id, timeout=timeout, retries=1)
    except Exception as e:
        return (False, f"probe call failed: {str(e)[:120]}")
    digits = "".join(ch for ch in reply if ch.isdigit())
    if VISION_PROBE_ANSWER in digits:
        return (True, "")
    return (False, f"answered '{reply.strip()[:40]}' instead of {VISION_PROBE_ANSWER}")


def preflight_vision(provider_config: ProviderConfig, role: str = "brain") -> Tuple[bool, str]:
    """Confirm EVERY candidate in *role*'s chain can actually see an image.

    Every candidate, not just the primary: ``call_llm`` falls through to
    ``fallbackOrder`` (and then to the legacy env provider) on any exception, so a
    transient error during a seed call could otherwise reroute the image-bearing
    request to a blind model — the silent-ignore failure this check exists to prevent.

    Unlike ``check_model_liveness``, this does probe cloud providers. A silently
    ignored reference costs a whole run, which is worth far more than one small call.
    """
    role_cfg = provider_config.get_role(role)
    candidate_ids = [role_cfg["providerId"]] + role_cfg.get("fallbackOrder", [])
    checked = 0
    for pid in candidate_ids:
        try:
            provider = provider_config.resolve(pid)
            model_id = provider_config.get_model_id(provider)
        except (KeyError, FileNotFoundError):
            continue  # unresolvable id; call_llm would use the legacy env provider
        ok, detail = probe_vision(provider, model_id)
        checked += 1
        if not ok:
            return (
                False,
                f"{role} model '{model_id}' (provider '{pid}') cannot see images — "
                f"the reference would be silently ignored ({detail})",
            )
    if checked == 0:
        return (False, f"no resolvable provider for role '{role}' to probe for vision support")
    return (True, "")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_llm_client.py -q`
Expected: PASS, all tests including the four new ones

- [ ] **Step 6: Commit**

```bash
git add assets/vision_probe.png llm_client.py tests/test_llm_client.py
git commit -m "feat: preflight_vision proves every brain candidate can read an image"
```

---

### Task 5: Wire reference acquisition into the loop

**Files:**
- Modify: `config.py` (add `REFERENCE_SCRIPT`)
- Modify: `loop.py:284-298` (`__init__`), `loop.py:714-723` (`_run_meta`), the fresh branch of `run()` after the `preflight_roles` block at `loop.py:1112-1117`
- Modify: `orchestrator.py:58` (pass the flag through)
- Test: `tests/test_run_state.py`

**Interfaces:**
- Consumes: `parse_args(...).reference` (Task 1), `capture_reference.js` (Tasks 2–3), `llm_client.preflight_vision` (Task 4).
- Produces:
  - `FrontendDesignLoop(intent=None, max_iterations=None, status=None, reference=None)`
  - `self.reference` — the raw `--reference` value, `str | None`
  - `self.reference_png` — the normalized PNG path, `str | None`, set by `_acquire_reference`
  - `FrontendDesignLoop._acquire_reference() -> str`, raising `Exception` on any failure

- [ ] **Step 1: Write the failing test**

Add to `tests/test_run_state.py`:

```python
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
```

Register it in the `__main__` block of `tests/test_run_state.py`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_run_state.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'reference'`

- [ ] **Step 3: Add the config path**

In `config.py`, immediately after the `CAPTURE_SCRIPT` line:

```python
REFERENCE_SCRIPT = PROJECT_DIR / "capture_reference.js"
```

- [ ] **Step 4: Thread the reference through the loop**

In `loop.py`, change the `__init__` signature and add two attributes:

```python
    def __init__(self, intent: str = None, max_iterations: int = None, status: LoopStatus = None, reference: str = None):
        self.intent = intent
        self.reference = reference  # raw --reference value: a path or a URL
        self.reference_png = None  # normalized PNG, set by _acquire_reference
        self.max_iterations = max_iterations or config.MAX_ITERATIONS
```

(Leave the rest of `__init__` exactly as it is.)

Add both keys to `_run_meta`, after `"intent"`:

```python
            "reference": self.reference,
            "reference_png": self.reference_png,
```

Add `_acquire_reference` immediately after `bootstrap`:

```python
    def _acquire_reference(self):
        """Normalize --reference into one bounded PNG under screenshots/.

        Raises on every failure path: a reference that was asked for and cannot be
        used must stop the run, never degrade it to a reference-less design.
        """
        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        out = config.SCREENSHOT_DIR / f"reference_{int(time.time())}.png"
        result = subprocess.run(
            ["node", str(config.REFERENCE_SCRIPT), self.reference, str(out)],
            capture_output=True,
            text=True,
            cwd=str(config.PROJECT_DIR),
            timeout=180,
        )
        if result.returncode != 0 or not out.exists():
            raise Exception(f"reference capture failed: {result.stderr.strip()[:300]}")
        self.reference_png = str(out)
        return self.reference_png
```

In `run()`, insert this directly after the `preflight_roles` block (which ends with its `return`) and before the `# Intent input (if no CLI arg)` comment:

```python
            # Reference seeding: capture first, then prove the brain can read it.
            # Both must happen before the intent wait so a failure surfaces at once.
            if self.reference:
                print(f"\n🖼️ Capturing design reference: {self.reference}")
                try:
                    self._acquire_reference()
                except Exception as e:
                    print(f"\n❌ {e}")
                    return
                print(f"✅ Reference captured: {self.reference_png}")
                ok, detail = llm_client.preflight_vision(self.provider_config)
                if not ok:
                    print(f"\n❌ {detail}")
                    print(f"   Choose a vision-capable model at http://localhost:{port}/provider_wizard.html?mode=settings and re-run.")
                    return
```

- [ ] **Step 5: Pass the flag from the CLI**

In `orchestrator.py`, change the loop construction in `main`:

```python
    loop = FrontendDesignLoop(args.intent, max_iterations=args.max_iterations, status=status, reference=args.reference)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_run_state.py tests/test_cli.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add config.py loop.py orchestrator.py tests/test_run_state.py
git commit -m "feat: acquire and record the reference before the intent wait"
```

---

### Task 6: Attach the reference to both seed prompts

**Files:**
- Modify: `loop.py:330-372` (`generate_theme`), `loop.py:437-463` (`generate_initial_code`)
- Test: `tests/test_theme_loop.py`

**Interfaces:**
- Consumes: `self.reference_png` (Task 5).
- Produces: no new callables. `generate_theme` and `generate_initial_code` pass `image_path=self.reference_png` to `llm_client.call_llm` and add one conditional paragraph each.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_theme_loop.py`:

```python
def _capture_call(loop_obj, method):
    """Run a seed method against a stubbed LLM, returning the recorded kwargs."""
    seen = {}

    def mock_llm(role, prompt, system_prompt=None, image_path=None, **kwargs):
        seen["prompt"] = prompt
        seen["image_path"] = image_path
        return json.dumps(
            {
                "hard_tokens": {"brand_primary": "#000", "brand_secondary": "#fff", "primary_font": "sans-serif"},
                "soft_tokens": {"accent_color": "#3b82f6", "border_radius": "4px", "spacing_unit": "4px"},
                "tailwind_config": {"theme": {"extend": {}}},
            }
        )

    original = llm_client.call_llm
    llm_client.call_llm = mock_llm
    try:
        method()
    finally:
        llm_client.call_llm = original
    return seen


def test_reference_paragraphs_absent_without_a_reference():
    print("  test_reference_paragraphs_absent_without_a_reference...", end=" ")
    loop = FrontendDesignLoop("a coffee shop")

    theme = _capture_call(loop, loop.generate_theme)
    assert theme["image_path"] is None
    assert "REFERENCE IMAGE" not in theme["prompt"]

    code = _capture_call(loop, loop.generate_initial_code)
    assert code["image_path"] is None
    assert "STRUCTURAL REFERENCE" not in code["prompt"]
    print("✅")


def test_reference_paragraphs_and_image_threaded():
    print("  test_reference_paragraphs_and_image_threaded...", end=" ")
    loop = FrontendDesignLoop("a coffee shop")
    loop.reference_png = "/tmp/reference_1.png"

    theme = _capture_call(loop, loop.generate_theme)
    assert theme["image_path"] == "/tmp/reference_1.png"
    assert "REFERENCE IMAGE" in theme["prompt"]
    assert "classify" in theme["prompt"], "the typeface limitation must be stated"

    code = _capture_call(loop, loop.generate_initial_code)
    assert code["image_path"] == "/tmp/reference_1.png"
    assert "STRUCTURAL REFERENCE" in code["prompt"]
    assert "the tokens win" in code["prompt"], "token precedence must be explicit"
    print("✅")
```

Register both in the `__main__` block of `tests/test_theme_loop.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_theme_loop.py -q`
Expected: FAIL — `assert 'REFERENCE IMAGE' in prompt`

- [ ] **Step 3: Add the theme paragraph**

In `generate_theme`, replace the opening of the method (the `print` and the `prompt = f"""` line) with:

```python
    def generate_theme(self):
        print("\n[0/4] Generating design theme...")
        reference_block = ""
        if self.reference_png:
            reference_block = """
        REFERENCE IMAGE: The attached image is a design reference. Derive the tokens from the colors and typography actually present in it — sample its real palette instead of inventing one.
        You cannot identify a typeface from a screenshot. Classify it instead ("geometric sans", "humanist sans", "transitional serif", "slab serif", "monospace") and set primary_font to the closest widely-available web font for that classification.
        """
        prompt = f"""
        Create a professional design theme for a website with the following intent: {self.intent}.
        {reference_block}
```

Then change the call at the end of the method to pass the image:

```python
        theme_str = llm_client.call_llm(
            "brain",
            prompt,
            system_prompt=THEME_SYSTEM_PROMPT,
            image_path=self.reference_png,
            provider_config=self.provider_config,
        )
```

- [ ] **Step 4: Add the structural paragraph**

In `generate_initial_code`, after the `theme_context` assignment and before `prompt = f"""`:

```python
        structural_reference = ""
        if self.reference_png:
            structural_reference = """
        STRUCTURAL REFERENCE: The attached image is a layout reference. Follow its visual hierarchy, section order, information density and component vocabulary. The INTENT alone decides what content exists — do not copy text, logos, imagery or subject matter from the reference.
        Where colors in the reference disagree with the MANDATORY DESIGN TOKENS above, the tokens win. Never sample a hex value out of the image.
        """
```

Insert `{structural_reference}` into the prompt immediately after `{theme_context}`:

```python
        prompt = f"""
        {theme_context}
        {structural_reference}

        MULTI-VIEW CONVENTION: If the design needs multiple views/screens
```

And pass the image on the call:

```python
        self.current_code = llm_client.call_llm(
            "brain",
            prompt,
            system_prompt=BRAIN_SYSTEM_PROMPT,
            image_path=self.reference_png,
            timeout=FULL_FILE_TIMEOUT,
            provider_config=self.provider_config,
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_theme_loop.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add loop.py tests/test_theme_loop.py
git commit -m "feat: seed theme and first draft from the reference image"
```

---

### Task 7: Theme fallbacks must abort on an image-seeded run

Two existing paths swallow theme failure and continue with a generic palette. Both are reasonable when the theme is the model's own invention. With a reference, either one silently discards the image-derived palette and lets `_inject_theme` force a generic palette onto a reference-shaped layout — the exact "two halves fight" failure the design exists to prevent.

**Files:**
- Modify: `loop.py:377-390` (the `json.JSONDecodeError` handler in `generate_theme`), `loop.py:1131-1136` (the `try/except` around `generate_theme` in `run()`)
- Test: `tests/test_theme_loop.py`

**Interfaces:**
- Consumes: `self.reference_png`, `self.reference` (Task 5).
- Produces: no new callables. `generate_theme` raises instead of writing the hardcoded fallback when `self.reference_png` is set.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_theme_loop.py`:

```python
def test_unparseable_theme_falls_back_without_a_reference():
    print("  test_unparseable_theme_falls_back_without_a_reference...", end=" ")
    loop = FrontendDesignLoop("a coffee shop")

    original = llm_client.call_llm
    llm_client.call_llm = lambda *a, **kw: "not json at all"
    try:
        loop.generate_theme()  # must NOT raise
    finally:
        llm_client.call_llm = original

    assert loop.theme_json["hard_tokens"]["brand_primary"] == "#000000"
    print("✅")


def test_unparseable_theme_aborts_with_a_reference():
    print("  test_unparseable_theme_aborts_with_a_reference...", end=" ")
    loop = FrontendDesignLoop("a coffee shop")
    loop.reference_png = "/tmp/reference_1.png"

    original = llm_client.call_llm
    llm_client.call_llm = lambda *a, **kw: "not json at all"
    try:
        loop.generate_theme()
        assert False, "an image-seeded run must not fall back to a generic palette"
    except Exception as e:
        assert "generic palette" in str(e)
    finally:
        llm_client.call_llm = original
    print("✅")
```

Register both in the `__main__` block.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_theme_loop.py -q`
Expected: FAIL on `test_unparseable_theme_aborts_with_a_reference` — no exception raised

- [ ] **Step 3: Make the parse fallback conditional**

In `generate_theme`, change the `except json.JSONDecodeError` handler so its first statement is the guard, leaving the existing fallback body untouched below it:

```python
        except json.JSONDecodeError as e:
            if self.reference_png:
                raise Exception(
                    f"theme JSON unparseable ({e}) — refusing to fall back to a generic palette "
                    "on an image-seeded run, which would put a generic palette on a reference-shaped layout"
                )
            print(f"❌ Failed to parse theme JSON: {e}")
```

- [ ] **Step 4: Make the run() handler conditional**

In `run()`, replace the `try/except` around `generate_theme`:

```python
            try:
                self.generate_theme()
            except Exception as e:
                if self.reference:
                    print(f"\n❌ Theme generation failed on an image-seeded run: {e}")
                    print("   The reference cannot be honoured without tokens derived from it. Exiting.")
                    return
                print(f"⚠️ Theme generation failed: {e}. Proceeding without design tokens.")
                self.theme_json = {}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_theme_loop.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add loop.py tests/test_theme_loop.py
git commit -m "feat: abort rather than fall back to a generic palette when seeded by an image"
```

---

### Task 8: Document the flag and verify the whole feature

**Files:**
- Modify: `README.md`
- Test: the full suite

**Interfaces:**
- Consumes: everything above.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS. Two failures in `tests/test_status_endpoint.py` (`test_start_origin_rejected`, `test_action_and_feedback_origin_rejected`) are **pre-existing and flaky** — they reproduce on `main` with a `ConnectionResetError` while reading the body of a 403. Confirm any failure you see is one of those two and nothing else.

- [ ] **Step 2: Document the flag in README.md**

Add to the usage section, after the existing invocation examples:

```markdown
### Seeding from a reference

Pass a screenshot or a live URL to seed the run's palette and first layout:

```bash
python3 orchestrator.py "a dog-walking service" --reference ~/Desktop/pricing-page.png
python3 orchestrator.py "a dog-walking service" --reference https://example.com/pricing
```

The reference sets the design tokens and the structure of the first draft — hierarchy,
section order, density — while the intent alone decides what content is on the page. It
is a starting point, not a target: the audit still scores against the intent, so
resemblance is expected to fade over successive iterations.

Requires a vision-capable brain model; the run aborts up front if the configured model
cannot read an image, rather than silently ignoring the reference. `--reference` cannot
be combined with `--resume`, because seeding only happens on a fresh run.
```

- [ ] **Step 3: Verify end to end against a real page**

Run, with your normal provider config in place:

```bash
python3 orchestrator.py "a pricing page for a dog-walking service" --reference https://example.com
```

Confirm in order: the reference is captured and its path printed; the vision preflight passes; `theme.json` colors plausibly relate to the reference rather than being generic; the first draft renders. Then interrupt the run — a full loop is not needed to verify seeding.

- [ ] **Step 4: Verify the blind-model abort**

Point the brain role at a text-only model in the provider wizard, then run the same command. Expected: the run stops before generating anything, with `... cannot see images — the reference would be silently ignored`. Restore your normal config afterwards.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: document --reference"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: §1 `--reference` → Task 1; §2 `capture_reference.js` → Tasks 2–3; §3 `preflight_vision` → Task 4; §4 prompt changes → Task 6; §5 theme fallbacks → Task 7; §6 run metadata → Task 5; error-handling table → Tasks 1 (resume rows), 2–3 (capture rows), 4 (probe row), 5 (subprocess row), 7 (theme rows). The startup sequence pinned in the spec's architecture diagram is Task 5, Step 4.

Deliberately unimplemented, matching the spec's *Non-goals* and *Deferred*: web shell support, multi-image refine, the strength dial, reference-as-rubric, and softening `hard_tokens` immutability.

**Type consistency.** `self.reference` (raw value) and `self.reference_png` (normalized path) are named identically in Tasks 5, 6 and 7. `probe_vision` and `preflight_vision` both return `(bool, str)` and are used that way in Tasks 4 and 5. `_acquire_reference` sets `self.reference_png` and returns it; Task 5's `run()` insertion relies on the attribute, not the return value. `config.REFERENCE_SCRIPT` is defined in Task 5 Step 3 and used in Step 4.

**One knowingly imprecise assertion.** `test_small_image_is_not_upscaled` asserts on the output *height* (`< 900`) rather than on the image's rendered width, because the canvas is always 1280 wide and only the height reveals whether the 400×300 source was stretched. The comment in the test explains the arithmetic.
