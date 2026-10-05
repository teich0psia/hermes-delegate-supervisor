"""Acceptance regressions for the independent review's three reproductions."""
import asyncio
import queue
import sys
import types
from types import SimpleNamespace

import pytest
from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
from hermes_delegate_supervisor.core import Supervisor
from hermes_delegate_supervisor.host import Host
import hermes_delegate_supervisor as plugin
from host_fixtures import gateway, tui, compression, body
from test_host import ctx, bind


def install(context, monkeypatch):
    cores = []
    class Capture(Supervisor):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            cores.append(self)
            self.clock = lambda: 1000
        def start(self): pass
    monkeypatch.setattr(plugin, "Supervisor", Capture)
    plugin.register(context)
    return cores[0]


def spawn(mgr, session="old", ident="a"):
    mgr.invoke_hook("subagent_start", parent_session_id=session,
        child_session_id="child-" + ident, child_subagent_id=ident)


def test_gateway_reset_after_admission_is_rejected_at_dispatch(ctx, monkeypatch):
    context, mgr = ctx
    bind(key="room", session="old")
    host = Host(context)
    g = gateway(mgr, monkeypatch, session="old", key="room")
    original = g._dispatch_plugin_message_injection
    assert host.wake(host.capture("old"), "old parent review")
    # Exact real scheduler already accepted; /resume swaps its entry before dispatch.
    g.entry.session_id = "unrelated-resumed-session"
    assert g.drain() == [False]
    assert not g.delivered
    host.close()
    assert g._dispatch_plugin_message_injection == original


def test_gateway_current_parent_and_late_consumer_race(ctx, monkeypatch):
    context, mgr = ctx
    bind(key="room", session="old")
    host = Host(context)
    g = gateway(mgr, monkeypatch, session="old", key="room")
    assert host.wake(host.capture("old"), "review")
    assert g.drain() == [True]
    event = g.delivered[0]
    assert event.metadata["gateway_session_id"] == "old"
    # Later queue consumption still applies the host's strict generation check.
    g.entry.session_id = "new"
    g._session_key_for_source = lambda source: "room"
    resolve = body("gateway/run_turn.py", "_hmwa_resolve_session",
        {"logger":__import__("logging").getLogger("test"), "suppress":__import__("contextlib").suppress})
    assert asyncio.run(resolve(g, event, event.source)) is None
    host.close()


def test_gateway_unload_rejects_already_scheduled_wake(ctx, monkeypatch):
    context, mgr = ctx
    bind()
    host = Host(context)
    g = gateway(mgr, monkeypatch)
    assert host.wake(host.capture("parent"), "review")
    host.close()
    assert g.drain() == [False] and not g.delivered
    # Unrelated plugin traffic is still ordinary host dispatch.
    assert g._schedule_plugin_message_injection(session_key="route-origin", content="other", plugin_id="other")
    assert g.drain() == [True]


def test_unknown_gateway_source_fails_closed(ctx, monkeypatch):
    context, mgr = ctx
    bind()
    host = Host(context)
    mgr.set_gateway_message_injector(object(), lambda **kw: True)
    assert not host.wake(host.capture("parent"), "review")
    g = gateway(mgr, monkeypatch)
    assert host.wake(host.capture("parent"), "review")
    replacement = lambda **kw: False
    g._dispatch_plugin_message_injection = replacement
    # Source drift refuses further delivery; unload cannot clobber the new owner.
    assert not host.wake(host.capture("parent"), "review")
    host.close()
    assert g._dispatch_plugin_message_injection is replacement
    assert g.drain() == [False]


def test_same_key_unrelated_session_never_adopts_old_children(ctx, monkeypatch):
    context, mgr = ctx
    bind(key="room", session="old")
    s = install(context, monkeypatch)
    spawn(mgr)
    s.children["a"].due = 0
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    bind(key="room", session="unrelated-resumed-session")
    result = mgr.invoke_hook("pre_llm_call", session_id="unrelated-resumed-session", user_message="hello")
    assert not any(isinstance(r, dict) and "YOUR running children" in r.get("context", "") for r in result)
    assert not s.children
    s.close()


