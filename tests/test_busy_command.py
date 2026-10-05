"""Native admission/busy/inline/wiring bodies; no live runner, model or transport."""
import asyncio
import contextlib
import logging
import os
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_delegate_supervisor.busy import BUSY_REPLY
from host_fixtures import body
from test_host import ctx, bind
from test_regressions import spawn
from test_supervision_command import gateway_commands


@pytest.fixture
def runtime(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    command("10m")
    bind(key="room", session="old"); spawn(mgr)
    p = s.parents["room"]
    p.busy = True; p.pending = {"a"}; p.queued = "a" * 32
    s.children["a"].due = 1200
    seen = []
    scope = dict(logger=logging.getLogger("busy-test"), MessageType=MessageType, Platform=Platform,
                 os=os, time=time, contextlib=contextlib, Path=Path)
    run = sys.modules["gateway.run"]
    run._AGENT_PENDING_SENTINEL = object()
    run._is_slack_ignored_channel = lambda *a: False
    @contextlib.asynccontextmanager
    async def profile_scope(home):
        token = set_hermes_home_override(home)
        try: yield
        finally: reset_hermes_home_override(token)
    run._async_profile_runtime_scope = profile_scope
    run.get_hermes_home = get_hermes_home
    monkeypatch.setitem(sys.modules, "hermes_cli.observability.shared_metrics_events",
                        SimpleNamespace(record_gateway_slash_command=lambda e: None))
    g.config.multiplex_profiles = False
    # This runner's launch home is the test profile, not the runner harness HOME.
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: s.host.home)
    g._standalone_launch_scope = contextlib.nullcontext
    g.allowed = True; g.bot_allowed = True; g.slash_denied = False
    g._scale_to_zero_note_real_inbound = lambda: None
    async def pre(event, source): return event
    g._hm_pre_gateway_dispatch_hook = pre
    g._is_user_authorized_for_source = lambda source: seen.append(("auth", get_hermes_home())) or g.allowed
    g._admit_bot_message_for_source = lambda source: seen.append(("bot", source.user_id)) or g.bot_allowed
    g._check_slash_access = lambda source, name: seen.append(("slash", name, get_hermes_home())) or ("slash denied" if g.slash_denied else None)
    g._hm_estop_gate = lambda *a: None
    async def intercept(*a): return None
    g._hm_pending_reply_intercepts = intercept
    g._hm_evict_idle_stale_agent = lambda *a: None
    g._hm_evict_reaped_agent = lambda *a: None
    g._is_session_running = lambda key: True
    for name in ("_hm_admit_event", "_hm_handle_running_session_message", "_hm_busy_slash_or_photo", "_handle_message"):
        setattr(g, name, body("gateway/run_inbound.py", name, scope).__get__(g))
    g.mode = "steer"
    g._effective_busy_input_mode = lambda source: g.mode
    g._effective_busy_text_mode = lambda source: "queue"
    async def approval(*a): return False
    g._route_plaintext_approval_while_busy = approval
    async def prepare(event): return event.text
    g._prepare_busy_steer_text = prepare
    g._agent_has_active_subagents = lambda agent: False
    g._peek_session_state = lambda key: SimpleNamespace(turn=SimpleNamespace(agent=SimpleNamespace(steer=lambda t: None), busy_ack_ts=0))
    g._BusySteerOutcome = SimpleNamespace
    g._try_agent_verb = lambda agent, verb, text, key, **kw: seen.append((verb, text)) or True
    g._fold_into_running_turn = lambda *a: None
    g._queue_or_replace_pending_event = lambda key, event: seen.append(("queue", event.text))
    g._resolve_busy_steer_or_redirect = body("gateway/run_busy.py", "_resolve_busy_steer_or_redirect", scope).__get__(g)
    g._handle_active_session_busy_message = body("gateway/run_busy.py", "_handle_active_session_busy_message", scope).__get__(g)
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    g._canonicalize = body("gateway/run_adapters.py", "_canonicalize", dict(scope, suppress=contextlib.suppress)).__get__(g)
    g._transport_owner = lambda source: None
    g._owning_profile = lambda adapter, platform: (True, getattr(adapter, "_owner_profile", None))
    g._primary_profile_name = "default"
    g._profile_name_for_source = lambda source, **kw: source.profile
    g._resolve_profile_home_for_source = lambda source: g.other if source.profile == "other" else s.host.home
    g._admit_primary_source = body("gateway/run_adapters.py", "_admit_primary_source", scope).__get__(g)
    g._async_scope_or_null = lambda factory, home: factory(home)
    g._routed_profile_home = lambda profile: g.other if profile == "other" else s.host.home
    for name in ("_standalone_scoped", "_primary_message_handler", "_primary_busy_session_handler", "_make_default_profile_message_handler", "_make_profile_message_handler", "_make_default_profile_busy_session_handler", "_make_profile_busy_session_handler", "_multiplex_on", "_wire_adapter_handlers"):
        setattr(g, name, body("gateway/run_adapters.py", name, dict(scope, _UNSET=object())).__get__(g))
    g.session_store = object()
    g._busy_text_mode = "queue"
    g._handle_adapter_fatal_error = lambda *a: None
    g._handle_reaction_event = lambda *a: None
    g._recover_telegram_topic_thread_id = lambda *a: None
    g._make_adapter_auth_check = lambda *a, **kw: lambda source: True
    g._primary_platform_event_handler = lambda: lambda *a: None
    class Adapter:
        name = "offline"; platform = Platform.DISCORD; _owner_profile = None
        _handle_message_while_active = body("gateway/platforms/base.py", "_handle_message_while_active", dict(scope, merge_pending_message_event=lambda pending, key, event, **kw: seen.append(("base-queue", event.text))))
        _dispatch_inline_reply = body("gateway/platforms/base.py", "_dispatch_inline_reply", dict(scope, _thread_metadata_for_event=lambda e: {"thread_id":"offline-thread"}, _reply_anchor_for_event=lambda e: e.message_id, _mark_notify_metadata=lambda m: m))
        _canonicalize = body("gateway/platforms/base.py", "_canonicalize", scope)
        _owner_transport_profile = body("gateway/platforms/base.py", "_owner_transport_profile", scope)
        def __init__(self):
            self.gateway_runner = g; self._active_sessions = {"room":object()}; self._pending_messages = {"room":object()}
        def _unwrap_ephemeral(self, response): return response, 0
        async def _send_with_retry(self, **kw):
            seen.append(("reply", kw)); return SimpleNamespace(success=True, message_id="offline")
        def _is_queue_text_debounce_candidate(self, event): return False
        def _start_session_processing(self, *a): raise AssertionError("new model turn")
    for name in ("set_message_handler", "set_fatal_error_handler", "set_session_store", "set_busy_session_handler", "set_topic_recovery_fn", "set_authorization_check", "set_platform_event_handler"):
        setattr(Adapter, name, body("gateway/platforms/base.py", name, scope))
    adapter = Adapter()
    # Real native wiring BEFORE guard publication captures standalone busy callback.
    g._wire_adapter_handlers(adapter)
    captured = adapter._busy_session_handler
    g.adapters = {Platform.DISCORD:adapter}; g._profile_adapters = {}
    mgr.set_gateway_message_injector(g, g._schedule_plugin_message_injection)
    assert hasattr(g._hm_busy_slash_or_photo, "_delegate_supervisor_busy_guard")
    assert adapter._busy_session_handler is captured
    def event(text="/supervision off", **kw):
        source = SessionSource(platform=Platform.DISCORD, chat_id="room", user_id="user", chat_type="group", profile=kw.pop("profile", None))
        return MessageEvent(text=text, source=source, message_id="anchor", **kw)
    return s, mgr, g, adapter, Adapter, seen, event


