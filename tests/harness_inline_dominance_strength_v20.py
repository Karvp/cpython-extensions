from __future__ import annotations

import argparse
import itertools
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls

_COUNTER = itertools.count()


def build_branch(start: int, step: int, scale: int, offset: int, *, post_update: bool = False):
    name = f"dom_sr_{next(_COUNTER)}"
    update = f"i += ({step})" if step >= 0 else f"i -= ({-step})"
    expression = f"i * ({scale}) + ({offset})"
    if post_update:
        body = f'''        {update}
        if choose:
            total += {expression}
        else:
            total += {expression}
'''
    else:
        body = f'''        if choose:
            total += {expression}
        else:
            total += {expression}
        {update}
'''
    source = f'''def {name}(count, choose):
    i = {start}
    total = 0
    while count > 0:
{body}        count -= 1
    return total
'''
    namespace: dict[str, object] = {}
    exec(source, namespace)
    return inline_calls(region_dataflow=True, policy="speed")(namespace[name])


def expected_branch(start: int, step: int, scale: int, offset: int, count: int, *, post_update: bool) -> int:
    i = start
    total = 0
    for _ in range(count):
        if post_update:
            i += step
        total += i * scale + offset
        if not post_update:
            i += step
    return total


def generated_branch_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xD016A7E)
    calls = 0
    for _ in range(functions):
        start = rng.randrange(-30, 31)
        step = rng.choice((-7, -3, -1, 1, 2, 4, 9))
        scale = rng.choice((-13, -5, -2, 2, 7, 11))
        offset = rng.randrange(-20, 21)
        post_update = bool(rng.getrandbits(1))
        fn = build_branch(start, step, scale, offset, post_update=post_update)
        for _call in range(calls_per_function):
            count = rng.randrange(0, 30)
            choose = bool(rng.getrandbits(1))
            expected = expected_branch(start, step, scale, offset, count, post_update=post_update)
            actual = fn(count, choose)
            if actual != expected:
                raise AssertionError((start, step, scale, offset, post_update, count, choose, expected, actual))
            calls += 1
        if fn.__inline_stats__.cfg_strength_reduced_values < 1:
            raise AssertionError("branch-distributed strength reduction did not activate")
    print(f"generated dominance strength differential: {calls:,} calls passed")


def generated_early_exit_controls(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0xEA71E)
    calls = 0
    for index in range(functions):
        name = f"dom_control_{index}_{next(_COUNTER)}"
        source = f'''def {name}(count, use_affine):
    i = 0
    total = 0
    while count > 0:
        if use_affine:
            total += i * 7 + 3
        else:
            total += 1
        i += 2
        count -= 1
    return total
'''
        namespace: dict[str, object] = {}
        exec(source, namespace)
        fn = inline_calls(region_dataflow=True, policy="speed")(namespace[name])
        for _call in range(calls_per_function):
            count = rng.randrange(0, 25)
            use_affine = bool(rng.getrandbits(1))
            i = 0
            expected = 0
            for _ in range(count):
                expected += i * 7 + 3 if use_affine else 1
                i += 2
            actual = fn(count, use_affine)
            if actual != expected:
                raise AssertionError((count, use_affine, expected, actual))
            calls += 1
        if fn.__inline_stats__.cfg_strength_reduced_values != 0:
            raise AssertionError("speed policy reduced an update path with zero affine savings")
    print(f"generated early-exit controls: {calls:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    fn = build_branch(3, 4, 9, -7, post_update=True)
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            count = rng.randrange(0, 35)
            choose = bool(rng.getrandbits(1))
            expected = expected_branch(3, 4, 9, -7, count, post_update=True)
            actual = fn(count, choose)
            if actual != expected:
                failures.append((count, choose, expected, actual))
                return

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded dominance strength: {threads * rounds:,} calls passed")


def crash_isolated(rounds: int) -> None:
    program = r'''
from python_extensions import inline_calls
@inline_calls(region_dataflow=True, policy="speed")
def run(count, choose):
    i = 1
    total = 0
    while count > 0:
        if choose:
            total += i * 11 - 4
        else:
            total += i * 11 - 4
        i += 3
        count -= 1
    return total
rounds = ROUND_COUNT
checksum = 0
for index in range(rounds):
    count = index % 19
    choose = bool(index & 1)
    actual = run(count, choose)
    i = 1
    expected = 0
    for _ in range(count):
        expected += i * 11 - 4
        i += 3
    if actual != expected:
        raise AssertionError((index, actual, expected))
    checksum ^= actual
if run.__inline_stats__.cfg_strength_reduced_values < 1:
    raise AssertionError("dominance strength reduction did not activate")
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
    print(f"crash-isolated dominance strength: {rounds:,} calls passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        generated_branch_differential(30, 200)
        generated_early_exit_controls(20, 150)
        threaded_stress(4, 5_000)
        crash_isolated(100_000)
    else:
        generated_branch_differential(300, 1_000)
        generated_early_exit_controls(200, 1_000)
        threaded_stress(8, 100_000)
        crash_isolated(2_000_000)


if __name__ == "__main__":
    main()