def test_cli_compression_recovers_without_natural_next_turn(ctx, monkeypatch):
    context, mgr = ctx
    cli = SimpleNamespace(session_id="old", agent=SimpleNamespace(session_id="old"),
        _pending_input=queue.Queue(), _interrupt_queue=queue.Queue(),
        _tui_process_one_input=CLITuiRuntimeMixin._tui_process_one_input,
        _tui_process_loop=CLITuiRuntimeMixin._tui_process_loop, _should_exit=False)
    mgr._cli_ref = cli
    bind(key="", session="old")
    s = install(context, monkeypatch)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    mgr.invoke_hook("pre_llm_call", session_id="old", user_message="spawn")
    spawn(mgr)
    s.children["a"].due = 0
    compression(s.host.home)
    cli.agent.session_id = "compressed"
    bind(key="", session="compressed")
    mgr.invoke_hook("post_llm_call", session_id="compressed", platform="cli")
    assert "old" not in s.parents and not s.parents["compressed"].busy
    s.tick()  # CLI still syncing: cannot deliver early
    assert cli._pending_input.empty()
    cli.session_id = "compressed"
    # Eligibility advances on the ordinary monotonic retry deadline.
    s.clock = lambda: 1042
    s.tick()
    assert "delegate_supervisor_request" in cli._pending_input.get_nowait().message
    assert cli._interrupt_queue.empty() and s.children["a"].parent == "compressed"
    s.close()


@pytest.mark.parametrize("surface", ["tui", "desktop"])
def test_ui_compression_recovers_after_post_hook_and_key_sync(ctx, monkeypatch, surface):
    context, mgr = ctx
    ui, _ = tui(mgr)
    bind(key="old-key", session="old")
    s = install(context, monkeypatch)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    mgr.invoke_hook("pre_llm_call", session_id="old", user_message="spawn")
    spawn(mgr)
    s.children["a"].due = 0
    compression(s.host.home)
    ui["agent"].session_id = "compressed"
    bind(key="old-key", session="compressed")
    mgr.invoke_hook("post_llm_call", session_id="compressed", platform=surface)
    assert not s.parents["old-key"].busy
    ui["session_key"] = "compressed"  # host sync, AFTER post_llm_call
    ui["running"] = False
    s.tick()  # no natural turn is required for recovery
    assert s.children["a"].parent == "compressed" and "old-key" not in s.parents
    assert "delegate_supervisor_request" in ui["queued_prompt"]["text"]
    # A subsequent ordinary turn consumes current targets and the exact reservation.
    bind(key="compressed", session="compressed")
    results = mgr.invoke_hook("pre_llm_call", session_id="compressed", user_message=ui["queued_prompt"]["text"])
    assert any("YOUR running children" in r.get("context", "") for r in results if isinstance(r, dict))
    assert s.parents["compressed"].queued is None
    s.close()


def test_changed_cli_id_without_committed_compression_is_not_continuation(ctx, monkeypatch):
    context, mgr = ctx
    mgr._cli_ref = SimpleNamespace(session_id="old", agent=SimpleNamespace(session_id="old"),
        _pending_input=queue.Queue(), _tui_process_one_input=CLITuiRuntimeMixin._tui_process_one_input,
        _tui_process_loop=CLITuiRuntimeMixin._tui_process_loop)
    bind(key="", session="old")
    s = install(context, monkeypatch)
    mgr.invoke_hook("pre_llm_call", session_id="old")
    spawn(mgr)
    mgr._cli_ref.agent.session_id = "arbitrary-new-id"
    bind(key="", session="arbitrary-new-id")
    mgr.invoke_hook("post_llm_call", session_id="arbitrary-new-id", platform="cli")
    assert s.parents["old"].busy and s.children["a"].parent == "old"
    s.close()


def test_lineage_refuses_explicit_fork_and_unknown_publisher(ctx):
    context, _ = ctx
    host = Host(context)
    compression(host.home, config={"_branched_from":"old"})
    assert not host.continues("old", "compressed")
    host.lineage.supported = False
    assert not host.continues("old", "compressed")
    host.close()


def test_real_pre_hook_payload_acknowledges_wake(ctx, monkeypatch):
    from agent.turn_context import _collect_pre_llm_call_context
    import hermes_cli.lifecycle
    context, mgr = ctx
    bind(key="room", session="old")
    g = gateway(mgr, monkeypatch, session="old", key="room")
    s = install(context, monkeypatch)
    spawn(mgr)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    s.children["a"].due = 0
    s.tick()
    assert s.parents["room"].queued and g.drain() == [True]
    monkeypatch.setattr(hermes_cli.lifecycle, "invoke_hook", mgr.invoke_hook)
    text = _collect_pre_llm_call_context(SimpleNamespace(session_id="old", model="offline", platform="discord"),
        effective_task_id="room", turn_id="turn", original_user_message=g.delivered[0].text,
        messages=[], conversation_history=[])
    assert "YOUR running children" in text and s.parents["room"].queued is None
    s.close()


