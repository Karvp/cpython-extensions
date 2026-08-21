from __future__ import annotations

import argparse
import random

from python_extensions import clear_inline_registry, inline_calls, inline_function, verify_code


def build_pair(index: int, shape: int, c1: int, c2: int):
    callee_name = f"generated_helper_{index}"
    caller_name = f"generated_caller_{index}"
    if shape == 0:
        body = f"    a = x\n    b = a + {c1}\n    c = b * {c2}\n    return c\n"
    elif shape == 1:
        body = f"    a = x + {c1}\n    return a * a + a\n"
    elif shape == 2:
        body = f"    enabled = {bool(c1 & 1)!r}\n    if enabled:\n        return x + {c2}\n    return x - {c2}\n"
    elif shape == 3:
        body = f"    alias = x\n    x = x + {c1}\n    return alias + x + {c2}\n"
    else:
        body = f"    a = x\n    b = a\n    c = b + {c1}\n    return c * {c2}\n"

    namespace = globals()
    exec(f"def {callee_name}(x):\n{body}", namespace)
    raw = namespace[callee_name]
    registered = inline_function(register_only=True)(raw)
    namespace[callee_name] = registered
    exec(f"def {caller_name}(x):\n    return {callee_name}(x)\n", namespace)
    caller = inline_calls(policy="always", shared_regions=False)(namespace[caller_name])
    return registered, caller


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--functions", type=int, default=300)
    parser.add_argument("--inputs", type=int, default=1000)
    args = parser.parse_args()

    rng = random.Random(0xDA7AF10)
    calls = 0
    for index in range(args.functions):
        shape = rng.randrange(5)
        c1 = rng.randrange(-9, 10)
        c2 = rng.choice([value for value in range(-7, 8) if value != 0])
        baseline, optimized = build_pair(index, shape, c1, c2)
        verify_code(optimized.__code__)
        for _ in range(args.inputs):
            x = rng.randrange(-1000, 1001)
            expected = baseline(x)
            actual = optimized(x)
            if actual != expected:
                raise AssertionError((index, shape, c1, c2, x, expected, actual))
            calls += 1
    print(f"generated dataflow differential: {args.functions:,} functions / {calls:,} calls passed")


if __name__ == "__main__":
    main()
