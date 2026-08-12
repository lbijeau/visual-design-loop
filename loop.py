"""The design loop engine: theme gen, code gen, render, capture, audit, refine."""

import json
import os
import re
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import diff_edit
import export
import llm_client
import report
import visual_audit
from provider_config import ProviderConfig
from run_state import RunState
from status import LoopStatus

BRAIN_SYSTEM_PROMPT = "You are an expert frontend developer specializing in Tailwind CSS."
THEME_SYSTEM_PROMPT = "You are a world-class Design Systems Engineer."
KEYWORD_ACTIONS = {"DONE": "accept", "KEEP": "accept", "UNDO": "undo", "SAVE": "save"}
RESERVED_AXIS_WORDS = {"hover", "focus"}
SYNTHETIC_TAGS = {"close_failed", "missing_focus"}

# Wall-clock ceiling for calls that emit a whole HTML file. Sized for slow local
# models: a multi-view app runs ~60k chars, which a ~70 chars/s endpoint streams
# in ~14 min. llm_client.IDLE_TIMEOUT still aborts a genuinely stalled stream.
FULL_FILE_TIMEOUT = 1800

DIFF_INSTRUCTION = """\
Output ONLY SEARCH/REPLACE edit blocks for the changes needed to fix the reported issues — nothing else, no full file, no prose.
Each block has this exact form:
<<<<<<< SEARCH
(text copied VERBATIM from CURRENT CODE, exact whitespace/indentation, with enough surrounding lines to be UNIQUE in the file)
=======
(the replacement text)
>>>>>>> REPLACE
Example:
<<<<<<< SEARCH
    <h1 class="text-xl">Welcome</h1>
=======
    <h1 class="text-2xl font-bold">Welcome</h1>
>>>>>>> REPLACE
Emit only the blocks required. Do not include markdown code fences."""


def is_base_cell(cell: str) -> bool:
    """True for a plain view@breakpoint cell (no state overlay or pseudo sheet)."""
    return "+" not in cell and "#" not in cell


def default_shot(records) -> str:
    """The default view's screenshot; falls back to the first captured view.
    None only when nothing was captured (render_and_capture raises first)."""
    if records and records[0].get("screenshot"):
        return records[0]["screenshot"]
    return next((r["screenshot"] for r in records if r.get("screenshot")), None)


def record_cell(r) -> str:
    return cell_key(r["id"], r.get("breakpoint", config.DEFAULT_BREAKPOINT), r.get("state"), r.get("pseudo"))


def match_views(feedback, view_ids) -> list:
    """View IDs the feedback plausibly targets: the full ID or any of its
    hyphen-separated tokens appears as a whole word, case-insensitive.
    Reserved axis words (hover, focus) are excluded from split-token
    matching — a bare 'focus' token never matches a 'focus-mode' view."""
    if not feedback:
        return []
    words = set(re.findall(r"[a-z0-9-]+", feedback.lower()))
    matched = []
    for vid in view_ids:
        tokens = {vid} | (set(vid.split("-")) - RESERVED_AXIS_WORDS)
        if tokens & words:
            matched.append(vid)
    return matched


def cell_key(vid: str, bp: str, state: str = None, pseudo: str = None) -> str:
    base = f"{vid}@{bp}"
    if state:
        base = f"{base}+{state}"
    return f"{base}#{pseudo}" if pseudo else base


def split_cell(cell: str):
    vid, _, rest = cell.partition("@")
    rest, _, pseudo = rest.partition("#")
    bp, _, state = rest.partition("+")
    return vid, (bp or config.DEFAULT_BREAKPOINT), (state or None), (pseudo or None)


def match_breakpoints(feedback, bp_names) -> list:
    """Breakpoint names the feedback targets, via BREAKPOINT_KEYWORDS
    whole-word matching ('phone' -> mobile)."""
    if not feedback:
        return []
    words = set(re.findall(r"[a-z0-9-]+", feedback.lower()))
    matched = []
    for name in bp_names:
        keywords = {kw for kw, target in config.BREAKPOINT_KEYWORDS.items() if target == name}
        if keywords & words:
            matched.append(name)
    return matched


def match_states(feedback, state_ids) -> list:
    """State IDs the feedback targets — same hyphen-token rule as views,
    except tokens that are breakpoint keywords never count: 'mobile' in
    'the mobile layout' targets the breakpoint, not a 'mobile-menu' state
    (which would silently drop the base cells the user meant). The full id
    always matches."""
    if not feedback:
        return []
    words = set(re.findall(r"[a-z0-9-]+", feedback.lower()))
    plain = words - set(config.BREAKPOINT_KEYWORDS) - RESERVED_AXIS_WORDS
    return [sid for sid in state_ids if sid in words or set(sid.split("-")) & plain]


def match_pseudo(feedback, pseudo_names) -> list:
    """Pseudo axes the feedback targets. Bare 'hover' matches; 'focus' only
    beside focus vocabulary — bare 'focus' is usually the imperative verb
    ('focus on the header') and a false match would repeatedly attach the
    wrong screenshot to refines."""
    if not feedback:
        return []
    text = feedback.lower()
    words = set(re.findall(r"[a-z0-9-]+", text))
    matched = []
    for name in pseudo_names:
        if name == "hover" and "hover" in words:
            matched.append(name)
        elif name == "focus" and re.search(r"focus[- ](?:ring|rings|indicator|indicators|state|states|outline)|:focus", text):
            matched.append(name)
    return matched


def match_cells(feedback, cells) -> list:
    """Filter captured cell keys by four independently-matched axes.
    Axis separation is load-bearing: matchers see bare IDs, never cell keys.
    [] = global (caller audits everything). View+state double-match -> UNION.
    A pseudo match narrows to sheet cells (of the matched-or-all views).
    Output preserves capture order."""
    parsed = [(c,) + split_cell(c) for c in cells]
    view_ids = list(dict.fromkeys(p[1] for p in parsed))
    bp_names = list(dict.fromkeys(p[2] for p in parsed))
    state_ids = list(dict.fromkeys(p[3] for p in parsed if p[3]))
    pseudo_names = list(dict.fromkeys(p[4] for p in parsed if p[4]))
    mv = match_views(feedback, view_ids)
    mb = match_breakpoints(feedback, bp_names)
    ms = match_states(feedback, state_ids)
    mp = match_pseudo(feedback, pseudo_names)
    if not mv and not mb and not ms and not mp:
        return []
    vs = set(mv) if mv else set(view_ids)
    bs = set(mb) if mb else set(bp_names)
    out = []
    for cell, vid, bp, state, pseudo in parsed:
        if bp not in bs:
            continue
        if mp and ms:
            take = pseudo in set(mp) and state in ms  # the named overlay's sheets only
        elif mp:
            take = pseudo in set(mp) and vid in vs  # sheets (base+overlay) of matched-or-all views
        elif not ms:
            take = vid in vs  # bases + states + sheets of the view
        elif not mv:
            take = state in ms  # the state's cells, sheets included
        else:
            take = vid in vs or state in ms  # union on double-match (vs == set(mv) here)
        if take:
            out.append(cell)
    return out


def _looks_like_document(text: str) -> bool:
    """True if a block-less response is a full HTML document worth salvaging."""
    low = text.lower()
    return "<!doctype" in low or "<html" in low


