const { chromium } = require("playwright");
const path = require("path");

async function captureShell(url) {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();

  await page.goto(url);

  // Wait for the iframe to load and the summary text to be present
  try {
    await page.waitForSelector("#summary-text", { timeout: 5000 });
  } catch (e) {
    console.log("Warning: #summary-text not found within 5s, taking screenshot anyway.");
  }

  const screenshotsDir = path.resolve(__dirname, "screenshots");
  const screenshotPath = path.join(screenshotsDir, `shell_capture_${Date.now()}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true });

  await browser.close();
  return screenshotPath;
}

const targetUrl = process.argv[2];
if (!targetUrl) {
  console.error("Please provide the server URL");
  process.exit(1);
}

captureShell(targetUrl)
  .then((path) => console.log(`Shell screenshot saved to: ${path}`))
  .catch((err) => console.error(err));
