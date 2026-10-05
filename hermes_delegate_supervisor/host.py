"""Narrow host adapter: public injector plus lease-fenced classic-CLI FIFO delivery."""
from __future__ import annotations

import contextvars
import hashlib
import inspect
import textwrap
from dataclasses import dataclass
import logging
import queue
from pathlib import Path
from dataclasses import replace
from typing import Callable

from .adapters import (GatewayGuard, Lineage, _EXPECTED, DISPATCH_HASH, TUI_HASH, GUARD_LOCK,
                       function_supported, source_supported)

log = logging.getLogger(__name__)
# Dedented source fingerprint of the audited serial queue consumer (Python-version independent).
_CLI_CONSUMER_SHA256 = "caed133bd6913f5fd7ed110b67c18a656eb7c64e6972af647d699906ecc4e45c"


def _classic_consumer_supported(cli):
    try:
        function = getattr(cli, "_tui_process_loop", None)
        if not callable(function):
            return False
        source = textwrap.dedent(inspect.getsource(function)).strip()
        digest = hashlib.sha256(source.encode()).hexdigest()
        return digest == _CLI_CONSUMER_SHA256
    except (TypeError, OSError):
        return False


_CLI_INPUT_SHA256 = "de62de5af55e3a5abc16f6d991018be398937f87822eb876b028cf371d329487"


@dataclass(frozen=True)
class _CLIWake:
    guard: _CLIGuard
    host: Host
    route: Route
    message: str
    consume: Callable[[bool], bool]


class _CLIGuard:
    """Fence only our typed queue entries at the serial worker's dispatch boundary.

    Do not restore while an envelope may already be dequeued. On unload the
    inert guard drains those entries, then restores only its own instance slot.
    User/plugin inputs pass through unchanged, without holding GUARD_LOCK.
    """
    def __init__(self, cli, original):
        self.owner, self.original = cli, original
        self.queue = cli._pending_input
        self.homes = {}
        self.outstanding = 0
        self.had_previous = "_tui_process_one_input" in cli.__dict__
        self.previous = cli.__dict__.get("_tui_process_one_input")
        def dispatch(user_input):
            if not isinstance(user_input, _CLIWake):
                return original(user_input)
            if user_input.guard is not self:
                return None  # never forward an unknown supervisor envelope
            try:
                with GUARD_LOCK:
                    owned = user_input.host in self.homes.get(user_input.host.home, ())
                cli_session = str(getattr(cli, "session_id", "") or "")
                agent_session = str(getattr(getattr(cli, "agent", None), "session_id", "") or "")
                current = (owned and not user_input.host.closed and not cli._should_exit
                           and not cli._agent_running and cli_session == agent_session == user_input.route.session)
                # No guard/queue lock around the supervisor condition lock or chat.
                if user_input.consume(current):
                    return original(user_input.message)
                return None
            finally:
                with GUARD_LOCK:
                    self.outstanding -= 1
                    self._restore_if_drained()
        self.dispatch = dispatch
        setattr(dispatch, "_delegate_supervisor_cli_guard", self)
        cli._tui_process_one_input = dispatch

    def _restore_if_drained(self):
        if self.homes or self.outstanding:
            return
        if getattr(self.owner, "_tui_process_one_input", None) is self.dispatch:
            if self.had_previous:
                self.owner._tui_process_one_input = self.previous
            else:
                del self.owner._tui_process_one_input

    def remove(self, home, host):
        holders = self.homes.get(home, set())
        holders.discard(host)
        if not holders:
            self.homes.pop(home, None)
        self._restore_if_drained()


@dataclass(frozen=True)
class Route:
    key: str
    session: str
    context: object
    cli: object = None
    ui: object = None


