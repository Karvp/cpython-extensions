from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

CHILD = r'''
import dis, json, timeit
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def flag(): return True
@inline_function(register_only=True)
def choose(flag, value):
    if flag: return value + 7
    return value - 11
@inline_calls(region_dataflow=True)
def while_invariant(value, count):
    produced = flag()
    alias = produced
    total = 0
    while count > 0:
        total += choose(alias, value)
        count -= 1
    return total
@inline_calls(region_dataflow=True)
def for_invariant(value, values):
    produced = flag()
    alias = produced
    total = 0
    for _ in values:
        total += choose(alias, value)
    return total
@inline_calls(region_dataflow=True)
def changed(value, count):
    produced = flag()
    alias = produced
    total = 0
    while count > 0:
        total += choose(alias, value)
        alias = False
        count -= 1
    return total
VALUES = tuple(range(8))
def ns(stmt, number):
    return timeit.timeit(stmt, globals=globals(), number=number) * 1e9 / number
out = {}
for name, stmt in [
    ('while_invariant', 'while_invariant(13, 8)'),
    ('for_invariant', 'for_invariant(13, VALUES)'),
    ('changed_control', 'changed(13, 8)'),
]:
    fn = globals()[name if name != 'changed_control' else 'changed']
    out[name] = {
        'ns': ns(stmt, NUMBER),
        'code': len(fn.__code__.co_code),
        'locals': len(fn.__code__.co_varnames),
        'loop_headers': getattr(fn.__inline_stats__, 'cfg_loop_headers', 0),
        'loop_invariant_facts': getattr(fn.__inline_stats__, 'cfg_loop_invariant_facts', 0),
        'loop_variant_kills': getattr(fn.__inline_stats__, 'cfg_loop_variant_kills', 0),
    }
print(json.dumps(out))
'''


def run(source: Path, number: int) -> dict[str, dict[str, float | int]]:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(source / 'src')
    program = CHILD.replace('NUMBER', str(number))
    proc = subprocess.run(
        [sys.executable, '-c', program],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--baseline-src', type=Path, required=True)
    ap.add_argument('--current-src', type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument('--processes', type=int, default=5)
    ap.add_argument('--number', type=int, default=300_000)
    args = ap.parse_args()

    samples = {'baseline': [], 'current': []}
    for _ in range(args.processes):
        samples['baseline'].append(run(args.baseline_src, args.number))
        samples['current'].append(run(args.current_src, args.number))

    print(f'Python {sys.version.split()[0]} — median of {args.processes} isolated processes')
    print('scenario              v0.12 ns   current ns  speedup   code 0.12->current  locals')
    for scenario in ('while_invariant', 'for_invariant', 'changed_control'):
        b = statistics.median(sample[scenario]['ns'] for sample in samples['baseline'])
        c = statistics.median(sample[scenario]['ns'] for sample in samples['current'])
        bmeta = samples['baseline'][0][scenario]
        cmeta = samples['current'][0][scenario]
        print(
            f'{scenario:20s} {b:9.2f} {c:11.2f} {b/c:8.3f}x  '
            f"{bmeta['code']:4d}->{cmeta['code']:<4d}       "
            f"{bmeta['locals']}->{cmeta['locals']}"
        )
    print('\ncurrent loop diagnostics:')
    for scenario in ('while_invariant', 'for_invariant', 'changed_control'):
        meta = samples['current'][0][scenario]
        print(
            scenario,
            'headers=', meta['loop_headers'],
            'invariants=', meta['loop_invariant_facts'],
            'variant_kills=', meta['loop_variant_kills'],
        )


if __name__ == '__main__':
    main()
