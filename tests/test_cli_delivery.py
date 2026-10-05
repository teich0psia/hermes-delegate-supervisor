"""Focused P2 acceptance: real serial consumer, input dispatch and /resume bodies.

Only rendering, session storage/env sync and chat/LLM are fixtures. No entrypoint
imports, real CLI startup, credentials or live children.
"""
import queue
import sys
import types
from contextlib import suppress
from types import SimpleNamespace

import pytest
from hermes_cli.cli_tui_runtime_mixin import CLITuiRuntimeMixin
from hermes_delegate_supervisor.host import Host
from host_fixtures import body, compression
from test_host import ctx, bind
from test_regressions import install, spawn


@pytest.fixture
def cli_runtime(ctx, monkeypatch):
    context, mgr = ctx
    bind(key="", session="old")
    cli = SimpleNamespace(session_id="old", agent=SimpleNamespace(session_id="old", reset_session_state=lambda: None),
        _pending_input=queue.Queue(), _interrupt_queue=queue.Queue(), _should_exit=False, _agent_running=False,
        _pending_resume_sessions=None, _pending_agent_seed=None, _app=SimpleNamespace(invalidate=lambda: None),
        _tui_unwrap_input=lambda text: (text, False, False), _typed_voice_stop=lambda text: False,
        handle_bang_shell=lambda text: False, _print_user_message_preview=lambda text: None,
        _turn_summary_begin=lambda: None)
    cli._tui_process_loop = types.MethodType(CLITuiRuntimeMixin._tui_process_loop, cli)
    cli._tui_process_one_input = types.MethodType(CLITuiRuntimeMixin._tui_process_one_input, cli)
    original = cli._tui_process_one_input
    module = types.ModuleType("cli")
    for name in ("_DIM", "_RST"):
        setattr(module, name, "")
    module._PASTE_REF_RE = __import__("re").compile(r"(?!)")
    module._cprint = lambda *a: None
    module._detect_file_drop = lambda text: None
    module._looks_like_slash_command = lambda text: text.startswith("/")
    module._strip_leaked_bracketed_paste_wrappers = lambda text: text
    module._strip_leaked_terminal_responses_with_meta = lambda text: (text, False)
    module._sync_process_session_id = lambda ident: bind(key="", session=ident)
    monkeypatch.setitem(sys.modules, "cli", module)
    # Keep the real compression module/constants for the SQLite publication fixture.
    import agent.context_compressor
    monkeypatch.setattr(agent.context_compressor, "is_user_originated_turn", lambda m: m.get("role") == "user")
    scope = dict(suppress=suppress, _cp=lambda *a: None, _t=lambda *a, **kw: "", _tn=lambda *a, **kw: "",
        _command_arg=lambda command: command.split(maxsplit=1)[1], _end_current_session=lambda *a: None,
        _without_session_meta=lambda messages: messages)
    scope["_sync_agent_to_session"] = body("hermes_cli/cli_commands_mixin.py", "_sync_agent_to_session", dict(suppress=suppress))
    resume = body("hermes_cli/cli_commands_mixin.py", "_handle_resume_command", scope)
    cli._session_db = SimpleNamespace(get_resume_conversations=lambda ident: ([], []), reopen_session=lambda ident: None)
    cli._resolve_resume_target = lambda target: (target, {})
    cli._restore_session_cwd = cli._restore_session_yolo = cli._restore_session_model = lambda meta: None
    def command(text):
        if text == "/exit":
            cli._should_exit = True
        else:
            resume(cli, text)
        return None
    cli._tui_run_slash_input = command
    cli._tui_after_turn = lambda: setattr(cli, "_agent_running", False)
    received = []
    cli.review_contexts = []
    def chat(text, **kw):
        received.append((cli.session_id, text))
        results = mgr.invoke_hook("pre_llm_call", session_id=cli.session_id, user_message=text)
        cli.review_contexts.extend(r["context"] for r in results if isinstance(r, dict) and r.get("context"))
        mgr.invoke_hook("post_llm_call", session_id=cli.session_id, platform="cli")
    cli.chat = chat
    mgr._cli_ref = cli
    s = install(context, monkeypatch)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    spawn(mgr)
    s.children["a"].due = 0
    yield cli, s, mgr, received, original
    s.close()


def drain(cli):
    cli._pending_input.put("/exit")
    cli._tui_process_loop()
    assert cli._pending_input.empty() and cli._interrupt_queue.empty()


def test_cli_accepted_wake_is_discarded_after_actual_resume(cli_runtime):
    cli, s, _, received, _ = cli_runtime
    cli._pending_input.put("/resume unrelated-session")
    s.tick()
    assert s.parents["old"].queued  # admitted while still in old
    cli._pending_input.put("ordinary user input")
    drain(cli)
    assert cli.session_id == cli.agent.session_id == "unrelated-session"
    assert received == [("unrelated-session", "ordinary user input")]
    assert s.parents["old"].queued is None
    print("P2 ACCEPTANCE: admitted for old; actual resume/FIFO consumer discarded stale wake")