class Host:
    def __init__(self, ctx):
        from hermes_constants import get_hermes_home
        from gateway.session_context import get_session_env, async_delivery_supported
        from tools.delegate_tool_registry import list_active_subagents
        if not all(callable(getattr(ctx, name, None)) for name in
                   ("inject_message", "register_hook", "register_command", "register_middleware", "on_unload", "get_config", "_gateway_injection_allowed")):
            raise RuntimeError("required PluginContext capabilities unavailable")
        self.ctx = ctx
        self.home = get_hermes_home().resolve()
        self.env = get_session_env
        self.async_supported = async_delivery_supported
        self.snapshot = list_active_subagents
        import hermes_constants
        self.root = Path(hermes_constants.__file__).resolve().parent
        self.lineage = Lineage(self.home, self.root)
        self.guards = []
        self.closed = False
        self.failed_batch_supported = all(source_supported(self.root, *spec) for spec in (
            ("tools/delegate_tool.py", "delegate_task", "f2c4b58d48559aa82a742cc112d0161018eca84dc28b6ae1b8e31743257fd25b"),
            ("tools/delegate_tool.py", "_build_children", "8a68d6d5bea14fc34111b2a2fc8a0b58516a4f3965cb727692ffae1a0d0aec9c"),
        ))

    def continues(self, old, new):
        return self.lineage.continues(old, new)

    def _ui(self, key):
        slot = getattr(self.ctx._manager, "_tui_message_injector", None)
        if not slot or not function_supported(slot[1], TUI_HASH):
            return None
        namespace = slot[1].__globals__
        with namespace["_sessions_lock"]:
            matches = [s for s in namespace["_sessions"].values()
                       if isinstance(s, dict) and s.get("session_key") == key]
            return matches[0] if len(matches) == 1 else None

    def refresh(self, route):
        """Follow only a proven compression on the *same* live surface object."""
        if route.cli is not None:
            current = str(getattr(route.cli, "session_id", "") or "")
            agent_id = str(getattr(getattr(route.cli, "agent", None), "session_id", "") or "")
            if current and current == agent_id and self.continues(route.session, current):
                return replace(route, key=current, session=current)
        elif route.ui is not None:
            current = str(getattr(route.ui.get("agent"), "session_id", "") or "")
            key = route.ui.get("session_key")
            if key and self._ui(key) is route.ui and self.continues(route.session, current):
                return replace(route, key=key, session=current)
        return route

    def _guard_gateway(self):
        with GUARD_LOCK:
            return not self.closed and self._guard_gateway_locked()

    def _guard_gateway_locked(self):
        slot = getattr(self.ctx._manager, "_gateway_message_injector", None)
        if not slot:
            return False
        owner, scheduler = slot
        if getattr(scheduler, "__self__", None) is not owner or not function_supported(
            scheduler, "04e48da92240f9faab36594f97f315224c42a6167bb760b1e7d4ff12a9c7bfa0"
        ):
            return False
        dispatch = getattr(owner, "_dispatch_plugin_message_injection", None)
        guard = getattr(dispatch, "_delegate_supervisor_guard", None)
        if guard is None:
            if not function_supported(dispatch, DISPATCH_HASH) or not source_supported(
                self.root, "gateway/run_turn.py", "_hmwa_resolve_session",
                "a317ac5bdd4f66e2709a8f5343b9c66b905122d8996222e260fd76f41b4bfd09"
            ):
                return False
            guard = GatewayGuard(owner, dispatch)
            setattr(guard.dispatch, "_delegate_supervisor_guard", guard)
        if not guard.enabled:
            return False
        if guard not in self.guards:
            self.guards.append(guard)
        guard.homes.setdefault(self.home, set()).add(self)
        return True

    def _guard_cli(self, cli):
        with GUARD_LOCK:
            pending = getattr(cli, "_pending_input", None)
            if (self.closed or type(pending) is not queue.Queue or pending.maxsize != 0
                    or not _classic_consumer_supported(cli)):
                return None
            function = getattr(cli, "_tui_process_one_input", None)
            guard = getattr(function, "_delegate_supervisor_cli_guard", None)
            if guard is None:
                if not function_supported(function, _CLI_INPUT_SHA256):
                    return None
                guard = _CLIGuard(cli, function)
            if guard.owner is not cli or cli._pending_input is not guard.queue:
                return None
            guard.homes.setdefault(self.home, set()).add(self)
            if guard not in self.guards:
                self.guards.append(guard)
            return guard

    def close(self):
        with GUARD_LOCK:
            self.closed = True
            for guard in self.guards:
                guard.remove(self.home, self)
            self.guards.clear()

    def capture(self, session):
        from hermes_constants import get_hermes_home
        import os
        if get_hermes_home().resolve() != self.home or not session:
            return None
        if not self.async_supported() or os.environ.get("HERMES_SINGLE_QUERY_SESSION"):
            return None
        # Bound session context only. Do not manufacture messaging keys from durable ids.
        key = self.env("HERMES_SESSION_KEY", "")
        bound_session = self.env("HERMES_SESSION_ID", "")
        if key and bound_session and bound_session != session:
            log.warning("delegate_supervisor INACTIVE: parent/context session mismatch")
            return None
        manager = getattr(self.ctx, "_manager", None)
        cli = getattr(manager, "_cli_ref", None)
        if cli is not None:
            # Shape gate for the single-consumer classic REPL. Never its interrupt queue.
            pending = getattr(cli, "_pending_input", None)
            if type(pending) is not queue.Queue or pending.maxsize != 0:
                log.warning("delegate_supervisor INACTIVE: unsupported classic CLI queue")
                return None
            if not _classic_consumer_supported(cli) or self._guard_cli(cli) is None:
                log.warning("delegate_supervisor INACTIVE: unsupported classic CLI consumer/dispatcher")
                return None
            current = str(getattr(cli, "session_id", "") or getattr(getattr(cli, "agent", None), "session_id", "") or "")
            if current != session and not (self.continues(current, session)
                    and getattr(getattr(cli, "agent", None), "session_id", None) == session):
                return None
            return Route(session, session, contextvars.copy_context(), cli)
        if not key:
            log.warning("delegate_supervisor INACTIVE: no addressed parent wake route")
            return None
        # API servers bind keys too, but do not own an automatic model-turn consumer.
        platform = self.env("HERMES_SESSION_PLATFORM", "")
        source = self.env("HERMES_SESSION_SOURCE", "")
        if platform in {"api_server", "kanban", "tool", "webhook"} or source in {"api_server", "kanban", "tool", "webhook"}:
            log.warning("delegate_supervisor INACTIVE: surface has no automatic parent turn")
            return None
        return Route(key, session, contextvars.copy_context(), ui=self._ui(key))

    async def command_route(self):
        """Resolve native slash callers without inventing handler kwargs.

        CLI owns its live id; TUI binds it. Gateway slash binds only the key,
        so use the published host's read-only canonical lookup, not os.environ.
        No session is created on behalf of a never-started conversation.
        """
        from hermes_constants import get_hermes_home
        from gateway.session_context import scoped_current_session_id
        from agent.delegation_context import is_delegated_child_context
        if self.closed or get_hermes_home().resolve() != self.home or is_delegated_child_context():
            return None
        cli = getattr(self.ctx._manager, "_cli_ref", None)
        if cli is not None:
            session = str(getattr(cli, "session_id", "") or "")
            with scoped_current_session_id(session):
                return self.capture(session)
        session = self.env("HERMES_SESSION_ID", "")
        if session:
            return self.capture(session)
        key = self.env("HERMES_SESSION_KEY", "")
        slot = getattr(self.ctx._manager, "_gateway_message_injector", None)
        if not key or not slot:
            return None
        owner, scheduler = slot
        if (getattr(scheduler, "__self__", None) is not owner or not function_supported(
                scheduler, "04e48da92240f9faab36594f97f315224c42a6167bb760b1e7d4ff12a9c7bfa0")):
            return None
        entry = await owner.async_session_store.lookup_by_session_key(key)
        if entry is None or self.closed:
            return None
        session = str(entry.session_id or "")
        return self.capture(session)

    def live_ids(self):
        rows = self.snapshot()
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("subagent_id"), str) for r in rows):
            raise RuntimeError("unsupported live-subagent snapshot")
        return {r["subagent_id"] for r in rows if r.get("status") == "running"}

    def wake(self, route, message):
        return self.wake_checked(route, message, lambda current: current)

    def wake_checked(self, route, message, consume):
        def deliver():
            from hermes_constants import get_hermes_home
            if self.closed or get_hermes_home().resolve() != self.home:
                return False
            if route.cli is not None:
                cli = route.cli
                current = str(getattr(cli, "session_id", "") or getattr(getattr(cli, "agent", None), "session_id", "") or "")
                if current != route.session or getattr(cli, "_should_exit", False):
                    return False
                with GUARD_LOCK:
                    guard = self._guard_cli(cli)
                    if guard is None or self.closed:
                        return False
                    guard.outstanding += 1
                    try:
                        cli._pending_input.put(_CLIWake(guard, self, route, message, consume))
                    except Exception:
                        guard.outstanding -= 1
                        raise
                return True
            if route.ui is not None:
                # Public inject falls through on a UI miss; that is unsafe after
                # key rotation. Keep the same profile-scoped consent gate but
                # address only this audited UI host, with no gateway fallback.
                if self._ui(route.key) is not route.ui or not self.ctx._gateway_injection_allowed():
                    return False
                with route.ui["history_lock"]:
                    if str(getattr(route.ui.get("agent"), "session_id", "") or "") != route.session:
                        return False
                    return self.ctx._manager.inject_tui_message(
                        session_key=route.key, content=message, plugin_id=self.ctx.plugin_id)
            if not self._guard_gateway():
                log.warning("delegate_supervisor INACTIVE: unsupported gateway identity dispatcher")
                return False
            token = _EXPECTED.set((self.home, self, route.key, route.session))
            try:
                return self.ctx.inject_message(message, role="user", session_key=route.key)
            finally:
                _EXPECTED.reset(token)
        # Preserves originating profile and consent lookup without modifying os.environ.
        return bool(route.context.copy().run(deliver))
