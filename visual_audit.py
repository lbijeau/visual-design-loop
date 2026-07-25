"""Visual audit: builds the auditor prompt, parses the JSON verdict.

Transport (provider resolution, image encoding, retries) lives in llm_client.
"""

import json
import os
import re
import sys
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_client


class AuditParseError(Exception):
    """The eyes model returned something that is not parseable audit JSON."""


def clean_json(text: str) -> str:
    """
    Extracts the first valid JSON object found in the text,
    stripping markdown blocks or surrounding prose.
    """
    match = re.search(r"(\{.*\})", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


def _build_audit_prompt(original_intent: str, view_context: str = None) -> str:
    """Grounded, calibrated audit prompt. Pure — depends only on intent + optional view_context."""
    context_block = f"\n    VIEW CONTEXT:\n    {view_context}\n" if view_context else ""
    return f"""
    You are a UI/UX auditor. Audit the screenshot against the user's intent, judging only what is
    actually visible on screen.

    USER INTENT:
    {original_intent}
    {context_block}
    STEP 1 — INVENTORY. Scan the screenshot top to bottom and list, in "components_present", every
    distinct section or component you can actually see (header, hero, gauge/dial, stat cards,
    log/table rows, footer, etc.). Be thorough.

    STEP 2 — AUDIT each of:
    1. Alignment: are elements aligned, centered, and balanced?
    2. Contrast: is the text readable against its background?
    3. Spacing: is whitespace sufficient — cramped or too sparse?
    4. Components: for every element the intent requests, check it against your STEP 1 inventory.
       You may flag a component "missing" ONLY if it is genuinely absent from components_present.
    5. Quality: do the visible components look like the intended thing, and is the execution
       correct? Be rigorous — report real flaws (e.g. a gauge whose labels or needle are wrong,
       a control that does not match the brief, misaligned or broken elements).

    STEP 3 — SCORE 0-100 for how well the VISIBLE design meets the intent, using these bands:
      90-100 = complete and essentially flawless (ship-ready);
      70-89  = complete and on-brief, with minor or moderate issues;
      40-69  = notable flaws, or several missing/broken elements;
      0-39   = broken, largely empty, or unstyled.
    A complete page that has a real, non-trivial flaw belongs in 70-89 — not 90+.

    OUTPUT FORMAT — respond in valid JSON with this structure:
    {{
      "components_present": ["<each visible section you listed in STEP 1>"],
      "overall_score": 0-100,
      "bugs": [
        {{
          "id": 1,
          "category": "Alignment|Contrast|Spacing|Components|Quality",
          "issue": "Description of the problem",
          "location": "Grid sector (e.g., B4)",
          "severity": "Critical|Important|Minor",
          "suggestion": "Specific CSS/Tailwind fix"
        }}
      ],
      "summary": "Short summary of the visual state"
    }}
    Never mark a requested element "missing" if it appears in components_present. If no bugs are
    found, return an empty list for "bugs".
    """


def perform_visual_audit(image_path: str, original_intent: str, view_context: str = None) -> Dict:
    """Structured visual audit of a screenshot using the configured 'eyes' provider.
    Grounded (inventory-first) + calibrated; components_present is prompt scaffolding, stripped
    from the returned dict so downstream sees the standard shape."""
    prompt = _build_audit_prompt(original_intent, view_context)
    content = llm_client.call_llm(
        role="eyes",
        prompt=prompt,
        image_path=image_path,
        response_format="json",
    )
    cleaned_content = clean_json(content)
    try:
        audit = json.loads(cleaned_content)
    except json.JSONDecodeError as e:
        raise AuditParseError(f"Failed to parse audit JSON: {e}")
    if isinstance(audit, dict):
        audit.pop("components_present", None)  # grounding scaffold — not for downstream
    return audit


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python visual_audit.py <image_path> <intent>")
        sys.exit(1)
    try:
        print(json.dumps(perform_visual_audit(sys.argv[1], sys.argv[2]), indent=2))
    except Exception as e:
        print(f"Error: {e}")
