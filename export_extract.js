// Harvest the Tailwind Play CDN's compiled CSS from a rendered page.
// Usage: node export_extract.js <html-file>
// Prints one JSON line: {"tailwindCss": "...", "stylesheetHrefs": [{"attr","url"}]}
// Exit codes: 0 ok, 1 error, 2 no compiled CSS found (e.g. CDN unreachable).
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

(async () => {
  const filePath = process.argv[2];
  if (!filePath) {
    console.error("Usage: node export_extract.js <html-file>");
    process.exit(1);
  }
  const source = fs.readFileSync(filePath, "utf8");

  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(`file://${path.resolve(filePath)}`);

  // Wait up to 10s for the Play CDN's compiled <style>: it must be absent
  // from the page source AND contain the compiler's --tw- custom properties.
  // Author-written <style> tags satisfy neither condition.
  let harvest = null;
  for (let i = 0; i < 20 && !harvest; i++) {
    harvest = await page.evaluate((src) => {
      const styles = Array.from(document.querySelectorAll("style"));
      const generated = styles.find(
        (s) => s.textContent.includes("--tw-") && !src.includes(s.textContent.slice(0, 200)),
      );
      if (!generated || !generated.textContent.trim()) return null;
      const links = Array.from(document.querySelectorAll('link[rel="stylesheet"]')).filter((l) =>
        /^https?:/.test(l.href),
      );
      return {
        tailwindCss: generated.textContent,
        stylesheetHrefs: links.map((l) => ({ attr: l.getAttribute("href"), url: l.href })),
      };
    }, source);
    if (!harvest) await page.waitForTimeout(500);
  }
  await browser.close();

  if (!harvest) {
    console.error("No compiled Tailwind CSS found (CDN unreachable or page has no Tailwind classes).");
    process.exit(2);
  }
  console.log(JSON.stringify(harvest));
})().catch((err) => {
  console.error(String(err));
  process.exit(1);
});
