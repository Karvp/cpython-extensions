from __future__ import annotations

import argparse
import random
import subprocess
import sys
import threading

from python_extensions import inline_calls, inline_function


@inline_function(register_only=True)
def _lazy_harness_affine(i):
    return i * 7 + 3


def build_changing_branch(start: int, step: int, *, post_update: bool = False):
    if post_update:
        update = f"        i += ({step})\n"
        branch = """        if i % 5 < 2:
            total += _lazy_harness_affine(i)
            total += _lazy_harness_affine(i)
        else:
            total += 1
"""
    else:
        update = ""
        branch = """        if i % 5 < 2:
            total += _lazy_harness_affine(i)
            total += _lazy_harness_affine(i)
        else:
            total += 1
"""
    tail_update = "" if post_update else f"        i += ({step})\n"
    source = f'''def generated(count):
    i = {start}
    total = 0
    while count > 0:
{update}{branch}{tail_update}        count -= 1
    return total
'''
    exec(source, globals())
    return inline_calls(region_dataflow=True, policy="speed")(globals()["generated"])


def expected(start: int, step: int, count: int, *, post_update: bool) -> int:
    i = start
    total = 0
    for _ in range(count):
        if post_update:
            i += step
        if i % 5 < 2:
            total += 2 * (i * 7 + 3)
        else:
            total += 1
        if not post_update:
            i += step
    return total


def generated_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0x0171A2)
    calls = 0
    for _ in range(functions):
        start = rng.randrange(-30, 31)
        step = rng.choice((-4, -3, -2, -1, 1, 2, 3, 4))
        post_update = bool(rng.getrandbits(1))
        fn = build_changing_branch(start, step, post_update=post_update)
        stats = fn.__inline_stats__
        if stats.cfg_strength_lazy_materializations < 1:
            raise AssertionError(("lazy strength did not activate", start, step, post_update, stats))
        if stats.cfg_strength_reduction_updates != 0:
            raise AssertionError(("unexpected global derived update", stats))
        for _call in range(calls_per_function):
            count = rng.randrange(0, 40)
            actual = fn(count)
            wanted = expected(start, step, count, post_update=post_update)
            if actual != wanted:
                raise AssertionError((start, step, post_update, count, wanted, actual))
            calls += 1
    print(f"generated lazy-strength differential: {calls:,} calls passed")


def pre_post_differential(functions: int, calls_per_function: int) -> None:
    rng = random.Random(0x017B4A)
    calls = 0
    for index in range(functions):
        start = rng.randrange(-20, 21)
        step = rng.choice((-5, -2, -1, 1, 2, 5))
        source = f'''def generated_{index}(count, enable):
    i = {start}
    total = 0
    while count > 0:
        if enable:
            total += _lazy_harness_affine(i)
            total += _lazy_harness_affine(i)
        i += ({step})
        if enable:
            total += _lazy_harness_affine(i)
            total += _lazy_harness_affine(i)
        else:
            total += 1
        count -= 1
    return total
'''
        exec(source, globals())
        fn = inline_calls(region_dataflow=True, policy="speed")(globals()[f"generated_{index}"])
        if fn.__inline_stats__.cfg_strength_lazy_materializations < 2:
            raise AssertionError(("pre/post lazy split did not activate", fn.__inline_stats__))
        for _call in range(calls_per_function):
            count = rng.randrange(0, 30)
            enable = bool(rng.getrandbits(1))
            i = start
            wanted = 0
            for _ in range(count):
                if enable:
                    wanted += 2 * (i * 7 + 3)
                i += step
                if enable:
                    wanted += 2 * (i * 7 + 3)
                else:
                    wanted += 1
            actual = fn(count, enable)
            if actual != wanted:
                raise AssertionError((start, step, count, enable, wanted, actual))
            calls += 1
    print(f"generated pre/post lazy differential: {calls:,} calls passed")


def threaded_stress(threads: int, rounds: int) -> None:
    fn = build_changing_branch(3, 2, post_update=False)
    expected_table = [expected(3, 2, count, post_update=False) for count in range(50)]
    barrier = threading.Barrier(threads)
    failures: list[object] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(rounds):
            count = rng.randrange(0, 50)
            actual = fn(count)
            wanted = expected_table[count]
            if actual != wanted:
                failures.append((count, wanted, actual))
                return

    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise AssertionError(failures[0])
    print(f"threaded lazy strength: {threads * rounds:,} calls passed")


def crash_isolated(rounds: int) -> None:
    program = r'''
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def affine(i):
    return i * 7 + 3
@inline_calls(region_dataflow=True, policy="speed")
def run(count):
    i = -7
    total = 0
    while count > 0:
        if i % 5 < 2:
            total += affine(i)
            total += affine(i)
        else:
            total += 1
        i += 3
        count -= 1
    return total
if run.__inline_stats__.cfg_strength_lazy_materializations != 1:
    raise AssertionError(run.__inline_stats__)
expected_table = []
for count in range(31):
    i = -7
    wanted = 0
    for _ in range(count):
        if i % 5 < 2:
            wanted += 2 * (i * 7 + 3)
        else:
            wanted += 1
        i += 3
    expected_table.append(wanted)
checksum = 0
for index in range(ROUND_COUNT):
    count = index % 31
    wanted = expected_table[count]
    actual = run(count)
    if actual != wanted:
        raise AssertionError((index, wanted, actual))
    checksum ^= actual
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
    print(f"crash-isolated lazy strength: {rounds:,} calls passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        generated_differential(30, 200)
        pre_post_differential(20, 150)
        threaded_stress(4, 5_000)
        crash_isolated(100_000)
    else:
        generated_differential(300, 1_000)
        pre_post_differential(200, 1_000)
        threaded_stress(8, 100_000)
        crash_isolated(2_000_000)


if __name__ == "__main__":
    main()
