"""Central configuration: project-relative paths, ports, limits, legacy env fallbacks.

Every path is derived from this file's location so the tool works from any cwd.
"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

THEME_PATH = PROJECT_DIR / "theme.json"
RENDER_PATH = PROJECT_DIR / "current_render.html"
SHELL_PATH = PROJECT_DIR / "design_shell.html"
FINAL_PATH = PROJECT_DIR / "final_result.html"
VERSION_PATH = PROJECT_DIR / "version.txt"
SCREENSHOT_DIR = PROJECT_DIR / "screenshots"
CAPTURE_SCRIPT = PROJECT_DIR / "capture.js"
REFERENCE_SCRIPT = PROJECT_DIR / "capture_reference.js"
PROMPT_TEMPLATES_PATH = PROJECT_DIR / "prompt_templates.json"
RUN_STATE_DIR = PROJECT_DIR / "run_state"
REPORTS_DIR = PROJECT_DIR / "reports"

DEFAULT_PORT = 8000
MAX_ITERATIONS = 5

# Responsive breakpoints — ordered, DEFAULT first: records[0] must be
# default view @ default breakpoint for default_shot and legacy consumers.
BREAKPOINTS = [("desktop", (1280, 800)), ("mobile", (375, 812)), ("tablet", (768, 1024))]
DEFAULT_BREAKPOINT = "desktop"
BREAKPOINT_KEYWORDS = {"mobile": "mobile", "phone": "mobile", "tablet": "tablet", "desktop": "desktop"}

# Legacy env-var fallbacks — used only when a role's provider cannot be resolved
# from providers.json (see llm_client.call_llm).
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