def test_real_routing_fast_rejection_removes_only_failed_batch(ctx, monkeypatch):
    import os
    import tools.delegate_tool as dt
    sys.path.insert(0, os.environ["ROUTING_SOURCE"])
    from hermes_delegate_routing import patches, _state
    context, mgr = ctx
    bind()
    s = install(context, monkeypatch)
    run_agent = types.ModuleType("run_agent")
    monkeypatch.setitem(sys.modules, "run_agent", run_agent)
    built = []
    class Child:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.session_id = f"rejected-child-{len(built)}"
            self.reasoning_config = {}
            self.service_tier = None
            self.closed = False
            built.append(self)
        def close(self): self.closed = True
    monkeypatch.setattr(run_agent, "AIAgent", Child, raising=False)
    monkeypatch.setattr(dt, "_resolve_child_runtime", lambda *a, model=None, override_provider=None, **kw:
        {"model":model, "provider":override_provider, "base_url":None})
    monkeypatch.setattr(dt, "_resolve_child_credential_pool", lambda *a, **kw: None)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda name, **kw: mgr.invoke_hook(name, **kw))
    parent = SimpleNamespace(session_id="parent", model="unsupported-model", provider="openai",
        enabled_toolsets=["file"], request_overrides={}, _active_children=[])
    # A different queued batch has no registry entry; never drop it via age/snapshot.
    spawn(mgr, session="parent", ident="pool-waiting")
    args = {"tasks":[{"goal":"accepted", "fast":False}, {"goal":"rejected", "fast":True}]}
    from hermes_cli.middleware import run_tool_execution_middleware
    monkeypatch.setattr("hermes_cli.plugins._delivery_manager", lambda: mgr)
    monkeypatch.setattr(dt, "_build_child_agent", patches.make_build_child_wrapper(dt._build_child_agent))
    siblings = []
    token = _state.ROUTING.set({0:{"fast":False, "_fast_batch_children":siblings},
                                1:{"fast":True, "_fast_batch_children":siblings}})
    def terminal(call_args):
        children, error = dt._build_children(call_args["tasks"], [None, None],
            {"model":"unsupported-model", "provider":"openai", "base_url":None, "api_key":None, "api_mode":None},
            top_role="leaf", max_iterations=1, parent_agent=parent, routing_cfg={}, live_deleg_id=None, live_writers=[])
        assert children == [] and "unsupported" in error
        return dt.tool_error(error)
    try:
        result = run_tool_execution_middleware("delegate_task", args, terminal, session_id="parent", tool_call_id="failed")
        assert "error" in result
        assert len(built) == 2 and all(child.closed and child not in parent._active_children for child in built)
        s.host.snapshot = lambda: []
        for _ in range(3): s.tick()
        assert all(child._subagent_id not in s.children and not s.is_child_session(child.session_id) for child in built)
        assert set(s.children) == {"pool-waiting"}
    finally:
        _state.ROUTING.reset(token)
        s.close()


def test_ui_stale_generation_and_miss_never_fall_through_to_gateway(ctx):
    context, mgr = ctx
    ui, _ = tui(mgr)
    calls = []
    mgr.set_gateway_message_injector(object(), lambda **kw: calls.append(kw) or True)
    bind(key="old-key", session="old")
    host = Host(context)
    route = host.capture("old")
    ui["agent"].session_id = "unrelated"
    assert not host.wake(route, "review")
    ui["agent"].session_id = "old"
    ui["_closing"] = True
    assert not host.wake(route, "review")
    assert not calls
    host.close()


def test_parallel_failed_and_pool_waiting_batches_are_isolated(ctx, monkeypatch):
    import contextvars
    import threading
    from hermes_cli.middleware import run_tool_execution_middleware
    context, mgr = ctx
    bind(session="parent")
    s = install(context, monkeypatch)
    monkeypatch.setattr("hermes_cli.plugins._delivery_manager", lambda: mgr)
    barrier = threading.Barrier(2)
    failures = []
    def run(ident, failed):
        def terminal(args):
            spawn(mgr, session="parent", ident=ident)
            barrier.wait(timeout=2)
            return '{"error":"build failed"}' if failed else '{"status":"dispatched"}'
        try:
            run_tool_execution_middleware("delegate_task", {"tasks":[{"goal":ident}]}, terminal,
                session_id="parent", tool_call_id=ident)
        except Exception as exc:
            failures.append(exc)
    threads = [threading.Thread(target=contextvars.copy_context().run, args=(run, ident, failed))
               for ident, failed in (("failed", True), ("pool-waiting", False))]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=3)
    assert not any(thread.is_alive() for thread in threads) and not failures
    s.host.snapshot = lambda: []
    s.tick()
    assert set(s.children) == {"pool-waiting"} and not s.is_child_session("child-failed")
    s.close()
