"""Adversarial differential/stress harness for specialization v1.21.

This intentionally complements (rather than duplicates) v120:
* difficult Python call signatures and fallback binding
* side-effecting equality and identity-only guards
* canonical float/complex guards, including NaNs and signed zero
* bounded high-cardinality adaptive profiling
* concurrent promotion races
* monitoring lifecycle churn / weak-reference release
* recursive closures, tracing, descriptors, and async wrappers

Run quick:  PYTHONPATH=src python -X dev -W error tests/harness_specialization_v121_adversarial.py
Run full:   ... harness_specialization_v121_adversarial.py --full
"""
from __future__ import annotations

import asyncio
import gc
import math
import random
import struct
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

from python_extensions import hotpath, partial, specialize, verify_code
from python_extensions._specialize import _MONITORING_HOTPATHS


def _same_result(base, optimized, args=(), kwargs=None):
    kwargs = {} if kwargs is None else kwargs
    try:
        expected = base(*args, **kwargs)
    except BaseException as exc:
        expected_exc = exc
    else:
        expected_exc = None
    try:
        actual = optimized(*args, **kwargs)
    except BaseException as exc:
        actual_exc = exc
    else:
        actual_exc = None
    if expected_exc is not None or actual_exc is not None:
        assert type(actual_exc) is type(expected_exc), (expected_exc, actual_exc, args, kwargs)
        assert getattr(actual_exc, "args", None) == getattr(expected_exc, "args", None), (
            expected_exc,
            actual_exc,
            args,
            kwargs,
        )
        return
    assert actual == expected, (expected, actual, args, kwargs)


def _call_binding_stress(rng: random.Random, count: int) -> int:
    def baseline(a, /, b=3, *items, mode="add", scale=2, **options):
        payload = a + b + sum(items) + options.get("extra", 0)
        if mode == "add":
            return payload + scale
        if mode == "mul":
            return payload * scale
        if mode == "neg":
            return -payload
        return payload - scale

    inline = specialize(constants={"mode": "mul"}, policy="always")(baseline)
    wrapper = specialize(constants={"mode": "mul"}, policy="always", dispatch="wrapper")(baseline)
    adaptive = hotpath(
        threshold=8,
        max_variants=3,
        types=False,
        constants=("mode",),
        policy="always",
        backend="wrapper",
    )(baseline)
    assert inline.__python_extensions_dispatch_mode__ == "inline"
    assert wrapper.__python_extensions_dispatch_mode__ == "wrapper"
    verify_code(inline.__code__)
    verify_code(wrapper.__code__)

    modes = ("add", "mul", "neg", "miss")
    for i in range(count):
        a = rng.randint(-50, 50)
        b = rng.randint(-10, 10)
        scale = rng.randint(-5, 5)
        mode = modes[rng.randrange(4)]
        style = i % 6
        if style == 0:
            args, kwargs = (a,), {"b": b, "mode": mode, "scale": scale}
        elif style == 1:
            args, kwargs = (a, b), {"mode": mode, "scale": scale, "extra": i & 7}
        elif style == 2:
            args, kwargs = (a, b, 1, -2, 3), {"mode": mode, "scale": scale}
        elif style == 3:
            args, kwargs = (a,), {"mode": mode, "scale": scale, "unknown": i}
        elif style == 4:
            # Intentionally invalid: duplicate b.
            args, kwargs = (a, b), {"b": b + 1, "mode": mode}
        else:
            # Intentionally invalid: positional-only a as keyword.
            args, kwargs = (), {"a": a, "b": b, "mode": mode}
        _same_result(baseline, inline, args, kwargs)
        _same_result(baseline, wrapper, args, kwargs)
        _same_result(baseline, adaptive, args, kwargs)
    stats = adaptive.specialization_stats()
    assert stats.variants_created == 3
    assert stats.profiling_active is False
    return count * 3


