import json
import os
import subprocess
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config
from loop import FrontendDesignLoop
from server import LivePreviewServer

# We need to run a separate server for the driver to interact with
# This script will simulate a user interacting with the Design Shell


def run_demo_scenario():
    intent = "A high-tech futuristic space colony dashboard with deep purples and neon accents"
    loop = FrontendDesignLoop(intent)
    loop.bootstrap()
    preview = LivePreviewServer(status=loop.status)
    preview.start()

    try:
        # --- SHOT 1: THE INITIAL DRAFT ---
        print("\n--- Generating Shot 1: Initial Draft ---")
        loop.generate_theme()
        loop.generate_initial_code()

        # Capture the shell showing the first render
        with open(config.PROJECT_DIR / "summary.json", "w") as f:
            json.dump({"summary": "Visual Audit: 88/100. Initial draft looks solid. Minor spacing issues detected."}, f)
        subprocess.run(["node", str(config.PROJECT_DIR / "capture_shell.js"), f"http://localhost:{preview.port}/design_shell.html"])
        print("✅ Shot 1 Captured.")

        # --- SHOT 2: THE REGRESSION ---
        print("\n--- Generating Shot 2: The Regression ---")
        loop.theme_json = {
            "hard_tokens": {"brand_primary": "#e0e0e0", "brand_secondary": "#f5f5f5", "primary_font": "sans-serif"},
            "soft_tokens": {"accent_color": "#b0b0b0", "border_radius": "4px", "spacing_unit": "4px"},
            "tailwind_config": {
                "theme": {
                    "extend": {
                        "colors": {"brand": {"primary": "#e0e0e0", "secondary": "#f5f5f5"}, "accent": "#b0b0b0"},
                        "borderRadius": {"theme": "4px"},
                        "spacing": {"theme": "4px"},
                    }
                }
            },
        }
        from loop import default_shot

        img_path = default_shot(loop.render_and_capture())
        audit = loop.perform_audit(img_path)

        with open(config.PROJECT_DIR / "summary.json", "w") as f:
            json.dump(
                {
                    "summary": f"CRITICAL: Contrast Regression Detected. Audit Score: {audit.get('overall_score')}/100. Text is fading into background."
                },
                f,
            )

        subprocess.run(["node", str(config.PROJECT_DIR / "capture_shell.js"), f"http://localhost:{preview.port}/design_shell.html"])
        print("✅ Shot 2 Captured.")

        # --- SHOT 3: THE CONVERGENCE ---
        print("\n--- Generating Shot 3: The Convergence ---")
        loop.theme_json = {
            "hard_tokens": {"brand_primary": "#0f0f23", "brand_secondary": "#1a1a3e", "primary_font": "Inter, sans-serif"},
            "soft_tokens": {"accent_color": "#00d4ff", "border_radius": "8px", "spacing_unit": "8px"},
            "tailwind_config": {
                "theme": {
                    "extend": {
                        "colors": {"brand": {"primary": "#0f0f23", "secondary": "#1a1a3e"}, "accent": "#00d4ff"},
                        "borderRadius": {"theme": "8px"},
                        "spacing": {"theme": "8px"},
                    }
                }
            },
        }
        loop.render_and_capture()

        with open(config.PROJECT_DIR / "summary.json", "w") as f:
            json.dump({"summary": "Visual Audit: 92/100. Regression resolved. Contrast and alignment restored."}, f)

        subprocess.run(["node", str(config.PROJECT_DIR / "capture_shell.js"), f"http://localhost:{preview.port}/design_shell.html"])
        print("✅ Shot 3 Captured.")

    finally:
        preview.stop()


if __name__ == "__main__":
    run_demo_scenario()