def test_cli_idle_wake_and_unrelated_fifo_inputs_are_preserved(cli_runtime):
    cli, s, _, received, _ = cli_runtime
    # The producer must never put into the interrupt queue, even while busy.
    cli._agent_running = True
    s.tick()
    cli._pending_input.put("first")
    cli._pending_input.put(("last", []))
    assert cli._interrupt_queue.empty()
    cli._agent_running = False
    drain(cli)
    assert [text for _, text in received][1:] == ["first", "last"]
    assert "delegate_supervisor_request" in received[0][1]
    assert all(session == "old" for session, _ in received)
    assert s.parents["old"].queued is None  # exact token reached real plugin hooks
    assert len(cli.review_contexts) == 1 and "YOUR running children is due: a" in cli.review_contexts[0]


@pytest.mark.parametrize("release", ["child_complete", "parent_stop", "unload", "natural_review"])
def test_cli_queued_lease_is_revoked_before_consumption(cli_runtime, release):
    cli, s, mgr, received, original = cli_runtime
    s.tick()
    token = s.parents["old"].queued
    replacement = None
    if release == "child_complete":
        mgr.invoke_hook("subagent_stop", child_session_id="child-a")
        assert "old" not in s.parents
        # A new child in this same durable session must not revive the old lease.
        spawn(mgr, ident="b")
        s.host.snapshot = lambda: [{"subagent_id":"b", "status":"running"}]
        s.children["b"].due = 0
        s.tick()
        replacement = s.parents["old"].queued
    elif release == "parent_stop":
        mgr.invoke_hook("agent_loop_stopped", session_id="old")
    elif release == "unload":
        assert mgr.unload("delegate_supervisor")
    else:
        assert s.turn_start(s.parents["old"].route)
        s.turn_end(s.parents["old"].route)
        assert s.parents["old"].queued is None  # revocation, not fake delivery ack
        # New review period starts even if the revoked envelope has not drained.
        s.clock = lambda: 1042
        s.tick()
        replacement = s.parents["old"].queued
    cli._pending_input.put("still a user input")
    drain(cli)
    if release in {"child_complete", "natural_review"}:
        assert replacement and replacement != token
        assert len(received) == 2 and received[0][0] == "old"
        assert replacement in received[0][1] and token not in received[0][1]
        assert received[1] == ("old", "still a user input")
        assert s.parents["old"].queued is None
    else:
        assert received == [("old", "still a user input")]
    if release == "unload":
        assert cli._tui_process_one_input == original


def test_cli_old_compression_lease_cannot_cancel_new_reservation(cli_runtime):
    cli, s, _, received, _ = cli_runtime
    s.tick()
    old_token = s.parents["old"].queued
    compression(s.host.home)
    cli.session_id = cli.agent.session_id = "compressed"
    bind(key="", session="compressed")
    s.tick()  # audited continuation replaces the old reservation, not its envelope
    token = s.parents["compressed"].queued
    assert token and token != old_token
    drain(cli)
    assert len(received) == 1 and received[0][0] == "compressed"
    assert token in received[0][1] and old_token not in received[0][1]
    assert s.parents["compressed"].queued is None
    assert len(cli.review_contexts) == 1


def test_cli_guard_source_drift_and_other_owner_are_not_clobbered(cli_runtime):
    cli, s, _, received, original = cli_runtime
    s.tick()
    guard = cli._tui_process_one_input
    other_calls = []
    def other(text):
        other_calls.append(text)
        return guard(text)
    cli._tui_process_one_input = other
    assert s.host.capture("old") is None
    assert not s.host.wake(s.parents["old"].route, "must not enqueue")
    s.close()
    assert cli._tui_process_one_input is other
    cli._pending_input.put("unrelated plugin/user")
    drain(cli)
    assert received == [("old", "unrelated plugin/user")]
    assert cli._tui_process_one_input is other
    cli._tui_process_one_input = original


def test_cli_unload_between_dequeue_and_dispatch_is_still_fenced(cli_runtime):
    cli, s, mgr, received, original = cli_runtime
    s.tick()
    envelope = cli._pending_input.get_nowait()  # real loop's get/dispatch seam
    guard = cli._tui_process_one_input
    assert mgr.unload("delegate_supervisor")
    # Immediate restore would let a dequeued envelope bypass the guard.
    assert cli._tui_process_one_input is guard
    guard(envelope)
    assert not received and cli._tui_process_one_input == original
    cli._pending_input.put("user after unload")
    drain(cli)
    assert received == [("old", "user after unload")]


def test_cli_unknown_dispatcher_is_inactive(cli_runtime):
    cli, s, _, _, _ = cli_runtime
    cli._tui_process_one_input = lambda text: None
    assert s.host.capture("old") is None
    assert not s.host.wake(s.parents["old"].route, "review")
    assert cli._pending_input.empty()
