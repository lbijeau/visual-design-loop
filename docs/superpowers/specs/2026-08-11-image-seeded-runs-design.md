# Image-seeded design runs

**Date:** 2026-08-11
**Status:** approved, not yet implemented
**Revision:** 2 — revised after a code-grounded review; see *Review corrections* at the end.

## Problem

A run is seeded entirely by a text intent. Everything visual — palette, typography, layout, density — is invented by the brain from that sentence, so two runs of the same intent can produce unrelated-looking pages and there is no way to say "like this."

Users already have the reference in hand: a screenshot of a page they like, a Figma export, a competitor's landing page. This adds a second seed input alongside the intent.

## What the reference controls

| Controls | Does not control |
|---|---|
| `theme.json` tokens — palette, type classification, radius, spacing density | The audit. The `eyes` role and its rubric are untouched. |
| Initial layout — hierarchy, section order, density, component vocabulary | Content and copy. The intent alone decides what is on the page. |
| | Iterations after the first. The reference is dropped after seeding. |

The reference is a **starting point**, not a target. Convergence is still judged against the intent by the existing audit, so a run cannot be scored on fidelity to the reference and cannot fail for drifting from it.

Fidelity is **structural cues**, not reproduction: a reference of a SaaS pricing page plus an intent about a dog-walking service yields a dog-walking page laid out like that pricing page. This keeps the feature robust when reference and intent describe different kinds of product, which is the common case.

**Resemblance is expected to decay.** After seeding, the audit judges only the intent and every refine call attaches the *current render* (`loop.py:1184`), so refinement pressure points away from the reference from iteration 1 onward. This is the intended trade for not making the reference a rubric, but it must be stated plainly or users will read the feature as "make it look like this" and be surprised by iteration 3.

## Non-goals

- **Web shell support.** v1 is CLI-only; `/start` keeps its intent-only contract. The shell can follow once the prompts are proven.
- **Text-only brain models.** A vision-capable brain is required. There is no transcription fallback that turns the reference into a text brief.
- **Multi-image calls.** `_build_messages` keeps its single image slot (`llm_client.py:96`).
- **A reference-strength dial.** One fidelity level, tuned in the prompt.
- **Reference fidelity scoring.** Deliberately excluded; see the table above.
- **Resuming an image-seeded run.** `--reference` requires a fresh run; see §1.

## Architecture

The current seed path:

```
intent ──▶ generate_theme (brain, text)          ──▶ theme.json
       ──▶ generate_initial_code (brain, text)   ──▶ current_render.html
           └─ then: capture ──▶ audit (eyes) ──▶ refine (brain, image = current render)
```

With a reference, the two seed steps each receive the image in their existing single slot. The real startup sequence, which the implementation must preserve:

```
provider wizard  ──▶ preflight_roles  ──▶ capture_reference.js  ──▶ preflight_vision
   (loop.py:1105)     (loop.py:1112)      screenshots/reference_<ts>.png    │
                                                                            ▼
                                              intent wait (loop.py:1119) ──▶ generate_theme
                                                                        └──▶ generate_initial_code
```

`preflight_roles` runs *inside* the fresh branch of `run()`, after the provider wizard — on a first run `providers.json` does not exist until the wizard completes, so neither preflight can happen at process startup. Capture is placed before `preflight_vision` only because the probe needs no reference; either order works, but the sequence must be pinned so the implementer does not put Playwright work behind an intent wait.

Everything after `generate_initial_code` is unchanged.

### Why both seed steps

Passing the reference only to `generate_initial_code` would leave `theme.json` derived from the intent text, so `_inject_theme` (`loop.py:464`) would force a generic palette onto a reference-shaped layout and the two halves would fight. Fusing theme and HTML into one call would dissolve the `theme.json` boundary that `update_theme`, `validate_theme`, the hard/soft token contract and the shell all depend on. Two calls, one per existing step, preserves both.

The cost is **two image-bearing calls where there are zero today** — roughly 1.6k image tokens each at 1280px wide / `deviceScaleFactor: 1`, negligible beside a full-file HTML generation, and bounded by the height clamp in §2.

## Components

### 1. `--reference` (orchestrator.py)