@pytest.mark.parametrize("mode", ["queue", "steer", "interrupt"])
@pytest.mark.parametrize("lane", ["adapter", "direct"])
def test_busy_reject_no_normal_input_or_state_changes(runtime, mode, lane):
    s, mgr, g, adapter, cls, seen, event = runtime
    g.mode = mode
    parent = s.parents["room"]; child = s.children["a"]
    before = (parent.enabled, parent.override, parent.busy, set(parent.pending), parent.queued, child.due)
    active = dict(adapter._active_sessions); pending = dict(adapter._pending_messages)
    if lane == "adapter":
        asyncio.run(adapter._handle_message_while_active(event(), "room"))
        replies = [row[1] for row in seen if row[0] == "reply"]
        assert len(replies) == 1 and replies[0]["content"] == BUSY_REPLY
        assert replies[0]["reply_to"] == "anchor" and replies[0]["metadata"]["thread_id"] == "offline-thread"
    else:
        assert asyncio.run(g._primary_message_handler()(event())) == BUSY_REPLY
    assert [row[0] for row in seen if row[0] != "reply"] == ["auth", "bot", "slash"]
    assert before == (parent.enabled, parent.override, parent.busy, set(parent.pending), parent.queued, child.due)
    assert s.parents["room"] is parent and s.children["a"] is child
    assert adapter._active_sessions == active and adapter._pending_messages == pending
    print("BUSY REJECT:", lane, mode, "auth/bot/slash -> refusal; no settings/queue/steer/interrupt")


