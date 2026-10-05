"""Profile-owned busy slash rejection, without changing native host files.

Two ingress seams only: adapter active-message bypass -> native scoped handler,
then runner busy-command recognition AFTER native admission. Synchronous manager
publication and adapter wiring attach them without a background polling window.
"""
from __future__ import annotations

import logging

from .adapters import GUARD_LOCK, function_supported, source_supported

log = logging.getLogger(__name__)
BUSY_REPLY = "The parent is busy. Use /supervision after it becomes idle."
_BUSY_HASH = "3c19eb3635efe4743bdd66f19f1fe566df015369382a4a6b2ace1a31a7fc5f07"
_WIRE_HASH = "b26f28b80599565385285cf00d3e823ee3b8889e7bdb78d2eae2a3c731d72260"
_ACTIVE_HASH = "22fe801b05de44419590bb5ce5e1fa08a5b412c94f2c1c699c23effc7f96ec62"
_INLINE_HASH = "bc187577c66d4612441793de9c96eefbd716912ea68b59ff18587a016a473fec"
_CANONICAL_HASH = "1f26fa34cefa8db516fb602c94d3487988bf0614243b0ae60614efe9fc17f4d8"
_PUBLISH_HASH = "0b174dfa51d56f69af6cc35ee5d784e64b8091c3b41248c002f27efa543b4bb5"
_PIPELINE = (
    ("gateway/run_inbound.py", "_hm_admit_event", "b1bfc021f55761e90e57df78c131b8a7b4379be9d8c87d41b18d0700768476a5"),
    ("gateway/run_inbound.py", "_handle_message", "a6b2f673bc01142479e3f5c2579afe87b2ef0551582049accd232a285babca4d"),
    ("gateway/run_adapters.py", "_standalone_scoped", "6424033759f4b000eb6e06aeb0f56425caf7eaa16941f03fe4bdd003a7ffaf2e"),
    ("gateway/run_adapters.py", "_make_default_profile_message_handler", "45dfa1c67dbdc3bc56c007e1b4291357cd3f302abd7c64f0fbe9387dca848da3"),
    ("gateway/run_adapters.py", "_make_profile_message_handler", "2ab5419576d02079eab64b13202250b94afa9b01de5835b5bd82ee963fe3aef5"),
)


class _Slot:
    """Restore only our own instance shadow, including inherited-method cases."""
    def __init__(self, owner, name, replacement):
        self.owner, self.name, self.replacement = owner, name, replacement
        self.had_previous = name in owner.__dict__
        self.previous = owner.__dict__.get(name)
        setattr(owner, name, replacement)

    def restore(self):
        if getattr(self.owner, self.name, None) is self.replacement:
            if self.had_previous:
                setattr(self.owner, self.name, self.previous)
            else:
                delattr(self.owner, self.name)


