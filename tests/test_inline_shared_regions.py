from __future__ import annotations

import dis
import threading

from python_extensions import inline_calls, inline_function, verify_code


@inline_function(register_only=True, shared_region=True)
def shared_heavy(value):
    value = value * 3 + 1
    value = value * 5 - 2
    value = value * 7 + 3
    value = value * 11 - 4
    value = value * 13 + 5
    value = value * 17 - 6
    value = value * 19 + 7
    value = value * 23 - 8
    return value




@inline_function(register_only=True)
def ordinary_unmarked(value):
    value = value * 3 + 1
    value = value * 5 + 2
    value = value * 7 + 3
    return value

def raw_chain(value):
    a = shared_heavy(value)
    b = shared_heavy(a)
    c = shared_heavy(b)
    d = shared_heavy(c)
    e = shared_heavy(d)
    f = shared_heavy(e)
    g = shared_heavy(f)
    h = shared_heavy(g)
    return h


DUPLICATED = inline_calls(shared_regions=False, policy="always")(raw_chain)
SHARED = inline_calls(
    shared_regions="auto",
    shared_min_body_instructions=1,
    policy="always",
)(raw_chain)


def test_shared_region_semantics_and_stats():
    for value in (-3, 0, 1, 2, 9):
        assert SHARED(value) == raw_chain(value)
    stats = SHARED.__inline_stats__
    assert stats.calls_shared == 8
    assert stats.shared_regions == 1
    assert stats.calls_inlined == 0
    assert verify_code(SHARED.__code__).valid


def test_shared_region_reuses_one_body_and_reduces_duplicate_growth():
    assert len(SHARED.__code__.co_code) < len(DUPLICATED.__code__.co_code)
    instructions = list(dis.get_instructions(SHARED, adaptive=False))
    assert not any(i.opname == "LOAD_GLOBAL" and i.argval == "shared_heavy" for i in instructions)
    assert any(i.opname == "JUMP_FORWARD" for i in instructions)
    assert any(i.opname == "JUMP_BACKWARD" for i in instructions)


def test_shared_continuation_is_frame_local_under_threads():
    failures: list[tuple[int, int, int]] = []
    barrier = threading.Barrier(8)

    def worker(seed: int) -> None:
        barrier.wait()
        for step in range(2000):
            value = (seed * 17 + step) % 11 - 5
            expected = raw_chain(value)
            actual = SHARED(value)
            if actual != expected:
                failures.append((value, expected, actual))
                return

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures


def test_exception_protected_calls_share_within_same_context():
    @inline_calls(shared_min_body_instructions=1, policy="always")
    def protected(value):
        try:
            a = shared_heavy(value)
            b = shared_heavy(a)
            c = shared_heavy(b)
            return c
        except ArithmeticError:
            return -1

    assert protected(2) == shared_heavy(shared_heavy(shared_heavy(2)))
    assert protected.__inline_stats__.calls_shared == 3
    assert protected.__inline_stats__.shared_regions == 1
    assert protected.__inline_stats__.protected_shared_regions == 1
    assert protected.__inline_stats__.calls_inlined == 0
    assert verify_code(protected.__code__).valid


def test_unmarked_callee_is_not_auto_shared():
    @inline_calls(shared_min_body_instructions=1, policy="always")
    def caller(value):
        a = ordinary_unmarked(value)
        b = ordinary_unmarked(a)
        return ordinary_unmarked(b)

    assert caller(3) == ordinary_unmarked(ordinary_unmarked(ordinary_unmarked(3)))
    assert caller.__inline_stats__.calls_shared == 0
    assert caller.__inline_stats__.calls_inlined == 3


@inline_function(register_only=True, shared_region=True)
def branching_shared(value, bias=2):
    if value < 0:
        return -value + bias
    return value * 2 + bias


def test_shared_region_multiple_returns_and_defaults():
    @inline_calls(shared_min_body_instructions=1, policy="always")
    def caller(value):
        a = branching_shared(value)
        b = branching_shared(a)
        return branching_shared(b)

    for value in (-4, -1, 0, 3):
        expected = branching_shared(branching_shared(branching_shared(value)))
        assert caller(value) == expected
    assert caller.__inline_stats__.calls_shared == 3


def test_shared_region_falls_back_for_incompatible_expression_stack_shapes():
    @inline_calls(shared_min_body_instructions=1, policy="always")
    def caller(value):
        a = 10 + branching_shared(value)
        b = branching_shared(a)
        return 5 * branching_shared(b)

    expected = 5 * branching_shared(branching_shared(10 + branching_shared(2)))
    assert caller(2) == expected
    # Differing caller expression stack depths must never produce invalid bytecode.
    assert verify_code(caller.__code__).valid


@inline_function(register_only=True, shared_region=True)
def protected_divide(value):
    quotient = 100 // value
    quotient += 1
    quotient *= 2
    return quotient - 3


def test_protected_shared_region_preserves_caller_exception_handler():
    @inline_calls(shared_min_body_instructions=1, policy="always")
    def caller(value):
        try:
            a = protected_divide(value)
            b = protected_divide(a)
            return protected_divide(b)
        except ZeroDivisionError:
            return -999

    def baseline(value):
        try:
            a = protected_divide(value)
            b = protected_divide(a)
            return protected_divide(b)
        except ZeroDivisionError:
            return -999

    for value in (0, 1, 2, -2, 7):
        assert caller(value) == baseline(value)
    assert caller.__inline_stats__.protected_shared_regions == 1
    assert caller.__inline_stats__.calls_shared == 3
    assert verify_code(caller.__code__).valid


@inline_function(register_only=True, shared_region=True)
def callee_with_own_try(value):
    try:
        result = 10 // value
    except ZeroDivisionError:
        result = 0
    return result + 1


def test_protected_shared_region_falls_back_when_callee_has_own_try():
    @inline_calls(shared_min_body_instructions=1, policy="always")
    def caller(value):
        try:
            a = callee_with_own_try(value)
            b = callee_with_own_try(a)
            return callee_with_own_try(b)
        except ArithmeticError:
            return -1

    expected = callee_with_own_try(callee_with_own_try(callee_with_own_try(0)))
    assert caller(0) == expected
    assert caller.__inline_stats__.protected_shared_regions == 0
    assert caller.__inline_stats__.calls_shared == 0
    assert caller.__inline_stats__.calls_inlined == 0
    assert caller.__inline_stats__.calls_skipped_unsupported == 3
    assert verify_code(caller.__code__).valid
