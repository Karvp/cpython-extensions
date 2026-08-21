from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import textwrap
from pathlib import Path


CHILD = textwrap.dedent(r'''
    import statistics
    import timeit
    from python_extensions import inline_calls, inline_function

    @inline_function(register_only=True)
    def flag(): return True

    @inline_function(register_only=True)
    def choose(flag, value):
        if flag: return value + 1
        return value - 1

    @inline_function(register_only=True)
    def two(): return 2

    @inline_function(register_only=True)
    def scale(value): return value * 3 + 1

    @inline_function(register_only=True)
    def identity(value): return value

    def decorate(function):
        try:
            return inline_calls(region_dataflow=True)(function)
        except TypeError:
            # v0.10 does not expose region_dataflow yet.
            return inline_calls()(function)

    @decorate
    def constant_chain(value):
        produced = flag()
        first = produced
        second = first
        return choose(second, value)

    @decorate
    def arithmetic_chain():
        produced = two()
        alias = produced
        return scale(alias)

    @decorate
    def dynamic_chain(value):
        produced = identity(value)
        first = produced
        second = first
        return scale(second)

    def median_ns(function, *args):
        values = timeit.repeat(
            lambda: function(*args),
            number=1_000_000,
            repeat=7,
        )
        return statistics.median(values) / 1_000_000 * 1e9

    for name, function, args in (
        ("constant-copy-chain", constant_chain, (123,)),
        ("constant-arithmetic-chain", arithmetic_chain, ()),
        ("dynamic-copy-chain", dynamic_chain, (123,)),
    ):
        print(
            name,
            f"{median_ns(function, *args):.3f}",
            len(function.__code__.co_code),
            function.__code__.co_nlocals,
        )
''')


def run(source: Path) -> dict[str, tuple[float, int, int]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source)
    process = subprocess.run(
        [sys.executable, "-c", CHILD],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    output: dict[str, tuple[float, int, int]] = {}
    for line in process.stdout.splitlines():
        name, ns, code, locals_count = line.split()
        output[name] = (float(ns), int(code), int(locals_count))
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-src", required=True, type=Path)
    parser.add_argument("--current-src", required=True, type=Path)
    args = parser.parse_args()

    baseline = run(args.baseline_src)
    current = run(args.current_src)
    print(f"Python: {sys.version.splitlines()[0]}")
    print("scenario                       v0.10 ns   v0.11 ns   speedup   code 0.10->0.11   locals")
    for name in baseline:
        old_ns, old_code, old_locals = baseline[name]
        new_ns, new_code, new_locals = current[name]
        print(
            f"{name:30} {old_ns:9.3f} {new_ns:10.3f} "
            f"{old_ns / new_ns:8.3f}x   {old_code:4}->{new_code:<4}       "
            f"{old_locals}->{new_locals}"
        )


if __name__ == "__main__":
    main()