@pytest.mark.parametrize("denial", ["auth", "bot", "slash"])
def test_native_admission_and_acl(runtime, denial):
    s, mgr, g, adapter, cls, seen, event = runtime
    g.allowed = denial != "auth"; g.bot_allowed = denial != "bot"; g.slash_denied = denial == "slash"
    asyncio.run(adapter._handle_message_while_active(event(), "room"))
    assert [row[0] for row in seen if row[0] != "reply"] == {"auth":["auth"], "bot":["auth","bot"], "slash":["auth","bot","slash"]}[denial]
    replies = [row[1]["content"] for row in seen if row[0] == "reply"]
    assert replies == (["slash denied"] if denial == "slash" else [])
    assert s.parents["room"].enabled


@pytest.mark.parametrize("text,internal", [("hello",False), ("/unknown",False), ("/supervision off",True)])
def test_other_commands_and_internal_keep_original_active_path(runtime, text, internal):
    s, mgr, g, adapter, cls, seen, event = runtime
    asyncio.run(adapter._handle_message_while_active(event(text, internal=internal), "room"))
    if internal:
        # Native path refuses an internal wake when a human pending slot exists.
        assert seen == []
    else:
        assert any(row[0] in {"steer", "queue", "base-queue"} for row in seen)
    assert not any(row[0] in {"slash", "reply"} for row in seen)


def test_multiplex_routed_and_secondary_profile_boundaries(runtime, tmp_path):
    s, mgr, g, adapter, cls, seen, event = runtime
    g.other = tmp_path / "other"; g.other.mkdir()
    g.config.multiplex_profiles = True
    g._wire_adapter_handlers(adapter)  # Real primary routed handler factory.
    asyncio.run(adapter._handle_message_while_active(event(), "room"))
    assert ("slash", "supervision", s.host.home) in seen
    seen.clear()
    asyncio.run(adapter._handle_message_while_active(event(profile="other"), "room"))
    assert not any(row[0] in {"slash", "reply"} for row in seen)
    assert any(row[0] == "steer" for row in seen)
    secondary = cls(); secondary._owner_profile = "default"
    g._wire_adapter_handlers(secondary, message_handler=g._make_profile_message_handler("default"), busy_session_handler=g._make_profile_busy_session_handler("default"))
    seen.clear()
    asyncio.run(secondary._handle_message_while_active(event(), "room"))
    assert ("slash", "supervision", s.host.home) in seen
    assert get_hermes_home() == s.host.home


def test_late_publication_reconnect_and_unload_restore(runtime):
    s, mgr, g, adapter, cls, seen, event = runtime
    # registration preceded host publication; new adapters get guarded synchronously.
    replacement = cls()
    g._wire_adapter_handlers(replacement)
    assert "_handle_message_while_active" in replacement.__dict__
    asyncio.run(replacement._handle_message_while_active(event(), "room"))
    assert any(row[0] == "reply" and row[1]["content"] == BUSY_REPLY for row in seen)
    captured = replacement._handle_message_while_active
    assert mgr.unload("delegate_supervisor")
    for owner, name in ((adapter,"_handle_message_while_active"),(replacement,"_handle_message_while_active"),(mgr,"set_gateway_message_injector")):
        assert name not in owner.__dict__
    assert not hasattr(g._hm_busy_slash_or_photo, "_delegate_supervisor_busy_guard")
    seen.clear()
    asyncio.run(captured(event(), "room"))  # old captured wrapper is inert after unload
    assert not any(row[0] == "reply" for row in seen)


def test_source_drift_and_foreign_replacement_are_not_overwritten(runtime):
    s, mgr, g, adapter, cls, seen, event = runtime
    unknown = cls()
    async def foreign(event, key): return None
    unknown._handle_message_while_active = foreign
    g._wire_adapter_handlers(unknown)
    assert unknown._handle_message_while_active is foreign
    adapter._handle_message_while_active = foreign
    g._wire_adapter_handlers = lambda *a, **kw: None
    assert mgr.unload("delegate_supervisor")
    assert adapter._handle_message_while_active is foreign
    assert "_wire_adapter_handlers" in g.__dict__


def test_native_unpatched_reproduction(runtime):
    s, mgr, g, adapter, cls, seen, event = runtime
    # Same actual native active body, deliberately bypass our instance shadow.
    asyncio.run(cls._handle_message_while_active(adapter, event(), "room"))
    assert ("steer", "/supervision off") in seen
    assert not any(row[0] == "slash" for row in seen)
    assert s.parents["room"].enabled
    print("UNPATCHED REPRO: literal /supervision off -> model steer; no slash ACL")


def test_builtin_reasoning_keeps_native_busy_reject(runtime):
    s, mgr, g, adapter, cls, seen, event = runtime
    from hermes_cli.commands import resolve_command
    assert resolve_command("reasoning").busy_policy == "reject"
    g._dispatch_busy_slash_command = body("gateway/run_busy.py", "_dispatch_busy_slash_command",
        {"t":lambda key, **kw: "native reject " + kw["command"]}).__get__(g)
    assert asyncio.run(g._primary_message_handler()(event("/reasoning high"))) == "native reject reasoning"
    assert ("slash", "reasoning", s.host.home) in seen


