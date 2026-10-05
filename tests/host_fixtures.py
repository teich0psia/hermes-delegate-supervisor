"""Real host bodies without importing the gateway/live-agent entry points."""
import ast
import asyncio
import concurrent.futures
import contextvars
import logging
from pathlib import Path
import sys
import threading
import time
import types
import uuid
from types import SimpleNamespace

import hermes_constants
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.platforms.event import MessageEvent, MessageType

ROOT = Path(hermes_constants.__file__).resolve().parent


def body(relative, name, scope=None):
    path = ROOT / relative
    tree = ast.parse(path.read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    namespace = dict(scope or {})
    # Host modules commonly postpone annotations; extracted bodies need the same rule.
    import __future__
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace[name]


def gateway(manager, monkeypatch, session="parent", key="route-origin"):
    pending = []
    delivered = []
    homes = []
    def schedule(coro, loop, **kwargs):
        pending.append((coro, contextvars.copy_context()))
        return concurrent.futures.Future()
    run = types.ModuleType("gateway.run")
    run.safe_schedule_threadsafe = schedule
    monkeypatch.setitem(sys.modules, "gateway.run", run)
    scope = dict(asyncio=asyncio, concurrent=concurrent, logging=logging,
                 logger=logging.getLogger("supervisor-test"), MessageEvent=MessageEvent, MessageType=MessageType)
    class Gateway:
        _schedule_plugin_message_injection = body("gateway/run_inbound.py", "_schedule_plugin_message_injection", scope)
        _dispatch_plugin_message_injection = body("gateway/run_inbound.py", "_dispatch_plugin_message_injection", scope)
    class Store:
        async def lookup_by_session_key(self, requested):
            assert requested == key
            return g.entry
    class Adapter:
        async def handle_message(self, event):
            delivered.append(event)
            homes.append(hermes_constants.get_hermes_home())
    g = Gateway()
    g._running = True
    g._draining = False
    g._gateway_loop = SimpleNamespace(is_closed=lambda: False)
    g.async_session_store = Store()
    g.entry = SimpleNamespace(origin=object(), session_id=session)
    source = SessionSource(platform=Platform.DISCORD, chat_id=key, user_id="offline-user")
    g._restored_source = lambda entry: source
    g._is_user_authorized_for_source = lambda source, **kwargs: True
    g._delivery_adapter_for = lambda source: Adapter()
    g.pending, g.delivered, g.homes = pending, delivered, homes
    def drain():
        results = [context.run(asyncio.run, coro) for coro, context in pending]
        pending.clear()
        return results
    g.drain = drain
    manager.set_gateway_message_injector(g, g._schedule_plugin_message_injection)
    return g


def tui(manager, key="old-key", session_id="old"):
    from tui_gateway import plugin_inject
    session = {"session_key":key, "agent":SimpleNamespace(session_id=session_id),
               "history_lock":threading.RLock(), "running":True}
    def enqueue(session, content, transport):
        session["queued_prompt"] = {"text":content, "transport":transport}
    drained = threading.Event()
    namespace = SimpleNamespace(_sessions={"ui-parent":session}, _sessions_lock=threading.RLock(),
        _enqueue_prompt=enqueue, _drain_queued_prompt=lambda *a: drained.set(), time=time, uuid=uuid, threading=threading)
    plugin_inject.register(namespace)
    manager.set_tui_message_injector(object(), namespace.inject_tui_session_message)
    return session, drained


def compression(home, old="old", new="compressed", config=None):
    # Real schema and atomic publication: no compression model or live agent.
    from hermes_state import SessionDB
    db = SessionDB(home / "state.db")
    db.create_session(old, source="cli", model="offline")
    db.publish_compression_child(parent_session_id=old, child_session_id=new, source="cli",
        messages=[{"role":"assistant", "content":"offline handoff"}], require_compression_lease=False,
        model_config=config)
    db.close()
