# Image-seeded design runs

**Date:** 2026-08-11
**Status:** approved, not yet implemented

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

## Non-goals

- **Web shell support.** v1 is CLI-only; `/start` keeps its intent-only contract. The shell can follow once the prompts are proven.
- **Text-only brain models.** A vision-capable brain is required. There is no transcription fallback that turns the reference into a text brief.
- **Multi-image calls.** `_build_messages` keeps its single image slot.
- **A reference-strength dial.** One fidelity level, tuned in the prompt.
- **Reference fidelity scoring.** Deliberately excluded; see the table above.

## Architecture

The current seed path:

```
intent ──▶ generate_theme (brain, text)          ──▶ theme.json
       ──▶ generate_initial_code (brain, text)   ──▶ current_render.html
           └─ then: capture ──▶ audit (eyes) ──▶ refine (brain, image = current render)
```

With a reference, the two seed steps each receive the image in their existing single slot:

```
--reference <path|url>
        │
        ▼
capture_reference.js ──▶ screenshots/reference_<ts>.png ──┬──▶ generate_theme
        │                          │                      └──▶ generate_initial_code
        │                          └─ path recorded in run meta (save_run)
        ▼
   preflight_vision(brain)  ── fails ──▶ abort before any generation
```

Everything after `generate_initial_code` is unchanged.

### Why both seed steps

Passing the reference only to `generate_initial_code` would leave `theme.json` derived from the intent text, so `_inject_theme` would force a generic palette onto a reference-shaped layout and the two halves would fight. Fusing theme and HTML into one call would dissolve the `theme.json` boundary that `update_theme`, `validate_theme`, the hard/soft token contract and the shell all depend on. Two calls, one per existing step, preserves both.

The added cost is one extra image call per run — roughly 1.6k image tokens at the current 1280px / `deviceScaleFactor: 1` setting, negligible beside a full-file HTML generation.

## Components

### 1. `--reference` (orchestrator.py)

One new argument accepting a local path or an `http(s)` URL. Dispatch happens once, at the edge, and the distinction dies there: everything downstream knows only a PNG path.

`--reference` does not require `intent` on the command line. Capture and the vision probe both run at startup, independent of the intent, so a run started with `--reference` and no intent captures the reference immediately and then waits for the intent to arrive from the shell as it does today. This needs no extra guard.

### 2. `capture_reference.js` (new)

A small script, separate from `capture.js`. `capture.js` injects `data-v-id` attributes and the red coordinate-grid overlay for the auditor's benefit; a reference shot must have neither, and that file's complexity is already load-bearing.

It normalizes **both** input kinds to one bounded PNG:

- **URL** — navigate, wait for network idle, screenshot at 1280×800, `deviceScaleFactor: 1`.
- **Local image** — open the image in a page scaled to fit 1280px wide, screenshot that.

Routing user files through the same script buys a guaranteed PNG whatever the user supplied, bounded dimensions so a 4K screenshot cannot quietly cost ~8k image tokens, and no new Python imaging dependency.

Output: `screenshots/reference_<ts>.png`, path printed on stdout.

### 3. `preflight_vision(provider_config, role="brain")` (llm_client.py)

Called from the existing preflight path **only when `--reference` is supplied**, so runs without a reference pay nothing.

A text-only model handed an image usually does not error — Ollama drops the image and the model invents a design from the prompt alone. The failure is silent, so the check must be an active capability probe rather than error handling.

The probe sends a committed ~1 KB PNG showing a three-digit number and asks for digits only. A model that can see answers correctly; one that cannot is guessing at 1-in-1000. A static committed PNG rather than a generated one keeps this free of Pillow or hand-rolled zlib. The result is cached per `(provider_id, model_id)` exactly as `check_model_liveness` already caches liveness.

On mismatch the run aborts with:

```
brain model '<id>' cannot see images — the reference would be silently ignored
```

### 4. Prompt changes (loop.py)

**`generate_theme`** — with a reference, add a paragraph instructing the model to derive `hard_tokens` and `soft_tokens` from the colors and type actually present in the image rather than inventing a palette. The JSON contract is unchanged.

The paragraph must state one limitation outright: a model cannot identify a *typeface* from a screenshot, only classify it ("geometric sans", "transitional serif"). `primary_font` is therefore a best-match web-safe or Google font for the observed classification. Implying the model can name the real font invites confident wrong answers.

**`generate_initial_code`** — with a reference, add a `STRUCTURAL REFERENCE` paragraph: take hierarchy, section order, density and component vocabulary; the intent decides what content exists; do not lift text, logos or imagery from the reference.

It must also carry an explicit precedence rule. The prompt already says *"You MUST use these tokens... Do not use any arbitrary hex colors"*, and the model is now looking at an image full of hexes. Because the theme was derived from the same image the two should agree, but where they disagree **the tokens win**. Without that sentence the model re-samples colors from the reference and bypasses the token contract that the `validate_theme` tripwire rests on.

Both paragraphs appear only when a reference is present. Reference-less runs produce byte-identical prompts to today.

### 5. Run metadata (run_state.py)

The reference PNG path and the original `--reference` value are recorded via `save_run`, so `--resume` reuses the captured PNG rather than re-fetching. A live URL may have changed between runs, and a resumed run that silently re-designs against different pixels would be a bad surprise.

## Error handling

One rule, applied everywhere: **a reference that was asked for and could not be obtained aborts the run, before any generation, with the specific reason.**

| Failure | Behaviour |
|---|---|
| URL unreachable, times out, or blocks headless Chrome | abort, reporting the URL and the error |
| Local file missing, unreadable, or not a PNG/JPEG | abort at argument parsing |
| `capture_reference.js` exits non-zero or writes no file | abort |
| Vision probe fails | abort with the message above |
| Resume: recorded PNG missing, original was a URL | re-capture |
| Resume: recorded PNG missing, original was a file | abort |

Never warn-and-continue. Silently designing without the reference is precisely the failure the probe exists to prevent, so the same principle has to cover the input side or the guarantee has a hole in it.

## Testing

| What | Where | Shape |
|---|---|---|
| `--reference` dispatch: URL vs path vs missing vs non-image | `test_cli.py` | pure argument parsing, no network |
| `capture_reference.js` contract | new file, mirroring `test_capture_views.py` | a local `file://` page as the "URL", plus a small PNG for the image path; assert the output exists and respects the width cap |
| `preflight_vision` pass, fail, and caching | `test_llm_client.py` | stubbed transport, no model |
| Prompt assembly and threading | `test_theme_loop.py` | the `STRUCTURAL REFERENCE` paragraph and precedence sentence appear only with a reference; `image_path` reaches both seed calls |
| Resume with a reference | `test_resume.py` | recorded path survives; missing file re-captures or aborts per the table above |

Explicitly untested: whether the output actually resembles the reference. That is model quality, not a contract, and a test asserting it would be flaky by construction.

## Deferred

Each of these was considered and consciously left out; none is blocked by this design.

- Web shell upload, including paste-from-clipboard.
- Reference as an audit rubric, which would require the `eyes` role to compare two images.
- Multi-image refine calls keeping the reference in play past iteration 0.
- A `--reference-strength loose|structural|close` dial.
