"""Session settings through real host command callers/binders; no live runtime."""
import asyncio
import contextlib
import contextvars
import logging
from types import SimpleNamespace

import pytest
import hermes_cli.plugins as plugins
from gateway.session import build_session_context
from hermes_delegate_supervisor.core import Supervisor
from host_fixtures import body, gateway, tui, compression
from test_host import ctx, bind
from test_regressions import install, spawn
from test_cli_delivery import cli_runtime, drain


def gateway_commands(context, mgr, monkeypatch):
    s = install(context, monkeypatch)
    g = gateway(mgr, monkeypatch, session="old", key="room")
    # Real plugin registry lookup, not a fake handler. Prevent automatic discovery.
    monkeypatch.setattr(plugins, "_ensure_plugins_discovered", lambda: mgr)
    scope = dict(asyncio=asyncio, build_session_context=build_session_context,
                 logger=logging.getLogger("test"), _contextmanager=contextlib.contextmanager)
    for name in ("_set_session_env", "_clear_session_env", "_session_env_scope"):
        setattr(g, name, body("gateway/run.py", name, scope).__get__(g))
    g._hm_dispatch_quick_and_plugin_commands = body("gateway/run_inbound.py", "_hm_dispatch_quick_and_plugin_commands", scope).__get__(g)
    g._run_in_executor_with_context = body("gateway/run.py", "_run_in_executor_with_context", {"asyncio":asyncio, "copy_context":contextvars.copy_context}).__get__(g)
    for name in ("_hm_resolve_command", "_hm_command_hooks", "_hm_dispatch_idle_commands"):
        setattr(g, name, body("gateway/run_inbound.py", name, scope).__get__(g))
    async def emit(*args): return []
    async def canonical(*args): return False, None
    g.hooks = SimpleNamespace(emit_collect=emit)
    g._hm_dispatch_canonical_command = canonical
    g._check_slash_access = lambda source, name: None
    g._hm_quick_commands = lambda: {}
    g._session_key_for_source = lambda source: "room"
    g.config = SimpleNamespace(get_connected_platforms=lambda: [], group_sessions_per_user=True, thread_sessions_per_user=False)
    def command(arg="", chat="room"):
        from gateway.platforms.event import MessageEvent
        from gateway.session import SessionSource
        from gateway.config import Platform
        event = MessageEvent(text="/supervision" + (" " + arg if arg else ""), source=SessionSource(platform=Platform.DISCORD, chat_id=chat, user_id="user"))
        handled, output = asyncio.run(g._hm_dispatch_idle_commands(event, event.source, chat))
        assert handled
        return output
    return s, g, command


