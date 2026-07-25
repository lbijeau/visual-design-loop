"""Thread-safe shared state between the design loop and the HTTP server.

``LoopStatus`` is the single channel through which ``loop.py`` and ``server.py``
communicate.  All mutable fields are protected by ``threading.Lock``.
"""

import threading
from queue import Empty, Queue
from typing import Optional


class LoopStatus:
    """Thread-safe status tracking, audit storage, feedback queue, and events."""

    def __init__(self):
        self._lock = threading.Lock()
        self._phase = "wizard"
        self._message = ""
        self._iteration = 0
        self._max_iterations = 5
        self._feedback_count = 0
        self._model_label = ""
        self._latest_audit: Optional[dict] = None
        self._error = ""
        self._can_undo = False

        # Cross-thread communication
        self.feedback_queue: Queue = Queue()
        self.intent_event: threading.Event = threading.Event()
        self.provider_config_event: threading.Event = threading.Event()
        self.intent: str = ""

    # -- Status fields (locked) --

    def set_phase(self, phase: str, message: str = ""):
        with self._lock:
            self._phase = phase
            self._message = message
            self._error = ""

    def set_model_label(self, label: str):
        with self._lock:
            self._model_label = label

    def set_iteration(self, n: int):
        with self._lock:
            self._iteration = n

    def set_max_iterations(self, n: int):
        with self._lock:
            self._max_iterations = n

    def increment_feedback(self):
        with self._lock:
            self._feedback_count += 1

    def set_error(self, msg: str):
        with self._lock:
            self._error = msg

    def set_can_undo(self, flag: bool):
        with self._lock:
            self._can_undo = bool(flag)

    def get_status(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase,
                "iteration": self._iteration,
                "max_iterations": self._max_iterations,
                "feedback_count": self._feedback_count,
                "message": self._message,
                "model_label": self._model_label,
                "error": self._error,
                "can_undo": self._can_undo,
            }

    # -- Audit (locked) --

    def set_audit(self, audit: dict):
        with self._lock:
            self._latest_audit = audit

    def get_audit(self) -> Optional[dict]:
        with self._lock:
            return self._latest_audit

    # -- Message queue (typed) --

    def put_feedback(self, text: str):
        self.feedback_queue.put({"type": "feedback", "text": text})

    def put_action(self, kind: str):
        if kind not in ("accept", "undo", "save"):
            raise ValueError(f"Unknown action: {kind}")
        self.feedback_queue.put({"type": kind})

    def get_message(self, timeout: Optional[float] = None) -> Optional[dict]:
        try:
            return self.feedback_queue.get(timeout=timeout)
        except Empty:
            return None

    # -- Events --

    def set_intent(self, intent: str):
        self.intent = intent
        self.intent_event.set()

    def await_intent(self, timeout: float = 600) -> bool:
        result = self.intent_event.wait(timeout=timeout)
        if not result:
            print("⚠️ Intent input timed out (10 min). Exiting.")
        return result

    def signal_provider_config(self):
        self.provider_config_event.set()

    def await_provider_config(self, timeout: float = 600) -> bool:
        result = self.provider_config_event.wait(timeout=timeout)
        if not result:
            print("⚠️ Provider configuration timed out (10 min). Using defaults.")
        return result
