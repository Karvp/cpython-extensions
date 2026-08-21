from __future__ import annotations

import dis
import unittest

from python_extensions import (
    DuplicateLabelError,
    MissingLabelError,
    enable_goto,
)


class GotoTests(unittest.TestCase):
    def test_forward_goto(self):
        @enable_goto
        def flow(flag):
            result = 10
            if flag:
                goto .done
            result = 20
            label .done
            return result

        self.assertEqual(flow(True), 10)
        self.assertEqual(flow(False), 20)
        self.assertEqual(len(flow.__code__.co_code), len(flow.__wrapped__.__code__.co_code) if hasattr(flow, '__wrapped__') else len(flow.__code__.co_code))

    def test_backward_goto_loop(self):
        @enable_goto
        def countdown(value):
            total = 0
            label .again
            total += value
            value -= 1
            if value > 0:
                goto .again
            return total

        self.assertEqual(countdown(1), 1)
        self.assertEqual(countdown(5), 15)

    def test_missing_label(self):
        with self.assertRaises(MissingLabelError):
            @enable_goto
            def bad():
                goto .missing

    def test_duplicate_label(self):
        with self.assertRaises(DuplicateLabelError):
            @enable_goto
            def bad():
                label .same
                label .same

    def test_no_pseudo_global_loads_remain_on_executed_paths(self):
        @enable_goto
        def flow():
            goto .done
            label .done
            return 1

        self.assertEqual(flow(), 1)
        names = [instruction.argval for instruction in dis.get_instructions(flow)]
        self.assertNotIn("goto", names)
        self.assertNotIn("label", names)


if __name__ == "__main__":
    unittest.main()


def test_strict_rejects_crossing_finally_region():
    from python_extensions.goto import GotoControlFlowError

    try:
        @enable_goto
        def bad(value):
            try:
                goto .outside
            finally:
                value += 1
            label .outside
            return value
    except GotoControlFlowError:
        pass
    else:
        raise AssertionError("strict goto accepted a jump across finally protection")


def test_unsafe_allows_cross_region_decoration():
    @enable_goto(mode="unsafe")
    def raw(value):
        try:
            goto .outside
        finally:
            value += 1
        label .outside
        return value

    assert raw.__python_extensions_report__.as_dict()["mode"] == "unsafe"


def test_strict_allows_label_at_end_of_try_and_runs_finally():
    @enable_goto
    def flow(flag, out):
        try:
            if flag:
                goto .done
            out.append("body")
            label .done
        finally:
            out.append("finally")
        return out

    assert flow(True, []) == ["finally"]
    assert flow(False, []) == ["body", "finally"]


def test_strict_rejects_stack_invalid_jump_with_public_error():
    from python_extensions.goto import GotoControlFlowError

    try:
        @enable_goto
        def bad(flag):
            if flag:
                goto .inside
            for item in (1, 2, 3):
                label .inside
                return item
            return -1
    except GotoControlFlowError as exc:
        assert "invalid CPython stack/control flow" in str(exc)
    else:
        raise AssertionError("strict goto accepted a stack-invalid loop-body entry")



def test_strict_async_forward_can_cross_await_split():
    import asyncio

    @enable_goto
    async def flow(skip: bool, out: list[str]) -> int:
        if skip:
            goto .done
        out.append("before")
        await asyncio.sleep(0)
        out.append("after")
        label .done
        return len(out)

    async def run() -> None:
        out: list[str] = []
        assert await flow(False, out) == 2
        assert out == ["before", "after"]
        out = []
        assert await flow(True, out) == 0
        assert out == []

    asyncio.run(run())


def test_strict_async_backward_loop_can_cross_await_split():
    import asyncio

    @enable_goto
    async def flow(value: int) -> int:
        total = 0
        label .again
        if value <= 0:
            goto .done
        await asyncio.sleep(0)
        total += value
        value -= 1
        goto .again
        label .done
        return total

    assert asyncio.run(flow(0)) == 0
    assert asyncio.run(flow(7)) == 28


