from __future__ import annotations

import statistics
import timeit

from python_extensions import inline_calls, inline_function


def best_ns(stmt: str, ns: dict[str, object], *, number: int = 500_000, repeat: int = 7) -> float:
    samples = timeit.repeat(stmt, globals=ns, number=number, repeat=repeat)
    return min(samples) * 1e9 / number


@inline_function(register_only=True)
def ephemeral(x):
    a = x
    b = a + 1
    c = b * 2
    return c


def raw_ephemeral(x):
    return ephemeral(x)


@inline_calls(policy="always", shared_regions=False)
def opt_ephemeral(x):
    return ephemeral(x)


@inline_function(register_only=True)
def duplicate(x):
    value = x + 3
    return value * value


def raw_duplicate(x):
    return duplicate(x)


@inline_calls(policy="always", shared_regions=False)
def opt_duplicate(x):
    return duplicate(x)


@inline_function(register_only=True)
def copy_value(x):
    alias = x
    return alias * alias + alias


def raw_copy(x):
    return copy_value(x)


@inline_calls(policy="always", shared_regions=False)
def opt_copy(x):
    return copy_value(x)


@inline_function(register_only=True)
def constant_local(x):
    enabled = True
    if enabled:
        return x + 1
    return x - 1


def raw_constant(x):
    return constant_local(x)


@inline_calls(policy="always", shared_regions=False)
def opt_constant(x):
    return constant_local(x)


@inline_function(register_only=True)
def slot_a(x):
    a = x + 1
    return a * a + a


@inline_function(register_only=True)
def slot_b(x):
    b = x * 2
    return b * b + b


def raw_slots(x):
    return slot_b(slot_a(x))


@inline_calls(policy="always", shared_regions=False)
def opt_slots(x):
    return slot_b(slot_a(x))


SCENARIOS = [
    ("ephemeral-chain", raw_ephemeral, opt_ephemeral, 17),
    ("duplicate-temp", raw_duplicate, opt_duplicate, 17),
    ("copy-propagation", raw_copy, opt_copy, 17),
    ("constant-local", raw_constant, opt_constant, 17),
    ("cross-callee-slots", raw_slots, opt_slots, 7),
]


def main() -> None:
    print("scenario                 raw ns    optimized ns   speedup   bytes  nlocals  stats")
    for name, raw, opt, value in SCENARIOS:
        ns = {"raw": raw, "opt": opt, "x": value}
        raw_ns = best_ns("raw(x)", ns)
        opt_ns = best_ns("opt(x)", ns)
        stats = opt.__inline_stats__
        summary = (
            f"rt={getattr(stats, 'synthetic_roundtrips_elided', 0)} "
            f"cp={getattr(stats, 'synthetic_copies_propagated', 0)} "
            f"k={getattr(stats, 'synthetic_constants_propagated', 0)} "
            f"slots={getattr(stats, 'coalesced_local_slots', 0)}"
        )
        print(
            f"{name:22s} {raw_ns:9.2f} {opt_ns:14.2f} {raw_ns/opt_ns:8.3f}x "
            f"{len(opt.__code__.co_code):6d} {opt.__code__.co_nlocals:8d}  {summary}"
        )


if __name__ == "__main__":
    main()
