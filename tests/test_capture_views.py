"""Guarded contract tests for capture.js multi-view output (needs chromium)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

TAGGED_PAGE = """<html><head><title>t</title>
<script>
function show(id) {
  document.querySelectorAll('[data-view-panel]').forEach(p => p.hidden = true);
  document.querySelector('[data-view-panel="' + id + '"]').hidden = false;
}
</script></head><body>
<nav>
  <a data-view="dashboard" href="#" onclick="show('dashboard')">Dashboard</a>
  <a data-view="settings" href="#" onclick="show('settings')">Settings</a>
  <a data-view="broken" href="#">Broken</a>
</nav>
<section data-view-panel="dashboard"><h1>DASH</h1></section>
<section data-view-panel="settings" hidden><h1>SET</h1></section>
</body></html>"""
# 'broken' has no panel -> activation must yield an error record

UNTAGGED_PAGE = "<html><head><title>t</title></head><body><h1>single</h1></body></html>"

MATRIX_PAGE = """<html><head><title>t</title>
<style>@media (max-width: 500px) { nav a[data-view] { display: none } }</style>
<script>
function show(id) {
  document.querySelectorAll('[data-view-panel]').forEach(p => p.hidden = true);
  document.querySelector('[data-view-panel="' + id + '"]').hidden = false;
}
</script></head><body>
<nav>
  <a data-view="dashboard" href="#" onclick="show('dashboard')">Dashboard</a>
  <a data-view="settings" href="#" onclick="show('settings')">Settings</a>
</nav>
<section data-view-panel="dashboard"><h1>DASH</h1></section>
<section data-view-panel="settings" hidden><h1>SET</h1></section>
</body></html>"""

BP_ARG = '[["desktop", [1280, 800]], ["mobile", [375, 812]]]'


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


def _run_capture(page_html):
    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", dir=str(config.PROJECT_DIR))
    tmp.write(page_html)
    tmp.close()
    try:
        result = subprocess.run(
            ["node", str(config.CAPTURE_SCRIPT), tmp.name], capture_output=True, text=True, cwd=str(config.PROJECT_DIR), timeout=90
        )
        return result
    finally:
        os.remove(tmp.name)


def _run_capture_bp(page_html, bp_arg):
    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", dir=str(config.PROJECT_DIR))
    tmp.write(page_html)
    tmp.close()
    try:
        return subprocess.run(
            ["node", str(config.CAPTURE_SCRIPT), tmp.name, bp_arg], capture_output=True, text=True, cwd=str(config.PROJECT_DIR), timeout=120
        )
    finally:
        os.remove(tmp.name)


def test_tagged_page_captures_views_and_reports_errors():
    print("  test_tagged_page_captures_views_and_reports_errors...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture(TAGGED_PAGE)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    by_id = {r["id"]: r for r in records}
    assert list(by_id) == ["dashboard", "settings", "broken"]  # DOM order
    assert os.path.exists(by_id["dashboard"]["screenshot"])
    assert os.path.exists(by_id["settings"]["screenshot"])
    assert "error" in by_id["broken"] and "screenshot" not in by_id["broken"]
    print("✅")


def test_untagged_page_yields_default_record():
    print("  test_untagged_page_yields_default_record...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture(UNTAGGED_PAGE)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    assert len(records) == 1 and records[0]["id"] == "default"
    assert os.path.exists(records[0]["screenshot"])
    print("✅")


def test_breakpoints_argv_matrix():
    print("  test_breakpoints_argv_matrix...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(MATRIX_PAGE, BP_ARG)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    base_keys = [(r["id"], r["breakpoint"]) for r in records if not r.get("pseudo")]
    assert base_keys == [("dashboard", "desktop"), ("settings", "desktop"), ("dashboard", "mobile"), ("settings", "mobile")]
    shots = [r["screenshot"] for r in records if "screenshot" in r]
    # desktop: 2 views x 3 cells (base + hover + focus) = 6; mobile: nav hidden, 2 base only = 2
    assert len(shots) == 8 and len(set(shots)) == 8
    for s in shots:
        assert os.path.exists(s)
    print("✅")


def test_hamburger_hidden_nav_still_activates():
    """At 375px the nav links are display:none — programmatic el.click()
    must still activate the views (a physical click would fail)."""
    print("  test_hamburger_hidden_nav_still_activates...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(MATRIX_PAGE, '[["mobile", [375, 812]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    bases = [r for r in records if not r.get("pseudo") and not r.get("state")]
    assert all("screenshot" in r for r in bases), f"activation failed: {records}"
    assert [r["breakpoint"] for r in bases] == ["mobile", "mobile"]
    print("✅")


MODAL_UNTAGGED_PAGE = """<html><head><title>t</title></head><body>
<h1>Landing</h1>
<button data-state="info-modal"
  onclick="const p=document.querySelector('[data-state-panel=info-modal]'); p.hidden=!p.hidden">
  Info</button>