class BusyGuard:
    def __init__(self, owner):
        self.owner = owner
        self.hosts = set()
        self.slots = []
        self.adapters = {}
        original = owner._hm_busy_slash_or_photo

        async def busy(event, source, key):
            from hermes_constants import get_hermes_home
            if self.owns(event, get_hermes_home().resolve()):
                denied = owner._check_slash_access(source, "supervision")
                return True, denied if denied is not None else BUSY_REPLY
            return await original(event, source, key)

        setattr(busy, "_delegate_supervisor_busy_guard", self)
        self.slots.append(_Slot(owner, "_hm_busy_slash_or_photo", busy))
        wire_original = owner._wire_adapter_handlers

        def wire(adapter, **kwargs):
            result = wire_original(adapter, **kwargs)
            # Native wiring is synchronous and occurs before connect starts ingress.
            # Never replace its scoped message/busy callbacks (standalone captures one).
            with GUARD_LOCK:
                if self.hosts:
                    self.attach_adapter(adapter)
            return result

        self.slots.append(_Slot(owner, "_wire_adapter_handlers", wire))

    def owns(self, event, home):
        if event.internal or event.get_command() != "supervision":
            return False
        # No discovery/import here: only the exact registration of this plugin.
        return any(not host.closed and host.home == home and
                   host.ctx._manager._plugin_commands.get("supervision", {}).get("handler") is handler
                   for host, handler in tuple(self.hosts))

    def attach_adapter(self, adapter):
        if adapter in self.adapters:
            return
        original = getattr(adapter, "_handle_message_while_active", None)
        if not (function_supported(original, _ACTIVE_HASH) and
                function_supported(getattr(adapter, "_dispatch_inline_reply", None), _INLINE_HASH) and
                function_supported(getattr(adapter, "_canonicalize", None), _CANONICAL_HASH)):
            log.warning("delegate_supervisor busy INACTIVE: unsupported adapter ingress; native path unchanged")
            return

        async def active(event, key):
            if not event.internal and event.get_command() == "supervision":
                identity = adapter._canonicalize(event.source)
                # Ambient home is not the routed home on a shared multiplex bot.
                if getattr(self.owner.config, "multiplex_profiles", False):
                    home = identity.runtime_home.resolve() if identity is not None else None
                else:
                    from hermes_constants import get_hermes_home
                    with self.owner._standalone_launch_scope():
                        home = get_hermes_home().resolve()
                if home is not None and self.owns(event, home):
                    # Reuse authorization/bot admission, slash ACL, reply retry/thread/
                    # ephemeral handling. No cancellation or active/pending slot changes.
                    try:
                        await adapter._dispatch_inline_reply(event)
                    except Exception:
                        # Match native bypass failures: log, never retry as model input.
                        log.error("delegate_supervisor busy reply failed", exc_info=True)
                    return None
            return await original(event, key)

        slot = _Slot(adapter, "_handle_message_while_active", active)
        self.adapters[adapter] = slot
        self.slots.append(slot)

    def attach_existing(self):
        adapters = list((getattr(self.owner, "adapters", None) or {}).values())
        for mapping in (getattr(self.owner, "_profile_adapters", None) or {}).values():
            adapters.extend(mapping.values())
        for adapter in adapters:
            self.attach_adapter(adapter)

    def remove(self, home, lease):
        self.hosts = {(host, handler) for host, handler in self.hosts if host is not lease}
        if not self.hosts:
            for slot in reversed(self.slots):
                slot.restore()
            self.adapters.clear()


def _attach(host, handler, owner, scheduler):
    if host.closed or getattr(scheduler, "__self__", None) is not owner or not function_supported(
            scheduler, "04e48da92240f9faab36594f97f315224c42a6167bb760b1e7d4ff12a9c7bfa0"):
        return
    busy = getattr(owner, "_hm_busy_slash_or_photo", None)
    guard = getattr(busy, "_delegate_supervisor_busy_guard", None)
    if guard is None:
        if not (function_supported(busy, _BUSY_HASH) and
                function_supported(getattr(owner, "_wire_adapter_handlers", None), _WIRE_HASH) and
                all(source_supported(host.root, *spec) for spec in _PIPELINE)):
            log.warning("delegate_supervisor busy INACTIVE: unsupported runner; native path unchanged")
            return
        guard = BusyGuard(owner)
    if guard.owner is not owner:
        return
    guard.hosts.add((host, handler))
    if guard not in host.guards:
        host.guards.append(guard)
    guard.attach_existing()


class BusyPublication:
    """One profile manager's publication listener; no class/global registry patch."""
    def __init__(self, host, handler):
        self.host = host
        manager = host.ctx._manager
        original = manager.set_gateway_message_injector
        if not function_supported(original, _PUBLISH_HASH):
            raise RuntimeError("unsupported gateway publication")

        def publish(owner, scheduler):
            with GUARD_LOCK:
                self.attach(handler, owner, scheduler)
                return original(owner, scheduler)

        self.slot = _Slot(manager, "set_gateway_message_injector", publish)
        slot = getattr(manager, "_gateway_message_injector", None)
        if slot:
            self.attach(handler, *slot)

    def attach(self, handler, owner, scheduler):
        try:
            _attach(self.host, handler, owner, scheduler)
        except Exception:
            log.warning("delegate_supervisor busy INACTIVE: attachment failed; native fallback may remain", exc_info=True)

    def remove(self, home, lease):
        self.slot.restore()


def install_busy_rejection(host, handler):
    with GUARD_LOCK:
        try:
            publication = BusyPublication(host, handler)
            host.guards.append(publication)
        except Exception:
            log.warning("delegate_supervisor busy INACTIVE: publication unsupported; native path unchanged", exc_info=True)
