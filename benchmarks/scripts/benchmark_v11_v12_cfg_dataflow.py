from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import textwrap

SCENARIOS = {
    "branch_merge": """
@inline_calls(region_dataflow=True)
def target(value, cond):
    root = flag()
    if cond:
        alias = root
    else:
        alias = root
    return choose(alias, value)
""",
    "fixed_point": """
@inline_calls(region_dataflow=True)
def target(value, cond):
    root = flag()
    if cond:
        first = root
    else:
        first = root
    if first:
        alias = True
    else:
        alias = False
    return choose(alias, value)
""",
    "different_merge": """
@inline_calls(region_dataflow=True)
def target(value, cond):
    root = flag()
    if cond:
        alias = root
    else:
        alias = False
    return choose(alias, value)
""",
}

CHILD = r'''
import json, statistics, timeit
from python_extensions import inline_calls, inline_function
@inline_function(register_only=True)
def flag(): return True
@inline_function(register_only=True)
def choose(flag, value):
    if flag: return value + 7
    return value - 11
SCENARIO_SOURCE
assert target(123, True) == EXPECT_TRUE
assert target(123, False) == EXPECT_FALSE
samples = timeit.repeat("target(123, toggle)", setup="toggle=True", globals=globals(), number=1000000, repeat=7)
ns = statistics.median(samples) * 1e9 / 1000000
print(json.dumps({"ns": ns, "code": len(target.__code__.co_code), "locals": target.__code__.co_nlocals, "stats": getattr(target, "__inline_stats__", None).__dict__ if hasattr(getattr(target, "__inline_stats__", None), "__dict__") else {}}))
'''


def measure_once(src: str, scenario: str) -> dict[str, object]:
    source = SCENARIOS[scenario]
    expected_true = 130
    expected_false = 112 if scenario == "different_merge" else 130
    program = CHILD.replace("SCENARIO_SOURCE", source).replace("EXPECT_TRUE", str(expected_true)).replace("EXPECT_FALSE", str(expected_false))
    env = dict(os.environ)
    env["PYTHONPATH"] = src
    proc = subprocess.run([sys.executable, "-c", program], env=env, text=True, capture_output=True, check=True)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def measure(src: str, scenario: str, processes: int = 5) -> dict[str, object]:
    rows = [measure_once(src, scenario) for _ in range(processes)]
    return {
        "ns": statistics.median(float(row["ns"]) for row in rows),
        "code": rows[0]["code"],
        "locals": rows[0]["locals"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-src", required=True)
    ap.add_argument("--current-src", required=True)
    args = ap.parse_args()
    print(sys.version)
    print(f"baseline={args.baseline_src}")
    print(f"current={args.current_src}")
    for scenario in SCENARIOS:
        baseline = measure(args.baseline_src, scenario)
        current = measure(args.current_src, scenario)
        ratio = float(baseline["ns"]) / float(current["ns"])
        print(f"\n[{scenario}]")
        print(f"baseline {baseline['ns']:.2f} ns  code={baseline['code']} locals={baseline['locals']}")
        print(f"current  {current['ns']:.2f} ns  code={current['code']} locals={current['locals']}")
        print(f"speedup  {ratio:.3f}x")


if __name__ == "__main__":
    main()
