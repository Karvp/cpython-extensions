"""Production stress harness for the CPython 3.13 ``pygoto`` lowering.

The harness deliberately exercises semantic shapes rather than benchmark-only
fixtures: multi-label state machines, forward/backward jumps, generator resume,
try/finally cleanup, descriptor decoration, EXTENDED_ARG distances, and
concurrent calls to the same transformed functions.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import dis

from python_extensions import enable_goto
from python_extensions._core import verify_code


@enable_goto
def _machine(value: int) -> int:
    total = 0
    label .again
    if value <= 0:
        goto .done
    if value % 3 == 0:
        goto .three
    if value % 2 == 0:
        goto .even
    total += value
    goto .next
    label .three
    total += value * 3
    goto .next
    label .even
    total += value * 2
    label .next
    value -= 1
    goto .again
    label .done
    return total


def _machine_ref(value: int) -> int:
    total = 0
    while value > 0:
        if value % 3 == 0:
            total += value * 3
        elif value % 2 == 0:
            total += value * 2
        else:
            total += value
        value -= 1
    return total


@enable_goto
def _forward(flag: bool, value: int) -> int:
    result = value
    if flag:
        goto .done
    result = value * 7 + 3
    label .done
    return result


@enable_goto
def _generator(limit: int):
    value = 0
    label .again
    if value >= limit:
        goto .done
    incoming = yield value
    if incoming is not None:
        value = incoming
    else:
        value += 1
    goto .again
    label .done
    return value


@enable_goto
def _finally_flow(take: bool, out: list[str]) -> int:
    value = 10
    try:
        if take:
            goto .done
        value = 20
        out.append("body")
        label .done
    finally:
        out.append("finally")
    return value


@enable_goto
async def _async_forward(skip: bool, value: int) -> int:
    if skip:
        goto .done
    await asyncio.sleep(0)
    value = value * 3 + 1
    label .done
    return value


@enable_goto
async def _async_loop(value: int) -> int:
    total = 0
    label .again
    if value <= 0:
        goto .done
    await asyncio.sleep(0)
    total += value
    value -= 1
    goto .again
    label .done
    return total


@enable_goto
async def _async_finally_loop(value: int, out: list[str]) -> int:
    total = 0
    try:
        label .again
        if value <= 0:
            goto .done
        await asyncio.sleep(0)
        total += value
        value -= 1
        goto .again
        label .done
    finally:
        out.append("finally")
    return total


@enable_goto
async def _async_generator(limit: int):
    value = 0
    label .again
    if value >= limit:
        goto .done
    yield value
    value += 1
    goto .again
    label .done


async def _check_async_batch(iterations: int) -> int:
    calls = 0
    for i in range(iterations):
        value = i % 17
        skip = bool(i & 1)
        expected = value if skip else value * 3 + 1
        assert await _async_forward(skip, value) == expected
        calls += 1

        loop_value = i % 11
        assert await _async_loop(loop_value) == loop_value * (loop_value + 1) // 2
        calls += 1

        out: list[str] = []
        final_value = i % 8
        assert await _async_finally_loop(final_value, out) == final_value * (final_value + 1) // 2
        assert out == ["finally"]
        calls += 1

        limit = i % 7
        seen = [item async for item in _async_generator(limit)]
        assert seen == list(range(limit))
        calls += len(seen)
    return calls


class _Methods:
    @enable_goto
    @staticmethod
    def static(value: int) -> int:
        result = value + 1
        if value < 0:
            goto .negative
        goto .done
        label .negative
        result = -value
        label .done
        return result

    @enable_goto
    @classmethod
    def classy(cls, value: int) -> tuple[str, int]:
        result = 0
        if value:
            goto .nonzero
        goto .done
        label .nonzero
        result = value + 2
        label .done
        return cls.__name__, result


def _make_long_distance() -> tuple[object, object]:
    padding = "\n".join(f"    x = x + {i & 1}" for i in range(900))
    namespace = {"enable_goto": enable_goto}
    source = f'''\n@enable_goto\ndef long_forward(x):\n    goto .done\n{padding}\n    label .done\n    return x\n\n@enable_goto\ndef long_backward(n):\n    total = 0\n    x = 0\n    label .top\n{padding}\n    n -= 1\n    if n > 0:\n        goto .top\n    return total + n\n'''
    exec(compile(source, "<goto-v19-long>", "exec"), namespace)
    return namespace["long_forward"], namespace["long_backward"]


_LONG_FORWARD, _LONG_BACKWARD = _make_long_distance()


def _check_static_semantics() -> None:
    for value in range(-8, 12):
        expected = abs(value) if value < 0 else value + 1
        assert _Methods.static(value) == expected
        expected_class = ("_Methods", 0 if value == 0 else value + 2)
        assert _Methods.classy(value) == expected_class


def _check_generator(limit: int) -> int:
    gen = _generator(limit)
    seen: list[int] = []
    try:
        seen.append(next(gen))
        while True:
            seen.append(gen.send(None))
    except StopIteration as stop:
        assert stop.value == limit
    assert seen == list(range(limit))
    return len(seen)


def _verify_shapes() -> None:
    for func in (_machine, _forward, _generator, _finally_flow, _async_forward,
                 _async_loop, _async_finally_loop, _async_generator, _Methods.static,
                 _Methods.classy, _LONG_FORWARD, _LONG_BACKWARD):
        verify_code(func.__code__)
        names = {ins.argval for ins in dis.get_instructions(func)}
        assert "goto" not in names and "label" not in names

    # The generated distance is intentionally large enough to require a real
    # EXTENDED_ARG in at least one patched goto.
    long_ops = list(dis.get_instructions(_LONG_FORWARD, show_caches=True))
    assert any(ins.opname == "EXTENDED_ARG" for ins in long_ops)


def run(profile: str) -> int:
    if profile == "quick":
        machine_calls = 50_000
        forward_calls = 50_000
        generator_runs = 2_000
        finally_calls = 20_000
        method_calls = 20_000
        threaded_per_worker = 15_000
        long_calls = 250
        async_iterations = 2_000
    else:
        machine_calls = 650_000
        forward_calls = 650_000
        generator_runs = 25_000
        finally_calls = 300_000
        method_calls = 300_000
        threaded_per_worker = 125_000
        long_calls = 2_000
        async_iterations = 35_000

    _verify_shapes()
    _check_static_semantics()
    calls = 0

    for i in range(machine_calls):
        value = i % 31
        assert _machine(value) == _machine_ref(value)
    calls += machine_calls

    for i in range(forward_calls):
        flag = bool(i & 1)
        value = (i % 101) - 50
        expected = value if flag else value * 7 + 3
        assert _forward(flag, value) == expected
    calls += forward_calls

    yielded = 0
    for i in range(generator_runs):
        limit = 1 + i % 8
        yielded += _check_generator(limit)
    calls += yielded

    for i in range(finally_calls):
        out: list[str] = []
        take = bool(i & 1)
        result = _finally_flow(take, out)
        assert result == (10 if take else 20)
        assert out == (["finally"] if take else ["body", "finally"])
    calls += finally_calls

    for i in range(method_calls):
        value = (i % 41) - 20
        assert _Methods.static(value) == (abs(value) if value < 0 else value + 1)
        assert _Methods.classy(value) == ("_Methods", 0 if value == 0 else value + 2)
    calls += method_calls * 2

    # Long-distance functions are correctness/encoding stress, not a workload
    # benchmark.  Keep iteration count bounded while still repeatedly executing
    # the EXTENDED_ARG path in both directions.
    for i in range(long_calls):
        value = i % 17
        assert _LONG_FORWARD(value) == value
        assert _LONG_BACKWARD(1) == 0
    calls += long_calls * 2

    calls += asyncio.run(_check_async_batch(async_iterations))

    def worker(seed: int) -> int:
        subtotal = 0
        for i in range(threaded_per_worker):
            value = (i + seed) % 23
            got = _machine(value)
            assert got == _machine_ref(value)
            subtotal += 1
        return subtotal

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        threaded = sum(executor.map(worker, range(8)))
    assert threaded == threaded_per_worker * 8
    calls += threaded

    print(f"pygoto v19 {profile}: {calls:,} calls/yields passed")
    return calls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    args = parser.parse_args()
    run(args.profile)


if __name__ == "__main__":
    main()
