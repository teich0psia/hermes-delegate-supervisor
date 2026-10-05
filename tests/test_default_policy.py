"""Default policy and opt-in through native offline command/hook boundaries."""
import pytest
from hermes_constants import get_hermes_home
import hermes_delegate_supervisor as plugin
from hermes_delegate_supervisor.core import Supervisor
from host_fixtures import compression
from test_core import Host
from test_cli_delivery import cli_runtime
from test_host import ctx, bind
from test_regressions import install, spawn
from test_supervision_command import gateway_commands


def configure_default(value):
    # Only the fixture profile, before registration: exercise real get_config.
    path = get_hermes_home() / "config.yaml"
    path.write_text(path.read_text() + f"        enabled_by_default: {value}\n")
    return path.read_bytes()


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_core_rejects_non_boolean_defaults(value):
    with pytest.raises(ValueError, match="enabled_by_default"):
        Supervisor(Host(), enabled_by_default=value)


@pytest.mark.parametrize("value", ["null", "0", "1", "'false'", "[]", "{}"])
def test_invalid_config_fails_inactive_without_registration(ctx, value, caplog):
    context, mgr = ctx
    configure_default(value)
    plugin.register(context)
    assert "supervision" not in mgr._plugin_commands
    assert not mgr._hooks and not mgr._middleware
    assert "INACTIVE" in caplog.text and "enabled_by_default" in caplog.text


def test_default_off_registers_children_without_wake_or_context_then_on(ctx, monkeypatch):
    context, mgr = ctx
    config = configure_default("false")
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    assert s.enabled_by_default is False
    status = command()
    assert "disabled" in status and "source: default" in status
    assert "enabled source: default" in status and "interval source: default" in status
    bind(key="room", session="old")
    mgr.invoke_hook("pre_llm_call", session_id="old")
    spawn(mgr)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    s.children["a"].due = 0
    s.tick()
    assert not g.pending
    result = mgr.invoke_hook("pre_llm_call", session_id="old")
    assert not any(isinstance(r, dict) and r.get("context") for r in result)
    mgr.invoke_hook("post_llm_call", session_id="old")
    parent = s.parents["room"]
    assert "enabled" in command("on") and "42s" in command()
    assert s.parents["room"] is parent and set(s.children) == {"a"}
    assert parent.enabled_override is True and parent.override is None
    assert s.children["a"].due == 1042
    s.clock = lambda: 1042
    s.tick()
    token = parent.queued
    assert len(g.pending) == 1 and token and g.drain() == [True]
    bind(key="room", session="old")
    result = mgr.invoke_hook("pre_llm_call", session_id="old",
                             user_message=f"[delegate_supervisor_request:{token}]")
    assert any("YOUR running children is due: a" in r.get("context", "") for r in result if isinstance(r, dict))
    assert parent.queued is None
    assert (s.host.home / "config.yaml").read_bytes() == config
    print("DEFAULT-OFF: native config/binder/hooks -> no wake/context; on -> same child review")


@pytest.mark.parametrize("default", [True, False])
def test_on_preserves_interval_and_default_restores_both(ctx, monkeypatch, default):
    context, mgr = ctx
    config = configure_default(str(default).lower())
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    assert "enabled" in command("2m")
    bind(key="room", session="old"); spawn(mgr)
    parent = s.parents["room"]
    assert "disabled" in command("off")
    spawn(mgr, ident="b")
    parent.busy = True
    parent.pending = {"a"}
    parent.queued = "a" * 32
    status = command("on")
    assert "enabled" in status and "120s" in status
    assert "enabled source: override" in status and "interval source: override" in status
    assert s.parents["room"] is parent and parent.busy
    assert parent.pending == {"a"} and parent.queued == "a" * 32
    assert all(c.due == 1120 for c in s.children.values())
    status = command("default")
    assert ("enabled" if default else "disabled") in status
    assert "source: default" in status and "42s" in status
    assert parent.enabled_override is None and parent.override is None
    assert s.parents["room"] is parent and parent.busy and set(s.children) == {"a", "b"}
    assert parent.pending == ({"a"} if default else set())
    assert parent.queued == "a" * 32  # accepted gateway wake cannot be retracted
    assert all(c.due == 1042 for c in s.children.values())
    status = command("on")  # no interval override, but still a policy override
    assert "source: override" in status and "interval source: default" in status
    assert (s.host.home / "config.yaml").read_bytes() == config


@pytest.mark.parametrize("default", [True, False])
def test_explicit_on_lifetime_finish_stop_compression_reset_and_route_isolation(ctx, monkeypatch, default):
    context, mgr = ctx
    configure_default(str(default).lower())
    s, g, command = gateway_commands(context, mgr, monkeypatch)
    command("on")  # before first child, with default interval
    parent = s.parents["room"]
    bind(key="room", session="old"); spawn(mgr)
    mgr.invoke_hook("subagent_stop", child_session_id="child-a")
    mgr.invoke_hook("post_llm_call", session_id="old")
    assert s.parents["room"] is parent and parent.enabled_override is True
    spawn(mgr, ident="b")
    mgr.invoke_hook("agent_loop_stopped", session_id="old")
    assert not s.children and s.parents["room"] is parent
    assert parent.enabled_override is True and parent.override is None
    spawn(mgr, ident="c")
    due = s.children["c"].due
    compression(s.host.home)
    g.entry.session_id = "compressed"
    assert "enabled source: override" in command()
    assert s.parents["room"] is parent and parent.route.session == "compressed"
    assert s.children["c"].due == due
    bind(key="other-room", session="compressed")
    status = s.control(s.host.capture("compressed"), "status")
    assert "source: default" in status
    assert s.parents["other-room"].enabled is default
    assert s.parents["room"] is parent
    g.entry.session_id = "unrelated"
    assert "source: default" in command() and not s.children
    assert s.parents["room"].enabled is default
    command("on")
    mgr.invoke_hook("on_session_reset", session_key="room")
    assert "source: default" in command() and s.parents["room"].enabled is default
    command("on")
    mgr.invoke_hook("on_session_finalize", session_key="room")
    assert "source: default" in command() and s.parents["room"].enabled is default


def test_default_off_cli_default_revokes_accepted_lease(cli_runtime, ctx, monkeypatch):
    import hermes_cli.plugins as plugins
    from host_fixtures import body
    from test_cli_delivery import drain
    cli, s, mgr, received, original = cli_runtime
    # Reload only this isolated fixture to load real default-off config. No live
    # runtime is involved; the fixture's actual native CLI binder/consumer stay.
    assert mgr.unload("delegate_supervisor")
    configure_default("false")
    s = install(ctx[0], monkeypatch)
    s.host.snapshot = lambda: [{"subagent_id":"a", "status":"running"}]
    spawn(mgr)
    s.children["a"].due = 0
    s.tick()
    assert cli._pending_input.empty() and not s.parents["old"].enabled
    monkeypatch.setattr(plugins, "_ensure_plugins_discovered", lambda: mgr)
    outputs = []
    run = body("cli.py", "_run_plugin_slash_command", {"_cprint":outputs.append})
    run(cli, "/supervision", "on")
    s.children["a"].due = 0
    s.tick()
    parent = s.parents["old"]
    assert parent.queued
    run(cli, "/supervision", "default")
    assert "disabled" in outputs[-1] and "source: default" in outputs[-1]
    assert parent.queued is None and s.children
    drain(cli)
    assert not received and s.children
