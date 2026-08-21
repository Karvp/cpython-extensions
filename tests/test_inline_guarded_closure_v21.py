from __future__ import annotations

import dis
import threading

from python_extensions import inline_calls, inline_function


def _make_guarded_caller(*, policy="speed"):
    @inline_function(register_only=True)
    def add1(value):
        return value + 1

    target = add1

    def rebind(new_target):
        nonlocal target
        target = new_target

    @inline_calls(policy=policy)
    def caller(value):
        return target(value)

    return caller, rebind, add1


def test_registered_closure_callee_uses_identity_guard_and_inlines():
    caller, _, _ = _make_guarded_caller(policy="always")
    assert caller(10) == 11
    assert caller.__inline_stats__.calls_inlined == 1
    assert caller.__inline_stats__.guarded_closure_calls == 1
    assert dict(caller.__python_extensions_report__.details)["guarded_closure_calls"] == 1
    ops = [item.opname for item in dis.get_instructions(caller, adaptive=False)]
    assert "IS_OP" in ops
    assert "CALL" in ops  # cold rebound fallback remains exact


def test_registered_closure_callee_falls_back_after_nonlocal_rebind():
    caller, rebind, _ = _make_guarded_caller(policy="always")
    assert caller(3) == 4
    rebind(lambda value: value * 10)
    assert caller(3) == 30
    rebind(lambda value: -value)
    assert caller(3) == -3


def test_guarded_closure_load_is_evaluate_once_per_call_under_rebinding():
    caller, rebind, add1 = _make_guarded_caller(policy="always")
    stop = threading.Event()
    errors: list[object] = []

    def alt(value):
        return value + 1000

    def mutator():
        while not stop.is_set():
            rebind(alt)
            rebind(add1)

    thread = threading.Thread(target=mutator)
    thread.start()
    try:
        for value in range(20_000):
            result = caller(value)
            if result not in {value + 1, value + 1000}:
                errors.append((value, result))
                break
    finally:
        stop.set()
        thread.join()
    assert errors == []


def test_unregistered_closure_callable_is_left_ordinary():
    def factory():
        target = lambda value: value + 2

        @inline_calls(policy="always")
        def caller(value):
            return target(value)

        return caller

    caller = factory()
    assert caller(5) == 7
    assert caller.__inline_stats__.calls_inlined == 0
    assert caller.__inline_stats__.guarded_closure_calls == 0


def test_speed_policy_leaves_trivial_guarded_closure_call_ordinary():
    caller, _, _ = _make_guarded_caller(policy="speed")
    assert caller(10) == 11
    assert caller.__inline_stats__.calls_inlined == 0
    assert caller.__inline_stats__.guarded_closure_calls == 0
