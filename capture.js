const { chromium } = require("playwright");
const path = require("path");

async function capture(filePath) {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    // 1, not 2: these screenshots are consumed by the vision auditor, not by a human.
    // At 2x a 1280px layout is sent as a 2880x1800 image = 4608 image tokens and ~47 s
    // of prefill per audit cell; at 1x it is 1608 tokens and ~9.7 s (measured 2026-08-09,
    // Qwen3.6-35B-A3B + mmproj). The CSS geometry is identical — only Retina pixel
    // density is discarded. capture_shell.js stays at 2x for the human-facing preview.
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();

  const absolutePath = path.resolve(filePath);
  await page.goto(`file://${absolutePath}`);

  async function setupPage() {
    // 1. Inject Semantic Anchors (data-v-id) for the visual audit to reference
    await page.evaluate(() => {
      const allElements = document.querySelectorAll("*");
      allElements.forEach((el, index) => {
        if (el.tagName === "HTML" || el.tagName === "BODY") return;
        el.setAttribute("data-v-id", index);
      });
    });

    // 2. Inject Coordinate Grid Overlay
    await page.addStyleTag({
      content: `
        body::after {
          content: "";
          position: fixed;
          top: 0;
          left: 0;
          width: 100vw;
          height: 100vh;
          pointer-events: none;
          z-index: 2147483647;
          background-image:
            linear-gradient(to right, rgba(0, 0, 0, 0.15) 1px, transparent 1px),
            linear-gradient(to bottom, rgba(0, 0, 0, 0.15) 1px, transparent 1px);
          background-size: 10% 10%;
        }
      `,
    });

    await page.evaluate(() => {
      const overlay = document.createElement("div");
      overlay.id = "__vd_grid_overlay";
      overlay.style.position = "fixed";
      overlay.style.top = "0";
      overlay.style.left = "0";
      overlay.style.width = "100vw";
      overlay.style.height = "100vh";
      overlay.style.pointerEvents = "none";
      overlay.style.zIndex = "2147483647";
      overlay.style.color = "red";
      overlay.style.fontSize = "12px";
      overlay.style.fontFamily = "monospace";
      overlay.style.fontWeight = "bold";

      const cols = "ABCDEFGHIJ".split("");
      const rows = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"];

      cols.forEach((char, i) => {
        const label = document.createElement("div");
        label.textContent = char;
        label.style.position = "absolute";
        label.style.left = `${i * 10 + 5}%`;
        label.style.top = "2px";
        overlay.appendChild(label);
      });

      rows.forEach((num, i) => {
        const label = document.createElement("div");
        label.textContent = num;
        label.style.position = "absolute";
        label.style.left = "2px";
        label.style.top = `calc(${i * 10}% + 2px)`;
        overlay.appendChild(label);
      });

      document.body.appendChild(overlay);
    });
  }
  await setupPage();

  // 3. Discover views once (querySelectorAll sees hidden elements), then
  //    capture every view at every breakpoint.
  const viewIds = await page.evaluate(() =>
    Array.from(document.querySelectorAll("[data-view]")).map((el) => el.getAttribute("data-view")),
  );

  // 4. Discover declared UI states (modals, drawers) and their view ownership.
  const states = await page.evaluate(() =>
    Array.from(document.querySelectorAll("[data-state]")).map((el) => ({
      id: el.getAttribute("data-state"),
      owner: el.closest("[data-view-panel]") ? el.closest("[data-view-panel]").getAttribute("data-view-panel") : null,
    })),
  );
  const defaultView = viewIds.length ? viewIds[0] : "default";
  const duplicateViewIds = new Set(viewIds.filter((id, i) => viewIds.indexOf(id) !== i));
  const duplicateStateIds = new Set(states.map((s) => s.id).filter((id, i, ids) => ids.indexOf(id) !== i));

  const bpSlug = (bp) => String(bp).replace(/_/g, "-");

  const HOVER_SELECTOR =
    "a[href], button, input, select, textarea, summary, " +
    '[tabindex]:not([tabindex="-1"]), [role="button"], [role="link"], [role="tab"], ' +
    '[role="menuitem"], [onclick], [data-view], [data-state]';
  const FOCUS_SELECTOR =
    "a[href], button:not([disabled]), input:not([disabled]), " +
    'select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
  const SHEET_NODE_CAP = 150;
  const hasInteractive = await page.evaluate((sel) => !!document.querySelector(sel), HOVER_SELECTOR);

  let cdp = null;
  async function cdpSession() {
    if (!cdp) {
      cdp = await context.newCDPSession(page);
      await cdp.send("DOM.enable");
      await cdp.send("CSS.enable");
    }
    return cdp;
  }

  async function sheetElements(selector, rootSelector) {
    return page.evaluate(
      ([sel, rootSel, cap]) => {
        const root = rootSel ? document.querySelector(rootSel) : document;
        if (!root) return [];
        const out = [];
        for (const el of root.querySelectorAll(sel)) {
          const r = el.getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) continue;
          const text = (el.textContent || el.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 40);
          const vid = el.getAttribute("data-v-id");
          out.push({ vid, label: `${el.tagName.toLowerCase()} "${text}" [v${vid}]` });
          if (out.length >= cap) break;
        }
        return out;
      },
      [selector, rootSelector, SHEET_NODE_CAP],
    );
  }

  let trackedForced = [];
  async function forceNodes(vids, pseudoClasses) {
    const session = await cdpSession();
    const { root } = await session.send("DOM.getDocument");
    const ids = [];
    for (const vid of vids) {
      const { nodeId } = await session.send("DOM.querySelector", {
        nodeId: root.nodeId,
        selector: `[data-v-id="${vid}"]`,
      });
      if (nodeId) ids.push(nodeId);
    }
    for (const nodeId of ids) {
      await session.send("CSS.forcePseudoState", { nodeId, forcedPseudoClasses: pseudoClasses });
      trackedForced.push(nodeId);
    }
    return ids;
  }

  // Exception-safe per node: a mid-list failure must not leave later nodes forced,
  // and stale nodeIds (post-reload) must not throw.
  async function clearForcing(nodeIds) {
    const session = await cdpSession();
    for (const nodeId of nodeIds) {
      try {
        await session.send("CSS.forcePseudoState", { nodeId, forcedPseudoClasses: [] });
      } catch (err) {
        /* stale node after reload — nothing left to clear */
      }
    }
    trackedForced = trackedForced.filter((id) => !nodeIds.includes(id));
  }

  // Clean slate at the start of every sheet phase: an upstream leak must never
  // poison a later focus-gate baseline.
  async function defensiveClearForcing() {
    const stale = trackedForced;
    trackedForced = [];
    await clearForcing(stale);
  }

  async function styleSnapshots(vids) {
    return page.evaluate((ids) => {
      const ser = (el, pseudo) => {
        const cs = getComputedStyle(el, pseudo);
        let s = "";
        for (const p of cs) {
          if (p === "outline-offset") continue; // UA default changes with :focus-visible even when outline:none
          s += p + ":" + cs.getPropertyValue(p) + ";";
        }
        return s;
      };
      const out = {};
      for (const vid of ids) {
        const el = document.querySelector(`[data-v-id="${vid}"]`);
        out[vid] = el ? ser(el, null) + "|" + ser(el, "::before") + "|" + ser(el, "::after") : null;
      }
      return out;
    }, vids);
  }

  const FREEZE_CSS =
    "*, *::before, *::after { animation: none !important; transition: none !important; }" +
    "#__vd_grid_overlay { display: none !important; } body::after { display: none !important; }";

  async function injectFreeze() {
    const tag = await page.addStyleTag({ content: FREEZE_CSS });
    await page.waitForTimeout(100); // frozen values snap; give layout one beat
    return tag;
  }

  async function removeFreeze(tag) {
    try {
      await tag.evaluate((el) => el.remove());
    } catch (err) {
      /* handle is stale after a reload — the tag died with the page */
    }
  }

  // One hover+focus sheet pair. state=null -> document scope (base sheets);
  // state set -> panel scope. Assumes the freeze tag is ALREADY injected by
  // the caller (base wrapper or the state loop) so enumeration sees settled
  // geometry. Never throws: failures become error records.
  async function captureSheetPair(view, bp, state) {
    const rootSel = state ? `[data-state-panel="${state}"]` : null;
    const stateField = state ? { state } : {};
    const nameMid = state ? `${bpSlug(bp)}_${state}` : bpSlug(bp);
    await defensiveClearForcing();
    const hoverEls = await sheetElements(HOVER_SELECTOR, rootSel);
    if (!hoverEls.length) return;
    let ids = [];
    try {
      ids = await forceNodes(
        hoverEls.map((e) => e.vid),
        ["hover"],
      );
      await page.waitForTimeout(300);
      const record = {
        id: view,
        breakpoint: bp,
        ...stateField,
        pseudo: "hover",
        screenshot: await shoot(`${view}_${nameMid}_pseudo-hover`),
      };
      if (hoverEls.length >= SHEET_NODE_CAP) record.truncated = SHEET_NODE_CAP;
      results.push(record);
    } catch (err) {
      results.push({ id: view, breakpoint: bp, ...stateField, pseudo: "hover", error: String(err).slice(0, 200) });
    } finally {
      await clearForcing(ids);
    }
    ids = [];
    try {
      const activeVid = await page.evaluate(() =>
        document.activeElement && document.activeElement.getAttribute
          ? document.activeElement.getAttribute("data-v-id")
          : null,
      );
      // The activeElement's baseline already contains :focus styling (e.g.
      // an autofocused modal input) — a clean diff is unobtainable, so it is
      // excluded from the gate, never blurred (blur can fire handlers).
      const focusEls = (await sheetElements(FOCUS_SELECTOR, rootSel)).filter((e) => e.vid !== activeVid);
      const vids = focusEls.map((e) => e.vid);
      const before = await styleSnapshots(vids);
      ids = await forceNodes(vids, ["focus", "focus-visible"]);
      await page.waitForTimeout(300);
      const after = await styleSnapshots(vids);
      const offenders = focusEls
        .filter((e) => before[e.vid] !== null && before[e.vid] === after[e.vid])
        .map((e) => e.label);
      const record = {
        id: view,
        breakpoint: bp,
        ...stateField,
        pseudo: "focus",
        screenshot: await shoot(`${view}_${nameMid}_pseudo-focus`),
      };
      if (offenders.length) record.missing_focus = offenders;
      results.push(record);
    } catch (err) {
      results.push({ id: view, breakpoint: bp, ...stateField, pseudo: "focus", error: String(err).slice(0, 200) });
    } finally {
      await clearForcing(ids);
    }
  }

  function pushSheetErrors(view, bp, reason) {
    if (!hasInteractive) return;
    for (const p of ["hover", "focus"]) {
      results.push({ id: view, breakpoint: bp, pseudo: p, error: reason });
    }
  }

  async function captureSheets(view, bp) {
    const freeze = await injectFreeze();
    try {
      await captureSheetPair(view, bp, null);
    } finally {
      await removeFreeze(freeze);
    }
  }

  const ts = Date.now();
  const results = [];

  async function shoot(name) {
    const safe = name.replace(/[^a-z0-9-_]/gi, "_");
    const p = path.join(__dirname, "screenshots", `capture_${ts}_${safe}.png`);
    await page.screenshot({ path: p, fullPage: true });
    return p;
  }

  // Views and states share one trigger/panel contract; kind is 'view' or
  // 'state'. IDs are kebab-validated before reaching these selectors.
  async function clickTrigger(kind, id) {
    await page.evaluate(
      ([k, i]) => {
        const all = Array.from(document.querySelectorAll(`[data-${k}="${i}"]`));
        if (!all.length) throw new Error(`no [data-${k}] trigger found`);
        // With duplicates (desktop nav + mobile menu + footer), prefer a trigger that is
        // actually laid out at this breakpoint; fall back to the first in DOM order so
        // behaviour is unchanged for the single-trigger case.
        const visible = all.find((el) => {
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        });
        (visible || all[0]).click();
      },
      [kind, id],
    );
    await page.waitForTimeout(300);
  }

  async function panelVisible(kind, id) {
    return page.evaluate(
      ([k, i]) => {
        const panel = document.querySelector(`[data-${k}-panel="${i}"]`);
        if (!panel) return false;
        const r = panel.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      },
      [kind, id],
    );
  }

  function idError(kind, id, dupSet) {
    if (!/^[a-z0-9-]+$/.test(id)) return `invalid ${kind} id (must be lowercase-kebab)`;
    if (dupSet.has(id)) return `duplicate ${kind} id (multiple [data-${kind}] triggers share it)`;
    return null;
  }

  function ownedStates(view) {
    const owned = states.filter((s) => (s.owner || defaultView) === view);
    const seen = new Set();
    return owned.filter((s) => !seen.has(s.id) && seen.add(s.id)); // one entry per id per cell
  }

  // A view that can't be captured must not silently drop its states: error
  // records keep them in the cell matrix so convergence stays blocked with
  // the real reason.
  function pushStateErrors(view, bp, reason) {
    for (const st of ownedStates(view)) {
      results.push({ id: view, breakpoint: bp, state: st.id, error: reason });
    }
  }

  async function captureStates(view, bp) {
    for (const st of ownedStates(view)) {
      const err = idError("state", st.id, duplicateStateIds);
      if (err) {
        results.push({ id: view, breakpoint: bp, state: st.id, error: err });
        continue;
      }
      try {
        await clickTrigger("state", st.id); // open
        if (!(await panelVisible("state", st.id))) {
          results.push({
            id: view,
            breakpoint: bp,
            state: st.id,
            error: "state panel not visible after trigger click",
          });
          continue;
        }
        const shotPath = await shoot(`${view}_${bpSlug(bp)}_${st.id}`);
        const record = { id: view, breakpoint: bp, state: st.id, screenshot: shotPath };
        // Freeze BEFORE enumerating (entry transitions leave controls at
        // zero-rect) and hold it through the close-verification (removing it
        // earlier can restart the entry animation under the close settle).
        const freeze = await injectFreeze();
        try {
          await captureSheetPair(view, bp, st.id);
          await clickTrigger("state", st.id); // close (toggle)
          if (await panelVisible("state", st.id)) {
            record.close_failed = true; // toggle contract broken
          }
        } finally {
          await removeFreeze(freeze);
        }
        results.push(record); // before the fallback: a reload failure must not eat the good record
        if (record.close_failed) {
          // Keep the good shot; reload to restore a clean page for later cells.
          try {
            await page.reload();
            await page.waitForTimeout(300);
            await setupPage();
            if (viewIds.length) await clickTrigger("view", view);
          } catch (err) {
            // Page is in an unknown state; later cells will surface it as
            // their own error records (convergence stays blocked).
          }
        }
      } catch (err) {
        results.push({ id: view, breakpoint: bp, state: st.id, error: String(err).slice(0, 200) });
      }
    }
  }

  for (const [bp, [width, height]] of breakpoints) {
    try {
      await page.setViewportSize({ width, height });
      // Settle after resize too — untagged pages have no activation wait,
      // and fonts/Play-CDN observer work may lag the synchronous reflow.
      await page.waitForTimeout(300);
    } catch (err) {
      const ids = viewIds.length ? Array.from(new Set(viewIds)) : ["default"];
      for (const id of ids) {
        const reason = `viewport failed: ${String(err).slice(0, 150)}`;
        results.push({ id, breakpoint: bp, error: reason });
        pushStateErrors(id, bp, reason);
        pushSheetErrors(id, bp, reason);
      }
      continue;
    }

    if (viewIds.length === 0) {
      results.push({ id: "default", breakpoint: bp, screenshot: await shoot(`default_${bpSlug(bp)}`) });
      await captureStates("default", bp);
      await captureSheets("default", bp);
      continue;
    }
    const seenViews = new Set();
    for (const id of viewIds) {
      if (seenViews.has(id)) continue; // one record per id per bp
      seenViews.add(id);
      // Duplicate [data-view] triggers are TOLERATED for views (warn, don't fail).
      // A responsive page naturally repeats a nav link in the desktop bar, the mobile
      // menu, and the footer — which the generation prompt's own RESPONSIVE REQUIREMENT
      // encourages — so "exactly one trigger per view" is routinely violated and used to
      // abort the entire run ("No views captured"). Duplicates were never a functional
      // problem: clickTrigger acts on a single element, and now picks the one actually
      // laid out at this breakpoint.
      // Invalid (non-kebab) ids are still fatal. States keep the strict check.
      if (duplicateViewIds.has(id)) {
        console.warn(`warn: duplicate [data-view="${id}"] triggers; using the first visible one`);
      }
      const err = idError("view", id, new Set());
      if (err) {
        results.push({ id, breakpoint: bp, error: err });
        pushStateErrors(id, bp, `owning view '${id}' not captured: ${err}`);
        pushSheetErrors(id, bp, `owning view '${id}' not captured: ${err}`);
        continue;
      }
      try {
        // Programmatic click: fires the same handlers whether or not the
        // trigger is visible (hamburger navs hide it at mobile widths).
        await clickTrigger("view", id);
        if (!(await panelVisible("view", id))) {
          results.push({ id, breakpoint: bp, error: "panel not visible after activation" });
          pushStateErrors(id, bp, `owning view '${id}' not captured: panel not visible after activation`);
          pushSheetErrors(id, bp, `owning view '${id}' not captured: panel not visible after activation`);
          continue;
        }
        results.push({ id, breakpoint: bp, screenshot: await shoot(`${id}_${bpSlug(bp)}`) });
        await captureStates(id, bp);
        await captureSheets(id, bp);
      } catch (err2) {
        results.push({ id, breakpoint: bp, error: String(err2).slice(0, 200) });
        pushStateErrors(id, bp, `owning view '${id}' not captured: ${String(err2).slice(0, 150)}`);
        pushSheetErrors(id, bp, `owning view '${id}' not captured: ${String(err2).slice(0, 150)}`);
      }
    }
  }

  await browser.close();
  return results;
}

const filePath = process.argv[2];
if (!filePath) {
  console.error("Please provide an HTML file path");
  process.exit(1);
}
let breakpoints;
try {
  breakpoints = process.argv[3] ? JSON.parse(process.argv[3]) : [["desktop", [1280, 800]]];
} catch (err) {
  console.error(`Invalid breakpoints argument: ${err}`);
  process.exit(1);
}

capture(filePath)
  .then((results) => {
    if (!results.some((r) => r.screenshot)) {
      console.error("No views captured.");
      process.exit(1);
    }
    console.log(JSON.stringify({ views: results }));
  })
  .catch((err) => {
    console.error(String(err));
    process.exit(1);
  });