def test_gateway_actual_empty_id_binder_before_spawn_and_after_finish(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    config = (s.host.home / "config.yaml").read_bytes()
    assert "default" in command() and "42s" in command()
    assert "override" in command("10m") and "600s" in command()
    bind(key="room", session="old")
    spawn(mgr)
    assert s.children["a"].due == 1600
    mgr.invoke_hook("subagent_stop", child_session_id="child-a")
    assert "600s" in command() and "targets: none" in command()
    bind(key="room", session="old")
    spawn(mgr, ident="b")
    assert s.children["b"].due == 1600
    assert (s.host.home / "config.yaml").read_bytes() == config
    print("SESSION ACCEPTANCE: real gateway slash binder supplies empty durable id; lookup pins old, no reload")


def test_interval_preserves_parent_busy_pending_queued_and_targets(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    bind(key="room", session="old"); spawn(mgr)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    s.children["a"].due = 0; s.tick()
    p = s.parents["room"]; token = p.queued
    p.busy = True
    assert "120s" in command("2m")
    assert s.parents["room"] is p and p.busy and p.pending == {"a"} and p.queued == token
    assert s.children["a"].due == 1120
    s.tick(); assert len(g.pending) == 1
    assert "42s" in command("default") and p.override is None
    assert p.pending == {"a"} and p.queued == token and s.children["a"].due == 1042
    assert g.drain() == [True]


def test_off_preserves_children_and_suppresses_accepted_gateway_review(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    bind(key="room", session="old"); spawn(mgr)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    s.children["a"].due = 0; s.tick()
    assert "disabled" in command("off")
    assert set(s.children) == {"a"} and s.parents["room"].queued
    assert g.drain() == [True]  # accepted transport cannot be retracted
    bind(key="room", session="old")
    assert s.turn_start(s.host.capture("old"), [s.parents["room"].queued]) is None
    spawn(mgr, ident="b"); s.turn_end(s.host.capture("old"))
    s.tick(); assert not g.pending and set(s.children) == {"a", "b"}
    assert "enabled" in command("default")
    assert all(c.due == 1042 for c in s.children.values())


@pytest.mark.parametrize("arg", ["0s", "-1m", "nanm", "infs", "1e999h", "10", "1ms", "0.5s", "1m extra"])
def test_invalid_commands_do_not_mutate(ctx, monkeypatch, arg):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    command("10m")
    parent = s.parents["room"]
    assert "Usage:" in command(arg)
    assert s.parents["room"] is parent and parent.override == 600


def test_reset_resume_compression_and_profile_boundaries(ctx, monkeypatch, tmp_path):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    command("10m")
    bind(key="room", session="old"); spawn(mgr)
    old = s.parents["room"]
    compression(s.host.home)
    g.entry.session_id = "compressed"
    assert "600s" in command() and s.parents["room"] is old and s.children
    g.entry.session_id = "unrelated"
    assert "42s" in command() and not s.children
    command("off")
    mgr.invoke_hook("on_session_reset", session_key="room")
    assert "default" in command() and "enabled" in command()
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    t = set_hermes_home_override(tmp_path / "other")
    try:
        assert "unavailable" in command("10m")
    finally:
        reset_hermes_home_override(t)
    assert "default" in command()


def test_cli_real_slash_caller_revokes_off_lease(cli_runtime, monkeypatch):
    cli, s, mgr, received, original = cli_runtime
    monkeypatch.setattr(plugins, "_ensure_plugins_discovered", lambda: mgr)
    outputs = []
    run = body("cli.py", "_run_plugin_slash_command", {"_cprint":outputs.append})
    s.tick(); assert s.parents["old"].queued
    run(cli, "/supervision", "off")
    assert "disabled" in outputs[-1] and s.parents["old"].queued is None
    drain(cli)
    assert not received and s.children
    # No per-turn binder: authoritative CLI id is sufficient, including another session.
    cli.session_id = cli.agent.session_id = "other"
    run(cli, "/supervision", "")
    assert "default" in outputs[-1]


def test_tui_desktop_real_command_dispatch_and_context_binder(ctx, monkeypatch):
    context, mgr = ctx
    s = install(context, monkeypatch)
    session, _ = tui(mgr, key="old", session_id="old")
    monkeypatch.setattr(plugins, "_ensure_plugins_discovered", lambda: mgr)
    # Actual TUI binder reverse-resolves the agent; no hand-set session ContextVars.
    scope = dict(contextlib=contextlib, _session_for_key=lambda key: session if key == session["session_key"] else None,
        _resolve_session_platform=lambda: "tui", _current_profile_name=lambda: "default",
        _session_source=lambda session: "desktop", profile_name_for_home=lambda h: "default",
        _methods_browser_control=SimpleNamespace(_is_authenticated_identity=lambda i: False))
    setter = body("tui_gateway/server.py", "_set_session_context", scope)
    clear = body("tui_gateway/server.py", "_clear_session_context", scope)
    namespace = dict(_tools_mod=__import__, _set_session_context=setter, _clear_session_context=clear,
        contextlib=contextlib, _ok=lambda rid, data: data)
    namespace["_tools_mod"] = lambda name: __import__(name, fromlist=["*"])
    run = body("tui_gateway/methods_tools.py", "_run_plugin_command", namespace)
    namespace.update(_run_plugin_command=run, _plugin_command_handler=plugins.get_plugin_command_handler)
    dispatch = body("tui_gateway/methods_tools.py", "_dispatch_plugin", namespace)
    result = dispatch(1, {}, session, "supervision", "3m")
    assert result["type"] == "plugin" and "180s" in result["output"]
    bind(key="old", session="old"); spawn(mgr)
    s.parents["old"].busy = True
    assert "disabled" in dispatch(2, {}, session, "supervision", "off")["output"]
    assert s.parents["old"].busy and s.children
    compression(s.host.home)
    session["agent"].session_id = "compressed"
    assert "override" in dispatch(3, {}, session, "supervision", "")["output"]
    assert s.parents["old"].route.session == "compressed"


def test_gateway_native_busy_plugin_command_boundary(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.config import Platform
    busy = body("gateway/run_inbound.py", "_hm_busy_slash_or_photo", {"MessageType":MessageType})
    event = MessageEvent(text="/supervision off", source=SessionSource(platform=Platform.DISCORD, chat_id="room", user_id="user"))
    # Host resolve_command() covers only built-ins; plugin slash is NOT a busy control.
    assert asyncio.run(busy(g, event, event.source, "room")) == (False, None)
    print("UNPATCHED HOST: native messaging busy path ignores plugin slash; guarded adapter tested separately")

def test_gateway_permission_gate_missing_session_and_other_conversation(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    command("10m")
    before = s.parents["room"]
    def denied(source, name):
        assert name == "supervision" and source.user_id == "user"
        return "denied by host"
    g._check_slash_access = denied
    assert command("off") == "denied by host"
    assert before.override == 600 and before.enabled
    g._check_slash_access = lambda source, name: None
    g.entry = None
    assert "unavailable" in command("off")
    assert before.enabled
    g.entry = SimpleNamespace(session_id="other")
    assert "default" in command()
    assert s.parents["room"] is not before
    # A separate native route in the same profile keeps its own default.
    bind(key="different-room", session="independent")
    separate = s.host.capture("independent")
    assert "default" in s.control(separate, "")
    assert s.parents["different-room"].override is None


def test_compression_before_canonical_repoint_keeps_settings_and_timers(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    command("1.5m")
    bind(key="room", session="old"); spawn(mgr)
    p = s.parents["room"]; due = s.children["a"].due
    compression(s.host.home)
    bind(key="room", session="compressed")
    mgr.invoke_hook("pre_llm_call", session_id="compressed")
    assert p.route.session == "compressed" and s.children["a"].due == due
    # Gateway index is still old here. The command must not demote to old/reset.
    assert "90s" in command() and s.parents["room"] is p
    assert p.route.session == "compressed" and s.children["a"].due == due
    mgr.invoke_hook("agent_loop_stopped", session_id="compressed")
    assert not s.children and p.override == 90
    assert "90s" in command()
    assert mgr.unload("delegate_supervisor")
    assert "supervision" not in mgr._plugin_commands and not s.parents


def test_off_setting_before_spawn_survives_last_finish(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    command("2m"); assert "120s" in command("off")
    bind(key="room", session="old"); spawn(mgr)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    s.children["a"].due = 0; s.tick()
    assert not g.pending
    mgr.invoke_hook("subagent_stop", child_session_id="child-a")
    mgr.invoke_hook("post_llm_call", session_id="old")
    assert "disabled" in command() and "120s" in command()
    assert "enabled" in command("1s")
    bind(key="room", session="old"); spawn(mgr, ident="b")
    s.host.snapshot = lambda: [{"subagent_id":"b", "status":"running"}]
    s.clock = lambda: 1001
    s.tick(); assert len(g.pending) == 1
    assert g.drain() == [True]

def test_same_durable_id_on_another_gateway_route_does_not_transfer_override(ctx, monkeypatch):
    context, mgr = ctx
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    command("10m")
    parent = s.parents["room"]
    g._session_key_for_source = lambda source: source.chat_id
    async def lookup(key):
        assert key in {"room", "other-room"}
        return g.entry  # another conversation explicitly resumed the same history
    g.async_session_store.lookup_by_session_key = lookup
    assert "default" in command("", chat="other-room")
    assert s.parents["room"] is parent and parent.override == 600
    assert s.parents["other-room"].override is None
