"""In-memory, monotonic scheduling. No model calls, routing, or completion delivery."""
from __future__ import annotations

import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)
WAKE = "[delegate_supervisor] Periodic review request. Act ONLY if current supervisor context explicitly names running targets; without that context (including supervision off), do not review or steer any child."


def interval_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("interval_seconds must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("interval_seconds must be a finite positive number")
    return float(value)


@dataclass
class Child:
    ident: str
    session: str
    parent: str
    due: float
    seen: bool = False


@dataclass
class Parent:
    route: object
    busy: bool = False
    pending: set = field(default_factory=set)
    queued: str | None = None
    retry_at: float = 0
    override: float | None = None
    disabled: bool = False

    @property
    def enabled(self):
        return not self.disabled


class Supervisor:
    def __init__(self, host, interval=600, clock=time.monotonic):
        self.host = host
        self.interval = interval_seconds(interval)
        self.clock = clock
        self.children = {}
        self.parents = {}
        self._cv = threading.Condition(threading.RLock())
        self._closed = False
        self._thread = None

    def effective_interval(self, parent):
        return parent.override if isinstance(parent.override, float) else self.interval

    def control(self, route, raw_args):
        """Atomic session policy update; no timer teardown or global config writes."""
        arg = raw_args.strip().lower()
        value = None
        if arg not in {"", "status", "off", "default"}:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", arg)
            try:
                if match is None:
                    raise ValueError()
                value = interval_seconds(float(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]])
                if value < 1 or not math.isfinite(self.clock() + value):
                    raise ValueError()
            except (ValueError, OverflowError):
                return "Usage: /supervision [<duration: 10m, 30s, 1h> | off | default] (minimum 1s). No change."
        if route is None:
            return "Supervision unavailable: no supported current conversation. Send a normal message first if this gateway has no session yet."
        with self._cv:
            if self._closed:
                return "Supervision unavailable: plugin is inactive."
            parent = self.parents.get(route.key)
            # A command's canonical gateway index can lag committed compression.
            # Never move a known current generation backwards to that ancestor.
            if not (parent is not None and parent.route.session != route.session
                    and self.host.continues(route.session, parent.route.session)):
                parent = self._parent(route)
            if arg and arg != "status":
                parent.disabled = arg == "off"
                if arg != "off":
                    parent.override = value
                now = self.clock()
                for child in self.children.values():
                    if child.parent == parent.route.key:
                        child.due = now + self.effective_interval(parent)
                parent.retry_at = 0
                if not parent.enabled:
                    parent.pending.clear()
                    if getattr(parent.route, "cli", None) is not None:
                        parent.queued = None
                # Enabled changes keep unresolved work and accepted reservation.
                self._cv.notify_all()
            targets = sorted(c.ident for c in self.children.values() if c.parent == parent.route.key)
            return (f"Supervision: {'enabled' if parent.enabled else 'disabled'}; "
                    f"interval: {self.effective_interval(parent):g}s; "
                    f"source: {'default' if parent.override is None and parent.enabled else 'override'}; "
                    f"targets: {', '.join(targets) or 'none'}; "
                    f"parent: {'busy' if parent.busy else 'idle'}; "
                    f"pending: {len(parent.pending)}; accepted wake: {'yes' if parent.queued else 'no'}.")

    def start(self):
        with self._cv:
            if self._thread is None and not self._closed:
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="delegate-supervisor")
                self._thread.start()

    def _parent(self, route):
        parent = self.parents.get(route.key)
        if parent is not None and parent.route.session == route.session:
            parent.route = route
            return parent
        continues = getattr(self.host, "continues", lambda old, new: False)
        candidates = [(k, p) for k, p in self.parents.items()
                      if (k == route.key
                          or (getattr(route, "cli", None) is not None and route.cli is getattr(p.route, "cli", None))
                          or (getattr(route, "ui", None) is not None and route.ui is getattr(p.route, "ui", None)))
                      and continues(p.route.session, route.session)]
        if len(candidates) == 1:
            old_key, parent = candidates[0]
            if route.key in self.parents and self.parents[route.key] is not parent:
                self.stop_parent(route.key)
            self.parents.pop(old_key, None)
            self.parents[route.key] = parent
            for child in self.children.values():
                if child.parent == old_key:
                    child.parent = route.key
            if parent.route.session != route.session:
                # An accepted wake pinned to the closed generation may be refused.
                parent.queued = None
            parent.route = route
            return parent
        if parent is not None:
            # /resume or reset is NOT compression. Never adopt its old children.
            self.stop_parent(route.key)
        parent = Parent(route)
        self.parents[route.key] = parent
        return parent

    def is_child_session(self, session):
        with self._cv:
            return any(child.session == session for child in self.children.values())

    def spawn(self, route, ident, child_session):
        if not route or not ident or not child_session:
            return
        with self._cv:
            if self._closed or ident in self.children:
                return
            parent = self._parent(route)
            self.children[ident] = Child(ident, child_session, route.key,
                                         self.clock() + self.effective_interval(parent))
            self._cv.notify_all()

    def finish(self, *, ident=None, child_session=None):
        with self._cv:
            for key, child in list(self.children.items()):
                if (ident and key == ident) or (child_session and child.session == child_session):
                    self._remove(key)
            self._cv.notify_all()

    def _remove(self, ident):
        child = self.children.pop(ident, None)
        if child is None:
            return
        parent = self.parents.get(child.parent)
        if parent:
            parent.pending.discard(ident)
            if not any(c.parent == child.parent for c in self.children.values()):
                if getattr(parent.route, "cli", None) is not None:
                    parent.queued = None  # irrevocably revoke its queued CLI lease
                if not parent.busy and not parent.queued and parent.override is None and parent.enabled:
                    self.parents.pop(child.parent, None)

    def stop_parent(self, key, *, clear_settings=True):
        with self._cv:
            for ident, child in list(self.children.items()):
                if child.parent == key:
                    self._remove(ident)
            parent = self.parents.get(key)
            if clear_settings or parent is None or (parent.override is None and parent.enabled):
                self.parents.pop(key, None)
            else:
                parent.busy = False
                parent.pending.clear()
                if getattr(parent.route, "cli", None) is not None:
                    parent.queued = None
            self._cv.notify_all()

    def stop_session(self, session, *, clear_settings=True):
        with self._cv:
            for key, parent in list(self.parents.items()):
                if parent.route.session == session:
                    self.stop_parent(key, clear_settings=clear_settings)

    def turn_start(self, route, acknowledged_tokens=()):
        if route is None:
            return None
        # Snapshot is read outside the lock; a lifecycle hook remains the definitive removal.
        live = self.host.live_ids()
        with self._cv:
            if self._closed:
                return None
            parent = self._parent(route)
            parent.busy = True
            if parent.queued in acknowledged_tokens:
                parent.queued = None
            self._reconcile(live)
            if not parent.enabled:
                parent.pending.clear()
                return None
            for ident, child in self.children.items():
                if child.parent == route.key and ident in live and self.clock() >= child.due:
                    parent.pending.add(ident)
            targets = sorted(parent.pending & live & self.children.keys())
            parent.pending.clear()
            if targets and getattr(parent.route, "cli", None) is not None:
                # CLI envelopes remain fenced: resolving their work revokes the
                # old lease, it is not an acknowledgement of FIFO delivery.
                parent.queued = None
            for ident in targets:
                self.children[ident].due = self.clock() + self.effective_interval(parent)
            if not targets:
                return None
            return {"context": (
                "[delegate_supervisor] Periodic review of YOUR running children is due: "
                + ", ".join(targets)
                + ". Inspect delegate_task(action='list') and relevant evidence if needed. "
                "Decide yourself whether course correction is necessary; use the existing "
                "delegate_task(action='steer', subagent_id=..., message=...) only for a still-running child. "
                "Do not re-delegate, change routing, poll for completion, or act on finished children. "
                "Normal completions and stall notifications remain host-owned."
            )}

    def turn_end(self, route):
        if route is None:
            return
        with self._cv:
            if self._closed:
                return
            parent = self._parent(route)
            if parent:
                parent.busy = False
                parent.route = route
                if parent.override is None and parent.enabled and not parent.queued and not any(c.parent == route.key for c in self.children.values()):
                    self.parents.pop(route.key, None)
            self._cv.notify_all()

    def _reconcile(self, live):
        for ident, child in list(self.children.items()):
            if ident in live:
                child.seen = True
            elif child.seen:
                self._remove(ident)

    def _consume_cli_lease(self, route, parent, token, current):
        """Called only at classic CLI dispatch, never while holding a queue lock.

        A natural turn can resolve pending work without acknowledging the old
        token. Discard that envelope and retire only its own reservation, never
        the new token installed after compression or a different parent.
        """
        with self._cv:
            valid = (current and not self._closed and self.parents.get(route.key) is parent
                     and parent.route.session == route.session and parent.queued == token
                     and parent.enabled and not parent.busy and bool(parent.pending & self.children.keys()))
            if not valid and parent.queued == token:
                parent.queued = None
                if (self.parents.get(route.key) is parent and not parent.busy
                        and parent.override is None and parent.enabled and not any(c.parent == route.key for c in self.children.values())):
                    self.parents.pop(route.key, None)
            return valid

    def tick(self):
        live = self.host.live_ids()
        with self._cv:
            if self._closed:
                return
            refresh = getattr(self.host, "refresh", lambda route: route)
            for parent in list(self.parents.values()):
                route = refresh(parent.route)
                if route != parent.route:
                    self._parent(route)
            self._reconcile(live)
            now = self.clock()
            for ident, child in self.children.items():
                if self.parents[child.parent].enabled and ident in live and now >= child.due:
                    self.parents[child.parent].pending.add(ident)
            for key, parent in list(self.parents.items()):
                if not parent.enabled or parent.busy or parent.queued or not parent.pending or now < parent.retry_at:
                    continue
                # Reserve before admission; synchronous/reentrant turn_start also sees this.
                token = uuid.uuid4().hex
                parent.queued = token
                try:
                    message = WAKE + f"\n[delegate_supervisor_request:{token}]"
                    checked = getattr(self.host, "wake_checked", None)
                    if checked is not None and getattr(parent.route, "cli", None) is not None:
                        route = parent.route
                        accepted = checked(route, message,
                            lambda current, r=route, p=parent, t=token: self._consume_cli_lease(r, p, t, current))
                    else:
                        accepted = self.host.wake(parent.route, message)
                except Exception:
                    accepted = False
                    log.warning("delegate_supervisor: wake failed; retry deferred", exc_info=True)
                if not accepted:
                    if parent.queued == token:
                        parent.queued = None
                    parent.retry_at = now + self.effective_interval(parent)
                    log.warning("delegate_supervisor: wake not accepted; supervision pending for %s", key)

    def close(self):
        with self._cv:
            self._closed = True
            self.children.clear()
            self.parents.clear()
            self._cv.notify_all()
        close = getattr(self.host, "close", None)
        if close:
            close()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self):
        while True:
            with self._cv:
                if self._closed:
                    return
                self._cv.wait(timeout=min([5, self.interval] + [
                    self.effective_interval(p) for p in self.parents.values() if p.enabled]))
                if self._closed:
                    return
            try:
                self.tick()
            except Exception:
                log.warning("delegate_supervisor INACTIVE: host snapshot failed", exc_info=True)
                self.close()
                return