One new argument accepting a local path or an `http(s)` URL. Dispatch happens once, at the edge, and the distinction dies there: everything downstream knows only a PNG path.

`parse_args` stays filesystem-free, as it is today (`orchestrator.py:18`). It validates only the *shape* of the value — URL or not — and the file's existence and decodability are established by `capture_reference.js` in the startup sequence. There is no format allowlist: Chromium renders webp, gif and avif into the normalized PNG just as happily as PNG and JPEG, so restricting the input would be arbitrary.

**`--reference` requires a fresh run.** Seeding only ever executes in the fresh branch of `run()`, and `_restore_run` refuses to resume unless a completed iteration exists (`loop.py:1262`) — which can only be true once seeding is long over. A resumed run therefore *cannot* consume a reference. So:

- `--reference` with `--resume` is rejected by argparse.
- `--reference` with an unfinished run and no `--resume`/`--fresh` skips the interactive `decide_resume` prompt (`orchestrator.py:39`) and proceeds fresh, printing that it is doing so.

`--reference` does not require `intent` on the command line. Capture and the probe both run before the intent wait, so a run started with `--reference` and no intent captures the reference and then waits for the intent from the shell as it does today.

### 2. `capture_reference.js` (new)

A small script, separate from `capture.js`. `capture.js` injects `data-v-id` attributes and the red coordinate-grid overlay for the auditor's benefit; a reference shot must have neither, and that file's complexity is already load-bearing.

It normalizes **both** input kinds to one bounded PNG at 1280px wide, `deviceScaleFactor: 1`.

**URL.** Navigate with `load`, then a fixed settle delay. **Not `networkidle`** — analytics beacons, websockets and long-polling keep the network busy indefinitely on most real marketing pages, so `networkidle` would time out and the abort rule below would turn a perfectly renderable page into a hard failure. `networkidle` may be awaited best-effort with a short timeout, but never as the gate.

**Height policy.** `fullPage: true`, clamped: capture at most `REFERENCE_MAX_HEIGHT` (2400px, three viewports) and downscale the result to keep it within the token budget. A viewport-only 1280×800 shot would defeat the feature's own purpose — section order *is* the below-the-fold content, and the structural prompt in §4 asks for section order. Unbounded `fullPage` would defeat the bounded-token rationale. The clamp is the only way to hold both.

**Local image.** Not `page.goto("file://…png")` — that renders Chromium's *image document*, which centers the image on a theme-dependent letterbox background, flattens transparency onto it, and would feed that letterbox to palette extraction. Instead an HTML shim: explicit white background, `img { max-width: 100%; height: auto; display: block }` so an image narrower than 1280 is **never upscaled** into blur, and the same height clamp applies.

Routing user files through the same script buys a guaranteed PNG whatever the user supplied, bounded dimensions so a 4K screenshot cannot quietly cost ~8k image tokens, and no new Python imaging dependency.

Output: `screenshots/reference_<ts>.png`, path printed on stdout. Nothing else globs that directory in a way this collides with — `generate_showcase.py:68` matches only `shell_capture*`, and `report.py` embeds paths recorded in events. The corollary is that the reference will *not* appear in the run report unless it is deliberately recorded as an event; v1 does not.

### 3. `preflight_vision(provider_config, role="brain")` (llm_client.py)

Called from the startup sequence **only when `--reference` is supplied**, so runs without a reference pay nothing.

**It must probe every candidate in the brain chain, not just the primary.** `call_llm` walks `[providerId] + fallbackOrder` and falls through to a legacy env provider on any exception (`llm_client.py:317`). Probing only the primary would leave a transient error during `generate_theme` free to reroute the image-bearing call to an unprobed, possibly blind fallback — reintroducing precisely the silent-ignore failure the probe exists to prevent. Every candidate must pass, or the run aborts. This keeps `call_llm`'s fallback semantics untouched, which the alternative (pinning seed calls to the probed provider) would not.

The probe must issue its own transport call against a resolved `(provider, model_id)`, the way `check_model_liveness` does — **not** go through `call_llm`, whose fallback chain could otherwise let the probe "pass" on a different provider than the seed calls later use.

