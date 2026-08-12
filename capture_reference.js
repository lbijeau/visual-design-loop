const { chromium } = require("playwright");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");

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

// Not page.goto("file://…png"): that renders Chromium's image document, which
// centers the image on a theme-dependent letterbox, flattens transparency onto
// it, and would feed that letterbox to palette extraction. The shim fixes the
// background, and max-width:100% scales a large image down while leaving a small
// one at its own size rather than upscaling it into blur.
async function shootImage(page, filePath) {
  const href = pathToFileURL(path.resolve(filePath)).href;
  // setContent creates an about:blank/data page whose origin blocks file://
  // subresources, so the image would never load. Write the shim to a temp file
  // and navigate to it: a file:// page may load file:// images.
  const shim = path.join(os.tmpdir(), `ref-shim-${Date.now()}.html`);
  fs.writeFileSync(
    shim,
    `<!doctype html><html><body style="margin:0;background:#ffffff">` +
      `<img id="ref" src="${href}" style="display:block;max-width:100%;height:auto">` +
      `</body></html>`,
  );
  try {
    await page.goto(pathToFileURL(shim).href);
    await page.waitForFunction(
      () => {
        const img = document.getElementById("ref");
        return img && img.complete && img.naturalWidth > 0;
      },
      null,
      { timeout: 15000 },
    );
  } catch (err) {
    // Keep the underlying reason: a waitForFunction timeout on a large-but-valid
    // image reads very differently from a genuinely corrupt file, and collapsing
    // both into "could not decode" sends the user after the wrong problem.
    throw new Error(`could not load image ${filePath}: ${err.message}`);
  } finally {
    fs.unlinkSync(shim);
  }
}

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
    if (full > MAX_HEIGHT) {
      // The seed prompt asks the model to follow the reference's section order, so a
      // silent truncation would hand it a partial page while claiming otherwise.
      console.error(
        `warn: reference is ${full}px tall; seeding from the top ${MAX_HEIGHT}px only ` +
          `(everything below is not sent to the model)`,
      );
    }
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