<div data-state-panel="info-modal" hidden><h2>MODAL CONTENT</h2></div>
</body></html>"""

BROKEN_TOGGLE_PAGE = """<html><head><title>t</title>
<script>
function show(id) {
  document.querySelectorAll('[data-view-panel]').forEach(p => p.hidden = true);
  document.querySelector('[data-view-panel="' + id + '"]').hidden = false;
}
</script></head><body>
<nav>
  <a data-view="view-a" href="#" onclick="show('view-a')">A</a>
  <a data-view="view-b" href="#" onclick="show('view-b')">B</a>
</nav>
<section data-view-panel="view-a">
  <button data-state="sticky-modal"
    onclick="document.querySelector('[data-state-panel=sticky-modal]').hidden=false">
    Open</button>
</section>
<section data-view-panel="view-b" hidden><h1>B</h1></section>
<div data-state-panel="sticky-modal" hidden><h2>WONT CLOSE</h2></div>
</body></html>"""


def test_untagged_page_with_modal_captures_state():
    print("  test_untagged_page_with_modal_captures_state...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(MODAL_UNTAGGED_PAGE, '[["desktop", [1280, 800]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    bases = [r for r in records if not r.get("pseudo") and not r.get("state")]
    states = [r for r in records if r.get("state")]
    assert len(bases) == 1 and bases[0]["id"] == "default"
    assert len(states) == 1
    _, state = bases[0], states[0]
    assert state["id"] == "default" and state["state"] == "info-modal"
    assert os.path.exists(state["screenshot"])
    assert "close_failed" not in state
    print("✅")


def test_broken_toggle_reload_fallback():
    print("  test_broken_toggle_reload_fallback...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(BROKEN_TOGGLE_PAGE, '[["desktop", [1280, 800]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    by_key = {(r["id"], r.get("state")): r for r in records if not r.get("pseudo")}
    sticky = by_key[("view-a", "sticky-modal")]
    assert sticky.get("close_failed") is True
    assert os.path.exists(sticky["screenshot"])  # shot still good
    # Reload fallback must not poison later captures:
    assert "screenshot" in by_key[("view-b", None)], f"view-b broke: {records}"
    print("\u2705")


def test_state_ownership_scopes_to_view():
    print("  test_state_ownership_scopes_to_view...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(BROKEN_TOGGLE_PAGE, '[["desktop", [1280, 800]]]')
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    state_records = [r for r in records if r.get("state")]
    assert all(r["id"] == "view-a" for r in state_records), (
        f"state leaked to another view: {state_records}"
    )  # trigger sits in view-a's panel
    print("\u2705")


DUPLICATE_VIEW_PAGE = """<html><head><title>t</title>
<script>
function show(id) {
  document.querySelectorAll('[data-view-panel]').forEach(p => p.hidden = true);
  document.querySelector('[data-view-panel="' + id + '"]').hidden = false;
}
</script></head><body>
<nav>
  <a data-view="dashboard" href="#" onclick="show('dashboard')">Dashboard</a>
  <a data-view="dashboard" href="#" onclick="show('dashboard')">Home</a>
  <a data-view="settings" href="#" onclick="show('settings')">Settings</a>
</nav>
<section data-view-panel="dashboard"><h1>DASH</h1></section>
<section data-view-panel="settings" hidden><h1>SET</h1></section>
</body></html>"""


COLLIDING_PANELS_PAGE = """<html><head><title>t</title>
<script>
function show(id) {
  document.querySelectorAll('[data-view-panel]').forEach(p => p.hidden = true);
  document.querySelector('[data-view-panel="' + id + '"]').hidden = false;
}
</script></head><body>
<nav>
  <a data-view="dashboard" href="#" onclick="show('dashboard')">Dashboard</a>
  <a data-view="settings" href="#" onclick="show('settings')">Settings</a>
