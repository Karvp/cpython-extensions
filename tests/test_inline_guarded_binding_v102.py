from __future__ import annotations

import asyncio
import dis
import functools
import threading

import pytest

from python_extensions import (
    clear_inline_registry,
    inline_calls,
    inline_function,
    verify_code,
)


def test_guarded_global_rebind_uses_loaded_fallback() -> None:
    @inline_function(register_only=True)
    def target(value):
        return value + 1

    namespace = globals()
    target_name = "_guard_v102_global_target"
    raw_name = "_guard_v102_global_raw"
    namespace[target_name] = target
    exec(
        f"def {raw_name}(value):\n    return {target_name}(value)\n",
        namespace,
    )
    try:
        guarded = inline_calls(policy="always", binding="guarded")(namespace[raw_name])
        assert guarded(4) == 5
        namespace[target_name] = lambda value: value * 10
        assert guarded(4) == 40
        assert verify_code(guarded.__code__).valid
    finally:
        namespace.pop(target_name, None)
        namespace.pop(raw_name, None)


def test_guarded_code_and_positional_defaults_mutation() -> None:
    @inline_function(register_only=True)
    def target(value, bias=2):
        return value + bias

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return target(value)

    assert guarded(3) == 5
    target.__defaults__ = (11,)
    assert guarded(3) == target(3) == 14

    old_code = target.__code__

    def replacement(value, bias=11):
        return value * 100 + bias

    target.__code__ = replacement.__code__
    try:
        assert guarded(3) == target(3) == 311
    finally:
        target.__code__ = old_code


def test_guarded_kwdefaults_in_place_mutation() -> None:
    @inline_function(register_only=True)
    def target(value, *, bias=2):
        return value + bias

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return target(value)

    assert guarded(3) == 5
    assert target.__kwdefaults__ is not None
    target.__kwdefaults__["bias"] = 17
    assert guarded(3) == target(3) == 20


def test_guarded_mutable_default_object_stays_live_without_false_deopt() -> None:
    bucket: list[int] = []

    @inline_function(register_only=True)
    def target(value, values=bucket):
        values.append(value)
        return len(values)

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return target(value)

    assert guarded(1) == 1
    bucket.append(99)
    assert guarded(2) == 3
    assert target.__defaults__ is not None
    assert target.__defaults__[0] is bucket


def test_guarded_static_and_class_method_replacement() -> None:
    class Helpers:
        factor = 3

        @staticmethod
        @inline_function(register_only=True)
        def static(value):
            return value + 2

        @classmethod
        @inline_function(register_only=True)
        def klass(cls, value):
            return value * cls.factor

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return Helpers.static(value), Helpers.klass(value)

    assert guarded(4) == (6, 12)
    Helpers.static = staticmethod(lambda value: value * 10)
    Helpers.klass = classmethod(lambda cls, value: value + cls.factor)
    assert guarded(4) == (40, 7)
    assert verify_code(guarded.__code__).valid


def test_guarded_instance_method_observes_receiver_and_method_mutation() -> None:
    class Handler:
        def __init__(self, factor):
            self.factor = factor

        @inline_function(register_only=True)
        def apply(self, value):
            return self.factor * value

    handler = Handler(2)

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return handler.apply(value)

    assert guarded(5) == 10
    handler.factor = 7
    assert guarded(5) == 35
    Handler.apply = lambda self, value: self.factor + value
    assert guarded(5) == 12


def test_guarded_partial_keyword_mutation() -> None:
    @inline_function(register_only=True)
    def target(value, *, bias=1):
        return value + bias

    configured = functools.partial(target, bias=3)

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return configured(value)

    assert guarded(4) == 7
    assert configured.keywords is not None
    configured.keywords["bias"] = 20
    assert guarded(4) == configured(4) == 24


def test_guarded_callable_object_type_call_replacement() -> None:
    class Scale:
        def __init__(self, factor):
            self.factor = factor

        @inline_function(register_only=True)
        def __call__(self, value):
            return self.factor * value

    scaler = Scale(3)

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return scaler(value)

    assert guarded(4) == 12
    scaler.factor = 5
    assert guarded(4) == 20
    Scale.__call__ = lambda self, value: self.factor + value
    assert guarded(4) == 9


