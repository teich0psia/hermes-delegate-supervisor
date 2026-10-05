"""Audited identity boundaries missing from the public plugin API.

No host files or delegation/routing callables are changed. Runtime wrappers
are limited to audited instance delivery boundaries (gateway and classic CLI).
"""
from __future__ import annotations

import ast
import contextvars
import copy
import hashlib
import inspect
import json
from typing import Any
import sqlite3
import textwrap
import threading

GUARD_LOCK = threading.RLock()

_EXPECTED: contextvars.ContextVar[Any] = contextvars.ContextVar("delegate_supervisor_expected", default=None)
DISPATCH_HASH = "a49a8feea1159f1e3293f0582c3336ba0dcbe5d3a850f6ab903a280a5ca7ddd7"
TUI_HASH = "335cf18d3727a5be6f3bcdf46e6e4386e3d6d9f983bd39a8497193ccbf01478f"


def digest(source):
    return hashlib.sha256(textwrap.dedent(source).strip().encode()).hexdigest()


def function_supported(function, expected):
    try:
        return digest(inspect.getsource(function)) == expected
    except (OSError, TypeError):
        return False


def source_supported(root, relative, name, expected):
    try:
        source = (root / relative).read_text()
        tree = ast.parse(source)
        nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if len(nodes) != 1:
            return False
        node = nodes[0]
        return digest("\n".join(source.splitlines()[node.lineno - 1:node.end_lineno])) == expected
    except (OSError, SyntaxError):
        return False


class GatewayGuard:
    """Keep public consent/scheduling/auth; fence its awaited session lookup.

    ContextVars are copied into the scheduled coroutine/task by the audited
    scheduler. The strict event metadata is then rechecked by the host consumer.
    A copied row prevents /resume's in-place mutation changing the pinned id.
    """
    def __init__(self, owner, original):
        self.owner = owner
        self.original = original
        self.enabled = True
        self.homes = {}
        self.previous = owner.__dict__.get("_dispatch_plugin_message_injection")
        self.had_previous = "_dispatch_plugin_message_injection" in owner.__dict__
        async def dispatch(**kwargs):
            expected = _EXPECTED.get()
            if expected is None:
                return await original(**kwargs)
            home, lease, key, session = expected
            if not self.enabled or lease not in self.homes.get(home, ()) or key != kwargs.get("session_key"):
                return False
            guard = self
            class Store:
                async def lookup_by_session_key(self, lookup_key):
                    entry = await guard.owner.async_session_store.lookup_by_session_key(lookup_key)
                    if (entry is None or entry.session_id != session or not guard.enabled
                            or lease not in guard.homes.get(home, ())):
                        return None
                    return copy.copy(entry)
            class Proxy:
                async_session_store = Store()
                def __getattr__(self, name):
                    return getattr(guard.owner, name)
            return await original.__func__(Proxy(), **kwargs)
        self.dispatch = dispatch
        owner._dispatch_plugin_message_injection = dispatch

    def remove(self, home, lease):
        holders = self.homes.get(home, set())
        holders.discard(lease)
        if not holders:
            self.homes.pop(home, None)
        if self.homes:
            return
        self.enabled = False  # already scheduled bound wrappers also fail closed
        if getattr(self.owner, "_dispatch_plugin_message_injection", None) is self.dispatch:
            if self.had_previous:
                self.owner._dispatch_plugin_message_injection = self.previous
            else:
                del self.owner._dispatch_plugin_message_injection


class Lineage:
    """Read only the originating profile DB; never construct SessionDB (writes).

    A committed, unique non-fork child of a compression-ended row is evidence.
    An arbitrary changed id, branch, reset, delegate child or ambiguous fork is not.
    """
    def __init__(self, home, root):
        self.path = home / "state.db"
        self.supported = all(source_supported(root, *spec) for spec in (
            ("agent/conversation_compression.py", "_publish_rotated_compaction", "550f1fdbd76c8273c7c2bdc93801f790fd840b5f75d7d1a9a92f659f63595084"),
            ("hermes_state_compression.py", "publish_compression_child", "73de8eea7d2f13c9739b91439c7aa4e2a67dcda0934c000535f6fbc26760bbcd"),
        ))

    def continues(self, old, new):
        if old == new:
            return True
        if not self.supported or not old or not new or not self.path.is_file():
            return False
        db = None
        try:
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.2) as db:
                db.execute("BEGIN")  # all edges from one read snapshot
                seen = set()
                current = old
                while current not in seen:
                    seen.add(current)
                    row = db.execute("SELECT ended_at, end_reason FROM sessions WHERE id=?", (current,)).fetchone()
                    if not row or row[0] is None or row[1] != "compression":
                        return False
                    candidates = []
                    for ident, source, config in db.execute("SELECT id, source, model_config FROM sessions WHERE parent_session_id=?", (current,)):
                        cfg = json.loads(config) if config else {}
                        if source == "tool" or any(cfg.get(k) == current for k in ("_branched_from", "_delegate_from", "_reset_from")):
                            continue
                        candidates.append(ident)
                    if len(candidates) != 1:
                        return False
                    current = candidates[0]
                    if current == new:
                        return True
                return False
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            return False
        finally:
            # sqlite connection context managers commit/rollback, but do not close.
            if db is not None:
                db.close()