def _partial_stress(rng: random.Random, count: int) -> int:
    def baseline(a, /, b=2, c=3, *, mode="x", suffix="!"):
        before = (a, b, c, mode, suffix)
        c = c + 1
        if mode == "x":
            return before, a + b * c, suffix
        return before, a - b * c, suffix

    fixed = partial(baseline, b=7, mode="x")
    verify_code(fixed.__code__)
    for i in range(count):
        a = rng.randint(-1000, 1000)
        c = rng.randint(-20, 20)
        suffix = str(i & 15)
        expected = baseline(a, b=7, c=c, mode="x", suffix=suffix)
        assert fixed(a, c=c, suffix=suffix) == expected
    return count


class _ExplosiveEq:
    def __init__(self, label: str):
        self.label = label
        self.calls = 0

    def __eq__(self, other):
        self.calls += 1
        raise RuntimeError("user equality observed")

    __hash__ = None


def _identity_guard_stress(count: int) -> int:
    marker = _ExplosiveEq("marker")

    def baseline(value, token):
        return value + (10 if token is marker else -10)

    inline = specialize(constants={"token": marker}, policy="always")(baseline)
    wrapper = specialize(constants={"token": marker}, policy="always", dispatch="wrapper")(baseline)
    other = _ExplosiveEq("other")
    for i in range(count):
        token = marker if (i & 1) else other
        expected = baseline(i, token)
        assert inline(i, token) == expected
        assert wrapper(i, token) == expected
    assert marker.calls == 0 and other.calls == 0
    return count * 2


def _float_from_bits(bits: int) -> float:
    return struct.unpack("!d", bits.to_bytes(8, "big"))[0]


def _numeric_guard_stress(rng: random.Random, count: int) -> int:
    special_bits = 0x7FF8_0000_0000_1234
    special_nan = _float_from_bits(special_bits)

    def classify_float(value, token):
        if math.isnan(token):
            return value + 100
        return value + int(math.copysign(1.0, token))

    float_guard = specialize(
        constants={"token": special_nan}, dispatch="wrapper", policy="always"
    )(classify_float)

    def classify_complex(value, token):
        return value + (1000 if math.isnan(token.real) else int(token.real) + int(token.imag))

    complex_token = complex(_float_from_bits(0x7FF8_0000_0000_4321), -0.0)
    complex_guard = specialize(
        constants={"token": complex_token}, dispatch="wrapper", policy="always"
    )(classify_complex)

    float_pool = [
        -0.0,
        +0.0,
        float("inf"),
        float("-inf"),
        1.5,
        _float_from_bits(special_bits),
        _float_from_bits(0x7FF8_0000_0000_5678),
    ]
    complex_pool = [
        complex_token,
        complex(_float_from_bits(0x7FF8_0000_0000_4321), -0.0),
        complex(_float_from_bits(0x7FF8_0000_0000_9999), -0.0),
        complex(1.0, -0.0),
        complex(1.0, +0.0),
    ]
    for _ in range(count):
        value = rng.randint(-1000, 1000)
        f = float_pool[rng.randrange(len(float_pool))]
        z = complex_pool[rng.randrange(len(complex_pool))]
        assert float_guard(value, f) == classify_float(value, f)
        assert complex_guard(value, z) == classify_complex(value, z)
    fstats = float_guard.specialization_stats()
    zstats = complex_guard.specialization_stats()
    assert fstats.variant_hits > 0 and fstats.fallback_calls > 0
    assert zstats.variant_hits > 0 and zstats.fallback_calls > 0
    return count * 2