def test_unknown_runner_is_inactive_and_own_registration_required(runtime):
    s, mgr, g, adapter, cls, seen, event = runtime
    guard = g._hm_busy_slash_or_photo._delegate_supervisor_busy_guard
    entry = mgr._plugin_commands["supervision"]
    mgr._plugin_commands["supervision"] = dict(entry, handler=lambda text: text)
    try:
        asyncio.run(adapter._handle_message_while_active(event(), "room"))
        assert ("steer", "/supervision off") in seen
    finally:
        mgr._plugin_commands["supervision"] = entry
    # Replace the exact native seam before publishing another owner: don't bless it.
    import copy
    other = copy.copy(g)
    async def foreign(event, source, key): return False, None
    other._hm_busy_slash_or_photo = foreign
    other._schedule_plugin_message_injection = g._schedule_plugin_message_injection.__func__.__get__(other)
    mgr.set_gateway_message_injector(other, other._schedule_plugin_message_injection)
    assert other._hm_busy_slash_or_photo is foreign
    assert guard.owner is g


def test_shared_runner_guard_keeps_other_registered_profile(runtime, tmp_path, monkeypatch):
    s, mgr, g, adapter, cls, seen, event = runtime
    from hermes_cli.plugins import PluginManager, PluginContext, PluginManifest
    from test_regressions import install
    other_home = tmp_path / "other"; other_home.mkdir()
    g.other = other_home
    token = set_hermes_home_override(other_home)
    other_mgr = PluginManager(scope_key=str(other_home))
    try:
        other_ctx = PluginContext(PluginManifest(name="delegate_supervisor", key="delegate_supervisor"), other_mgr)
        other_mgr.set_gateway_message_injector(g, g._schedule_plugin_message_injection)
        other_s = install(other_ctx, monkeypatch)  # attach existing published slot at register time
        guard = g._hm_busy_slash_or_photo._delegate_supervisor_busy_guard
        assert len(guard.hosts) == 2
        assert mgr.unload("delegate_supervisor")
        assert len(guard.hosts) == 1
        assert asyncio.run(g._hm_busy_slash_or_photo(event(), event().source, "room")) == (True, BUSY_REPLY)
        assert other_mgr.unload("delegate_supervisor")
        assert not hasattr(g._hm_busy_slash_or_photo, "_delegate_supervisor_busy_guard")
    finally:
        other_mgr.unload("delegate_supervisor")
        reset_hermes_home_override(token)


def test_real_publication_attaches_before_return(runtime, monkeypatch):
    s, mgr, g, adapter, cls, seen, event = runtime
    import hermes_cli.plugins as plugins
    from test_regressions import install
    assert mgr.unload("delegate_supervisor")
    mgr.clear_gateway_message_injector(g)
    new_s = install(s.host.ctx, monkeypatch)
    assert not hasattr(g._hm_busy_slash_or_photo, "_delegate_supervisor_busy_guard")
    monkeypatch.setattr(plugins, "_plugin_managers_by_home", {str(new_s.host.home):mgr})
    monkeypatch.setattr(plugins, "_plugin_manager", None)
    monkeypatch.setattr(plugins, "_published_gateway_message_injector", None)
    plugins.publish_gateway_message_host(g, g._schedule_plugin_message_injection)
    assert hasattr(g._hm_busy_slash_or_photo, "_delegate_supervisor_busy_guard")
    assert "_handle_message_while_active" in adapter.__dict__
    assert mgr._gateway_message_injector[0] is g
    seen.clear()
    asyncio.run(adapter._handle_message_while_active(event(), "room"))
    assert any(row[0] == "reply" and row[1]["content"] == BUSY_REPLY for row in seen)


def test_unserved_multiplex_route_keeps_native_drop(runtime):
    s, mgr, g, adapter, cls, seen, event = runtime
    from gateway.profile_routing import ProfileRouteRejected
    def rejected(*a, **kw): raise ProfileRouteRejected("not served")
    g.config.multiplex_profiles = True
    g._profile_name_for_source = rejected
    g._wire_adapter_handlers(adapter)
    ev = event()
    asyncio.run(adapter._handle_message_while_active(ev, "room"))
    assert ev.source.profile_route_rejected is True
    assert seen == []


def test_reply_failure_does_not_fall_back_to_model_input(runtime, caplog):
    s, mgr, g, adapter, cls, seen, event = runtime
    async def failed_send(**kw): raise OSError("offline transport failed")
    adapter._send_with_retry = failed_send
    asyncio.run(adapter._handle_message_while_active(event(), "room"))
    assert [row[0] for row in seen] == ["auth", "bot", "slash"]
    assert "busy reply failed" in caplog.text
    assert s.parents["room"].enabled
