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
