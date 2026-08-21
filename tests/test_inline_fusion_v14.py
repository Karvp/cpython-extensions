from __future__ import annotations

import dis
import unittest

from python_extensions.inline import inline_calls, inline_function


@inline_function(register_only=True)
def _fusion_inc(x):
    return x + 1


@inline_function(register_only=True)
def _fusion_double(x):
    return x * 2


@inline_function(register_only=True)
def _fusion_flag():
    return True


@inline_function(register_only=True)
def _fusion_choose(enabled, x):
    if enabled:
        return x + 1
    return x - 1


@inline_function(register_only=True)
def _fusion_div(x):
    return 100 // x


@inline_calls(fusion_strategy="safe")
def safe_chain(x):
    a = _fusion_inc(x)
    b = _fusion_double(a)
    return b - 3, locals().get("a"), locals().get("b")


@inline_calls(fusion_strategy="aggressive")
def aggressive_chain(x):
    a = _fusion_inc(x)
    b = _fusion_double(a)
    return b - 3


@inline_calls(fusion_strategy="safe")
def constant_chain(x):
    enabled = _fusion_flag()
    y = _fusion_choose(enabled, x)
    return y, locals().get("enabled")


@inline_calls(fusion_strategy="off")
def fusion_off_chain(x):
    a = _fusion_inc(x)
    return _fusion_double(a)


@inline_calls(fusion_strategy="safe")
def reassigned_local(x):
    a = _fusion_inc(x)
    a = a + 5
    return _fusion_double(a)


@inline_calls(fusion_strategy="safe")
def exception_local(x):
    a = _fusion_inc(x)
    try:
        return _fusion_div(x - x)
    except ZeroDivisionError:
        return a, locals().get("a")


class FusionTests(unittest.TestCase):
    def test_safe_handoff_preserves_locals(self):
        self.assertEqual(safe_chain(10), (19, 11, 22))
        stats = safe_chain.__inline_stats__
        self.assertGreaterEqual(stats.fused_result_handoffs, 2)
        self.assertEqual(stats.aggressive_result_handoffs, 0)
        names = [i.opname for i in dis.get_instructions(safe_chain, adaptive=False)]
        self.assertIn("COPY", names)

    def test_aggressive_handoff_elides_single_use_locals(self):
        self.assertEqual(aggressive_chain(10), 19)
        self.assertNotIn("a", aggressive_chain.__code__.co_varnames)
        self.assertNotIn("b", aggressive_chain.__code__.co_varnames)
        self.assertGreaterEqual(
            aggressive_chain.__inline_stats__.aggressive_result_handoffs, 2
        )

    def test_constant_handoff_unlocks_branch_folding(self):
        self.assertEqual(constant_chain(10), (11, True))
        stats = constant_chain.__inline_stats__
        self.assertGreaterEqual(stats.constant_result_handoffs, 1)
        self.assertGreaterEqual(stats.constant_branches_folded, 1)
        names = [i.opname for i in dis.get_instructions(constant_chain, adaptive=False)]
        self.assertNotIn("POP_JUMP_IF_FALSE", names)
        self.assertNotIn("TO_BOOL", names)

    def test_off_keeps_handoff_unfused(self):
        self.assertEqual(fusion_off_chain(8), 18)
        self.assertEqual(fusion_off_chain.__inline_stats__.fused_result_handoffs, 0)

    def test_reassigned_local_is_not_constant_or_dynamic_fused(self):
        self.assertEqual(reassigned_local(4), 20)
        self.assertEqual(reassigned_local.__inline_stats__.fused_result_handoffs, 0)

    def test_safe_fusion_preserves_local_during_exception(self):
        self.assertEqual(exception_local(9), (10, 10))

    def test_invalid_fusion_strategy(self):
        with self.assertRaises(ValueError):
            inline_calls(fusion_strategy="unknown")(lambda x: x)


if __name__ == "__main__":
    unittest.main()
