"""Tests for own-write classification through HA context IDs.

The message order in these tests follows the live probe in spikes/task-2:
HA sends the state_changed event before the call_service result.
"""

from hagent_app.own_writes import Classified, OwnWriteTracker


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make() -> tuple[OwnWriteTracker[str], Clock]:
    clock = Clock()
    return OwnWriteTracker[str](hold=5.0, ttl=60.0, clock=clock), clock


def test_event_before_result_is_held_then_marked_agent() -> None:
    tracker, _ = make()
    token = tracker.call_started("heating")
    assert tracker.event("ctx-1", "e1") == []
    assert tracker.held == 1
    assert tracker.call_finished(token, "ctx-1") == [
        Classified("e1", "ctx-1", "agent", "heating")
    ]
    assert tracker.held == 0


def test_result_before_event_marks_agent_at_once() -> None:
    tracker, _ = make()
    token = tracker.call_started("heating")
    assert tracker.call_finished(token, "ctx-1") == []
    assert tracker.event("ctx-1", "e1") == [
        Classified("e1", "ctx-1", "agent", "heating")
    ]


def test_one_call_can_cause_several_events() -> None:
    tracker, _ = make()
    token = tracker.call_started("lights")
    tracker.event("ctx-1", "e1")
    tracker.event("ctx-1", "e2")
    released = tracker.call_finished(token, "ctx-1")
    assert [c.payload for c in released] == ["e1", "e2"]
    assert tracker.event("ctx-1", "e3")[0].origin == "agent"


def test_external_change_with_no_call_in_flight_is_external_at_once() -> None:
    tracker, _ = make()
    assert tracker.event("ctx-x", "e1") == [Classified("e1", "ctx-x", "external", None)]


def test_external_change_during_a_call_is_external_after_the_result() -> None:
    tracker, _ = make()
    token = tracker.call_started("heating")
    assert tracker.event("ctx-x", "ext") == []
    tracker.event("ctx-1", "own")
    released = tracker.call_finished(token, "ctx-1")
    assert [(c.payload, c.origin) for c in released] == [
        ("ext", "external"),
        ("own", "agent"),
    ]


def test_held_event_is_released_as_external_after_hold() -> None:
    tracker, clock = make()
    tracker.call_started("heating")
    tracker.event("ctx-x", "ext")
    clock.now = 4.9
    assert tracker.tick() == []
    clock.now = 5.0
    assert tracker.tick() == [Classified("ext", "ctx-x", "external", None)]


def test_release_keeps_arrival_order() -> None:
    tracker, _ = make()
    first = tracker.call_started("a")
    second = tracker.call_started("b")
    tracker.event("ctx-b", "eb")
    tracker.event("ctx-a", "ea")
    # The head event belongs to the second call, so nothing is released yet.
    assert tracker.call_finished(first, "ctx-a") == []
    released = tracker.call_finished(second, "ctx-b")
    assert [(c.payload, c.agent_id) for c in released] == [("eb", "b"), ("ea", "a")]


def test_no_op_call_expires_after_ttl() -> None:
    tracker, clock = make()
    token = tracker.call_started("heating")
    tracker.call_finished(token, "ctx-1")
    clock.now = 61.0
    tracker.tick()
    assert tracker.event("ctx-1", "late")[0].origin == "external"


def test_failed_call_resolves_held_events_as_external() -> None:
    tracker, _ = make()
    token = tracker.call_started("heating")
    tracker.event("ctx-x", "ext")
    assert tracker.call_finished(token, None) == [
        Classified("ext", "ctx-x", "external", None)
    ]
    assert tracker.in_flight == 0


def test_replay_of_live_probe_sequence() -> None:
    """Replay the order and context IDs that spikes/task-2/result.json records.

    HA 2026.11.1 sent each event (seq 15, 61) before its result (seq 16, 62).
    The external toggle came from another connection with a new context ID.
    """
    tracker, _ = make()
    out: list[Classified[str]] = []
    for service, context_id in (
        ("turn_on", "01M7E5K9X1MCQX7K6SZM7GAKPN"),
        ("toggle", "01M7E5KCXEHVQ6P2VXH5HY2G81"),
    ):
        token = tracker.call_started("probe")
        out += tracker.event(context_id, service)
        out += tracker.call_finished(token, context_id)
    noop = tracker.call_started("probe")
    out += tracker.call_finished(noop, "01M7E5KAWZ6735W4QTR2NW4D5M")
    out += tracker.event("01M7E5KDXRAP5JE0W082ACX4MR", "external toggle")
    assert [(c.payload, c.origin) for c in out] == [
        ("turn_on", "agent"),
        ("toggle", "agent"),
        ("external toggle", "external"),
    ]
