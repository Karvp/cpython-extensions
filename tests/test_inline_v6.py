from __future__ import annotations

import dis
import functools
import random
import threading
import unittest

from inline_function import (
    InlineStats,
    clear_inline_registry,
    inline_calls,
    inline_function,
)


class InlineV6Tests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_inline_registry()

    def test_bound_method_alias(self):
        class Offset:
            def __init__(self, bias):
                self.bias = bias

            @inline_function(register_only=True)
            def apply(self, value):
                return self.bias + value

        obj = Offset(7)
        globals()["BOUND_ALIAS"] = obj.apply

        @inline_calls
        def merged(value):
            return BOUND_ALIAS(value)

        self.assertEqual(merged(5), 12)
        self.assertNotIn("CALL", [i.opname for i in dis.get_instructions(merged)])

    def test_positional_partial(self):
        @inline_function(register_only=True)
        def combine(a, b, c=4):
            return a * 100 + b * 10 + c

        globals()["PARTIAL_COMBINE"] = functools.partial(combine, 2)

        @inline_calls
        def merged(value):
            return PARTIAL_COMBINE(value)

        self.assertEqual(merged(3), 234)
        self.assertNotIn("CALL", [i.opname for i in dis.get_instructions(merged)])

    def test_keyword_partial_without_caller_keywords(self):
        @inline_function(register_only=True)
        def combine(a, b=3):
            return a + b

        globals()["KEYWORD_PARTIAL"] = functools.partial(combine, b=8)

        @inline_calls
        def merged(value):
            return KEYWORD_PARTIAL(value)

        self.assertEqual(merged(4), 12)
        self.assertNotIn("CALL", [i.opname for i in dis.get_instructions(merged)])

    def test_keyword_partial_with_caller_keyword_is_conservative(self):
        @inline_function(register_only=True)
        def combine(a, b=3, c=4):
            return a + b + c

        globals()["OVERLAP_PARTIAL"] = functools.partial(combine, b=8)

        @inline_calls
        def merged(value):
            return OVERLAP_PARTIAL(value, c=7)

        self.assertEqual(merged(4), 19)
        self.assertTrue(any(i.opname.startswith("CALL") for i in dis.get_instructions(merged)))

    def test_callable_object(self):
        class Doubler:
            @inline_function(register_only=True)
            def __call__(self, value):
                return value * 2

        globals()["DOUBLER"] = Doubler()

        @inline_calls
        def merged(value):
            return DOUBLER(value)

        self.assertEqual(merged(11), 22)
        self.assertNotIn("CALL", [i.opname for i in dis.get_instructions(merged)])

    def test_mutated_receiver_falls_back_to_real_local(self):
        class Box:
            def __init__(self, value):
                self.value = value

            @inline_function(register_only=True)
            def apply(self, x):
                self = type(self)(100)
                return self.value + x

        box = Box(2)
        globals()["MUTATING_BOUND"] = box.apply

        @inline_calls
        def merged(value):
            return MUTATING_BOUND(value)

        self.assertEqual(merged(5), 105)
        instructions = list(dis.get_instructions(merged))
        self.assertFalse(any(i.opname == "LOAD_GLOBAL" and i.argval == "MUTATING_BOUND" for i in instructions))

    def test_default_read_only_forwarding(self):
        marker = object()

        @inline_function(register_only=True)
        def choose(value, default=marker):
            return value, default
        globals()["CHOOSE_DEFAULT"] = choose

        @inline_calls
        def merged(value):
            return CHOOSE_DEFAULT(value)

        self.assertIs(merged(4)[1], marker)
        names = [i.opname for i in dis.get_instructions(merged)]
        self.assertNotIn("STORE_FAST", names)
        self.assertNotIn("CALL", names)

    def test_speed_policy_skips_variadic_regression(self):
        @inline_function(register_only=True)
        def collect(a, *items, **options):
            return a, items, options
        globals()["COLLECT_VARIADIC"] = collect

        @inline_calls
        def speed(value):
            return COLLECT_VARIADIC(value, 2, q=3)

        @inline_calls(policy="always")
        def forced(value):
            return COLLECT_VARIADIC(value, 2, q=3)

        expected = (1, (2,), {"q": 3})
        self.assertEqual(speed(1), expected)
        self.assertEqual(forced(1), expected)
        self.assertTrue(any(i.opname.startswith("CALL") for i in dis.get_instructions(speed)))
        self.assertFalse(any(i.opname.startswith("CALL") for i in dis.get_instructions(forced)))
        self.assertEqual(speed.__inline_stats__.calls_skipped_unprofitable, 1)
        self.assertEqual(forced.__inline_stats__.calls_skipped_unprofitable, 0)

    def test_speed_policy_still_inlines_later_profitable_site(self):
        @inline_function(register_only=True)
        def collect(a, *items):
            return a + sum(items)

        @inline_function(register_only=True)
        def add(a, b):
            return a + b
        globals()["COLLECT_SPEED"] = collect
        globals()["ADD_SPEED"] = add

        @inline_calls
        def merged(x, y):
            first = COLLECT_SPEED(x, y)
            return first + ADD_SPEED(x, y)

        self.assertEqual(merged(2, 3), 10)
        names = [i.opname for i in dis.get_instructions(merged)]
        self.assertEqual(sum(name.startswith("CALL") for name in names), 1)
        self.assertEqual(merged.__inline_stats__.calls_inlined, 1)
        self.assertEqual(merged.__inline_stats__.calls_skipped_unprofitable, 1)

    def test_randomized_new_forms(self):
        @inline_function(register_only=True)
        def formula(self, a, b=9):
            return self.bias + a * 3 - b

        class Target:
            bias = 11

        target = Target()
        globals()["FORMULA_ALIAS"] = formula.__get__(target, Target)
        globals()["FORMULA_PARTIAL"] = functools.partial(formula, target, 4)

        @inline_calls
        def alias(a, b):
            return FORMULA_ALIAS(a, b)

        @inline_calls
        def partial(b):
            return FORMULA_PARTIAL(b)

        rng = random.Random(1234)
        for _ in range(20_000):
            a = rng.randrange(-1000, 1000)
            b = rng.randrange(-1000, 1000)
            self.assertEqual(alias(a, b), target.bias + a * 3 - b)
            self.assertEqual(partial(b), target.bias + 4 * 3 - b)

    def test_concurrent_bound_alias(self):
        class Affine:
            def __init__(self, scale, bias):
                self.scale = scale
                self.bias = bias

            @inline_function(register_only=True)
            def apply(self, value):
                return self.scale * value + self.bias

        obj = Affine(7, 3)
        globals()["AFFINE_ALIAS"] = obj.apply

        @inline_calls
        def merged(value):
            return AFFINE_ALIAS(value)

        failures: list[tuple[int, int]] = []

        def worker(seed: int) -> None:
            rng = random.Random(seed)
            for _ in range(25_000):
                value = rng.randrange(-10000, 10000)
                result = merged(value)
                expected = 7 * value + 3
                if result != expected:
                    failures.append((result, expected))
                    return

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(failures)


if __name__ == "__main__":
    unittest.main()
