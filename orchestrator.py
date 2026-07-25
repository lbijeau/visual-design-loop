"""CLI entry point: wires config, status, server, and loop together.

Usage: python3 orchestrator.py ["design intent"] [--max-iterations N] [--port P] [--resume | --fresh]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from loop import FrontendDesignLoop
from run_state import RunState
from server import LivePreviewServer
from status import LoopStatus


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="StyleSentry design loop")
    p.add_argument("intent", nargs="?", default=None, help="design intent (omit to enter it in the web shell)")
    p.add_argument("--max-iterations", type=int, default=config.MAX_ITERATIONS, help=f"iteration budget (default {config.MAX_ITERATIONS})")
    p.add_argument("--port", type=int, default=config.DEFAULT_PORT, help=f"base port; hunts base..base+100 (default {config.DEFAULT_PORT})")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--resume", action="store_true", help="resume an unfinished run without prompting")
    g.add_argument("--fresh", action="store_true", help="discard any unfinished run state")
    return p.parse_args(argv)


def decide_resume(args, run_state: RunState, ask=input) -> bool:
    """Startup decision: resume, or start fresh (clearing happens in run())."""
    if args.fresh:
        run_state.clear()
        return False
    if args.resume:
        if run_state.has_unfinished():
            return True
        print("No unfinished run found — starting fresh.")
        return False
    if run_state.has_unfinished():
        try:
            meta = run_state.load_run()
        except Exception:
            return False
        reply = ask(
            f'Unfinished run found ("{meta.get("intent", "?")}", '
            f"iteration {meta.get('iteration', '?')} of "
            f"{meta.get('max_iterations', '?')}). Resume? [Y/n] "
        )
        return reply.strip().lower() not in ("n", "no")
    return False


def main():
    args = parse_args()
    resume = decide_resume(args, RunState())
    status = LoopStatus()
    server = LivePreviewServer(status=status, port=args.port)
    loop = FrontendDesignLoop(args.intent, max_iterations=args.max_iterations, status=status)
    server.start()
    try:
        loop.run(port=server.port, resume=resume)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