</nav>
<section data-view-panel="dashboard"><h1>DASH</h1></section>
<section data-view-panel="settings" hidden><h1>SET ONE</h1></section>
<section data-view-panel="settings" hidden><h1>SET TWO</h1></section>
</body></html>"""
# two screens claiming one id -> only the first is reachable

ORPHAN_PANEL_PAGE = """<html><head><title>t</title>
<script>
function show(id) {
  document.querySelectorAll('[data-view-panel]').forEach(p => p.hidden = true);
  document.querySelector('[data-view-panel="' + id + '"]').hidden = false;
}
</script></head><body>
<nav>
  <a data-view="dashboard" href="#" onclick="show('dashboard')">Dashboard</a>
  <a data-view="dashboard" href="#" onclick="show('dashboard')">Settings</a>
</nav>
<section data-view-panel="dashboard"><h1>DASH</h1></section>
<section data-view-panel="settings" hidden><h1>SET</h1></section>
</body></html>"""
# the second nav link should have said 'settings' -> that screen has no trigger

OFFCANVAS_DRAWER_PAGE = """<html><head><title>t</title>
<style>#drawer { position: fixed; top: 0; left: 0; width: 240px; height: 100%; transform: translateX(-100%); }</style>
<script>
function show(id) {
  document.querySelectorAll('[data-view-panel]').forEach(p => p.hidden = true);
  document.querySelector('[data-view-panel="' + id + '"]').hidden = false;
}
</script></head><body>
<div id="drawer"><a data-view="settings" href="#" onclick="void(0)">Settings (drawer copy)</a></div>
<nav>
  <a data-view="dashboard" href="#" onclick="show('dashboard')">Dashboard</a>
  <a data-view="settings" href="#" onclick="show('settings')">Settings</a>
</nav>
<section data-view-panel="dashboard"><h1>DASH</h1></section>
<section data-view-panel="settings" hidden><h1>SET</h1></section>
</body></html>"""
# the drawer copy is earlier in the DOM and has a non-zero rect, but is off-canvas
# and inert; picking it would leave the panel hidden


def test_duplicate_view_triggers_tolerated():
    """A view reachable from several nav copies is normal on a responsive page:
    warn once, capture it, do not abort the run."""
    print("  test_duplicate_view_triggers_tolerated...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(DUPLICATE_VIEW_PAGE, BP_ARG)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    dash = [r for r in records if r["id"] == "dashboard" and not r.get("pseudo")]
    assert len(dash) == 2, f"expected ONE base record per breakpoint: {dash}"
    for r in dash:
        assert "error" not in r, f"duplicate triggers still fatal: {r}"
        assert os.path.exists(r["screenshot"])
    assert result.stderr.count("has 2 [data-view] triggers") == 1, f"warned per breakpoint: {result.stderr}"
    sett = [r for r in records if r["id"] == "settings" and not r.get("pseudo")]
    assert len(sett) == 2 and all(os.path.exists(r["screenshot"]) for r in sett)
    print("✅")


def test_colliding_view_panels_error_record():
    """Duplicate PANELS are an id collision, not a repeated nav link: the second
    screen is unreachable, so it must stay fatal rather than vanish."""
    print("  test_colliding_view_panels_error_record...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(COLLIDING_PANELS_PAGE, '[["desktop", [1280, 800]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    sett = [r for r in records if r["id"] == "settings" and not r.get("pseudo")]
    assert len(sett) == 1, f"expected ONE base record for the collided id: {sett}"
    assert "duplicate view id" in sett[0]["error"]
    assert "screenshot" not in sett[0]
    # The well-formed view on the same page still captures normally:
    dash = [r for r in records if r["id"] == "dashboard" and not r.get("pseudo")]
    assert len(dash) == 1 and os.path.exists(dash[0]["screenshot"])
    print("✅")


def test_orphan_view_panel_error_record():
    """A panel no trigger points at would otherwise leave no trace at all — the
    run would converge having audited half the app."""
    print("  test_orphan_view_panel_error_record...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(ORPHAN_PANEL_PAGE, '[["desktop", [1280, 800]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    sett = [r for r in records if r["id"] == "settings" and not r.get("pseudo")]
    assert len(sett) == 1, f"unreachable screen left no record: {records}"
    assert "no [data-view] trigger" in sett[0]["error"]
    dash = [r for r in records if r["id"] == "dashboard" and not r.get("pseudo")]
    assert len(dash) == 1 and os.path.exists(dash[0]["screenshot"])
    print("✅")


def test_offcanvas_duplicate_trigger_not_preferred():
    """An off-canvas drawer copy reports a non-zero rect but is not rendered at
    this breakpoint — the real nav link must win despite being later in the DOM."""
    print("  test_offcanvas_duplicate_trigger_not_preferred...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(OFFCANVAS_DRAWER_PAGE, '[["desktop", [1280, 800]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    sett = [r for r in records if r["id"] == "settings" and not r.get("pseudo") and not r.get("state")]
    assert len(sett) == 1 and "screenshot" in sett[0], f"clicked the off-canvas copy: {sett}"
    assert os.path.exists(sett[0]["screenshot"])
    print("✅")


DUPLICATE_TRIGGER_PAGE = """<html><head><title>t</title></head><body>
<h1>Landing</h1>
<button data-state="signup-modal"
  onclick="const p=document.querySelector('[data-state-panel=signup-modal]'); p.hidden=!p.hidden">
  Sign up</button>