def _megamorphic_stress(count: int) -> int:
    def baseline(value, token):
        if token == ("never",):
            return value + 1
        return value - 1

    optimized = hotpath(
        threshold=10_000,
        max_variants=2,
        types=False,
        constants=("token",),
        policy="always",
        max_profiled_shapes=32,
        profile_budget=min(count, 50_000),
        backend="wrapper",
    )(baseline)
    budget = min(count, 50_000)
    for i in range(count):
        selector = i % 4
        if selector == 0:
            token = (i % 1009, -0.0, "x")
        elif selector == 1:
            token = frozenset((i % 997, "k"))
        elif selector == 2:
            token = (complex(float(i % 17), -0.0), i % 991)
        else:
            token = (i % 983, bytes((i & 255,)))
        assert optimized(i, token) == baseline(i, token)
    stats = optimized.specialization_stats()
    assert stats.profile_calls == budget
    assert stats.profiled_shapes <= 32
    assert stats.profile_evictions > 0
    assert stats.profile_budget_exhausted is True
    assert stats.profiling_active is False
    assert stats.variants_created == 0
    return count


def _concurrent_promotion_stress(each: int) -> int:
    def baseline(value, mode):
        if mode == "a": return value + 1
        if mode == "b": return value + 2
        if mode == "c": return value + 3
        if mode == "d": return value + 4
        if mode == "e": return value + 5
        return value - 1

    optimized = hotpath(
        threshold=32,
        max_variants=4,
        types=False,
        constants=("mode",),
        policy="always",
        backend="wrapper",
        profile_budget=max(20_000, each * 16),
    )(baseline)
    barrier = threading.Barrier(16)
    modes = ("a", "b", "c", "d", "e", "miss")

    def worker(tid: int):
        barrier.wait()
        subtotal = 0
        for i in range(each):
            mode = modes[(i + tid) % len(modes)]
            value = (tid << 16) ^ i
            got = optimized(value, mode)
            expected = baseline(value, mode)
            assert got == expected
            subtotal ^= got
        return subtotal

    with ThreadPoolExecutor(max_workers=16) as pool:
        totals = list(pool.map(worker, range(16)))
    assert len(totals) == 16
    stats = optimized.specialization_stats()
    assert stats.variants_created == 4
    assert stats.profiling_active is False
    return each * 16


def _recursive_monitoring_stress(count: int) -> int:
    def outer(bias):
        def baseline(n, mode="plus"):
            if n <= 0:
                if mode == "plus": return bias
                return -bias
            return baseline(n - 1, mode) + (1 if mode == "plus" else -1)
        return baseline

    baseline = outer(7)
    optimized = hotpath(
        threshold=4,
        max_variants=1,
        types=False,
        constants=("mode",),
        policy="always",
        backend="monitoring",
    )(baseline)
    for i in range(count):
        n = i % 12
        mode = "plus" if i % 5 else "minus"
        assert optimized(n, mode) == baseline(n, mode)
    assert optimized.__python_extensions_dispatch_mode__ == "monitoring-inline"
    return count


def _trace_stress(count: int) -> int:
    trace_calls = 0

    def tracer(frame, event, arg):
        nonlocal trace_calls
        if event == "call":
            trace_calls += 1
        return tracer

    def baseline(value, mode):
        if mode == "fast":
            return value * 3 + 1
        return value - 7

    optimized = hotpath(
        threshold=8,
        max_variants=1,
        types=False,
        constants=("mode",),
        policy="always",
        backend="monitoring",
    )(baseline)
    old = sys.gettrace()
    sys.settrace(tracer)
    try:
        for i in range(count):
            mode = "fast" if i % 3 else "slow"
            assert optimized(i, mode) == baseline(i, mode)
    finally:
        sys.settrace(old)
    assert trace_calls > 0
    return count


def _descriptor_stress(count: int) -> int:
    class Demo:
        factor = 5

        @staticmethod
        @specialize(constants={"mode": "x"}, policy="always")
        def static(value, mode="x"):
            return value + 1 if mode == "x" else value - 1

        @classmethod
        @hotpath(threshold=4, types=False, constants=("mode",), policy="always", backend="wrapper")
        def cls(cls, value, mode="x"):
            return value * cls.factor if mode == "x" else value - cls.factor

    for i in range(count):
        mode = "x" if i & 1 else "y"
        assert Demo.static(i, mode) == (i + 1 if mode == "x" else i - 1)
        assert Demo.cls(i, mode) == (i * 5 if mode == "x" else i - 5)
    return count * 2


