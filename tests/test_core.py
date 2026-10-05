import threading
from types import SimpleNamespace

import pytest
from hermes_delegate_supervisor.core import Supervisor, interval_seconds


class Host:
    def __init__(self):
        self.ids = set()
        self.messages = []
        self.accept = True
    def live_ids(self):
        return set(self.ids)
    def wake(self, route, message):
        self.messages.append((route.key, message))
        return self.accept


@pytest.fixture
def rig():
    clock = [0.0]
    host = Host()
    s = Supervisor(host, clock=lambda: clock[0])
    route = SimpleNamespace(key="parent", session="sid")
    s.spawn(route, "a", "child-a")
    host.ids.add("a")
    yield s, host, clock, route
    s.close()


@pytest.mark.parametrize("value", [0, -1, True, "600", None, float("nan"), float("inf")])
def test_invalid_interval(value):
    with pytest.raises(ValueError):
        interval_seconds(value)


def test_default_and_configurable_interval(rig):
    s, host, clock, _ = rig
    assert s.interval == 600
    clock[0] = 599
    s.tick()
    assert host.messages == []
    clock[0] = 600
    s.tick()
    assert len(host.messages) == 1
    assert Supervisor(host, interval=30).interval == 30


def test_busy_coalesces_at_next_turn_boundary(rig):
    s, host, clock, route = rig
    s.turn_start(route)
    s.spawn(route, "b", "child-b")
    host.ids.add("b")
    clock[0] = 1200
    for _ in range(4):
        s.tick()
    assert host.messages == []
    assert s.parents[route.key].pending == {"a", "b"}
    context = s.turn_start(route)["context"]
    assert "a, b" in context
    assert s.children["a"].due == 1800
    assert s.turn_start(route) is None


def test_idle_admission_only_once_until_entry(rig):
    s, host, clock, route = rig
    clock[0] = 600
    for _ in range(4):
        s.tick()
    assert len(host.messages) == 1
    assert s.turn_start(route, [s.parents[route.key].queued])
    s.turn_end(route)
    clock[0] = 1199
    s.tick()
    assert len(host.messages) == 1
    clock[0] = 1200
    s.tick()
    assert len(host.messages) == 2


def test_due_at_natural_entry_without_timer_tick(rig):
    s, host, clock, route = rig
    clock[0] = 700
    assert s.turn_start(route)
    assert not host.messages


def test_completion_and_stop_clear_due_and_queued_context(rig):
    s, host, clock, route = rig
    clock[0] = 600
    s.tick()
    s.finish(child_session="child-a")
    assert not s.children
    assert s.turn_start(route) is None
    s.spawn(route, "b", "child-b")
    s.stop_parent(route.key)
    s.tick()
    assert not s.children and not s.parents


def test_observed_registry_removal_does_not_duplicate_completion(rig):
    s, host, clock, _ = rig
    s.tick()
    host.ids.clear()
    clock[0] = 600
    s.tick()
    assert not s.children and not host.messages


def test_queued_not_yet_started_child_does_not_wake(rig):
    s, host, clock, _ = rig
    host.ids.clear()
    clock[0] = 600
    s.tick()
    assert s.children and not host.messages
    host.ids.add("a")
    s.tick()
    assert len(host.messages) == 1


def test_duplicate_spawn_preserves_deadline(rig):
    s, host, clock, route = rig
    clock[0] = 500
    s.spawn(route, "a", "child-a")
    assert s.children["a"].due == 600


def test_rejection_is_not_success_or_retry_storm(rig):
    s, host, clock, route = rig
    host.accept = False
    clock[0] = 600
    s.tick()
    s.tick()
    assert len(host.messages) == 1 and not s.parents[route.key].queued
    clock[0] = 1200
    host.accept = True
    s.tick()
    assert len(host.messages) == 2 and s.parents[route.key].queued


def test_two_parents_are_addressed_separately(rig):
    s, host, clock, _ = rig
    other = SimpleNamespace(key="other", session="other-sid")
    s.spawn(other, "b", "child-b")
    host.ids.add("b")
    s.turn_start(other)
    clock[0] = 600
    s.tick()
    assert [r for r, _ in host.messages] == ["parent"]
    assert s.turn_start(other)["context"]


def test_unload_cancels_thread_and_all_future_actions(rig):
    s, host, clock, route = rig
    s.start()
    s.close()
    assert not s._thread.is_alive()
    clock[0] = 600
    s.spawn(route, "b", "child-b")
    s.tick()
    assert not host.messages and not s.children


def test_concurrent_ticks_have_one_admission(rig):
    s, host, clock, _ = rig
    clock[0] = 600
    threads = [threading.Thread(target=s.tick) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(host.messages) == 1


def test_unrelated_turn_does_not_acknowledge_admitted_wake(rig):
    s, host, clock, route = rig
    clock[0] = 600
    s.tick()
    token = s.parents[route.key].queued
    assert s.turn_start(route)  # natural user turn handles the due review
    s.turn_end(route)
    clock[0] = 1200
    s.tick()
    assert len(host.messages) == 1 and s.parents[route.key].queued == token
    assert s.turn_start(route, ["wrong-token"])
    assert s.parents[route.key].queued == token
    assert s.turn_start(route, [token]) is None
    assert not s.parents[route.key].queued


def test_reentrant_admission_ack_is_not_overwritten(rig):
    s, host, clock, route = rig
    clock[0] = 600
    def wake(_route, _message):
        token = s.parents[route.key].queued
        assert s.turn_start(route, [token])
        return True
    host.wake = wake
    s.tick()
    assert s.parents[route.key].queued is None