<button data-state="signup-modal"
  onclick="const p=document.querySelector('[data-state-panel=signup-modal]'); p.hidden=!p.hidden">
  Join now</button>
<button data-state="info-modal"
  onclick="const p=document.querySelector('[data-state-panel=info-modal]'); p.hidden=!p.hidden">
  Info</button>
<div data-state-panel="signup-modal" hidden><h2>SIGN UP</h2></div>
<div data-state-panel="info-modal" hidden><h2>INFO</h2></div>
</body></html>"""


def test_duplicate_state_triggers_error_record():
    print("  test_duplicate_state_triggers_error_record...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(DUPLICATE_TRIGGER_PAGE, '[["desktop", [1280, 800]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    dups = [r for r in records if r.get("state") == "signup-modal"]
    assert len(dups) == 1, f"expected ONE record for the duplicated state: {dups}"
    assert "duplicate state id" in dups[0]["error"]
    assert "screenshot" not in dups[0]
    # The well-formed state on the same page still captures normally:
    info = [r for r in records if r.get("state") == "info-modal"]
    assert len(info) == 1 and os.path.exists(info[0]["screenshot"])
    assert "close_failed" not in info[0]
    print("✅")


BAD_VIEW_WITH_STATE_PAGE = """<html><head><title>t</title>
<script>
function show(id) {
  document.querySelectorAll('[data-view-panel]').forEach(p => p.hidden = true);
  document.querySelector('[data-view-panel="' + id + '"]').hidden = false;
}
</script></head><body>
<nav>
  <a data-view="View A" href="#">A</a>
  <a data-view="view-b" href="#" onclick="show('view-b')">B</a>
</nav>
<section data-view-panel="View A">
  <button data-state="help-modal"
    onclick="const p=document.querySelector('[data-state-panel=help-modal]'); p.hidden=!p.hidden">
    Help</button>