def test_strict_async_generator_loop_can_cross_yield_split():
    import asyncio

    @enable_goto
    async def flow(limit: int):
        value = 0
        label .again
        if value >= limit:
            goto .done
        yield value
        value += 1
        goto .again
        label .done

    async def collect(limit: int) -> list[int]:
        return [item async for item in flow(limit)]

    assert asyncio.run(collect(0)) == []
    assert asyncio.run(collect(6)) == list(range(6))


def test_strict_async_still_rejects_crossing_finally_handler_stack():
    import asyncio
    from python_extensions.goto import GotoControlFlowError

    with __import__("pytest").raises(GotoControlFlowError):
        @enable_goto
        async def bad(value: int) -> int:
            try:
                await asyncio.sleep(0)
                goto .outside
            finally:
                value += 1
            label .outside
            return value


def test_strict_async_try_loop_across_await_runs_finally_once():
    import asyncio

    @enable_goto
    async def flow(value: int, out: list[str]) -> int:
        total = 0
        try:
            label .again
            if value <= 0:
                goto .done
            await asyncio.sleep(0)
            total += value
            value -= 1
            goto .again
            label .done
        finally:
            out.append("finally")
        return total

    out: list[str] = []
    assert asyncio.run(flow(5, out)) == 15
    assert out == ["finally"]



def test_marker_free_enable_goto_is_identity_preserving_noop():
    def plain(value: int) -> int:
        return value + 1

    transformed = enable_goto(plain)
    assert transformed is plain
    report = transformed.__python_extensions_report__.as_dict()
    assert report["gotos"] == 0
    assert report["labels"] == 0
    assert report["early_jump_lowering"] is False
    assert report["no_op"] is True


def test_goto_report_exposes_early_lowering_savings_and_extended_args():
    namespace = {"enable_goto": enable_goto}
    body = "\n".join(f"    value += {i & 1}" for i in range(420))
    source = (
        "@enable_goto\n"
        "def long_forward(value):\n"
        "    goto .done\n"
        f"{body}\n"
        "    label .done\n"
        "    return value\n"
    )
    exec(source, namespace)
    long_forward = namespace["long_forward"]
    assert long_forward(7) == 7
    report = long_forward.__python_extensions_report__.as_dict()
    assert report["early_jump_lowering"] is True
    assert report["extended_arg_units"] >= 1
    assert report["marker_units_elided"] > 0


def test_marker_span_guard_rejects_a_synthetic_cross_region_span():
    import dis
    import pytest
    from python_extensions.goto import (
        GotoControlFlowError,
        _PseudoStatement,
        _marker_exception_signature,
    )

    def protected(value):
        try:
            value += 1
        finally:
            value += 2
        return value

    entries = list(dis.Bytecode(protected).exception_entries)
    assert entries
    region = entries[0]
    # Build a synthetic pseudo span that starts in the protected range and extends
    # to an instruction at/after its boundary. Strict lowering must refuse to place
    # an early jump anywhere in a marker whose semantic handler stack is unstable.
    offsets = [i.offset for i in dis.get_instructions(protected, adaptive=False)]
    start = next(offset for offset in offsets if region.start <= offset < region.end)
    end = next(offset for offset in offsets if offset >= region.end)
    marker = _PseudoStatement("goto", "synthetic", start, end)
    with pytest.raises(GotoControlFlowError, match="straddles a protected"):
        _marker_exception_signature(protected.__code__, marker)


def test_marker_free_report_has_zero_lowering_telemetry():
    def plain(value):
        return value

    transformed = enable_goto(plain)
    report = transformed.__python_extensions_report__.as_dict()
    assert report["extended_arg_units"] == 0
    assert report["marker_units_elided"] == 0


def test_goto_report_counts_verified_synthetic_jumps():
    @enable_goto
    def f(value):
        if value:
            goto .done
        value = 4
        label .done
        return value

    details = dict(f.__python_extensions_report__.details)
    assert details["synthetic_jumps_verified"] == 2
    assert f(1) == 1
    assert f(0) == 4
