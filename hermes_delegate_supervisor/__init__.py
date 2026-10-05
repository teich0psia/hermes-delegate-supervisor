"""Hermes plugin entry point. No model/provider/schema patches."""
import contextvars
import json
import logging
import re
from typing import Any

from .core import Supervisor
from .host import Host

log = logging.getLogger(__name__)
__version__ = "0.3.0"


def register(ctx):
    try:
        host = Host(ctx)
        supervisor = Supervisor(host, ctx.get_config("interval_seconds", 600),
                                enabled_by_default=ctx.get_config("enabled_by_default", True))
    except Exception:
        log.warning("delegate_supervisor INACTIVE: unsupported host or invalid config", exc_info=True)
        return
    batch: contextvars.ContextVar[Any] = contextvars.ContextVar("delegate_supervisor_batch", default=None)

    def execution(*, tool_name, args, next_call, session_id=None, **kwargs):
        if tool_name != "delegate_task" or not isinstance(args, dict) or str(args.get("action") or "spawn").strip().lower() != "spawn":
            return next_call(args)
        # Unlike bounded pre/post hooks, execution middleware owns the calling
        # context around the whole build. Parallel calls get disjoint collectors.
        token = batch.set((session_id, set()))
        try:
            result = next_call(args)
            try:
                payload = json.loads(result) if isinstance(result, str) else result
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and payload.get("error") and host.failed_batch_supported:
                for ident in batch.get()[1]:
                    supervisor.finish(ident=ident)
            return result
        finally:
            batch.reset(token)

    def start(parent_session_id=None, child_subagent_id=None, child_session_id=None,
              parent_subagent_id=None, **kwargs):
        if parent_subagent_id:
            log.warning("delegate_supervisor: nested parents unsupported; no root rerouting")
            return
        route = host.capture(str(parent_session_id or ""))
        if route:
            supervisor.spawn(route, child_subagent_id, child_session_id)
            current = batch.get()
            if current is not None and current[0] == parent_session_id:
                current[1].add(child_subagent_id)

    def finish(child_session_id=None, **kwargs):
        supervisor.finish(child_session=child_session_id)

    def before(session_id=None, parent_session_id=None, user_message=None, **kwargs):
        if parent_session_id or supervisor.is_child_session(session_id):
            return None
        try:
            text = user_message if isinstance(user_message, str) else "\n".join(
                part.get("text", "") for part in (user_message or []) if isinstance(part, dict)
            ) if isinstance(user_message, list) else ""
            tokens = re.findall(r"\[delegate_supervisor_request:([0-9a-f]{32})\]", text)
            return supervisor.turn_start(host.capture(str(session_id or "")), tokens)
        except Exception:
            log.warning("delegate_supervisor INACTIVE: turn boundary unsupported", exc_info=True)
            supervisor.close()
            return None

    def after(session_id=None, platform=None, **kwargs):
        if platform != "subagent" and not supervisor.is_child_session(session_id):
            supervisor.turn_end(host.capture(str(session_id or "")))

    def stop(session_key=None, session_id=None, *, clear_settings=True, **kwargs):
        route = host.capture(str(session_id or "")) if session_id else None
        key = session_key or (route.key if route is not None else None)
        if key:
            supervisor.stop_parent(key, clear_settings=clear_settings)
        elif session_id:
            supervisor.stop_session(str(session_id), clear_settings=clear_settings)

    def stopped(**kwargs):
        stop(clear_settings=False, **kwargs)

    async def supervision(raw_args):
        route = await host.command_route()
        if route is not None and supervisor.is_child_session(route.session):
            route = None
        return supervisor.control(route, raw_args)

    def tool_done(tool_name=None, args=None, result=None, **kwargs):
        if tool_name != "delegate_task" or not isinstance(args, dict) or args.get("action") != "stop":
            return
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict) and payload.get("status") == "interrupt_requested":
            supervisor.finish(ident=payload.get("subagent_id"))

    cleanup = ctx.on_unload(supervisor.close)
    if cleanup is None:
        supervisor.close()
        log.warning("delegate_supervisor INACTIVE: unload registration declined")
        return
    for name, callback in (
        ("subagent_start", start), ("subagent_stop", finish),
        ("pre_llm_call", before), ("post_llm_call", after),
        ("post_tool_call", tool_done), ("agent_loop_stopped", stopped),
        ("on_session_reset", stop), ("on_session_finalize", stop),
    ):
        ctx.register_hook(name, callback)
    ctx.register_middleware("tool_execution", execution)
    command = ctx.register_command("supervision", supervision,
        description="Show or change this conversation's automatic child supervision",
        args_hint="[<duration> | on | off | default]", argument_mode="text")
    if command is None:
        log.warning("delegate_supervisor: /supervision registration declined; timers unchanged")
    else:
        from .busy import install_busy_rejection
        install_busy_rejection(host, supervision)
    supervisor.start()
    log.info("delegate_supervisor ACTIVE: interval_seconds=%s enabled_by_default=%s",
             supervisor.interval, supervisor.enabled_by_default)
