from __future__ import annotations

import timeit

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True, shared_region=True)
def heavy(value):
    value = value * 3 + 1
    value = value * 5 - 2
    value = value * 7 + 3
    value = value * 11 - 4
    value = value * 13 + 5
    value = value * 17 - 6
    value = value * 19 + 7
    value = value * 23 - 8
    return value


def raw(value):
    a = heavy(value)
    b = heavy(a)
    c = heavy(b)
    d = heavy(c)
    e = heavy(d)
    f = heavy(e)
    g = heavy(f)
    h = heavy(g)
    return h


DUPLICATED = inline_calls(shared_regions=False, policy="always")(raw)
SHARED = inline_calls(
    shared_regions=True,
    shared_min_body_instructions=1,
    policy="always",
)(raw)


def measure(function, number=100_000):
    for _ in range(10_000):
        function(2)
    return min(timeit.repeat(lambda: function(2), number=number, repeat=5)) / number * 1e9


if __name__ == "__main__":
    expected = raw(2)
    assert DUPLICATED(2) == expected == SHARED(2)
    print("CPython shared-inline benchmark")
    for name, function in [("normal", raw), ("duplicated", DUPLICATED), ("shared", SHARED)]:
        print(f"{name:10s} {measure(function):9.2f} ns  code={len(function.__code__.co_code):4d} bytes")
    print("duplicated stats:", DUPLICATED.__inline_stats__)
    print("shared stats:    ", SHARED.__inline_stats__)