</section>
<section data-view-panel="view-b" hidden><h1>B</h1></section>
<div data-state-panel="help-modal" hidden><h2>HELP</h2></div>
</body></html>"""
# 'View A' is not lowercase-kebab -> still fatal, and it owns a state


def test_errored_view_reports_owned_states():
    """A view that errors must not silently drop its owned states: they get
    error records so the loop blocks convergence with the real reason."""
    print("  test_errored_view_reports_owned_states...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(BAD_VIEW_WITH_STATE_PAGE, '[["desktop", [1280, 800]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    by_key = {(r["id"], r.get("state")): r for r in records if not r.get("pseudo")}
    assert "invalid view id" in by_key[("View A", None)]["error"]
    helper = by_key.get(("View A", "help-modal"))
    assert helper is not None, f"owned state vanished: {records}"
    assert "owning view" in helper["error"]
    assert "screenshot" in by_key[("view-b", None)]  # rest of the page fine
    print("✅")


def test_viewport_failure_reports_states():
    """A breakpoint whose viewport resize fails must emit error records for
    declared states too, not only for views."""
    print("  test_viewport_failure_reports_states...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(MODAL_UNTAGGED_PAGE, '[["desktop", [1280, 800]], ["bad", [-1, -1]]]')
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    bad = [r for r in records if r["breakpoint"] == "bad"]
    assert any("viewport failed" in r.get("error", "") and r.get("state") == "info-modal" for r in bad), (
        f"no state error record for failed viewport: {bad}"
    )
    good = [r for r in records if r["breakpoint"] == "desktop"]
    assert any(r.get("state") == "info-modal" and "screenshot" in r for r in good)
    print("✅")


SHEET_PAGE = """<html><head><title>t</title><style>
button:focus-visible { outline: 3px solid red; }
.noring { outline: none; }
.noring:focus-visible { outline: none; }
.hov:hover { background: purple; }
</style></head><body>
<h1>Landing</h1>
<button class="hov">Styled button</button>
<a href="#" class="noring">Learn more</a>
<a href="#" class="noring">Learn more</a>
<div onclick="void(0)">Decor tile</div>
</body></html>"""

TRANSITION_RING_PAGE = """<html><head><title>t</title><style>
a { transition: all 0.5s; }
a:focus-visible { box-shadow: 0 0 0 4px blue; outline: none; }
</style></head><body><a href="#">Transitioned ring</a></body></html>"""

ANIMATED_NORING_PAGE = """<html><head><title>t</title><style>
@keyframes pulse { 0% { opacity: 1; } 50% { opacity: .5; } 100% { opacity: 1; } }
a { animation: pulse 1s infinite; outline: none; }
a:focus-visible { outline: none; }
</style></head><body><a href="#">Pulsing naked link</a></body></html>"""


def _sheet_records(page, bp_arg='[["desktop", [1280, 800]]]'):
    result = _run_capture_bp(page, bp_arg)
    assert result.returncode == 0, f"capture failed: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])["views"]


def test_sheets_captured_with_offenders():
    print("  test_sheets_captured_with_offenders...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(SHEET_PAGE)
    by_pseudo = {r.get("pseudo"): r for r in records if r.get("pseudo")}
    assert set(by_pseudo) == {"hover", "focus"}
    for r in by_pseudo.values():
        assert r["id"] == "default" and os.path.exists(r["screenshot"])
        assert "state" not in r
    offenders = by_pseudo["focus"].get("missing_focus", [])
    naked = [o for o in offenders if 'a "Learn more"' in o]
    assert len(naked) == 2 and naked[0] != naked[1], f"offenders: {offenders}"
    assert not any("Styled button" in o for o in offenders)
    assert not any("Decor tile" in o for o in offenders)
    print("✅")


def test_transitioned_ring_not_flagged():
    print("  test_transitioned_ring_not_flagged...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(TRANSITION_RING_PAGE)
    focus = next(r for r in records if r.get("pseudo") == "focus")
    assert "missing_focus" not in focus, f"freeze fix regressed: {focus}"
    print("✅")


def test_animated_suppressed_ring_flagged():
    print("  test_animated_suppressed_ring_flagged...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(ANIMATED_NORING_PAGE)
    focus = next(r for r in records if r.get("pseudo") == "focus")
    assert any("Pulsing naked link" in o for o in focus.get("missing_focus", [])), f"animation defeated the diff: {focus}"
    print("✅")


def test_no_interactive_no_sheets():
    print("  test_no_interactive_no_sheets...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(UNTAGGED_PAGE)
    assert not any(r.get("pseudo") for r in records), f"sheets on empty page: {records}"
    print("✅")


def test_reload_fallback_then_sheets():
    """A close_failed reload during the state phase must not poison the later
    sheet capture — nodeIds are resolved fresh per sheet."""
    print("  test_reload_fallback_then_sheets...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(BROKEN_TOGGLE_PAGE)
    va = [r for r in records if r.get("pseudo") and r["id"] == "view-a"]
    assert {r["pseudo"] for r in va} == {"hover", "focus"}, f"sheets missing: {records}"
    assert all(os.path.exists(r["screenshot"]) for r in va), f"stale nodeIds after reload fallback: {va}"
    print("✅")


def test_errored_view_reports_sheets():
    print("  test_errored_view_reports_sheets...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(BAD_VIEW_WITH_STATE_PAGE)
    errs = [r for r in records if r.get("pseudo") and r["id"] == "View A"]
    assert {r["pseudo"] for r in errs} == {"hover", "focus"}, f"sheet errors missing: {records}"
    assert all("owning view" in r["error"] for r in errs)
    good = [r for r in records if r.get("pseudo") and r["id"] == "view-b"]
    assert {r["pseudo"] for r in good} == {"hover", "focus"}
    assert all(os.path.exists(r["screenshot"]) for r in good)
    print("✅")


MODAL_SHEET_PAGE = """<html><head><title>t</title><style>
button:focus-visible, a:focus-visible, input:focus-visible { outline: 3px solid red; }
.noring { outline: none; }
.noring:focus-visible { outline: none; }
</style></head><body>
<h1>Landing</h1>
<a href="#">Base link</a>
<button data-state="signup-modal"
  onclick="const p=document.querySelector('[data-state-panel=signup-modal]'); p.hidden=!p.hidden">
  Sign up</button>