def _is_audit_unavailable(audit: dict) -> bool:
    """True if *audit* is a failed-audit sentinel: score 0 with an 'unavailable' summary.

    Both producers in ``_audit_one_cell`` (bad-JSON and eyes-provider-error) emit this
    shape. Centralizing the check keeps the theme-validation guard from silently drifting
    if either summary is reworded.
    """
    return audit.get("overall_score", 0) == 0 and "unavailable" in str(audit.get("summary", "")).lower()


def _model_label(provider_config, role: str = "brain") -> str:
    """Status label ``'providerId/modelId'`` for *role*. Display-only: a stale/removed
    provider id yields ``'providerId/?'`` instead of raising — ``call_llm`` still runs such a
    config via its legacy env fallback, so the label must not crash the run (pre-flight handles
    real liveness separately).
    """
    pid = provider_config.get_role(role)["providerId"]
    try:
        model = provider_config.get_model_id(provider_config.resolve(pid))
        return f"{pid}/{model}"
    except (KeyError, FileNotFoundError):
        return f"{pid}/?"


def composite_audit(view_audits: dict, view_order: list) -> dict:
    """Fold per-view audits into the single-audit shape consumers expect:
    min score, worst view's bugs/summary ([view]-prefixed when >1), views map."""
    worst_id, worst = None, None
    views = {}
    for vid in view_order:
        entry = view_audits.get(vid)
        if not entry:
            continue
        a = entry["audit"]
        views[vid] = {"score": a.get("overall_score", 0), "bugs": len(a.get("bugs", []))}
        if any(b.get("synthetic") == "close_failed" for b in a.get("bugs", [])):
            views[vid]["close_failed"] = True  # shell chip must not show 'pass'
        if worst is None or a.get("overall_score", 0) < worst.get("overall_score", 0):
            worst_id, worst = vid, a
    if worst is None:
        return {"overall_score": 0, "bugs": [], "summary": "No views audited", "views": {}}
    out = dict(worst)
    if len(views) > 1:
        out["summary"] = f"[{worst_id}] {worst.get('summary', '')}"
    out["views"] = views
    return out


def close_failed_bug(state: str) -> dict:
    """Toggle-contract violation. Tagged so _audit_passes can refuse the
    >=95 escape — a converged run must never ship a broken toggle."""
    return {
        "severity": "important",
        "synthetic": "close_failed",
        "issue": f"state '{state}' trigger does not close the overlay when clicked again (toggle contract)",
        "location": "?",
    }


def synthetic_failure_audit(reason: str, label: str) -> dict:
    """Convergence-blocking audit for a cell that could not be activated."""
    return {
        "overall_score": 0,
        "bugs": [{"severity": "critical", "issue": f"{label} failed to activate: {reason}", "location": "?"}],
        "summary": f"{label} failed to activate: {reason}",
    }


def missing_focus_bug(label: str) -> dict:
    """Suppressed focus styling on one element. Deterministic (capture-side
    computed-style diff); allowlisted in SYNTHETIC_TAGS so it blocks the
    >=95 escape."""
    return {
        "severity": "important",
        "synthetic": "missing_focus",
        "issue": f"interactive element {label} shows no visible focus indicator (:focus-visible)",
        "location": "?",
    }


def aggregate_missing_focus_bug(labels) -> dict:
    """One composite-level bug for N offenders — the refine prompt gets a
    single actionable line instead of N competing ones."""
    shown = ", ".join(labels[:3])
    more = f" (+{len(labels) - 3} more)" if len(labels) > 3 else ""
    return {
        "severity": "important",
        "synthetic": "missing_focus",
        "issue": f"{len(labels)} interactive element(s) lack a visible focus indicator: {shown}{more}",
        "location": "?",
    }