**What failure looks like differs by dialect**, and the probe must treat all of these as *cannot see* rather than as transport trouble:

| Dialect | Behaviour with an image and no vision support |
|---|---|
| Ollama, no mmproj | Usually silent — the image is dropped and the model answers from the prompt alone. Some versions emit an in-stream error, which `_consume_stream_line` normalizes to `ConnectionError` (`llm_client.py:154`) and `_do_api_call` then treats as retriable (`llm_client.py:269`) — 3 retries of backoff before concluding anything. The probe must use a low retry count. |
| OpenAI proper | 400 on image content to a text model. |
| llama.cpp server | 500, "image input is not supported". |
| vLLM | errors |

So the spec's earlier blanket claim that the failure is "usually silent" was only true for Ollama. The probe still earns its place by making all four cases produce one clear, early, uniform abort.

**The probe image** is a committed PNG showing large, high-contrast three digits — *not* `123`, `000`, or any sequence a text-only model would produce as its lazy guess. Digits must be large enough that a small local vision model does not fail OCR on them; a false negative aborts a valid run, which is worse than the 1-in-1000 false pass. Answer matching is lenient: extract digits from the reply, so "The number is 427" passes.

A static committed PNG rather than a generated one keeps this free of Pillow or hand-rolled zlib.

**No caching.** The earlier claim that `check_model_liveness` caches per `(provider_id, model_id)` was false — it caches nothing (`llm_client.py:411`); the only cache is a dict created and discarded inside each `preflight_roles` call (`llm_client.py:464`). Since `preflight_vision` runs once per run for one role, caching would be meaningless.

**Cloud providers are probed, unlike liveness.** `check_model_liveness` deliberately declines to probe cloud builtins because there is no cheap check (`llm_client.py:452`). `preflight_vision` spends a real completion on them anyway. That is an intentional departure: a silently-ignored reference costs the user a whole run, which is worth far more than one small call.

On failure the run aborts before any generation with:

```
brain model '<id>' (provider '<pid>') cannot see images — the reference would be silently ignored
```

### 4. Prompt changes (loop.py)

**`generate_theme`** — with a reference, add a paragraph instructing the model to derive `hard_tokens` and `soft_tokens` from the colors and type actually present in the image rather than inventing a palette. The JSON contract is unchanged.

The paragraph must state one limitation outright: a model cannot identify a *typeface* from a screenshot, only classify it ("geometric sans", "transitional serif"). `primary_font` is therefore a best-match web-safe or Google font for the observed classification. Implying the model can name the real font invites confident wrong answers.

**`generate_initial_code`** — with a reference, add a `STRUCTURAL REFERENCE` paragraph: take hierarchy, section order, density and component vocabulary; the intent decides what content exists; do not lift text, logos or imagery from the reference.

It must also carry an explicit precedence rule. The prompt already says *"You MUST use these tokens... Do not use any arbitrary hex colors"*, and the model is now looking at an image full of hexes. Because the theme was derived from the same image the two should agree, but where they disagree **the tokens win**. Without that sentence the model re-samples colors from the reference and bypasses the token contract that the `validate_theme` tripwire rests on.

Both paragraphs appear only when a reference is present. Reference-less runs produce byte-identical prompts to today.

### 5. Theme fallbacks must not silently discard the reference

This is the load-bearing change the first revision missed, and it is not optional.

Two existing paths swallow theme failure:

- `generate_theme` catches a JSON parse error and writes a hardcoded black / white / `#3b82f6` theme (`loop.py:379`).
- `run()` catches any theme-generation exception and continues with `theme_json = {}`, printing "Proceeding without design tokens" (`loop.py:1133`).

Both are reasonable when the theme is the model's own invention. With a reference, either one discards the image-derived palette and lets `_inject_theme` force a generic palette onto a reference-shaped layout — the "two halves fight" failure that *Why both seed steps* says the whole design exists to prevent.

**With a reference present, both paths abort the run instead.** Without a reference, both keep today's behaviour exactly.

### 6. Run metadata (run_state.py)

The reference PNG path and the original `--reference` value are recorded for provenance, so a finished run's `run.json` says what it was seeded from.

