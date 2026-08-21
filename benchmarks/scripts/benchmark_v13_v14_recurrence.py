from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

CHILD = r'''
import json, timeit
from python_extensions import inline_calls, inline_function

@inline_function(register_only=True)
def even(i, value):
    if i % 2 == 0:
        return value + 7
    return value - 11

@inline_function(register_only=True)
def low_bits(i, value):
    if (i & 3) == 3:
        return value + 5
    return value - 9

@inline_function(register_only=True)
def nonnegative(i, value):
    if i >= 0:
        return value + 1
    return value - 1

@inline_calls(region_dataflow=True)
def parity(value, count):
    i = 0
    total = 0
    while count > 0:
        total += even(i, value)
        i += 2
        count -= 1
    return total

@inline_calls(region_dataflow=True)
def masks(value, count):
    i = 3
    total = 0
    while count > 0:
        total += low_bits(i, value)
        i += 4
        count -= 1
    return total

@inline_calls(region_dataflow=True)
def monotonic(value, count):
    i = 0
    total = 0
    while count > 0:
        total += nonnegative(i, value)
        i += 3
        count -= 1
    return total

@inline_calls(region_dataflow=True)
def dynamic(value, count, step):
    i = 0
    total = 0
    while count > 0:
        total += even(i, value)
        i += step
        count -= 1
    return total

for fn, args in ((parity, (13, 12)), (masks, (13, 12)), (monotonic, (13, 12)), (dynamic, (13, 12, 1))):
    for _ in range(2000):
        fn(*args)

def ns(callable_, number):
    return timeit.timeit(callable_, number=number) * 1e9 / number

out = {}
for name, fn, args in (
    ('parity', parity, (13, 12)),
    ('low_bits', masks, (13, 12)),
    ('monotonic', monotonic, (13, 12)),
    ('dynamic_control', dynamic, (13, 12, 1)),
):
    out[name] = {
        'ns': ns(lambda fn=fn, args=args: fn(*args), NUMBER),
        'code': len(fn.__code__.co_code),
        'locals': len(fn.__code__.co_varnames),
        'recurrences': getattr(fn.__inline_stats__, 'cfg_affine_recurrences', 0),
        'recurrence_folds': getattr(fn.__inline_stats__, 'cfg_recurrence_folds', 0),
    }
print(json.dumps(out))
'''


def run(source: Path, number: int) -> dict[str, dict[str, float | int]]:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(source / 'src')
    proc = subprocess.run(
        [sys.executable, '-c', CHILD.replace('NUMBER', str(number))],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-src', type=Path, required=True)
    parser.add_argument('--current-src', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--processes', type=int, default=5)
    parser.add_argument('--number', type=int, default=200_000)
    args = parser.parse_args()

    samples = {'baseline': [], 'current': []}
    for _ in range(args.processes):
        samples['baseline'].append(run(args.baseline_src, args.number))
        samples['current'].append(run(args.current_src, args.number))

    scenarios = ('parity', 'low_bits', 'monotonic', 'dynamic_control')
    print(f'Python {sys.version.split()[0]} — median of {args.processes} isolated processes')
    print('scenario             v0.13 ns   current ns  speedup   code 0.13->current  locals')
    for scenario in scenarios:
        baseline_ns = statistics.median(sample[scenario]['ns'] for sample in samples['baseline'])
        current_ns = statistics.median(sample[scenario]['ns'] for sample in samples['current'])
        baseline_meta = samples['baseline'][0][scenario]
        current_meta = samples['current'][0][scenario]
        print(
            f'{scenario:20s} {baseline_ns:9.2f} {current_ns:11.2f} {baseline_ns/current_ns:8.3f}x  '
            f"{baseline_meta['code']:4d}->{current_meta['code']:<4d}       "
            f"{baseline_meta['locals']}->{current_meta['locals']}"
        )

    print('\ncurrent recurrence diagnostics:')
    for scenario in scenarios:
        meta = samples['current'][0][scenario]
        print(
            scenario,
            'recurrences=', meta['recurrences'],
            'folds=', meta['recurrence_folds'],
        )


if __name__ == '__main__':
    main()