class FrontendDesignLoop:
    """The design loop engine.  Manages theme generation, code generation,
    rendering, capture, audit, and refinement."""

    def __init__(self, intent: str = None, max_iterations: int = None, status: LoopStatus = None, reference: str = None):
        self.intent = intent
        self.reference = reference  # raw --reference value: a path or a URL
        self.reference_png = None  # normalized PNG, set by _acquire_reference
        self.max_iterations = max_iterations or config.MAX_ITERATIONS
        self.status = status or LoopStatus()
        self.current_code = ""
        self.theme_json = {}
        self.history = []
        self.iteration = 0
        self.provider_config = ProviderConfig()
        self.run_state = RunState()
        self.undo_pointer = 0
        self._last_img = None
        self._capture_records = []
        self.view_audits = {}
        self._retry_feedback = None

    def bootstrap(self):
        """Create placeholder render/version files to prevent 404s before the first render."""
        with open(config.RENDER_PATH, "w") as f:
            f.write(
                "<html><body style='display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;'><h1>Generating first draft...</h1></body>"
            )
            f.write(self._get_reload_script())
            f.write("</html>")
        with open(config.VERSION_PATH, "w") as f:
            f.write("0")

    def _acquire_reference(self):
        """Normalize --reference into one bounded PNG under screenshots/.

        Raises on every failure path: a reference that was asked for and cannot be
        used must stop the run, never degrade it to a reference-less design.
        """
        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        out = config.SCREENSHOT_DIR / f"reference_{int(time.time())}.png"
        source = self.reference
        if not re.match(r"^https?://", source, re.IGNORECASE):
            # The subprocess runs with cwd=PROJECT_DIR, so a relative path would
            # otherwise resolve against the project rather than the user's shell.
            source = os.path.abspath(os.path.expanduser(source))
            if not os.path.exists(source):
                raise Exception(f"reference file not found: {source}")
        result = subprocess.run(
            ["node", str(config.REFERENCE_SCRIPT), source, str(out)],
            capture_output=True,
            text=True,
            cwd=str(config.PROJECT_DIR),
            timeout=180,
        )
        if result.returncode != 0 or not out.exists():
            raise Exception(f"reference capture failed: {result.stderr.strip()[:300]}")
        self.reference_png = str(out)
        return self.reference_png

    def _get_reload_script(self):
        return """
        <script>
            (function() {
                let lastVersion = null;
                async function checkVersion() {
                    try {
                        const res = await fetch('/version.txt');
                        const version = await res.text();
                        if (lastVersion && version !== lastVersion) {
                            window.location.reload();
                        }
                        lastVersion = version;
                    } catch (e) {}
                }
                setInterval(checkVersion, 500);
            })();
        </script>
        """

    def generate_theme(self):
        print("\n[0/4] Generating design theme...")
        reference_block = ""
        if self.reference_png:
            reference_block = """
        REFERENCE IMAGE: The attached image is a design reference. Derive the tokens from the colors and typography actually present in it — sample its real palette instead of inventing one.
        You cannot identify a typeface from a screenshot. classify it instead ("geometric sans", "humanist sans", "transitional serif", "slab serif", "monospace") and set primary_font to the closest widely-available web font for that classification.
        """
        prompt = f"""
        Create a professional design theme for a website with the following intent: {self.intent}.{reference_block}

        Return ONLY a JSON object with the following structure:
        {{
          "hard_tokens": {{
            "brand_primary": "hex color",
            "brand_secondary": "hex color",
            "primary_font": "font-family name"
          }},
          "soft_tokens": {{
            "accent_color": "hex color",
            "border_radius": "px or rem",
            "spacing_unit": "px or rem"
          }},
          "tailwind_config": {{
            "theme": {{
              "extend": {{
                "colors": {{
                  "brand": {{
                    "primary": "hex color",
                    "secondary": "hex color"
                  }},
                  "accent": "hex color"
                }},
                "borderRadius": {{
                  "theme": "px or rem"
                }},
                "spacing": {{
                  "theme": "px or rem"
                }}
              }}
            }}
          }}
        }}

        Ensure colors are high-contrast and professional. Do not include any markdown blocks.
        """
        theme_str = llm_client.call_llm(
            "brain",
            prompt,
            system_prompt=THEME_SYSTEM_PROMPT,
            image_path=self.reference_png,
            provider_config=self.provider_config,
        )
        theme_str = theme_str.replace("```json", "").replace("```", "").strip()

        try:
            self.theme_json = json.loads(theme_str)
            with open(config.THEME_PATH, "w") as f:
                json.dump(self.theme_json, f, indent=2)
            print(f"✅ Theme saved to {config.THEME_PATH}")
        except json.JSONDecodeError as e:
            if self.reference_png:
                raise Exception(
                    f"theme JSON unparseable ({e}) — refusing to fall back to a generic palette "
                    "on an image-seeded run, which would put a generic palette on a reference-shaped layout"
                )
            print(f"❌ Failed to parse theme JSON: {e}")
            self.theme_json = {
                "hard_tokens": {"brand_primary": "#000000", "brand_secondary": "#ffffff", "primary_font": "sans-serif"},
                "soft_tokens": {"accent_color": "#3b82f6", "border_radius": "4px", "spacing_unit": "4px"},
                "tailwind_config": {
                    "theme": {"extend": {"colors": {"brand": {"primary": "#000000", "secondary": "#ffffff"}, "accent": "#3b82f6"}}}
                },
            }
            with open(config.THEME_PATH, "w") as f:
                json.dump(self.theme_json, f, indent=2)

        return self.theme_json

    def update_theme(self, feedback: str):
        print(f"\n[0.3/4] Updating theme tokens based on feedback: {feedback}")
        soft_tokens = self.theme_json.get("soft_tokens", {})
        hard_tokens = self.theme_json.get("hard_tokens", {})

        prompt = f"""
        Update the design theme based on user feedback: "{feedback}"

        CURRENT HARD TOKENS (Immutable):
        {json.dumps(hard_tokens, indent=2)}

        CURRENT SOFT TOKENS (Can be modified):
        {json.dumps(soft_tokens, indent=2)}

        Update the soft tokens and the corresponding tailwind_config.
        Return ONLY a JSON object with the full theme structure (hard_tokens, soft_tokens, tailwind_config).
        Do not include any markdown blocks.
        """
        theme_str = llm_client.call_llm("brain", prompt, system_prompt=THEME_SYSTEM_PROMPT, provider_config=self.provider_config)
        theme_str = theme_str.replace("```json", "").replace("```", "").strip()

        try:
            self.theme_json = json.loads(theme_str)
            with open(config.THEME_PATH, "w") as f:
                json.dump(self.theme_json, f, indent=2)
            print(f"✅ Theme updated and saved to {config.THEME_PATH}")
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse updated theme JSON: {e}")

        return self.theme_json

    def validate_theme(self, gate_cell=None):
        """Render + audit ONE cell for the theme-change tripwire. Never
        writes status. gate_cell None -> default view @ default breakpoint."""
        print("🔍 Validating theme visually...")
        records = self.render_and_capture()
        shots = {record_cell(r): r["screenshot"] for r in records if r.get("screenshot")}
        view_ids = list(dict.fromkeys(r["id"] for r in records))
        if gate_cell is None or gate_cell not in shots:
            gate_cell = record_cell(records[0]) if records[0].get("screenshot") else next(iter(shots))
        return self._audit_one_cell(gate_cell, shots[gate_cell], view_ids, len(records))

    def generate_initial_code(self):
        print("\n[1/4] Generating initial draft...")
        theme_context = f"""
        MANDATORY DESIGN TOKENS:
        {json.dumps(self.theme_json, indent=2)}

        You MUST use these tokens for all colors, fonts, and spacing.
        Do not use any arbitrary hex colors or spacing values.
        """
        structural_reference = ""
        if self.reference_png:
            structural_reference = """
        STRUCTURAL REFERENCE: The attached image is a layout reference. Follow its visual hierarchy, section order, information density and component vocabulary. The INTENT alone decides what content exists — do not copy text, logos, imagery or subject matter from the reference.
        Where colors in the reference disagree with the MANDATORY DESIGN TOKENS above, the tokens win. Never sample a hex value out of the image.
        """
        prompt = f"""
        {theme_context}{structural_reference}

        MULTI-VIEW CONVENTION: If the design needs multiple views/screens (e.g. an app with sidebar navigation), keep everything in this single file with ALL views' full content present in the static markup. Tag each navigation trigger with data-view="<kebab-id>" and its content container with data-view-panel="<same-id>". Hide inactive panels with the hidden attribute and toggle visibility on click with a few lines of inline JavaScript. Each view id must name exactly ONE data-view-panel, and every data-view-panel must have at least one matching data-view trigger. A view may be reached from several triggers (the same link in the desktop nav, the mobile menu and the footer) — that is fine; two different screens sharing one id is not. Do not build panel content at runtime and do not add utility classes from JavaScript. Single-view pages must not use these attributes.

        RESPONSIVE REQUIREMENT: The page must be responsive. Use Tailwind responsive utilities (sm:/md:/lg:) so the layout renders well at mobile (375px), tablet (768px), and desktop widths.

        STATE CONVENTION: If the design includes modals, drawers, or overlays, tag each trigger with data-state="<kebab-id>" and its overlay container with data-state-panel="<same-id>". The SAME element that opens the overlay must also close it when clicked again (toggle); a separate close button may exist in addition, but the trigger must toggle. Each state id must be unique: exactly ONE trigger element per overlay. Close buttons inside the panel must NOT have a data-state attribute — use onclick directly instead. Keep overlay content in the static markup, hidden with the hidden attribute. Designs without overlays must not use these attributes.

        INTERACTION STYLES: Every interactive element (links, buttons, inputs, view/state triggers) must show a clearly visible :focus-visible indicator — a high-contrast ring or outline; never remove focus outlines without an equally visible replacement. Hover states must stay readable: text and background must never converge on hover.

        Create a modern, professional frontend page based on this intent: {self.intent}.
        Use Tailwind CSS via CDN: include <script src="https://cdn.tailwindcss.com"></script> in the <head>, exactly that URL.
        Output ONLY the complete HTML file content. Do not include markdown blocks.
        """
        self.current_code = llm_client.call_llm(
            "brain",
            prompt,
            system_prompt=BRAIN_SYSTEM_PROMPT,
            image_path=self.reference_png,
            timeout=FULL_FILE_TIMEOUT,
            provider_config=self.provider_config,
        )
        self.current_code = self.current_code.replace("```html", "").replace("```", "").strip()

    def _inject_theme(self, html: str, include_reload: bool = True) -> str:
        """Inject the theme's tailwind.config (and optionally the reload script)."""
        tailwind_config = self.theme_json.get("tailwind_config", {})
        config_script = f"""
        <script>
            tailwind.config = {json.dumps(tailwind_config)};
        </script>
        """
        if "</head>" in html:
            html = html.replace("</head>", f"{config_script}</head>")
        elif "<body>" in html:
            html = html.replace("<body>", f"{config_script}<body>")
        else:
            html = config_script + html
        if include_reload:
            reload_script = self._get_reload_script()
            if "</body>" in html:
                html = html.replace("</body>", f"{reload_script}</body>")
            else:
                html += reload_script
        return html

    def render_preview(self):
        """Write theme-injected current_code to the live preview and bump the
        reload version. No screenshot — undo and resume use this directly."""
        with open(config.RENDER_PATH, "w") as f:
            f.write(self._inject_theme(self.current_code))
        with open(config.VERSION_PATH, "w") as f:
            f.write(str(int(time.time())))

    def render_and_capture(self) -> list:
        """Render the preview and capture every discovered view at every breakpoint.
        Returns capture records in DOM order (first = default view @ default breakpoint):
        [{"id", "breakpoint", "screenshot"} | {"id", "breakpoint", "error"}].
        Raises on zero captures."""
        print("[2/4] Rendering and capturing...")
        self.render_preview()

        bp_arg = json.dumps([[name, list(dims)] for name, dims in config.BREAKPOINTS])
        result = subprocess.run(
            ["node", str(config.CAPTURE_SCRIPT), str(config.RENDER_PATH), bp_arg],
            capture_output=True,
            text=True,
            cwd=str(config.PROJECT_DIR),
        )
        if result.returncode != 0:
            raise Exception(f"Capture failed: {result.stderr}")
        try:
            records = json.loads(result.stdout.strip().splitlines()[-1])["views"]
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            raise Exception(f"Capture output unparseable: {e}")
        for r in records:
            if r.get("screenshot"):
                r["screenshot"] = os.path.abspath(r["screenshot"])
        if not any(r.get("screenshot") for r in records):
            raise Exception("Capture produced no view screenshots")
        return records

    def perform_audit(self, image_path: str) -> dict:
        print("[3/4] Performing visual audit...")
        audit = visual_audit.perform_visual_audit(image_path, self.intent)
        self.status.set_audit(audit)
        return audit

    def refine_code(self, audit_report: dict, image_path: str = None, human_feedback: str = None, view_label: str = None):  # noqa: C901  (diff/fallback routing; refactor deferred)
        print("[4/4] Refining code with visual grounding...")
        previous_score = audit_report.get("overall_score", 0)

        # Detect if feedback is actually a theme change request
        theme_keywords = [
            "color",
            "colour",
            "font",
            "spacing",
            "radius",
            "theme",
            "blue",
            "red",
            "green",
            "yellow",
            "darker",
            "lighter",
            "accent",
            "palette",
        ]
        theme_change_detected_match = human_feedback and re.search(
            r"\b(" + "|".join(theme_keywords) + r")s?\b", human_feedback, re.IGNORECASE
        )
        if theme_change_detected_match:
            print("🎨 Theme change detected in feedback. Updating design tokens...")
            self.update_theme(human_feedback)

            # START VALIDATION LOOP
            view_ids = list(dict.fromkeys(r["id"] for r in self._capture_records))
            gate_matches = match_views(human_feedback, view_ids)
            gate_vid = gate_matches[0] if len(gate_matches) == 1 else (view_ids[0] if view_ids else None)
            gate_cell = cell_key(gate_vid, config.DEFAULT_BREAKPOINT) if gate_vid else None
            prior = self.view_audits.get(gate_cell)
            previous_score = prior["audit"].get("overall_score", 0) if prior else previous_score

            attempts = 0
            max_attempts = 3
            validated = False

            while attempts < max_attempts:
                attempts += 1
                print(f"  - Validation attempt {attempts}/{max_attempts}...")

                current_audit = self.validate_theme(gate_cell)
                current_score = current_audit.get("overall_score", 0)
                critical_bugs = [b for b in current_audit.get("bugs", []) if str(b.get("severity", "")).lower() == "critical"]

                score_drop = previous_score - current_score

                # A failed-audit sentinel (score 0, "...unavailable...") is not a real
                # regression — don't trigger a spurious token correction over it. This
                # matters since retries=1 turns a one-off audit timeout into this sentinel.
                if _is_audit_unavailable(current_audit):
                    print("  ⚠️ Theme-validation audit unavailable — proceeding with current tokens.")
                    validated = True
                    break
                if score_drop > 15 or critical_bugs:
                    print(f"  ⚠️ Regression found! Score drop: {score_drop}, Critical bugs: {len(critical_bugs)}")
                    correction_prompt = f"""
                    The recent token update caused a visual regression:
                    {current_audit.get("summary", "No summary available")}

                    Please adjust the tokens to fix these issues while maintaining the user's original request: "{human_feedback}"
                    """
                    self.update_theme(correction_prompt)
                else:
                    print(f"  ✅ Theme validated. Score: {current_score} (Drop: {score_drop})")
                    validated = True
                    break

            if not validated:
                print("  ❌ Theme failed to stabilize after max attempts. Proceeding with last known version.")
            # END VALIDATION LOOP

        visual_summary = audit_report.get("summary", "")
        pivot_active = visual_summary in self.history
        if pivot_active:
            print("⚠️ Visual Oscillation Detected! Triggering Strategic Pivot...")
            pivot_instruction = "The previous approach to fixing these issues is causing the design to oscillate. Please try a completely different layout strategy."
        else:
            pivot_instruction = "Refine the code to fix the reported issues. Maintain all working parts of the design."

        self.history.append(visual_summary)

        feedback_section = f"\nUSER FEEDBACK: {human_feedback}" if human_feedback else ""

        if image_path:
            visual_context = "I have attached the current screenshot of the page. Please analyze it carefully and update the HTML to resolve the bugs in the report."
        else:
            visual_context = "I am refining based purely on the textual bug report. Maintain high-quality layout and token usage."
        if view_label:
            visual_context += f" The attached screenshot shows the {view_label}."

        base_prompt = f"""
        USER INTENT: {self.intent}

        DESIGN TOKENS CONSTRAINT:
        {json.dumps(self.theme_json, indent=2)}
        Constraint: You MUST use the provided tokens. Hard tokens are immutable. Soft tokens can be adjusted if the visual audit requires it.

        MULTI-VIEW CONVENTION: If the design needs multiple views/screens (e.g. an app with sidebar navigation), keep everything in this single file with ALL views' full content present in the static markup. Tag each navigation trigger with data-view="<kebab-id>" and its content container with data-view-panel="<same-id>". Hide inactive panels with the hidden attribute and toggle visibility on click with a few lines of inline JavaScript. Each view id must name exactly ONE data-view-panel, and every data-view-panel must have at least one matching data-view trigger. A view may be reached from several triggers (the same link in the desktop nav, the mobile menu and the footer) — that is fine; two different screens sharing one id is not. Do not build panel content at runtime and do not add utility classes from JavaScript. Single-view pages must not use these attributes.

        RESPONSIVE REQUIREMENT: The page must be responsive. Use Tailwind responsive utilities (sm:/md:/lg:) so the layout renders well at mobile (375px), tablet (768px), and desktop widths.

        STATE CONVENTION: If the design includes modals, drawers, or overlays, tag each trigger with data-state="<kebab-id>" and its overlay container with data-state-panel="<same-id>". The SAME element that opens the overlay must also close it when clicked again (toggle); a separate close button may exist in addition, but the trigger must toggle. Each state id must be unique: exactly ONE trigger element per overlay. Close buttons inside the panel must NOT have a data-state attribute — use onclick directly instead. Keep overlay content in the static markup, hidden with the hidden attribute. Designs without overlays must not use these attributes.

        INTERACTION STYLES: Every interactive element (links, buttons, inputs, view/state triggers) must show a clearly visible :focus-visible indicator — a high-contrast ring or outline; never remove focus outlines without an equally visible replacement. Hover states must stay readable: text and background must never converge on hover.

        CURRENT CODE:
        ---
        {self.current_code}
        ---
        VISUAL BUG REPORT:
        {json.dumps(audit_report, indent=2)}
        {feedback_section}

        {pivot_instruction}

        {visual_context}
        """

        # Only a strategic pivot goes straight to full-file. Theme changes fall
        # through to the diff-first path below (color edits are hex-swaps, which
        # diff well, with the same full-file fallback if a block can't be matched).
        if pivot_active:
            self.current_code = self._refine_full_file(base_prompt, image_path)
            self._log_refine_method("full-file-pivot")
            return

        diff_prompt = base_prompt + "\n" + DIFF_INSTRUCTION
        try:
            response = llm_client.call_llm(
                "brain",
                diff_prompt,
                system_prompt=BRAIN_SYSTEM_PROMPT,
                image_path=image_path,
                timeout=600,
                retries=1,
                provider_config=self.provider_config,
            )
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            self.current_code = self._refine_full_file(base_prompt, image_path)
            self._log_refine_method("fallback-timeout")
            return

        blocks = diff_edit.parse_edit_blocks(response)
        if not blocks:
            if _looks_like_document(response):
                self.current_code = response.replace("```html", "").replace("```", "").strip()
                self._log_refine_method("salvage")
                return
            self.current_code = self._refine_full_file(base_prompt, image_path)
            self._log_refine_method("fallback-no-blocks")
            return

        new_code, unmatched = diff_edit.apply_edits(self.current_code, blocks)
        if unmatched:
            self.current_code = self._refine_full_file(base_prompt, image_path)
            self._log_refine_method(f"fallback-unmatched-{len(unmatched)}")
            return

        self.current_code = new_code  # NOT fence-stripped
        self._log_refine_method(f"diff-{len(blocks)}-blocks")

    def _refine_full_file(self, base_prompt: str, image_path):
        """Full-file regeneration (pivot / theme / fallback). Fence-stripped."""
        full_prompt = base_prompt + "\n        Output ONLY the updated complete HTML file. Do not include markdown blocks.\n        "
        code = llm_client.call_llm(
            "brain",
            full_prompt,
            system_prompt=BRAIN_SYSTEM_PROMPT,
            image_path=image_path,
            timeout=FULL_FILE_TIMEOUT,
            retries=1,
            provider_config=self.provider_config,
        )
        return code.replace("```html", "").replace("```", "").strip()

    def _log_refine_method(self, method: str):
        print(f"  ↳ refine method: {method}")
        try:
            self.run_state.append_event({"event": "refine_method", "iteration": self.iteration, "method": method})
        except Exception:
            pass

    def _run_meta(self) -> dict:
        return {
            "intent": self.intent,
            "reference": self.reference,
            "reference_png": self.reference_png,
            "theme_json": self.theme_json,
            "iteration": self.iteration,
            "undo_pointer": self.undo_pointer,
            "phase": self.status.get_status()["phase"],
            "max_iterations": self.max_iterations,
            "finished": False,
        }

    def _cell_context(self, cell, view_ids, total_cells):
        if total_cells <= 1:
            return None
        vid, bp, state, pseudo = split_cell(cell)
        dims = dict(config.BREAKPOINTS).get(bp)
        size = f" ({dims[0]}\u00d7{dims[1]})" if dims else ""
        parts = [f"This screenshot shows the '{vid}' view rendered at the {bp} viewport{size}."]
        if len(view_ids) > 1:
            parts.append(
                f"The app has multiple views ({', '.join(view_ids)}); judge this "
                "view on its own merits — do NOT report elements belonging to "
                "other views as missing."
            )
        parts.append("Judge the responsive layout for this width — a stacked single-column layout is correct at mobile widths, not a bug.")
        if state:
            parts.append(
                f"The '{state}' overlay is open in this screenshot; judge the overlay and its relationship to the underlying view."
            )
        if pseudo == "hover":
            scope = f"inside the open '{state}' panel" if state else "in this screenshot"
            parts.append(
                f"Every interactive element {scope} has its hover state forced "
                "simultaneously; judge each element's hover styling "
                "(text/background contrast, readability). Ignore overlap or occlusion "
                "from menus or tooltips that appear only because hover is forced."
            )
            if state:
                parts.append("Background elements outside the panel are NOT forced — do not report their resting hover styling.")
        elif pseudo == "focus":
            scope = f"inside the open '{state}' panel" if state else "in this screenshot"
            parts.append(
                f"Every focusable element {scope} has its focus indicator forced; "
                "judge indicator visibility and contrast against surroundings."
            )
            if state:
                parts.append("Background elements outside the panel are NOT forced — do not report their resting focus styling.")
        return " ".join(parts)

    def _audit_one_cell(self, cell, shot, view_ids, total_cells):
        try:
            audit = visual_audit.perform_visual_audit(shot, self.intent, view_context=self._cell_context(cell, view_ids, total_cells))
        except visual_audit.AuditParseError as e:
            print(f"\u26a0\ufe0f Audit for cell '{cell}' was not valid JSON: {e}")
            audit = {"overall_score": 0, "bugs": [], "summary": "Audit unavailable — response was not valid JSON"}
        except Exception as e:
            print(f"\u26a0\ufe0f Eyes provider failed for cell '{cell}': {e}")
            print("  The automated visual audit is unavailable — review the screenshot yourself")
            print("  and provide feedback. You can also reconfigure the eyes provider in settings.")
            audit = {"overall_score": 0, "bugs": [], "summary": "Automated audit unavailable — eyes provider error"}
        for b in audit.get("bugs", []):
            b.pop("synthetic", None)  # trust in the gate never extends to LLM JSON
        rec = next((r for r in self._capture_records if record_cell(r) == cell), {})
        audit = self._maybe_close_failed(audit, rec)
        if rec.get("missing_focus"):
            audit = dict(audit)
            audit["bugs"] = list(audit.get("bugs", [])) + [missing_focus_bug(label) for label in rec["missing_focus"]]
        return audit

    def _audit_passes(self, audit) -> bool:
        if any(b.get("synthetic") in SYNTHETIC_TAGS for b in audit.get("bugs", [])):
            return False  # deterministic capture-side findings never escape
        return not audit.get("bugs") or audit.get("overall_score", 0) >= 95

    def _blocked_only_by_synthetic(self) -> bool:
        """True when convergence is blocked and EVERY blocked cell would pass
        without its synthetic bugs — i.e. only deterministic checks stand."""
        any_blocked = False
        for e in self.view_audits.values():
            a = e["audit"]
            if self._audit_passes(a):
                continue
            any_blocked = True
            rest = dict(a)
            rest["bugs"] = [b for b in a.get("bugs", []) if b.get("synthetic") not in SYNTHETIC_TAGS]
            if not self._audit_passes(rest):
                return False
        return any_blocked

    @staticmethod
    def _maybe_close_failed(audit, r):
        """Attach the toggle-contract bug when the capture flagged it."""
        if r.get("close_failed") and r.get("state") and not r.get("pseudo"):
            audit = dict(audit)
            audit["bugs"] = list(audit.get("bugs", [])) + [close_failed_bug(r["state"])]
        return audit

    def _composite_with_synthetic_bugs(self, cell_order):
        """The composite carries the worst cell's bugs only — synthetic
        bugs (close-failures, missing-focus) must reach it (refine prompt +
        status/shell) even from non-worst cells, deduplicated."""
        audit = composite_audit(self.view_audits, cell_order)
        existing = {b.get("issue") for b in audit.get("bugs", [])}
        seen_states = set()
        for r in self._capture_records:
            if r.get("close_failed") and r.get("state") and r["state"] not in seen_states:
                seen_states.add(r["state"])
                bug = close_failed_bug(r["state"])
                if bug["issue"] not in existing:
                    audit["bugs"] = list(audit.get("bugs", [])) + [bug]
        base_labels, overlay_labels = [], []
        for r in self._capture_records:
            for label in r.get("missing_focus", []):
                if r.get("state"):
                    overlay_labels.append(f"{label} (in {r['state']})")
                else:
                    base_labels.append(label)
        labels = list(dict.fromkeys(base_labels + overlay_labels))
        if labels:
            bug = aggregate_missing_focus_bug(labels)
            if bug["issue"] not in existing:
                audit["bugs"] = list(audit.get("bugs", [])) + [bug]
        return audit

    def _check_convergence(self) -> bool:
        """Every cell passes, counting only current-iteration audits; stale
        cells are topped up (vision calls) before deciding. On the first
        iteration, only the default cell is audited and convergence is
        always blocked so the user can provide feedback before the loop
        auto-exports."""
        if self.iteration == 1:
            return False  # always wait for feedback after first iteration
        cell_order = [record_cell(r) for r in self._capture_records]
        shots = {record_cell(r): r["screenshot"] for r in self._capture_records if r.get("screenshot")}
        view_ids = list(dict.fromkeys(r["id"] for r in self._capture_records))
        for cell in cell_order:
            e = self.view_audits.get(cell)
            if e and e["iteration"] == self.iteration and not self._audit_passes(e["audit"]):
                return False  # fresh failure: no top-up spend
        for cell in cell_order:
            e = self.view_audits.get(cell)
            if e is None or e["iteration"] != self.iteration:
                if cell not in shots:
                    return False
                print(f"  \U0001f50d Convergence sweep: auditing '{cell}'...")
                audit = self._audit_one_cell(cell, shots[cell], view_ids, len(cell_order))
                self.view_audits[cell] = {"audit": audit, "iteration": self.iteration}
        # Display uses the composite (worst cell's bugs only), but the gate must
        # be per-cell: composite_audit keeps just the min-score cell's bugs, so a
        # bug-free low-score cell would otherwise mask a bugged higher-score cell.
        self.status.set_audit(self._composite_with_synthetic_bugs(cell_order))
        return all(cell in self.view_audits and self._audit_passes(self.view_audits[cell]["audit"]) for cell in cell_order)

    def _next_action(self, timeout=None) -> dict:
        """Read one queue message; translate exact-match keywords; count
        genuine feedback. Keywords: DONE/KEEP -> accept, UNDO -> undo."""
        msg = self.status.get_message(timeout=timeout)
        if msg is None:
            return {"type": "feedback", "text": ""}
        if msg.get("type") == "feedback":
            text = msg.get("text") or ""
            action = KEYWORD_ACTIONS.get(text.strip().upper())
            if action:
                return {"type": action}
            self.status.increment_feedback()
            return {"type": "feedback", "text": text}
        return msg

    def _produce_iteration(self, feedback=None) -> dict:
        """Render + audit + snapshot current_code as the next iteration.
        Raises on render/capture failure."""
        self.iteration += 1
        self.status.set_iteration(self.iteration)
        self.status.set_phase("rendering", f"Rendering iteration {self.iteration}")
        records = self.render_and_capture()
        self._capture_records = records
        img_path = default_shot(records)
        self._last_img = img_path

        view_ids = list(dict.fromkeys(r["id"] for r in records))
        cell_order = [record_cell(r) for r in records]
        shots = {record_cell(r): r["screenshot"] for r in records if r.get("screenshot")}
        self.view_audits = {k: v for k, v in self.view_audits.items() if k in cell_order}

        self.status.set_phase("auditing", f"Analyzing iteration {self.iteration}")
        targets = match_cells(feedback, list(shots)) if (feedback and self.iteration > 1) else []
        if not targets:
            # First iteration: only audit the default (first base) cell for
            # maximum speed. The user can provide feedback to target other cells.
            if self.iteration == 1:
                base = next((c for c in (record_cell(r) for r in records) if is_base_cell(c)), None)
                targets = [base] if base else []
            else:
                targets = list(shots)
        if not targets:
            targets = list(shots)
        if feedback and self.iteration > 1 and targets == list(shots):
            state_ids = list(dict.fromkeys(r["state"] for r in self._capture_records if r.get("state")))
            if match_states(feedback, state_ids) and match_pseudo(feedback, ["hover", "focus"]):
                print("  \u2139\ufe0f the named overlay has no sheet cells — auditing everything")
        for r in records:
            cell = record_cell(r)
            if r.get("error"):
                label = (
                    (f"pseudo '{r['pseudo']}' sheet" + (f" ('{r['state']}' open)" if r.get("state") else ""))
                    if r.get("pseudo")
                    else f"state '{r['state']}'"
                    if r.get("state")
                    else f"view '{r['id']}'"
                )
                self.view_audits[cell] = {"audit": synthetic_failure_audit(r["error"], label), "iteration": self.iteration}

        for r in records:
            cell = record_cell(r)
            if not r.get("error") and cell in targets and cell in shots:
                audit = self._audit_one_cell(cell, shots[cell], view_ids, len(cell_order))
                self.view_audits[cell] = {"audit": audit, "iteration": self.iteration}

        audit = self._composite_with_synthetic_bugs(cell_order)
        self.status.set_audit(audit)
        print(f"Audit Score: {audit.get('overall_score', 'N/A')}/100")
        print(f"Bugs found: {len(audit.get('bugs', []))}")

        self.run_state.snapshot(self.iteration, self.current_code, self.theme_json, audit)
        self.undo_pointer = self.iteration
        self.status.set_can_undo(self.undo_pointer > 1)
        self.run_state.save_run(self._run_meta())
        self.run_state.append_event(
            {
                "event": "iteration_produced",
                "n": self.iteration,
                "score": audit.get("overall_score", 0),
                "bugs": len(audit.get("bugs", [])),
                "summary": audit.get("summary", ""),
                "screenshot": img_path,
                "feedback": feedback,
                "views": {
                    cell: {"score": self.view_audits[cell]["audit"].get("overall_score", 0), "screenshot": shots.get(cell)}
                    for cell in cell_order
                    if cell in self.view_audits
                },
            }
        )
        return audit

    def _undo(self, error_wait: bool = False):
        """Restore a snapshot. Normal wait: pointer-1 (step back). Error wait:
        pointer itself — the on-screen render was never snapshotted, so this
        discards the broken render instead of skipping the last good state."""
        from_pointer = self.undo_pointer
        target = self.undo_pointer if error_wait else self.undo_pointer - 1
        if target < 1 or self.run_state.latest_iteration() == 0:
            self.status.set_phase("awaiting-feedback", "Nothing to undo")
            print("Nothing to undo.")
            return
        code, theme, audit = self.run_state.load_snapshot(target)
        self.current_code = code
        self.theme_json = theme
        with open(config.THEME_PATH, "w") as f:
            json.dump(theme, f, indent=2)
        self.render_preview()
        self.status.set_audit(audit)
        self.view_audits = {
            vid: {"audit": {"overall_score": v.get("score", 0), "bugs": [], "summary": ""}, "iteration": target}
            for vid, v in audit.get("views", {}).items()
        }
        self.undo_pointer = target
        self.status.set_can_undo(self.undo_pointer > 1)
        self.run_state.save_run(self._run_meta())
        self.run_state.append_event(
            {
                "event": "undo",
                "restored": target,
                "from": from_pointer,
                "error_wait": error_wait,
            }
        )
        self.status.set_phase("awaiting-feedback", f"Restored iteration {target}")
        print(f"↩ Restored iteration {target}.")

    def _save_draft(self):
        """Export the current code as a standalone draft file without ending the loop."""
        import export as export_mod

        draft_path = config.PROJECT_DIR / "draft.html"
        try:
            html = export_mod.build_standalone(self.current_code, self.theme_json)
            print(f"💾 Draft saved ({len(html) // 1024} KB standalone)")
        except export_mod.ExportError as e:
            print(f"⚠️ Standalone export failed ({e}) — writing CDN-dependent fallback.")
            html = self._inject_theme(self.current_code, include_reload=False)
        with open(draft_path, "w") as f:
            f.write(html)
        print(f"💾 Draft saved to {draft_path}")
        self.status.set_phase("awaiting-feedback", f"Draft saved — iteration {self.iteration}")

    def _route_feedback(self, text):
        """Pick the screenshot to attach to a refine from feedback text: one
        unambiguously matched axis routes to a single cell (a matched state's
        owning view wins over the default view pick); anything ambiguous
        falls back to the default shot. Returns (image_path, label|None).
        Shared by the normal feedback path and the error-retry path — the
        two must never drift (PR #7)."""
        view_ids = list(dict.fromkeys(r["id"] for r in self._capture_records))
        bp_names = [n for n, _ in config.BREAKPOINTS]
        rv = match_views(text, view_ids)
        rb = match_breakpoints(text, bp_names)
        state_ids = list(dict.fromkeys(r["state"] for r in self._capture_records if r.get("state")))
        rs = match_states(text, state_ids)
        pseudo_names = list(dict.fromkeys(r["pseudo"] for r in self._capture_records if r.get("pseudo")))
        rp = match_pseudo(text, pseudo_names)
        shots = {record_cell(r): r["screenshot"] for r in self._capture_records if r.get("screenshot")}
        if (len(rv) == 1 or len(rb) == 1 or len(rs) == 1 or len(rp) == 1) and (rv or rb or rs or rp):
            vid = rv[0] if len(rv) == 1 else (view_ids[0] if view_ids else None)
            bp = rb[0] if len(rb) == 1 else config.DEFAULT_BREAKPOINT
            state = rs[0] if len(rs) == 1 else None
            pseudo = rp[0] if len(rp) == 1 else None
            cell = cell_key(vid, bp, state, pseudo) if vid else None
            if state and (cell not in shots):
                # a state's owning view may differ from the default pick;
                # the pseudo filter keeps sheet records from hijacking
                # state-only feedback (pseudo is None on a pure state match):
                owners = [
                    r
                    for r in self._capture_records
                    if r.get("state") == state and r.get("breakpoint", config.DEFAULT_BREAKPOINT) == bp and r.get("pseudo") == pseudo
                ]
                cell = record_cell(owners[0]) if owners else cell
        else:
            cell = None
        if cell and cell in shots:
            vid, bp, state, pseudo = split_cell(cell)
            label = (
                f"'{vid}' view at the {bp} viewport"
                + (f" with '{state}' open" if state else "")
                + (f" with {pseudo} states forced" if pseudo else "")
            )
            return shots[cell], label
        return self._last_img, None

    def _recover_iteration(self, error, audit, port) -> str:
        """Failure recovery wait. Returns:
        'accepted' — user accepted current state (accept/KEEP)
        'failed'   — retry failed twice; run() records outcome 'exhausted'
        'continue' — state restored (undo) or unchanged; wait again
        'retried'  — retry refine succeeded; caller produces the iteration,
                     passing self._retry_feedback"""
        print(f"\n⚠️ Iteration failed: {error}")
        self.run_state.append_event({"event": "error", "iteration": self.iteration, "message": str(error)})
        self.status.set_phase("awaiting-feedback", f"Error — retry iteration {self.iteration}")
        self.status.set_error(str(error))
        print("Current code preserved. Submit feedback to retry, DONE/KEEP to accept, UNDO to restore the last good snapshot.")
        print(f"\n👀 VIEW DESIGN SHELL: http://localhost:{port}/design_shell.html")
        msg = self._next_action(timeout=None)
        if msg["type"] == "accept":
            print("Keeping current code as-is.")
            return "accepted"
        if msg["type"] == "undo":
            self._undo(error_wait=True)
            return "continue"
        print("Retrying refinement...")
        self.status.set_phase("generating-code", f"Retrying iteration {self.iteration}")
        try:
            refine_img, refine_label = self._route_feedback(msg["text"])
            self.refine_code(audit, image_path=refine_img, human_feedback=msg["text"], view_label=refine_label)
            self._retry_feedback = msg["text"]
            return "retried"
        except Exception as e2:
            print(f"⚠️ Retry also failed: {e2}")
            print("Saving current code and exiting loop.")
            return "failed"

    def run(self, port, resume=False):  # noqa: C901  (legacy orchestration; refactor deferred)
        self.status.set_max_iterations(self.max_iterations)
        audit = None

        if resume:
            audit = self._restore_run(port)
            if audit is None:
                resume = False
            else:
                converged_initial = False

        if not resume:
            self.bootstrap()

            # Provider Configuration Wizard (pre-flight)
            self.status.set_phase("wizard", "Configure providers")
            print(f"\n⚙️ VIEW PROVIDER WIZARD: http://localhost:{port}/provider_wizard.html?mode=setup")
            print("Waiting for provider configuration...")
            self.status.await_provider_config(timeout=600)
            self.provider_config = ProviderConfig()
            self.status.set_model_label(_model_label(self.provider_config))
            print("✅ Provider configuration received.")

            # Pre-flight: confirm each role has a usable model before any generation.
            ok, detail = llm_client.preflight_roles(self.provider_config)
            if not ok:
                print(f"\n❌ {detail}")
                print(f"   Reconfigure the model at http://localhost:{port}/provider_wizard.html?mode=settings and re-run.")
                return

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

            # Discard the previous run only once this one is certain to proceed.
            # --reference forces resume=False (orchestrator.decide_resume), so clearing
            # any earlier would let a mistyped reference path destroy an unfinished run
            # the user never chose to abandon.
            self.run_state.clear()

            # Intent input (if no CLI arg)
            if not self.intent:
                self.status.set_phase("awaiting-intent", "Waiting for project description")
                print(f"\n✍️ DEFINE YOUR PROJECT: http://localhost:{port}/design_shell.html")
                print("Waiting for intent input...")
                if not self.status.await_intent(timeout=600):
                    print("No intent provided. Exiting.")
                    return
                self.intent = self.status.intent
                print(f"✅ Intent received: {self.intent}")
            self.run_state.save_run(self._run_meta())
            self.run_state.append_event({"event": "run_started", "intent": self.intent, "max_iterations": self.max_iterations})

            # Generate design tokens (silent — no user approval needed)
            self.status.set_phase("generating-theme", "Creating design tokens")
            try:
                self.generate_theme()
            except Exception as e:
                if self.reference:
                    print(f"\n❌ Theme generation failed on an image-seeded run: {e}")
                    print("   The reference cannot be honoured without tokens derived from it. Exiting.")
                    return
                print(f"⚠️ Theme generation failed: {e}. Proceeding without design tokens.")
                self.theme_json = {}
            self.run_state.save_run(self._run_meta())

            # Initial code + first iteration (render/audit/snapshot) pre-loop
            self.status.set_phase("generating-code", "Writing initial HTML")
            try:
                self.generate_initial_code()
            except Exception as e:
                print(f"\n❌ Initial code generation failed: {e}")
                print("Check your LLM provider. Exiting.")
                return
            try:
                audit = self._produce_iteration()
                converged_initial = self._check_convergence()
            except Exception as e:
                print(f"\n❌ Initial render failed: {e}")
                print("Exiting.")
                return

        # Wait-first iteration loop
        outcome = "exhausted"
        if not converged_initial:
            while self.iteration < self.max_iterations:
                self.status.set_phase("awaiting-feedback", f"Review iteration {self.iteration}")
                if self._blocked_only_by_synthetic():
                    print(
                        "⚠️ Convergence blocked only by deterministic checks "
                        "(focus indicators / toggle contract) — type DONE to accept as-is."
                    )
                print(f"\n👀 VIEW DESIGN SHELL: http://localhost:{port}/design_shell.html")
                print(f"Visual Summary: {audit.get('summary')}")
                print("Waiting for feedback... (DONE to accept, SAVE to save draft, UNDO to revert)")
                msg = self._next_action(timeout=None)

                if msg["type"] == "accept":
                    print("✅ Accepted — finishing.")
                    outcome = "accepted"
                    break
                if msg["type"] == "undo":
                    self._undo()
                    audit = self.status.get_audit() or audit
                    continue
                if msg["type"] == "save":
                    self._save_draft()
                    continue

                try:
                    self.status.set_phase("generating-code", f"Refining iteration {self.iteration + 1}")
                    refine_img, refine_label = self._route_feedback(msg["text"])
                    self.refine_code(audit, image_path=refine_img, human_feedback=msg["text"], view_label=refine_label)
                    audit = self._produce_iteration(feedback=msg["text"])
                except Exception as e:
                    result = self._recover_iteration(e, audit, port)
                    if result == "accepted":
                        outcome = "accepted"
                        break
                    if result == "failed":
                        break  # outcome stays 'exhausted'
                    if result == "continue":
                        audit = self.status.get_audit() or audit
                        continue
                    # result == "retried": produce from the retried code
                    try:
                        audit = self._produce_iteration(feedback=self._retry_feedback)
                    except Exception as e2:
                        print(f"⚠️ Render after retry failed: {e2}. Saving current code and exiting loop.")
                        break  # outcome stays 'exhausted'
                    finally:
                        self._retry_feedback = None

                if self._check_convergence():
                    outcome = "converged"
                    print("\n✅ Visual convergence reached!")
                    break
        else:
            outcome = "converged"
            print("\n✅ Visual convergence reached!")

        self.status.set_phase("converged", "Exporting standalone file...")
        print(f"\nFinal code generated after {self.iteration} iterations.")
        try:
            final_html = export.build_standalone(self.current_code, self.theme_json)
            print(f"📦 final_result.html is fully standalone ({len(final_html) // 1024} KB)")
        except export.ExportError as e:
            print(f"⚠️ Standalone export failed ({e}) — writing CDN-dependent fallback.")
            final_html = self._inject_theme(self.current_code, include_reload=False)
        with open(config.FINAL_PATH, "w") as f:
            f.write(final_html)
        self.status.set_phase("converged", "Design complete")
        print(f"Final result saved to {config.FINAL_PATH}")
        meta = self._run_meta()
        meta["finished"] = True
        self.run_state.save_run(meta)
        blocked_tags = sorted(
            {
                b.get("synthetic")
                for e in self.view_audits.values()
                if not self._audit_passes(e["audit"])
                for b in e["audit"].get("bugs", [])
                if b.get("synthetic") in SYNTHETIC_TAGS
            }
        )
        self.run_state.append_event(
            {
                "event": "finished",
                "outcome": outcome,
                "final_score": (audit or {}).get("overall_score", 0),
                **({"synthetic_blocks": blocked_tags} if blocked_tags else {}),
            }
        )
        try:
            report_path = report.write_report(self.run_state)
            print(f"📊 Run report: {report_path}")
        except Exception as e:
            print(f"⚠️ Report generation failed ({e}) — continuing.")

    def _restore_run(self, port):
        """Restore an unfinished run from run_state. Returns the snapshot's
        audit on success, or None when there is nothing worth restoring
        (caller falls back to a fresh run)."""
        try:
            meta = self.run_state.load_run()
        except Exception as e:
            print(f"⚠️ Could not read run state ({e}) — starting fresh.")
            return None
        latest = self.run_state.latest_iteration()
        if latest == 0:
            print("Previous run had no completed iterations — starting fresh.")
            return None

        # Wizard only if provider config is somehow missing
        if not (config.PROJECT_DIR / "providers.json").exists():
            self.status.set_phase("wizard", "Configure providers")
            print(f"\n⚙️ VIEW PROVIDER WIZARD: http://localhost:{port}/provider_wizard.html?mode=setup")
            print("Waiting for provider configuration...")
            self.status.await_provider_config(timeout=600)
        self.provider_config = ProviderConfig()
        self.status.set_model_label(_model_label(self.provider_config))

        self.intent = meta.get("intent")
        # run.json is rewritten wholesale from _run_meta() at every save site, so a
        # field not restored here is erased by the first save after a resume — losing
        # the record of what seeded the design.
        self.reference = meta.get("reference")
        self.reference_png = meta.get("reference_png")
        self.max_iterations = meta.get("max_iterations", self.max_iterations)
        self.status.set_max_iterations(self.max_iterations)
        self.iteration = meta.get("iteration", latest)
        self.undo_pointer = min(meta.get("undo_pointer", latest) or latest, latest)

        try:
            code, theme, audit = self.run_state.load_snapshot(self.undo_pointer)
        except Exception as e:
            print(f"⚠️ Could not load snapshot {self.undo_pointer} ({e}) — starting fresh.")
            return None
        self.current_code = code
        self.theme_json = theme
        with open(config.THEME_PATH, "w") as f:
            json.dump(theme, f, indent=2)

        self.render_preview()
        self.status.set_audit(audit)
        self.view_audits = {
            vid: {"audit": {"overall_score": v.get("score", 0), "bugs": [], "summary": ""}, "iteration": self.iteration - 1}
            for vid, v in audit.get("views", {}).items()
        }
        self.status.set_iteration(self.iteration)
        self.status.set_can_undo(self.undo_pointer > 1)
        self.run_state.append_event({"event": "resumed", "iteration": self.iteration})
        print(f'▶ Resumed "{self.intent}" at iteration {self.iteration}/{self.max_iterations}.')
        return audit