<div data-state-panel="signup-modal" hidden>
  <input placeholder="email">
  <button class="noring">Confirm</button>
</div>
</body></html>"""

AUTOFOCUS_MODAL_PAGE = """<html><head><title>t</title><style>
button:focus-visible, a:focus-visible, input:focus-visible { outline: 3px solid red; }
</style></head><body>
<h1>Landing</h1>
<button data-state="signup-modal"
  onclick="const p=document.querySelector('[data-state-panel=signup-modal]');
           p.hidden=!p.hidden; if(!p.hidden) p.querySelector('input').focus();">
  Sign up</button>
<div data-state-panel="signup-modal" hidden>
  <input placeholder="email">
  <button>Confirm</button>
</div>
</body></html>"""

TEXT_ONLY_MODAL_PAGE = """<html><head><title>t</title></head><body>
<h1>Landing</h1>
<a href="#">Base link</a>
<button data-state="info-modal"
  onclick="const p=document.querySelector('[data-state-panel=info-modal]'); p.hidden=!p.hidden">
  Info</button>
<div data-state-panel="info-modal" hidden><p>Just words.</p></div>
</body></html>"""

BROKEN_TOGGLE_MODAL_PAGE = """<html><head><title>t</title><style>
button:focus-visible, a:focus-visible { outline: 3px solid red; }
</style></head><body>
<h1>Landing</h1>
<a href="#">Base link</a>
<button data-state="sticky-modal"
  onclick="document.querySelector('[data-state-panel=sticky-modal]').hidden=false">
  Open</button>
