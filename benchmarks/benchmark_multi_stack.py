from __future__ import annotations

import argparse
import statistics
import timeit

import python_extensions.inline as inline_mod
from python_extensions import clear_inline_registry, inline_calls, inline_function


def _build(count: int, *, stack_resident: bool):
    clear_inline_registry()
    lines = ["def helper(x):"]
    for i in range(count):
        # One line intentionally encourages CPython 3.13 STORE_FAST_LOAD_FAST
        # fusion between adjacent assignments in larger fixtures.
        lines.append(f"    t{i} = x + {i + 1}")
    weighted = " + ".join(f"t{i} * {i + 2}" for i in range(count))
    reverse_tail = " + ".join(f"t{i}" for i in reversed(range(count)))
    lines.append(f"    return {weighted} + {reverse_tail}")
    lines += ["", "def caller(x):", "    return helper(x)"]
    ns: dict[str, object] = {}
    exec("\n".join(lines), ns)
    ns["helper"] = inline_function(register_only=True)(ns["helper"])

    original = inline_mod._schedule_stack_resident_synthetic_values
    if not stack_resident:
        inline_mod._schedule_stack_resident_synthetic_values = lambda items, names: (list(items), 0)
    try:
        caller = inline_calls(policy="always", shared_regions=False)(ns["caller"])
    finally:
        inline_mod._schedule_stack_resident_synthetic_values = original
    return caller


def _measure(function, number: int, repeat: int) -> float:
    for _ in range(50_000):
        function(-17)
    samples = [
        sample * 1e9 / number
        for sample in timeit.repeat(lambda: function(-17), number=number, repeat=repeat)
    ]
    return statistics.median(samples)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--number", type=int, default=300_000)
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    print("nested retained temporaries: fast-local spill vs multi-stack")
    print("count    local ns    stack ns   speedup   local/stack locals   local/stack bytes")
    for count in (2, 3, 4, 6, 8, 10, 12):
        local = _build(count, stack_resident=False)
        stack = _build(count, stack_resident=True)
        assert local(-17) == stack(-17)
        local_ns = _measure(local, args.number, args.repeat)
        stack_ns = _measure(stack, args.number, args.repeat)
        print(
            f"{count:5d}  {local_ns:10.2f}  {stack_ns:10.2f}  {local_ns / stack_ns:8.3f}x"
            f"   {local.__code__.co_nlocals:2d}/{stack.__code__.co_nlocals:<2d}"
            f"             {len(local.__code__.co_code):3d}/{len(stack.__code__.co_code):<3d}"
        )


if __name__ == "__main__":
    main()
