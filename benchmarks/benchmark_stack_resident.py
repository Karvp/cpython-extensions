from __future__ import annotations

import argparse
import statistics
import timeit

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def right_binary(x):
    temp = x + 1
    return temp * 2 + temp


@inline_function(register_only=True)
def deep_binary(x):
    temp = x + 1
    return (temp + 2) * (temp + 3) + temp


@inline_function(register_only=True)
def across_call(x):
    temp = x + 1
    return abs(temp) + temp


@inline_calls(policy="always", shared_regions=False)
def optimized_right(x):
    return right_binary(x)


@inline_calls(policy="always", shared_regions=False)
def optimized_deep(x):
    return deep_binary(x)


@inline_calls(policy="always", shared_regions=False)
def optimized_call(x):
    return across_call(x)


def _measure(function, *, number: int, repeat: int) -> tuple[float, list[float]]:
    for _ in range(50_000):
        function(-17)
    samples = [sample * 1e9 / number for sample in timeit.repeat(
        lambda: function(-17), repeat=repeat, number=number
    )]
    return statistics.median(samples), samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--number", type=int, default=300_000)
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()

    for name, function in (
        ("right_binary", optimized_right),
        ("deep_binary", optimized_deep),
        ("across_call", optimized_call),
    ):
        median, samples = _measure(function, number=args.number, repeat=args.repeat)
        stats = function.__inline_stats__
        print(
            f"{name:14s} {median:8.2f} ns  "
            f"code={len(function.__code__.co_code):4d} B  "
            f"locals={function.__code__.co_nlocals:2d}  "
            f"stack_resident={stats.stack_resident_values}"
        )
        print("  samples:", ", ".join(f"{sample:.2f}" for sample in samples))


if __name__ == "__main__":
    main()
