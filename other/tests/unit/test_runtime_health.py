from api.runtime.health import RuntimeHealth


def test_ready_and_draining_are_distinct_states():
    state = RuntimeHealth(role="worker", instance_id="worker-test")
    assert not state.snapshot()["ready"]

    state.mark_ready()
    ready = state.snapshot()
    assert ready["ready"]
    assert ready["accepting_work"]
    assert not ready["draining"]

    state.begin_draining()
    draining = state.snapshot()
    assert draining["draining"]
    assert not draining["ready"]
    assert not draining["accepting_work"]
