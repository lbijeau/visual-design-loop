"""Tests for config: all paths must be absolute and cwd-independent."""

import os
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_paths_are_absolute_and_project_relative():
    print("  test_paths_are_absolute_and_project_relative...", end=" ")
    # Simulate launching from a foreign cwd BEFORE importing config
    original_cwd = os.getcwd()
    os.chdir(tempfile.gettempdir())
    try:
        import config

        # config.py lives at the project root; anchor the expectation on its
        # location (not this test file's, which now sits under tests/).
        expected_dir = os.path.dirname(os.path.abspath(config.__file__))
        assert str(config.PROJECT_DIR) == expected_dir, f"PROJECT_DIR={config.PROJECT_DIR}"
        for name in (
            "THEME_PATH",
            "RENDER_PATH",
            "SHELL_PATH",
            "FINAL_PATH",
            "VERSION_PATH",
            "SCREENSHOT_DIR",
            "CAPTURE_SCRIPT",
            "PROMPT_TEMPLATES_PATH",
            "RUN_STATE_DIR",
            "REPORTS_DIR",
        ):
            p = getattr(config, name)
            assert os.path.isabs(str(p)), f"{name} is not absolute: {p}"
            assert str(p).startswith(expected_dir), f"{name} escapes project dir: {p}"
        assert config.THEME_PATH.name == "theme.json"
        assert config.RENDER_PATH.name == "current_render.html"
        assert config.SHELL_PATH.name == "design_shell.html"
        assert config.FINAL_PATH.name == "final_result.html"
        assert config.VERSION_PATH.name == "version.txt"
        assert config.SCREENSHOT_DIR.name == "screenshots"
        assert config.CAPTURE_SCRIPT.name == "capture.js"
        assert config.PROMPT_TEMPLATES_PATH.name == "prompt_templates.json"
        assert config.RUN_STATE_DIR.name == "run_state"
        assert config.REPORTS_DIR.name == "reports"
    finally:
        os.chdir(original_cwd)
    print("✅")


def test_constants():
    print("  test_constants...", end=" ")
    import config

    assert config.DEFAULT_PORT == 8000
    assert config.MAX_ITERATIONS == 5
    assert isinstance(config.OLLAMA_HOST, str) and config.OLLAMA_HOST
    assert isinstance(config.OLLAMA_API_KEY, str)
    assert config.BREAKPOINTS[0][0] == config.DEFAULT_BREAKPOINT == "desktop"
    assert dict(config.BREAKPOINTS)["mobile"] == (375, 812)
    assert config.BREAKPOINT_KEYWORDS["phone"] == "mobile"
    print("✅")


if __name__ == "__main__":
    print("\n=== Config Tests ===")
    test_paths_are_absolute_and_project_relative()
    test_constants()
    print("\nAll tests passed ✅")
