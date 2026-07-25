"""Shell regression test: design_shell.html's JS must boot without errors,
render the polled status, and deliver button clicks to the action queue.
Skipped when node/playwright/chromium are unavailable."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from server import LivePreviewServer

NODE_SCRIPT = """
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(process.argv[2]);
  await page.waitForTimeout(1500);
  const statusText = await page.textContent('#status-text');
  const acceptDisabled = await page.$eval('#accept-btn', b => b.disabled);
  const undoDisabled = await page.$eval('#undo-btn', b => b.disabled);
  const chips = await page.$$eval('.view-chip', els => els.map(e => e.textContent));
  let clicked = false;
  if (!acceptDisabled) { await page.click('#accept-btn'); clicked = true; await page.waitForTimeout(300); }
  console.log(JSON.stringify({ errors, statusText, acceptDisabled, undoDisabled, clicked, chips }));
  await browser.close();
})();
"""


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


def test_shell_boots_and_buttons_work():
    print("  test_shell_boots_and_buttons_work...", end=" ")
    if not chromium_available():
        print("SKIPPED (node/playwright not available)")
        return

    server = LivePreviewServer()
    server.status.set_phase("awaiting-feedback", "Review iteration 2")
    server.status.set_can_undo(True)
    server.status.set_audit(
        {
            "overall_score": 55,
            "bugs": [],
            "summary": "[settings@mobile] rough",
            "views": {"dashboard@desktop": {"score": 90, "bugs": 0}, "settings@mobile": {"score": 55, "bugs": 1}},
        }
    )
    server.start()

    # The script must live inside the project dir: node resolves require()
    # relative to the script file, and playwright is in the project node_modules.
    script = tempfile.NamedTemporaryFile(suffix=".js", delete=False, mode="w", dir=str(config.PROJECT_DIR))
    script.write(NODE_SCRIPT)
    script.close()
    try:
        result = subprocess.run(
            ["node", script.name, f"http://localhost:{server.port}/design_shell.html"],
            capture_output=True,
            text=True,
            cwd=str(config.PROJECT_DIR),
            timeout=60,
        )
        assert result.returncode == 0, f"node runner failed: {result.stderr}"
        data = json.loads(result.stdout.strip().splitlines()[-1])
        # The Tailwind CDN script fails when offline — environment noise, not a shell bug.
        errors = [e for e in data["errors"] if "tailwind" not in e.lower()]
        assert errors == [], f"page JS errors: {errors}"
        assert data["statusText"] == "Ready for feedback", f"updateStatus never ran (status text: {data['statusText']!r})"
        assert data["acceptDisabled"] is False, "Accept button should be enabled while awaiting feedback"
        assert data["undoDisabled"] is False, "Undo button should be enabled when can_undo is true"
        assert data["clicked"] is True
        assert any("dashboard@desktop" in c for c in data["chips"]), f"chips: {data['chips']}"
        assert any("settings@mobile" in c and "55" in c for c in data["chips"])
        msg = server.status.get_message(timeout=2)
        assert msg == {"type": "accept"}, f"accept click did not reach the queue: {msg}"
    finally:
        os.remove(script.name)
        server.stop()
    print("✅")


if __name__ == "__main__":
    print("\n=== Shell Regression Tests ===")
    test_shell_boots_and_buttons_work()
    print("\nAll tests passed ✅")
