"""Installed-host boundaries; only child/LLM execution and transport I/O are mocked."""
import contextvars
import json
import os
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from gateway.session_context import set_session_vars, clear_session_vars
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_delegate_supervisor.host import Host
from hermes_delegate_supervisor.core import Supervisor
import hermes_delegate_supervisor as plugin
from host_fixtures import gateway


@pytest.fixture
def ctx(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  entries:\n    delegate_supervisor:\n      allow_gateway_injection: true\n      settings:\n        interval_seconds: 42\n")
    token = set_hermes_home_override(home)
    mgr = PluginManager(scope_key=str(home))
    context = PluginContext(PluginManifest(name="delegate_supervisor", key="delegate_supervisor"), mgr)
    yield context, mgr
    mgr.unload("delegate_supervisor")
    reset_hermes_home_override(token)
    clear_session_vars([])


def bind(key="route-origin", session="parent", **kwargs):
    set_session_vars(session_key=key, session_id=session, platform="discord", **kwargs)


def test_profile_capture_and_public_injector_consent(ctx, tmp_path, monkeypatch):
    context, mgr = ctx
    bind()
    host = Host(context)
    route = host.capture("parent")
    g = gateway(mgr, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    token = set_hermes_home_override(other)
    try:
        assert host.capture("parent") is None
        assert host.wake(route, "review")
    finally:
        reset_hermes_home_override(token)
    assert g.drain() == [True]
    assert g.homes == [host.home]
    assert g.delivered[0].text == "review"
    assert g.delivered[0].metadata["gateway_session_id"] == "parent"
    assert get_hermes_home().resolve() == host.home
    host.close()


def test_injection_grant_is_default_off(ctx, monkeypatch):
    context, mgr = ctx
    bind()
    host = Host(context)
    route = host.capture("parent")
    (host.home / "config.yaml").write_text("plugins: {}\n")
    g = gateway(mgr, monkeypatch)
    assert not host.wake(route, "review")
    assert not g.pending
    host.close()


def test_classic_cli_queue_only_even_busy(ctx):
    context, mgr = ctx
    from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
    cli = SimpleNamespace(session_id="parent", agent=SimpleNamespace(session_id="parent"), _agent_running=True,
                          _pending_input=queue.Queue(), _interrupt_queue=queue.Queue(),
                          _tui_process_loop=CLITuiRuntimeMixin._tui_process_loop,
                          _tui_process_one_input=CLITuiRuntimeMixin._tui_process_one_input, _should_exit=False)
    mgr._cli_ref = cli
    host = Host(context)
    route = host.capture("parent")
    assert host.wake(route, "review")
    assert cli._pending_input.get_nowait().message == "review"
    assert cli._interrupt_queue.empty()
    cli.session_id = "new-session"
    assert not host.wake(route, "review")
    assert host.capture("parent") is None


def test_unknown_cli_shape_and_stateless_fail_inactive(ctx):
    context, mgr = ctx
    mgr._cli_ref = SimpleNamespace(_pending_input=queue.Queue())
    host = Host(context)
    assert host.capture("parent") is None
    mgr._cli_ref = None
    set_session_vars(session_key="api", session_id="parent", platform="api_server")
    assert host.capture("parent") is None
    set_session_vars(session_key="route", session_id="parent", platform="discord", async_delivery=False)
    assert host.capture("parent") is None


def test_plugin_hooks_auto_register_finish_stop_and_busy(ctx, monkeypatch):
    context, mgr = ctx
    bind()
    captured = []
    class Capture(Supervisor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.clock = lambda: now[0]
            captured.append(self)
        def start(self): pass
    now = [0]
    monkeypatch.setattr(plugin, "Supervisor", Capture)
    plugin.register(context)
    s = captured[0]
    assert s.interval == 42
    s.host.snapshot = lambda: [{"subagent_id": "child-id", "status": "running"}]
    g = gateway(mgr, monkeypatch)
    delivered = g.pending
    mgr.invoke_hook("pre_llm_call", session_id="parent")
    mgr.invoke_hook("subagent_start", parent_session_id="parent", child_session_id="child-session", child_subagent_id="child-id")
    assert list(s.children) == ["child-id"]
    now[0] = 42
    s.tick()
    assert not delivered
    result = mgr.invoke_hook("pre_llm_call", session_id="parent")
    assert any(isinstance(r, dict) and "child-id" in r.get("context", "") for r in result)
    mgr.invoke_hook("post_llm_call", session_id="parent")
    now[0] = 84
    s.tick()
    assert len(delivered) == 1
    assert g.drain() == [True]
    mgr.invoke_hook("subagent_stop", child_session_id="child-session")
    assert not s.children
    # Failed stop requests must not remove supervision; successful stop does.
    mgr.invoke_hook("subagent_start", parent_session_id="parent", child_session_id="child-session", child_subagent_id="child-id")
    mgr.invoke_hook("post_tool_call", tool_name="delegate_task", args={"action":"stop"}, result='{"error":"no"}')
    assert s.children
    mgr.invoke_hook("post_tool_call", tool_name="delegate_task", args={"action":"stop"}, result=json.dumps({"status":"interrupt_requested", "subagent_id":"child-id"}))
    assert not s.children
    mgr.invoke_hook("subagent_start", parent_session_id="parent", child_session_id="child-session", child_subagent_id="child-id")
    mgr.invoke_hook("agent_loop_stopped", session_key="route-origin")
    assert not s.children
    mgr.invoke_hook("subagent_start", parent_session_id="parent", parent_subagent_id="nested", child_session_id="nested-child", child_subagent_id="nested-id")
    assert not s.children
    s.close()


def test_real_registry_controls_snapshot_and_no_host_patches(ctx):
    context, mgr = ctx
    bind()
    import tools.delegate_tool as dt
    import tools.delegate_tool_registry as registry
    seams = (dt.delegate_task, dt._build_child_agent, dt._build_dynamic_schema_overrides)
    host = Host(context)
    agent = SimpleNamespace()
    registry._register_subagent({"subagent_id":"offline-child", "agent":agent, "status":"running"})
    try:
        assert "offline-child" in host.live_ids()
    finally:
        registry._unregister_subagent("offline-child", agent=agent)
    assert "offline-child" not in host.live_ids()
    assert seams == (dt.delegate_task, dt._build_child_agent, dt._build_dynamic_schema_overrides)


def test_directory_manifest_load_and_unload(ctx):
    context, mgr = ctx
    from hermes_cli.plugins_manifest import parse_manifest_file
    root = Path(__file__).resolve().parents[1]
    manifest = parse_manifest_file(root / "plugin.yaml", root, "user", "")
    mgr._load_plugin(manifest)
    assert mgr._plugins["delegate_supervisor"].enabled
    assert mgr.has_hook("subagent_start")
    entry = mgr._plugin_commands["supervision"]
    assert entry["plugin_key"] == "delegate_supervisor" and entry["argument_mode"] == "text"
    assert entry["args_hint"] == "[<duration> | off | default]"
    assert manifest.config_schema["interval_seconds"]["default"] == 600
    assert (root / "plugin.yaml").read_bytes() == (root / "hermes_delegate_supervisor/plugin.yaml").read_bytes()
    assert mgr.unload("delegate_supervisor")
    assert not mgr.has_hook("subagent_start")
    assert "supervision" not in mgr._plugin_commands


def test_real_routing_wrapper_host_build_preserves_explicit_values(ctx, monkeypatch):
    context, mgr = ctx
    bind()
    import sys
    routing_source = os.environ["ROUTING_SOURCE"]
    sys.path.insert(0, routing_source)
    from hermes_delegate_routing import patches, _state
    import tools.delegate_tool as dt
    # Do not import run_agent: its bootstrap can prepare/update the managed runtime.
    # A constructor-only fixture is the explicit offline boundary below host child construction.
    import types
    run_agent = types.ModuleType("run_agent")
    monkeypatch.setitem(sys.modules, "run_agent", run_agent)
    captured = []
    class Child:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.session_id = "offline-routed-child"
            self.reasoning_config = {"effort": "low"}
            self.service_tier = "priority"
    # Boundary below the real build/hook/routing path. No child execution or auth resolution.
    monkeypatch.setattr(run_agent, "AIAgent", Child, raising=False)
    monkeypatch.setattr(dt, "_resolve_child_runtime", lambda *a, model=None, override_provider=None, **kw: {"model":model, "provider":override_provider, "base_url":None})
    monkeypatch.setattr(dt, "_resolve_child_credential_pool", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda name, **kw: captured.append((name, kw)) or mgr.invoke_hook(name, **kw))
    monkeypatch.setattr(plugin.Supervisor, "start", lambda self: None)
    cores = []
    original_init = plugin.Supervisor.__init__
    def init(self, *a, **kw):
        original_init(self, *a, **kw)
        cores.append(self)
    monkeypatch.setattr(plugin.Supervisor, "__init__", init)
    plugin.register(context)
    parent = SimpleNamespace(session_id="parent", model="parent-model", provider="parent-provider", enabled_toolsets=["file"], request_overrides={"unrelated":7, "service_tier":"priority"})
    token = _state.ROUTING.set({0:{"model":"routed-model", "provider":"routed-provider", "reasoning_config":{"effort":"high"}, "fast":False, "request_overrides":{"unrelated":7, "service_tier":"priority"}}})
    try:
        build = patches.make_build_child_wrapper(dt._build_child_agent)
        child = build(task_index=0, goal="offline", context=None, toolsets=None, model="baseline", max_iterations=1, task_count=1, parent_agent=parent)
        assert child.model == "routed-model" and child.provider == "routed-provider"
        assert child.reasoning_config == {"effort":"high"}
        assert child.service_tier is None and child.request_overrides == {"unrelated":7}
        assert parent.request_overrides == {"unrelated":7, "service_tier":"priority"}
        assert child._subagent_id in cores[0].children
        assert any(name == "subagent_start" for name, _ in captured)
    finally:
        _state.ROUTING.reset(token)
        for s in cores: s.close()


def test_real_tui_injector_busy_queues_idle_starts_same_session(ctx):
    import threading
    import time
    import uuid
    from tui_gateway import plugin_inject
    context, mgr = ctx
    bind(key="ses-parent")
    origin = {"session_key":"ses-parent", "agent":SimpleNamespace(session_id="parent"), "history_lock":threading.RLock(), "running":True}
    sibling = {"session_key":"ses-sibling", "history_lock":threading.RLock(), "running":False}
    drained = threading.Event()
    def enqueue(session, content, transport):
        session["queued_prompt"] = {"text":content, "transport":transport}
    def drain(rid, sid, session):
        assert sid == "ui-origin" and session is origin
        drained.set()
    namespace = SimpleNamespace(_sessions={"ui-origin":origin, "ui-sibling":sibling},
                               _sessions_lock=threading.RLock(), _enqueue_prompt=enqueue,
                               _drain_queued_prompt=drain, time=time, uuid=uuid, threading=threading)
    plugin_inject.register(namespace)
    mgr.set_tui_message_injector(object(), namespace.inject_tui_session_message)
    host = Host(context)
    route = host.capture("parent")
    assert host.wake(route, "busy-review")
    assert origin["queued_prompt"]["text"] == "busy-review"
    assert "queued_prompt" not in sibling and not drained.is_set()
    origin["running"] = False
    assert host.wake(route, "idle-review")
    assert drained.wait(1)
    assert origin["queued_prompt"]["text"] == "idle-review"


def test_finalize_without_session_context_releases_original_parent(ctx, monkeypatch):
    context, mgr = ctx
    bind()
    cores = []
    class Capture(Supervisor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            cores.append(self)
        def start(self): pass
    monkeypatch.setattr(plugin, "Supervisor", Capture)
    plugin.register(context)
    mgr.invoke_hook("subagent_start", parent_session_id="parent", child_session_id="child", child_subagent_id="a")
    assert cores[0].children
    clear_session_vars([])
    mgr.invoke_hook("on_session_finalize", session_id="parent")
    assert not cores[0].children
    cores[0].close()


def test_abandoned_load_does_not_start_worker(ctx, monkeypatch):
    context, mgr = ctx
    context._abandon_load()
    started = []
    monkeypatch.setattr(plugin.Supervisor, "start", lambda self: started.append(self))
    plugin.register(context)
    assert not started and not mgr.has_hook("subagent_start")


def test_cli_source_drift_and_session_context_mismatch_fail_inactive(ctx):
    from hermes_delegate_supervisor.host import _classic_consumer_supported
    from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
    assert _classic_consumer_supported(SimpleNamespace(_tui_process_loop=CLITuiRuntimeMixin._tui_process_loop))
    assert not _classic_consumer_supported(SimpleNamespace(_tui_process_loop=lambda: None))
    context, mgr = ctx
    bind(session="unrelated")
    assert Host(context).capture("parent") is None