def test_guarded_closure_rebind_and_function_state_mutation() -> None:
    @inline_function(register_only=True)
    def first(value):
        return value + 1

    @inline_function(register_only=True)
    def second(value):
        return value * 2

    current = first

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return current(value)

    assert guarded(5) == 6
    current = second
    assert guarded(5) == 10

    old_code = second.__code__

    def replacement(value):
        return value - 9

    second.__code__ = replacement.__code__
    try:
        assert guarded(5) == -4
    finally:
        second.__code__ = old_code


def test_guarded_descriptor_lookup_runs_once_on_fast_and_fallback_paths() -> None:
    @inline_function(register_only=True)
    def initial(value):
        return value + 1

    def replacement(value):
        return value * 10

    class CountingDescriptor:
        def __init__(self):
            self.target = initial
            self.reads = 0

        def __get__(self, instance, owner):
            self.reads += 1
            return self.target

    descriptor = CountingDescriptor()

    class Owner:
        call = descriptor

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        return Owner.call(value)

    descriptor.reads = 0
    assert guarded(2) == 3
    assert descriptor.reads == 1

    descriptor.target = replacement
    descriptor.reads = 0
    assert guarded(2) == 20
    assert descriptor.reads == 1


def test_guarded_fallback_preserves_caller_exception_handler() -> None:
    @inline_function(register_only=True)
    def target(value):
        return 100 // value

    @inline_calls(policy="always", binding="guarded")
    def guarded(value):
        try:
            return target(value)
        except ZeroDivisionError:
            return -1

    assert guarded(4) == 25
    old_code = target.__code__

    def replacement(value):
        raise ZeroDivisionError(value)

    target.__code__ = replacement.__code__
    try:
        assert guarded(4) == -1
    finally:
        target.__code__ = old_code


def test_guarded_shared_region_deoptimizes_after_default_change() -> None:
    @inline_function(register_only=True, shared_region=True)
    def target(value, bias=1):
        value = value * 3 + bias
        value = value * 5 - bias
        return value

    @inline_calls(
        policy="always",
        binding="guarded",
        shared_regions="auto",
        shared_min_body_instructions=1,
    )
    def guarded(value):
        a = target(value)
        b = target(a)
        return target(b)

    def baseline(value):
        a = target(value)
        b = target(a)
        return target(b)

    assert guarded(2) == baseline(2)
    assert guarded.__inline_stats__.calls_shared == 3
    target.__defaults__ = (9,)
    assert guarded(2) == baseline(2)
    assert verify_code(guarded.__code__).valid


def test_guarded_async_caller_and_concurrent_calls() -> None:
    @inline_function(register_only=True)
    def target(value):
        return value * 2 + 1

    @inline_calls(policy="always", binding="guarded")
    async def guarded(value):
        await asyncio.sleep(0)
        return target(value)

    async def run():
        values = await asyncio.gather(*(guarded(i) for i in range(100)))
        assert values == [i * 2 + 1 for i in range(100)]

    asyncio.run(run())

    @inline_calls(policy="always", binding="guarded")
    def threaded(value):
        return target(value)

    failures: list[tuple[int, int]] = []
    barrier = threading.Barrier(8)

    def worker(seed):
        barrier.wait()
        for index in range(2000):
            value = seed * 2000 + index
            actual = threaded(value)
            expected = value * 2 + 1
            if actual != expected:
                failures.append((expected, actual))
                return

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not failures


def test_guarded_speed_policy_rejects_trivial_guard_cost() -> None:
    @inline_function(register_only=True)
    def target(value):
        return value + 1

    @inline_calls(policy="speed", binding="guarded")
    def guarded(value):
        return target(value)

    assert guarded(3) == 4
    assert guarded.__inline_stats__.calls_inlined == 0
    assert guarded.__inline_stats__.calls_skipped_unprofitable == 1


def test_frozen_mode_remains_explicit_snapshot_semantics() -> None:
    @inline_function(register_only=True)
    def target(value, bias=1):
        return value + bias

    @inline_calls(policy="always", binding="frozen")
    def frozen(value):
        return target(value)

    assert frozen(2) == 3
    target.__defaults__ = (10,)
    assert target(2) == 12
    assert frozen(2) == 3
    assert not any(
        instruction.opname == "LOAD_GLOBAL" and instruction.argval == "target"
        for instruction in dis.get_instructions(frozen, adaptive=False)
    )


def test_invalid_binding_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="binding"):
        inline_calls(binding="mystery")(lambda value: value)
