"""Stress/differential harness for partial, specialize, and hotpath."""
from __future__ import annotations

import random
import sys
from concurrent.futures import ThreadPoolExecutor

from python_extensions import hotpath, partial, specialize, verify_code


def _route(value: int, mode: str = "m0") -> int:
    if mode == "m0":
        return value + 1
    if mode == "m1":
        return value * 2
    if mode == "m2":
        return value - 7
    if mode == "m3":
        return value ^ 0x55
    if mode == "m4":
        return value * 5 + 3
    if mode == "m5":
        return value // 3
    if mode == "m6":
        return -value
    if mode == "m7":
        return value + 100
    return value


_partial_m7 = partial(_route, mode="m7")
_specialized = specialize(constants={"mode": "m7"}, max_variants=3)(_route)
_specialized.register_specialization(constants={"mode": "m4"})
_hot = hotpath(
    threshold=32,
    max_variants=3,
    types=False,
    constants=("mode",),
    policy="always",
)(_route)


class _A:
    pass


class _B:
    pass


class _C:
    pass


def _typed(value):
    if type(value) is _A:
        return 1
    if type(value) is _B:
        return 2
    if type(value) is _C:
        return 3
    return 0


_typed_specialized = specialize(types={"value": _C})(_typed)
_typed_hot = hotpath(threshold=32, policy="always")(_typed)


def _make_large_route(count: int = 64):
    namespace: dict[str, object] = {}
    lines = ["def route(value, mode):"]
    for index in range(count):
        prefix = "    if" if index == 0 else "    elif"
        lines.append(f"{prefix} mode == 'm{index}':")
        lines.append(f"        return value * 3 + {index}")
    lines.extend(["    else:", "        return value - 11"])
    exec("\n".join(lines), namespace)
    return namespace["route"]


_monitoring_baseline = _make_large_route()
_monitoring_hot = hotpath(
    threshold=32,
    max_variants=1,
    types=False,
    constants=("mode",),
    policy="speed",
    backend="monitoring",
)(_monitoring_baseline)


def _megamorphic(value: int, token: str) -> int:
    if token == "__special__":
        return value + 1000
    return value - 3


_megamorphic_hot = hotpath(
    threshold=64,
    max_variants=1,
    types=False,
    constants=("token",),
    policy="speed",
    max_profiled_shapes=16,
    profile_budget=2_000,
    backend="monitoring",
)(_megamorphic)


def run(*, full: bool) -> int:
    counts = {
        "partial": 600_000 if full else 20_000,
        "specialize": 1_000_000 if full else 30_000,
        "hotpath": 1_000_000 if full else 30_000,
        "types": 600_000 if full else 20_000,
        "threaded": 800_000 if full else 24_000,
        "monitoring": 800_000 if full else 24_000,
        "megamorphic": 200_000 if full else 8_000,
    }
    calls = 0
    rng = random.Random(0x120)
    modes = tuple(f"m{i}" for i in range(8)) + ("miss",)

    assert verify_code(_partial_m7.__code__).valid
    assert verify_code(_specialized.__code__).valid
    assert _specialized.__python_extensions_dispatch_mode__ == "inline"

    for i in range(counts["partial"]):
        value = (i % 2001) - 1000
        assert _partial_m7(value) == _route(value, "m7")
    calls += counts["partial"]

    for _ in range(counts["specialize"]):
        value = rng.randint(-10_000, 10_000)
        mode = modes[rng.randrange(len(modes))]
        assert _specialized(value, mode) == _route(value, mode)
    calls += counts["specialize"]

    for _ in range(counts["hotpath"]):
        value = rng.randint(-10_000, 10_000)
        # Keep three shapes hot enough to force promotion while retaining misses.
        mode = modes[rng.randrange(len(modes))]
        assert _hot(value, mode) == _route(value, mode)
    calls += counts["hotpath"]

    # Force one profitable late-route monitoring specialization, then exercise
    # both its specialized hit and the generic fallback with randomized traffic.
    for i in range(32):
        assert _monitoring_hot(i, "m63") == _monitoring_baseline(i, "m63")
    assert _monitoring_hot.__python_extensions_dispatch_mode__ == "monitoring-inline"
    for _ in range(counts["monitoring"]):
        value = rng.randint(-100_000, 100_000)
        route_index = rng.randrange(70)
        mode = f"m{route_index}" if route_index < 64 else "miss"
        assert _monitoring_hot(value, mode) == _monitoring_baseline(value, mode)
    calls += 32 + counts["monitoring"]

    # High-cardinality traffic must stop profiling at its finite budget, keep
    # the observed-shape table bounded, and converge to exact passthrough.
    for i in range(counts["megamorphic"]):
        token = f"token-{i % 997}"
        value = i - 500
        assert _megamorphic_hot(value, token) == _megamorphic(value, token)
    mega_stats = _megamorphic_hot.specialization_stats()
    assert mega_stats.profiled_shapes <= 16
    assert mega_stats.profile_budget_exhausted is True
    assert mega_stats.profiling_active is False
    assert mega_stats.variants_created == 0
    assert _megamorphic_hot.__python_extensions_dispatch_mode__ == "passthrough"
    calls += counts["megamorphic"]

    typed_values = (_A(), _B(), _C(), object())
    for i in range(counts["types"]):
        value = typed_values[i & 3]
        expected = _typed(value)
        assert _typed_specialized(value) == expected
        assert _typed_hot(value) == expected
    calls += counts["types"] * 2

    workers = 8
    each = counts["threaded"] // workers

    def worker(worker_id: int) -> int:
        subtotal = 0
        for i in range(each):
            value = i + worker_id
            mode = "m7" if (i & 1) else "m4"
            actual = _specialized(value, mode)
            expected = _route(value, mode)
            assert actual == expected
            subtotal += actual
        return subtotal

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(worker, range(workers)))
    assert all(isinstance(value, int) for value in results)
    calls += each * workers

    assert _hot.specialization_stats().variants_created <= 3
    assert _typed_hot.specialization_stats().variants_created >= 1
    return calls


if __name__ == "__main__":
    full = "--full" in sys.argv
    calls = run(full=full)
    print(f"specialization harness: PASS ({calls:,} calls)")
