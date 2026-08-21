from __future__ import annotations

import functools
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from python_extensions import inline_calls, inline_function, verify_code

OPS = 0

def tick(n=1):
    global OPS
    OPS += n

@inline_function(register_only=True)
def target(x, bias=1):
    return x * 3 + bias

@inline_calls(policy="always", binding="guarded")
def guarded(x):
    return target(x)


def stress_function_state(rounds=300_000):
    global target
    rng = random.Random(0x102)
    original = target
    original_code = target.__code__
    original_defaults = target.__defaults__

    def alternate(x, bias=7):
        return x * 5 - bias

    for i in range(rounds):
        x = rng.randint(-10_000, 10_000)
        mode = i & 3
        if mode == 0:
            target = original
            original.__code__ = original_code
            original.__defaults__ = (1,)
        elif mode == 1:
            target = lambda value: value - 91
        elif mode == 2:
            target = original
            original.__code__ = alternate.__code__
            original.__defaults__ = (7,)
        else:
            target = original
            original.__code__ = original_code
            original.__defaults__ = (19,)
        assert guarded(x) == target(x)
        tick()

    target = original
    original.__code__ = original_code
    original.__defaults__ = original_defaults


def stress_kwdefaults(rounds=200_000):
    @inline_function(register_only=True)
    def helper(x, *, bias=2):
        return x + bias

    @inline_calls(policy="always", binding="guarded")
    def caller(x):
        return helper(x)

    for i in range(rounds):
        helper.__kwdefaults__["bias"] = i % 127
        x = i % 997
        assert caller(x) == helper(x)
        tick()


def stress_descriptors(rounds=200_000):
    class Service:
        factor = 3

        @staticmethod
        @inline_function(register_only=True)
        def static(x):
            return x + 1

        @classmethod
        @inline_function(register_only=True)
        def cls(cls, x):
            return x * cls.factor

    @inline_calls(policy="always", binding="guarded")
    def caller(x):
        return Service.static(x), Service.cls(x)

    old_static = Service.__dict__["static"]
    old_cls = Service.__dict__["cls"]
    for i in range(rounds):
        if i & 1:
            Service.static = staticmethod(lambda x: x - 2)
            Service.cls = classmethod(lambda cls, x: x + cls.factor)
        else:
            Service.static = old_static
            Service.cls = old_cls
        x = i % 1009
        assert caller(x) == (Service.static(x), Service.cls(x))
        tick()
    Service.static = old_static
    Service.cls = old_cls


def stress_partial(rounds=200_000):
    @inline_function(register_only=True)
    def base(x, *, scale=2):
        return x * scale

    configured = functools.partial(base, scale=3)

    @inline_calls(policy="always", binding="guarded")
    def caller(x):
        return configured(x)

    for i in range(rounds):
        configured.keywords["scale"] = (i % 11) + 1
        x = i % 1013
        assert caller(x) == configured(x)
        tick()


def stress_stable_threads(workers=8, per_worker=250_000):
    @inline_function(register_only=True)
    def helper(x, bias=5):
        return x * 7 + bias

    @inline_calls(policy="always", binding="guarded")
    def caller(x):
        return helper(x)

    barrier = threading.Barrier(workers)
    def worker(seed):
        r = random.Random(seed)
        barrier.wait()
        count = 0
        for _ in range(per_worker):
            x = r.randint(-1_000_000, 1_000_000)
            assert caller(x) == helper(x)
            count += 1
        return count

    with ThreadPoolExecutor(max_workers=workers) as pool:
        total = sum(pool.map(worker, range(workers)))
    tick(total)
    assert verify_code(caller.__code__).valid


def main():
    started = time.perf_counter()
    stress_function_state()
    stress_kwdefaults()
    stress_descriptors()
    stress_partial()
    stress_stable_threads()
    result = {
        "status": "pass",
        "operations": OPS,
        "elapsed_s": time.perf_counter() - started,
        "guarded_code_valid": verify_code(guarded.__code__).valid,
    }
    print(json.dumps(result, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
