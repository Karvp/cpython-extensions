from __future__ import annotations

import platform
import sys
import timeit

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def branch_default(x, flag=True):
    if flag:
        return x + 1
    return x - 1


def raw_branch(x):
    return branch_default(x)


OPT_BRANCH = inline_calls(policy="always", shared_regions=False)(raw_branch)


@inline_function(register_only=True)
def repeated_read(a, b):
    return a * a + b * b


def raw_repeated_read(x, y):
    return repeated_read(x, y)


OPT_REPEATED_READ = inline_calls(policy="always", shared_regions=False)(raw_repeated_read)


@inline_function(register_only=True)
def local_helper(x):
    y = x + 1
    z = y * 2
    return z - 3


def raw_local_chain(x):
    a = local_helper(x)
    b = local_helper(a)
    c = local_helper(b)
    return local_helper(c)


OPT_LOCAL_CHAIN = inline_calls(policy="always", shared_regions=False)(raw_local_chain)


@inline_function(register_only=True)
def mode_helper(x, mode="fast"):
    if mode == "fast":
        return x + 1
    return x - 1


def raw_mode(x):
    return mode_helper(x)


OPT_MODE = inline_calls(policy="always", shared_regions=False)(raw_mode)


@inline_function(register_only=True)
def folded_math(x, scale=3, offset=4):
    return x + scale * 2 + offset - 1


def raw_math(x):
    return folded_math(x)


OPT_MATH = inline_calls(policy="always", shared_regions=False)(raw_math)


def measure(fn, args, number=1_000_000, repeat=7):
    for _ in range(20_000):
        fn(*args)
    return min(timeit.repeat(lambda: fn(*args), number=number, repeat=repeat)) / number * 1e9


def main() -> None:
    print(sys.version)
    print(platform.platform())
    print()
    scenarios = [
        ("default boolean branch", raw_branch, OPT_BRANCH, (17,), lambda x: x + 1),
        ("repeated read", raw_repeated_read, OPT_REPEATED_READ, (7, 9), lambda x, y: x*x + y*y),
        ("four local-heavy calls", raw_local_chain, OPT_LOCAL_CHAIN, (5,), None),
        ("default string mode", raw_mode, OPT_MODE, (17,), lambda x: x + 1),
        ("constant arithmetic", raw_math, OPT_MATH, (17,), lambda x: x + 9),
    ]
    print(f"{'scenario':26s} {'raw ns':>10s} {'opt ns':>10s} {'speedup':>9s} {'bytes':>7s} {'locals':>7s} {'direct ns':>10s}")
    for name, raw, opt, args, direct in scenarios:
        raw_ns = measure(raw, args)
        opt_ns = measure(opt, args)
        direct_ns = measure(direct, args) if direct is not None else float('nan')
        print(
            f"{name:26s} {raw_ns:10.2f} {opt_ns:10.2f} {raw_ns/opt_ns:8.3f}x "
            f"{len(opt.__code__.co_code):7d} {opt.__code__.co_nlocals:7d} {direct_ns:10.2f}"
        )
        print(f"  stats={opt.__inline_stats__}")


if __name__ == "__main__":
    main()
