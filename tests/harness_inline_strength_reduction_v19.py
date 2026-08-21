from __future__ import annotations

import argparse
import itertools
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls

_COUNTER = itertools.count()


def build_affine(start: int, step: int, scale: int, offset: int, uses: int = 2):
    name = f"strength_affine_{next(_COUNTER)}"
    body = "\n".join(
        f"        total += i * ({scale}) + ({offset})" for _ in range(uses)
    )
    update = f"i += ({step})" if step >= 0 else f"i -= ({-step})"
    source = f'''def {name}(count):
    i = {start}
    total = 0
    while count > 0:
{body}
        {update}
        count -= 1
    return total
'''
    namespace: dict[str, object] = {}
    exec(source, namespace)
    raw = namespace[name]
    return inline_calls(region_dataflow=True)(raw)


def build_dynamic():
    name = f"strength_dynamic_{next(_COUNTER)}"
    source = f'''def {name}(count, scale):
    i = 0
    total = 0
    while count > 0:
        total += i * scale + 3
        total += i * scale + 3
        i += 2
        count -= 1
    return total
'''
    namespace: dict[str, object] = {}
    exec(source, namespace)
    return inline_calls(region_dataflow=True)(namespace[name])


def expected_affine(start: int, step: int, scale: int, offset: int, uses: int, count: int) -> int:
    i = start
    total = 0
    for _ in range(count):
        total += uses * (i * scale + offset)
        i += step
    return total


def generated_affine_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0x57A3E19)
    calls = 0
    for _ in range(functions):
        start = rng.randrange(-50, 51)
        step = rng.choice((-7, -4, -2, 1, 2, 3, 5, 8))
        scale = rng.choice((-11, -7, -3, 2, 5, 9, 13))
        offset = rng.randrange(-25, 26)
        uses = rng.choice((2, 3, 4))
        fn = build_affine(start, step, scale, offset, uses)
        for _call in range(calls_per_function):
            count = rng.randrange(0, 30)
            expected = expected_affine(start, step, scale, offset, uses, count)
            actual = fn(count)
            if actual != expected:
                raise AssertionError((start, step, scale, offset, uses, count, expected, actual))
            calls += 1
        if fn.__inline_stats__.cfg_strength_reduced_values < 1:
            raise AssertionError("profitable affine expression was not strength reduced")
    print(f"generated strength-reduction differential: {calls:,} calls passed")


def generated_dynamic_controls(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xD19A11C)
    calls = 0
    for _ in range(functions):
        fn = build_dynamic()
        for _call in range(calls_per_function):
            count = rng.randrange(0, 25)
            scale = rng.choice((-9, -4, -1, 1, 3, 8))
            expected = expected_affine(0, 2, scale, 3, 2, count)
            actual = fn(count, scale)
            if actual != expected:
                raise AssertionError((count, scale, expected, actual))
            calls += 1
        if fn.__inline_stats__.cfg_strength_reduced_values != 0:
            raise AssertionError("dynamic scale was incorrectly strength reduced")
    print(f"generated dynamic-scale control: {calls:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    fn = build_affine(3, 4, 7, -5, 3)
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            count = rng.randrange(0, 35)
            expected = expected_affine(3, 4, 7, -5, 3, count)
            actual = fn(count)
            if actual != expected:
                failures.append((count, expected, actual))
                return

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded strength reduction: {threads * rounds:,} calls passed")


def crash_isolated(rounds: int) -> None:
    program = r'''
from python_extensions import inline_calls
@inline_calls(region_dataflow=True)
def run(count):
    i = 1
    total = 0
    while count > 0:
        total += i * 9 + 4
        total += i * 9 + 4
        total += i * 9 + 4
        i += 3
        count -= 1
    return total
rounds = ROUND_COUNT
checksum = 0
for index in range(rounds):
    count = index % 19
    actual = run(count)
    i = 1
    expected = 0
    for _ in range(count):
        expected += 3 * (i * 9 + 4)
        i += 3
    if actual != expected:
        raise AssertionError((index, actual, expected))
    checksum ^= actual
if run.__inline_stats__.cfg_strength_reduced_values < 1:
    raise AssertionError("strength reduction did not activate")
print(checksum)
'''.replace("ROUND_COUNT", str(rounds))
    completed = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", program],
        text=True,
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    print(f"crash-isolated strength reduction: {rounds:,} calls passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        generated_affine_differential(30, 200)
        generated_dynamic_controls(20, 150)
        threaded_stress(4, 5_000)
        crash_isolated(100_000)
    else:
        generated_affine_differential(300, 1_000)
        generated_dynamic_controls(200, 1_000)
        threaded_stress(8, 100_000)
        crash_isolated(2_000_000)


if __name__ == "__main__":
    main()
