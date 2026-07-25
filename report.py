"""Run report: render run_state's trajectory (events + snapshots) as one
self-contained, zero-JavaScript HTML timeline.

Pure reader — build_report() is a function of on-disk state. The loop calls
write_report() at finish inside a broad except; report failures never block
a run. Access config.REPORTS_DIR as an attribute (tests patch it)."""

import base64
import html
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


class ReportError(Exception):
    """No usable trajectory to report."""


OUTCOME_COLORS = {"accepted": "#00FF88", "converged": "#00F0FF", "exhausted": "#FFB347", "unfinished": "#888"}
SEVERITY_COLORS = {"critical": "#FF6B6B", "important": "#FFB347", "minor": "#aaaaaa"}

CSS = """
body { background:#0A0A0F; color:#e0e0e0; font-family:sans-serif; margin:0 auto;
       max-width:960px; padding:2rem; }
h1 { font-size:1.4rem; margin:0 0 0.25rem; }
.meta { color:#888; font-size:0.85rem; margin-bottom:2rem; }
.badge { display:inline-block; padding:2px 10px; border-radius:4px; color:#0A0A0F;
         font-weight:bold; font-size:0.8rem; }
.iteration-card { background:#1a1a2e; border:1px solid #333; border-radius:8px;
                  padding:1rem; margin:1rem 0; }
.iteration-card img { max-width:100%; border-radius:4px; border:1px solid #333; }
.placeholder { background:#333; color:#888; text-align:center; padding:3rem 0;
               border-radius:4px; font-size:0.85rem; }
.score { font-weight:bold; color:#00FF88; }
.summary { color:#ccc; font-size:0.9rem; margin:0.5rem 0; }
.bug { font-size:0.8rem; margin:0.15rem 0; }
.feedback { color:#00F0FF; font-size:0.85rem; margin-top:0.5rem; font-style:italic; }
.event-row { font-size:0.85rem; color:#888; margin:0.5rem 0 0.5rem 1rem; }
.event-row.error { color:#FF6B6B; }
footer { color:#555; font-size:0.75rem; margin-top:2.5rem; border-top:1px solid #222;
         padding-top:0.75rem; }
.view-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:0.75rem; }
.view-caption { color:#888; font-size:0.75rem; margin-top:0.25rem; text-align:center; }
"""


def _slugify(intent: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (intent or "").lower()).strip("-")[:40]
    return slug or "run"


def _cell_label(cell: str) -> str:
    """'cart@mobile#hover' -> 'cart @ mobile # hover';
    keys without delimiters pass through."""
    return str(cell).replace("@", " @ ").replace("+", " + ").replace("#", " # ")


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (TypeError, ValueError, OSError):
        return "?"


def _screenshot_tag(path) -> str:
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f'<img src="data:image/png;base64,{b64}" alt="iteration screenshot">'
    except (OSError, TypeError):
        return '<div class="placeholder">screenshot unavailable</div>'


def _bugs_html(run_state, n) -> str:
    try:
        _, _, audit = run_state.load_snapshot(n)
    except Exception:
        return ""
    rows = []
    for bug in audit.get("bugs", []):
        sev = str(bug.get("severity", "minor")).lower()
        color = SEVERITY_COLORS.get(sev, "#aaaaaa")
        issue = html.escape(str(bug.get("issue", "")))
        loc = html.escape(str(bug.get("location", "?")))
        rows.append(f'<div class="bug" style="color:{color}">• {issue} ({loc})</div>')
    return "\n".join(rows)


def _iteration_card(run_state, ev) -> str:
    n = ev.get("n", "?")
    score = ev.get("score", 0)
    summary = html.escape(str(ev.get("summary", "")))
    feedback = ev.get("feedback")
    feedback_html = f'<div class="feedback">💬 feedback: {html.escape(str(feedback))}</div>' if feedback else ""
    views = ev.get("views") or {}
    if len(views) > 1:
        cells = []
        for vid, info in views.items():
            cells.append(
                f'<div class="view-cell">{_screenshot_tag(info.get("screenshot"))}'
                f'<div class="view-caption">{html.escape(_cell_label(vid))} — '
                f"{info.get('score', '?')}/100</div></div>"
            )
        shot_html = f'<div class="view-grid">{"".join(cells)}</div>'
    else:
        shot_html = _screenshot_tag(ev.get("screenshot"))
    return f"""
<div class="iteration-card">
  <div><strong>Iteration {n}</strong> — <span class="score">{score}/100</span>
       <span style="color:#555;font-size:0.75rem;">{_fmt_ts(ev.get("ts"))}</span></div>
  {shot_html}
  <div class="summary">{summary}</div>
  {_bugs_html(run_state, n) if isinstance(n, int) else ""}
  {feedback_html}
</div>"""


def _event_row(ev) -> str:
    kind = ev.get("event")
    if kind == "undo":
        suffix = " (error recovery)" if ev.get("error_wait") else ""
        return f'<div class="event-row">↩ Restored iteration {ev.get("restored", "?")}{suffix}</div>'
    if kind == "error":
        return f'<div class="event-row error">⚠ {html.escape(str(ev.get("message", "")))}</div>'
    if kind == "resumed":
        return f'<div class="event-row">▶ Resumed at iteration {ev.get("iteration", "?")}</div>'
    return ""


def build_report(run_state) -> str:
    events = run_state.read_events()
    if not events:
        raise ReportError("no events recorded for this run")
    try:
        meta = run_state.load_run()
    except Exception:
        meta = {}
    intent = html.escape(str(meta.get("intent") or "(unknown intent)"))
    finishes = [e for e in events if e.get("event") == "finished"]
    outcome = finishes[-1].get("outcome", "unfinished") if finishes else "unfinished"
    final_score = finishes[-1].get("final_score", "?") if finishes else "?"
    iterations = [e for e in events if e.get("event") == "iteration_produced"]
    badge_color = OUTCOME_COLORS.get(outcome, "#888")

    timeline = []
    for ev in events:  # file order == chronological
        if ev.get("event") == "iteration_produced":
            timeline.append(_iteration_card(run_state, ev))
        else:
            timeline.append(_event_row(ev))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>StyleSentry run report — {intent}</title>
<style>{CSS}</style></head><body>
<h1>{intent}</h1>
<div class="meta">
  <span class="badge" style="background:{badge_color}">{html.escape(str(outcome))}</span>
  &nbsp;{len(iterations)} iteration(s) · final score {final_score}/100<br>
  {_fmt_ts(events[0].get("ts"))} → {_fmt_ts(events[-1].get("ts"))}
</div>
{"".join(timeline)}
<footer>Generated by StyleSentry · {_fmt_ts(time.time())}</footer>
</body></html>"""


def write_report(run_state) -> Path:
    html_text = build_report(run_state)
    reports_dir = config.REPORTS_DIR
    os.makedirs(reports_dir, exist_ok=True)
    try:
        intent = run_state.load_run().get("intent", "")
    except Exception:
        intent = ""
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slugify(intent)}.html"
    path = reports_dir / name
    with open(path, "w") as f:
        f.write(html_text)
    return path