<div data-state-panel="sticky-modal" hidden><button>Inside</button></div>
</body></html>"""

OPEN_FAIL_STATE_PAGE = """<html><head><title>t</title></head><body>
<h1>Landing</h1>
<a href="#">Base link</a>
<button data-state="ghost-modal" onclick="void(0)">Ghost</button>
<div data-state-panel="ghost-modal" hidden><button>Never seen</button></div>
</body></html>"""


def test_overlay_sheets_scoped_both_directions():
    print("  test_overlay_sheets_scoped_both_directions...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(MODAL_SHEET_PAGE)
    overlay = {r["pseudo"]: r for r in records if r.get("pseudo") and r.get("state") == "signup-modal"}
    assert set(overlay) == {"hover", "focus"}, f"overlay sheets missing: {records}"
    assert all(os.path.exists(r["screenshot"]) for r in overlay.values())
    off = overlay["focus"].get("missing_focus", [])
    assert any('button "Confirm"' in o for o in off), f"panel offender missed: {off}"
    base_focus = next(r for r in records if r.get("pseudo") == "focus" and not r.get("state"))
    assert not any("Confirm" in o for o in base_focus.get("missing_focus", [])), f"panel offender leaked into base gate: {base_focus}"
    print("\u2705")


def test_autofocused_element_not_flagged():
    print("  test_autofocused_element_not_flagged...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(AUTOFOCUS_MODAL_PAGE)
    focus = next(r for r in records if r.get("pseudo") == "focus" and r.get("state") == "signup-modal")
    assert not any("input" in o for o in focus.get("missing_focus", [])), f"autofocused input false-flagged: {focus}"
    print("\u2705")


def test_text_only_panel_no_sheet_cells():
    print("  test_text_only_panel_no_sheet_cells...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(TEXT_ONLY_MODAL_PAGE)
    assert not any(r.get("pseudo") and r.get("state") for r in records), f"sheet cells for an empty panel: {records}"
    # the base sheets still exist (page has interactive elements):
    assert any(r.get("pseudo") == "hover" and not r.get("state") for r in records)
    print("\u2705")


def test_broken_toggle_sheets_then_clean_captures():
    print("  test_broken_toggle_sheets_then_clean_captures...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(BROKEN_TOGGLE_MODAL_PAGE)
    sticky = next(r for r in records if r.get("state") == "sticky-modal" and not r.get("pseudo"))
    assert sticky.get("close_failed") is True
    overlay = [r for r in records if r.get("pseudo") and r.get("state") == "sticky-modal"]
    assert {r["pseudo"] for r in overlay} == {"hover", "focus"}
    assert all(os.path.exists(r["screenshot"]) for r in overlay)  # shot before close
    assert not any(r.get("close_failed") for r in overlay), f"close_failed leaked: {overlay}"
    # post-reload base sheets still capture (stale forced-node ids must not throw):
    base = [r for r in records if r.get("pseudo") and not r.get("state")]
    assert {r["pseudo"] for r in base} == {"hover", "focus"}
    assert all("error" not in r for r in base), f"reload poisoned base sheets: {base}"
    print("\u2705")


def test_open_failure_no_overlay_sheets():
    print("  test_open_failure_no_overlay_sheets...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(OPEN_FAIL_STATE_PAGE)
    ghost = [r for r in records if r.get("state") == "ghost-modal"]
    assert len(ghost) == 1 and "error" in ghost[0], f"expected only the open-failure: {ghost}"
    assert not any(r.get("pseudo") for r in ghost)
    print("\u2705")


def test_bp_underscore_no_filename_collision():
    print("  test_bp_underscore_no_filename_collision...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    records = _sheet_records(MODAL_SHEET_PAGE, '[["desk_top", [1280, 800]]]')
    shots = [r["screenshot"] for r in records if r.get("screenshot")]
    assert len(shots) == len(set(shots)), f"filename collision: {shots}"
    assert all(r["breakpoint"] == "desk_top" for r in records)  # records keep the raw name
    print("\u2705")


def test_stateless_pages_add_no_records():
    print("  test_stateless_pages_add_no_records...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return
    result = _run_capture_bp(UNTAGGED_PAGE, '[["desktop", [1280, 800]]]')
    records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
    assert len(records) == 1 and "state" not in records[0]
    print("\u2705")


if __name__ == "__main__":
    print("\n=== Capture Views Tests ===")
    test_tagged_page_captures_views_and_reports_errors()
    test_untagged_page_yields_default_record()
    test_breakpoints_argv_matrix()
    test_hamburger_hidden_nav_still_activates()
    test_untagged_page_with_modal_captures_state()
    test_broken_toggle_reload_fallback()
    test_state_ownership_scopes_to_view()
    test_duplicate_state_triggers_error_record()
    test_duplicate_view_triggers_tolerated()
    test_colliding_view_panels_error_record()
    test_orphan_view_panel_error_record()
    test_offcanvas_duplicate_trigger_not_preferred()
    test_errored_view_reports_owned_states()
    test_viewport_failure_reports_states()
    test_stateless_pages_add_no_records()
    test_sheets_captured_with_offenders()
    test_transitioned_ring_not_flagged()
    test_animated_suppressed_ring_flagged()
    test_no_interactive_no_sheets()
    test_reload_fallback_then_sheets()
    test_errored_view_reports_sheets()
    test_overlay_sheets_scoped_both_directions()
    test_autofocused_element_not_flagged()
    test_text_only_panel_no_sheet_cells()
    test_broken_toggle_sheets_then_clean_captures()
    test_open_failure_no_overlay_sheets()
    test_bp_underscore_no_filename_collision()
    print("\nAll tests passed \u2705")
