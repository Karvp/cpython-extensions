"""Cross-extension production stress harness for the 0.19 refinement line."""
from __future__ import annotations

import argparse
import concurrent.futures
import dis
import random

from python_extensions import (
    case,
    enable_goto,
    enable_switch,
    inline_calls,
    inline_function,
    optimize_extensions,
    switch,
    verify_code,
)


@inline_function(register_only=True)
def _neg_default(value=7):
    return -value


@inline_function(register_only=True)
def _mixed_unary(value=-7, mask=5, truth=()):
    return -value + ~mask + (1 if not truth else 0)


@inline_function(register_only=True)
def _positive_default(value=-9):
    return +value


@inline_function(register_only=True)
def _inc(value):
    return value + 1


@inline_calls(policy="speed")
def _inline_neg():
    return _neg_default()


@inline_calls(policy="speed")
def _inline_mixed():
    return _mixed_unary()


@inline_calls(policy="speed")
def _inline_positive():
    return _positive_default()


@enable_switch(mode="portable")
def _binary_switch(value, allow):
    with switch(value):
        if case(1, 2, 3, 4, 5, 6, 7, 8, when=allow):
            return value * 3 + 1
        if case():
            return -value if type(value) is int else -1


@enable_switch(mode="portable")
def _exception_switch(value):
    with switch(value):
        if case(1):
            try:
                return 11
            finally:
                pass
        if case():
            try:
                return 22
            finally:
                pass


@enable_goto
def _goto_countdown(value):
    total = 0
    label .again
    total += value
    value -= 1
    if value > 0:
        goto .again
    return total


@enable_goto
def _goto_forward(flag):
    value = 1
    if flag:
        goto .done
    value += 10
    label .done
    return value


class _DescriptorPipeline:
    bias = 5

    @optimize_extensions(switch=True, inline=True, goto=True)
    @staticmethod
    def static(value, rounds):
        total = 0
        label .again
        total = _inc(total)
        with switch(value):
            if case(1):
                bonus = 10
            if case():
                bonus = 20
        if total < rounds:
            goto .again
        return total + bonus

    @optimize_extensions(switch=True, inline=True, goto=True)
    @classmethod
    def klass(cls, value, rounds):
        total = 0
        label .again
        total = _inc(total)
        with switch(value):
            if case(1):
                bonus = cls.bias
            if case():
                bonus = 20
        if total < rounds:
            goto .again
        return total + bonus


def _build_long_gotos():
    body = "\n".join(f"    x += {i % 7}" for i in range(500))
    src = (
        "def forward(flag):\n"
        "    x = 0\n"
        "    if flag:\n"
        "        goto .done\n"
        + body
        + "\n    label .done\n    return x\n"
    )
    ns = {}
    exec(src, ns)
    forward = enable_goto(ns["forward"])

    src = (
        "def backward(n):\n"
        "    x = 0\n"
        "    label .again\n"
        + body
        + "\n    n -= 1\n    if n > 0:\n        goto .again\n    return x\n"
    )
    ns = {}
    exec(src, ns)
    backward = enable_goto(ns["backward"])
    return forward, backward, sum(i % 7 for i in range(500))


def run(profile: str) -> int:
    if profile == "full":
        switch_calls = 1_250_000
        inline_calls_n = 1_250_000
        goto_calls = 800_000
        descriptor_calls = 450_000
        thread_calls = 400_000
        long_calls = 25_000
    else:
        switch_calls = 60_000
        inline_calls_n = 60_000
        goto_calls = 40_000
        descriptor_calls = 20_000
        thread_calls = 20_000
        long_calls = 1_000

    calls = 0
    assert _binary_switch.__pyswitch_binary_route_plan_count__ == 1
    assert verify_code(_binary_switch.__code__).valid
    values = list(range(-3, 13))
    for i in range(switch_calls):
        value = values[(i * 17 + 3) % len(values)]
        allow = bool(i & 1)
        expected = value * 3 + 1 if allow and 1 <= value <= 8 else -value
        assert _binary_switch(value, allow) == expected
    calls += switch_calls

    for i in range(max(1, switch_calls // 8)):
        assert _exception_switch(1 if i & 1 else 9) == (11 if i & 1 else 22)
    calls += max(1, switch_calls // 8)
    assert verify_code(_exception_switch.__code__).valid

    assert _inline_neg.__inline_stats__.constant_unary_ops_folded >= 1
    assert _inline_mixed.__inline_stats__.constant_unary_ops_folded >= 2
    assert _inline_positive.__inline_stats__.constant_unary_ops_folded >= 1
    for _ in range(inline_calls_n):
        assert _inline_neg() == -7
        assert _inline_mixed() == 2  # 7 + -6 + 1
        assert _inline_positive() == -9
    calls += inline_calls_n * 3

    for i in range(goto_calls):
        n = i % 9 + 1
        assert _goto_countdown(n) == n * (n + 1) // 2
        assert _goto_forward(bool(i & 1)) == (1 if i & 1 else 11)
    calls += goto_calls * 2

    long_forward, long_backward, one_pass = _build_long_gotos()
    assert any(i.opname == "EXTENDED_ARG" for i in dis.get_instructions(long_forward, adaptive=False))
    assert any(i.opname == "EXTENDED_ARG" for i in dis.get_instructions(long_backward, adaptive=False))
    for i in range(long_calls):
        assert long_forward(True) == 0
        if not (i & 7):
            assert long_forward(False) == one_pass
            assert long_backward(2) == one_pass * 2
            calls += 2
        calls += 1

    for i in range(descriptor_calls):
        rounds = i % 5 + 1
        value = 1 if i & 1 else 9
        assert _DescriptorPipeline.static(value, rounds) == rounds + (10 if value == 1 else 20)
        assert _DescriptorPipeline.klass(value, rounds) == rounds + (_DescriptorPipeline.bias if value == 1 else 20)
    calls += descriptor_calls * 2

    # Shared immutable transformed functions must remain safe under concurrent calls.
    def worker(seed: int, count: int) -> int:
        rng = random.Random(seed)
        local = 0
        for _ in range(count):
            value = rng.randrange(1, 10)
            allow = bool(rng.getrandbits(1))
            expected = value * 3 + 1 if allow and value <= 8 else -value
            assert _binary_switch(value, allow) == expected
            n = rng.randrange(1, 8)
            assert _goto_countdown(n) == n * (n + 1) // 2
            assert _inline_neg() == -7
            assert _inline_positive() == -9
            local += 4
        return local

    workers = 8
    each = thread_calls // workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        threaded = sum(pool.map(lambda seed: worker(seed, each), range(workers)))
    calls += threaded

    return calls


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    total = run(args.profile)
    print(f"all-extensions-v19 {args.profile}: {total:,} calls passed")
