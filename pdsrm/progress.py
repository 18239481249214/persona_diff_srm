"""Live one-line progress display for parallel groups.

It wraps the model client, so collection code is untouched: every API call reports which dyad (or
agent, for questionnaires) it belongs to, what step it is, and how long it has been waiting. Permanent
log lines are printed through println() so they never collide with the live line."""
import shutil
import sys
import threading
import time
from collections import Counter

LABELS = {"conversation": "chat", "rating": "rate", "memory": "mem", "measure": "survey"}


class StopRequested(Exception):
    """Raised before a new API call once Ctrl+C has been pressed; the unfinished round is discarded."""


def _fmt(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Progress:
    def __init__(self, k, total_rounds):
        self.k, self.total_rounds = k, total_rounds
        self.live = sys.stdout.isatty()
        self.lock = threading.RLock()
        self.local = threading.local()
        self.stop_event = threading.Event()
        self.slots = {}  # slot -> {"stage": str, "counts": Counter, "active": [start times]}
        self.calls = self.rounds_done = 0
        self.t0 = time.time()
        self._halt = threading.Event()
        self._thread = None
        self._width = 0

    # ---------------------------------------------------------- lifecycle
    def start(self):
        self.t0 = time.time()
        if self.live:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._halt.set()
        if self._thread:
            self._thread.join()
        with self.lock:
            self._clear()

    def println(self, msg):
        with self.lock:
            self._clear()
            print(msg, flush=True)

    def _clear(self):
        if self.live and self._width:
            sys.stdout.write("\r" + " " * self._width + "\r")
            sys.stdout.flush()

    # ---------------------------------------------------------- slots
    def add(self, slots):
        with self.lock:
            for s in slots:
                self.slots[s] = {"stage": "waiting", "counts": Counter(), "active": []}

    def remove(self, slots):
        with self.lock:
            for s in slots:
                self.slots.pop(s, None)

    def bind(self, slot):
        self.local.slot = slot

    def current(self):
        return getattr(self.local, "slot", None)

    def round_done(self, n=1):
        with self.lock:
            self.rounds_done += n

    # ---------------------------------------------------------- around each API call
    def before(self, kind):
        if self.stop_event.is_set():
            raise StopRequested()
        slot = self.current()
        with self.lock:
            st = self.slots.get(slot)
            if st is None:
                return None
            st["counts"][kind] += 1
            n = st["counts"][kind]
            label = LABELS.get(kind, kind)
            st["stage"] = (f"{label} {n}/{self.k}" if kind == "conversation"
                           else f"{label} {n}/2" if kind == "rating" else f"{label} {n}")
            t = time.time()
            st["active"].append(t)
            return t

    def after(self, token):
        slot = self.current()
        with self.lock:
            self.calls += 1
            st = self.slots.get(slot)
            if st is not None and token in st["active"]:
                st["active"].remove(token)

    # ---------------------------------------------------------- rendering
    def _line(self):
        now = time.time()
        tail = f" | rounds {self.rounds_done}/{self.total_rounds} | {self.calls} calls | {_fmt(now - self.t0)}"
        if not self.slots:
            return "  working" + tail
        if len(self.slots) <= 3:
            parts = []
            for slot, st in self.slots.items():
                wait = f" {int(now - min(st['active']))}s" if st["active"] else ""
                parts.append(f"{slot} {st['stage']}{wait}")
            return "  " + " | ".join(parts) + tail
        waits = [now - t for st in self.slots.values() for t in st["active"]]
        groups = len({s.split("-")[0] for s in self.slots})
        slowest = f" | slowest {int(max(waits))}s" if waits else ""
        return f"  {groups} grp, {len(self.slots)} active{slowest}" + tail

    def _loop(self):
        while not self._halt.wait(1.0):
            with self.lock:
                cols = shutil.get_terminal_size((80, 20)).columns - 1
                line = self._line()[:cols]
                self._width = max(self._width, len(line))
                sys.stdout.write("\r" + line.ljust(self._width))
                sys.stdout.flush()


class ProgressClient:
    """Drop-in wrapper with the same complete() signature as the real client."""

    def __init__(self, client, progress):
        self.client, self.progress = client, progress
        if hasattr(client, "on_retry"):
            client.on_retry = lambda msg: progress.println(f"    ! [{progress.current()}] {msg}")

    def complete(self, messages, max_tokens, kind, expect=None):
        token = self.progress.before(kind)
        try:
            return self.client.complete(messages, max_tokens, kind, expect)
        finally:
            self.progress.after(token)


def carry_slot(progress, fn):
    """Wrap fn so a helper thread reports under the same progress slot as its parent."""
    slot = progress.current()

    def wrapped(*a, **kw):
        progress.bind(slot)
        return fn(*a, **kw)
    return wrapped
