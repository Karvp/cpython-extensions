from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
from pathlib import Path

CASE_V07 = r'''
import timeit
from python_extensions import inline_function, inline_calls

@inline_function(register_only=True)
def helper(x):
    a = x + 1; b = x + 2; c = x + 3
    return a * 2 + b * 3 + c * 4 + a + c + b

@inline_calls(policy={policy!r}, stack_strategy={strategy!r}, shared_regions=False)
def caller(x):
    return helper(x)

for value in range(-20, 21):
    assert caller(value) == helper(value)
number = {number}
time_ns = min(timeit.repeat('caller(7)', globals=globals(), number=number, repeat=7)) / number * 1e9
stats = caller.__inline_stats__
print(time_ns, len(caller.__code__.co_code), caller.__code__.co_nlocals,
      stats.stack_resident_values,
      getattr(stats, 'stack_spilled_values', -1))
'''

CASE_V06 = CASE_V07.replace(', stack_strategy={strategy!r}', '')


def measure(root: Path, template: str, *, policy: str, strategy: str, number: int, processes: int):
    values = []
    layouts = []
    env = dict(os.environ)
    env['PYTHONPATH'] = str(root / 'src')
    program = template.format(policy=policy, strategy=strategy, number=number)
    for _ in range(processes):
        out = subprocess.check_output([sys.executable, '-c', program], env=env, text=True).strip().split()
        values.append(float(out[0]))
        layouts.append(tuple(map(int, out[1:])))
    return statistics.median(values), layouts[-1], values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--v06', type=Path, required=True)
    parser.add_argument('--v07', type=Path, required=True)
    parser.add_argument('--number', type=int, default=1_000_000)
    parser.add_argument('--processes', type=int, default=7)
    args = parser.parse_args()

    old = measure(args.v06, CASE_V06, policy='speed', strategy='speed', number=args.number, processes=args.processes)
    speed = measure(args.v07, CASE_V07, policy='speed', strategy='speed', number=args.number, processes=args.processes)
    density = measure(args.v07, CASE_V07, policy='speed', strategy='density', number=args.number, processes=args.processes)

    print(sys.version)
    print('scenario: 3 candidates; a crosses b+c; b contains c')
    for label, result in [('v0.6 speed', old), ('v0.7 speed', speed), ('v0.7 density', density)]:
        median, layout, samples = result
        print(f'{label:14s} {median:9.2f} ns  code={layout[0]:3d} B  locals={layout[1]}  resident={layout[2]}  spilled={layout[3]}')
        print('  samples:', ', '.join(f'{value:.2f}' for value in samples))


if __name__ == '__main__':
    main()
