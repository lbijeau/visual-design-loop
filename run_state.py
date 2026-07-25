"""Persistent run state: run.json metadata + per-iteration snapshots.

Best-effort persistence — read failures degrade to "no state", write failures
warn and continue. A run must never die because the disk hiccuped.
Snapshots are append-only; undo moves a pointer, it never deletes files.
"""

import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


class RunState:
    def __init__(self, state_dir=None):
        self.state_dir = state_dir or config.RUN_STATE_DIR
        self.iterations_dir = self.state_dir / "iterations"
        self.run_path = self.state_dir / "run.json"
        self.events_path = self.state_dir / "events.jsonl"

    def _ensure_dirs(self):
        os.makedirs(self.iterations_dir, exist_ok=True)

    def has_unfinished(self) -> bool:
        """True if run.json exists and records an unfinished run.
        Corrupt or unreadable state is reported as no state (with a warning)."""
        try:
            with open(self.run_path) as f:
                return json.load(f).get("finished") is False
        except FileNotFoundError:
            return False
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️ Unreadable run state ({e}) — treating as no state.")
            return False

    def save_run(self, meta: dict):
        """Atomic write (tmp + rename) of run.json. Warns on failure."""
        try:
            self._ensure_dirs()
            tmp = str(self.run_path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp, self.run_path)
        except OSError as e:
            print(f"⚠️ Could not save run state: {e}")

    def load_run(self) -> dict:
        with open(self.run_path) as f:
            return json.load(f)

    def append_event(self, event: dict):
        """Append one event line to events.jsonl (ts auto-stamped).
        Best-effort — warns on failure, never raises."""
        try:
            self._ensure_dirs()
            record = dict(event)
            record.setdefault("ts", time.time())
            with open(self.events_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            print(f"⚠️ Could not append run event: {e}")

    def read_events(self) -> list:
        """All parseable events in file order; [] when the log is missing.
        Corrupt lines (e.g. a torn final line) are skipped silently."""
        try:
            with open(self.events_path) as f:
                lines = f.readlines()
        except (FileNotFoundError, OSError):
            return []
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def snapshot(self, n: int, code: str, theme: dict, audit: dict):
        """Write iteration n's raw code + {theme_json, audit}. Warns on failure."""
        try:
            self._ensure_dirs()
            with open(self.iterations_dir / f"{n:03d}.html", "w") as f:
                f.write(code)
            with open(self.iterations_dir / f"{n:03d}.json", "w") as f:
                json.dump({"theme_json": theme, "audit": audit}, f, indent=2)
        except OSError as e:
            print(f"⚠️ Could not snapshot iteration {n}: {e}")

    def load_snapshot(self, n: int):
        """Returns (code, theme_json, audit) for iteration n. Raises if missing."""
        with open(self.iterations_dir / f"{n:03d}.html") as f:
            code = f.read()
        with open(self.iterations_dir / f"{n:03d}.json") as f:
            data = json.load(f)
        return code, data.get("theme_json", {}), data.get("audit", {})

    def latest_iteration(self) -> int:
        try:
            names = os.listdir(self.iterations_dir)
        except (FileNotFoundError, OSError):
            return 0
        nums = [int(n[:3]) for n in names if n.endswith(".html") and n[:3].isdigit()]
        return max(nums, default=0)

    def clear(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)