The record must live in `_run_meta()` (`loop.py:714`), not be written once. `run.json` is rewritten wholesale from `_run_meta()` at six call sites (`loop.py:940, 982, 1127, 1137, 1228`), so a one-off `save_run` would be erased by the next one.

There is deliberately **no re-capture-on-resume logic**. A resumed run cannot re-run seeding (§1), so re-fetching a reference for it would be dead code.

## Error handling

One rule: **a reference that was asked for and could not be used aborts the run, before or during seeding, with the specific reason.** Never warn-and-continue — silently designing without the reference is the failure the probe exists to prevent, so the same principle must cover acquisition *and* consumption or the guarantee has a hole in it.

| Failure | Behaviour |
|---|---|
| URL unreachable, times out, or blocks headless Chrome | abort, reporting the URL and the error |
| Local file missing or not decodable by Chromium | abort when `capture_reference.js` runs |
| `capture_reference.js` exits non-zero or writes no file | abort |
| Vision probe fails for any brain candidate | abort with the message in §3 |
| Theme JSON unparseable | abort (overrides the `loop.py:379` fallback) |
| Theme generation raises | abort (overrides the `loop.py:1133` fallback) |
| `--reference` with `--resume` | rejected by argparse |
| `--reference` with an unfinished run | proceeds fresh, printing that it skipped resume |

## Testing

| What | Where | Shape |
|---|---|---|
| `--reference` dispatch: URL vs path vs missing; `--resume` rejection | `test_cli.py` | pure argument parsing, no network, no filesystem |
| `capture_reference.js` contract | new file, mirroring `test_capture_views.py` | a local `file://` page as the "URL", plus small PNGs for the image path; assert output exists, respects the width cap and the height clamp, and does **not** upscale a 400px-wide input |
| `preflight_vision`: pass, fail, every-candidate coverage, no `call_llm` recursion | `test_llm_client.py` | stubbed transport, no model |
| Prompt assembly and threading | `test_theme_loop.py` | the `STRUCTURAL REFERENCE` paragraph and precedence sentence appear only with a reference; `image_path` reaches both seed calls; reference-less prompts unchanged |
| Theme-fallback abort with a reference, fallback preserved without one | `test_theme_loop.py` | both `loop.py:379` and `loop.py:1133` paths |
| Reference survives repeated `save_run` | `test_run_state.py` | write, re-save from `_run_meta()`, assert still present |

Explicitly untested: whether the output actually resembles the reference. That is model quality, not a contract, and a test asserting it would be flaky by construction.

## Known trade-offs

- **Image-derived hard tokens are hard to correct.** `update_theme` presents `hard_tokens` as immutable (`loop.py:398`). Today those are the model's own invention; with a reference they are a vision model's *reading* of someone's screenshot, which will sometimes be wrong — a dark-mode letterbox, JPEG artifacts, a mis-sampled gradient. A user saying "the brand color should be teal" hits a prompt that forbids changing it. v1 accepts this; softening hard-token immutability for image-seeded runs is the obvious follow-up.
- **References accumulate.** Nothing cleans `screenshots/`, so reference PNGs pile up beside captures. Same as today's behaviour for captures; not made worse, not fixed.

## Deferred

Each was considered and consciously left out; none is blocked by this design.

- Web shell upload, including paste-from-clipboard.
- Reference as an audit rubric, which would require the `eyes` role to compare two images.
- Multi-image refine calls keeping the reference in play past iteration 0 — the fix for the resemblance decay noted above.
- A `--reference-strength loose|structural|close` dial.
- Softening `hard_tokens` immutability for image-seeded runs.

## Review corrections

Revision 2 incorporates a code-grounded review. Corrected factual claims: `check_model_liveness` caches nothing; `save_run` rewrites wholesale from `_run_meta()`; resume cannot re-run seeding, making the original re-capture table unreachable; `preflight_roles` runs after the provider wizard rather than at process startup; a text-only model's failure is silent only in the Ollama dialect. Corrected design holes: the theme-fallback paths (§5), the fallback-chain coverage of the probe (§3), the viewport-versus-`fullPage` contradiction and the `networkidle` gate (§2), the image-document letterbox and upscaling in local-image normalization (§2), and the `--reference`/resume interaction (§1).