async def _async_body(count: int) -> int:
    async def baseline(value, mode="fast"):
        if value & 127 == 0:
            await asyncio.sleep(0)
        if mode == "fast":
            return value + 1
        if mode == "slow":
            return value - 1
        raise LookupError(mode)

    specialized = specialize(
        constants={"mode": "fast"}, policy="always", dispatch="wrapper"
    )(baseline)
    adaptive = hotpath(
        threshold=8,
        max_variants=2,
        types=False,
        constants=("mode",),
        policy="always",
        backend="wrapper",
    )(baseline)
    modes = ("fast", "slow", "bad")
    for i in range(count):
        mode = modes[i % 3]
        try:
            expected = await baseline(i, mode)
        except Exception as e1:
            for fn in (specialized, adaptive):
                try:
                    await fn(i, mode)
                except Exception as e2:
                    assert type(e2) is type(e1) and e2.args == e1.args
                else:
                    raise AssertionError("optimized async call failed to raise")
        else:
            assert await specialized(i, mode) == expected
            assert await adaptive(i, mode) == expected
    return count * 2


def _monitoring_lifecycle_churn(count: int) -> int:
    refs = []
    for outer in range(count):
        def baseline(value, mode, bias=outer):
            if mode == "a":
                return value + bias
            return value - bias

        optimized = hotpath(
            threshold=2,
            max_variants=1,
            types=False,
            constants=("mode",),
            policy="always",
            backend="monitoring",
        )(baseline)
        assert optimized(1, "a") == 1 + outer
        assert optimized(2, "a") == 2 + outer
        assert optimized(3, "b") == 3 - outer
        if (outer & 31) == 0:
            refs.append(weakref.ref(optimized))
        del optimized
    # All callbacks must have detached. The loop's latest baseline is unrelated
    # to the monitored clone and may remain live until function exit.
    assert not _MONITORING_HOTPATHS._entries
    assert _MONITORING_HOTPATHS._claimed is False
    gc.collect(); gc.collect()
    assert all(ref() is None for ref in refs)
    return count * 3


def run(*, full: bool) -> int:
    rng = random.Random(0x1215_EED)
    if full:
        counts = dict(
            binding=800_000,
            partial=750_000,
            identity=500_000,
            numeric=650_000,
            mega=300_000,
            concurrent_each=80_000,
            recursive=300_000,
            trace=100_000,
            descriptor=400_000,
            async_count=40_000,
            churn=2_000,
        )
    else:
        counts = dict(
            binding=12_000,
            partial=10_000,
            identity=8_000,
            numeric=10_000,
            mega=8_000,
            concurrent_each=2_000,
            recursive=5_000,
            trace=2_000,
            descriptor=5_000,
            async_count=1_000,
            churn=100,
        )

    calls = 0
    calls += _call_binding_stress(rng, counts["binding"])
    calls += _partial_stress(rng, counts["partial"])
    calls += _identity_guard_stress(counts["identity"])
    calls += _numeric_guard_stress(rng, counts["numeric"])
    calls += _megamorphic_stress(counts["mega"])
    calls += _concurrent_promotion_stress(counts["concurrent_each"])
    calls += _recursive_monitoring_stress(counts["recursive"])
    calls += _trace_stress(counts["trace"])
    calls += _descriptor_stress(counts["descriptor"])
    calls += asyncio.run(_async_body(counts["async_count"]))
    calls += _monitoring_lifecycle_churn(counts["churn"])
    assert not _MONITORING_HOTPATHS._entries
    return calls


if __name__ == "__main__":
    calls = run(full="--full" in sys.argv)
    print(f"specialization adversarial harness: PASS ({calls:,} differential/stress calls)")
