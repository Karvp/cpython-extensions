from __future__ import annotations

import asyncio
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import pytest

from python_extensions import (
    DuplicateCaseError,
    SwitchSyntaxError,
    case,
    enable_switch,
    fallthrough,
    switch,
)


def _compile(source: str, *, mode: str = "portable", case_key_mode: str = "python", extra=None):
    ns = {"switch": switch, "case": case, "fallthrough": fallthrough}
    if extra:
        ns.update(extra)
    exec(compile(source, "<pyswitch-adversarial>", "exec"), ns)
    name = next(
        node.name
        for node in __import__("ast").parse(source).body
        if isinstance(node, (__import__("ast").FunctionDef, __import__("ast").AsyncFunctionDef))
    )
    return enable_switch(mode=mode, case_key_mode=case_key_mode, source=source)(ns[name])


def test_balanced_dispatch_temporaries_are_unbound_before_guard_and_body():
    source = '''def inspect_route(x, seen):
    with switch(x + 0):
        if case(1, when=(seen.append(tuple(sorted(k for k in locals() if k.startswith("__pyswitch_")))) or True)):
            return tuple(sorted(k for k in locals() if k.startswith("__pyswitch_")))
        if case():
            return tuple(sorted(k for k in locals() if k.startswith("__pyswitch_")))
'''
    fn = _compile(source)
    seen = []
    assert fn(1, seen) == ()
    assert seen == [()]
    assert fn(9, []) == ()
    assert fn.__pyswitch_backend__ == "portable-balanced-v18"


def test_balanced_dispatch_temporaries_do_not_linger_after_switch():
    source = '''def after(x):
    out = 0
    with switch(x + 0):
        if case(1, when=True):
            out = 1
        if case():
            out = 2
    hidden = tuple(sorted(k for k in locals() if k.startswith("__pyswitch_")))
    return out, hidden
'''
    fn = _compile(source)
    assert fn(1) == (1, ())
    assert fn(2) == (2, ())


def test_nontrailing_or_nested_fallthrough_is_rejected_early():
    bad_sources = [
        '''def bad(x):
    with switch(x):
        if case(1):
            fallthrough()
            return 1
        if case():
            return 0
''',
        '''def bad(x):
    with switch(x):
        if case(1):
            if x:
                fallthrough()
            return 1
        if case():
            return 0
''',
        '''def bad(x):
    with switch(x):
        if case(1):
            fallthrough(1)
        if case():
            return 0
''',
    ]
    for source in bad_sources:
        with pytest.raises(SwitchSyntaxError, match="final direct statement"):
            _compile(source)


def test_nested_switch_owns_its_own_fallthrough_marker():
    source = '''def nested(a, b):
    out = []
    with switch(a):
        if case(1):
            with switch(b):
                if case(2):
                    out.append(2)
                    fallthrough()
                if case(3):
                    out.append(3)
                if case():
                    out.append(9)
        if case():
            out.append(0)
    return tuple(out)
'''
    fn = _compile(source)
    assert fn(1, 2) == (2, 3)
    assert fn(1, 3) == (3,)
    assert fn(1, 8) == (9,)
    assert fn(8, 2) == (0,)
    assert fn.__pyswitch_switch_count__ == 2


class _HashProbe:
    def __init__(self, value, log, *, hash_value=None):
        self.value = value
        self.log = log
        self.hash_value = hash(value) if hash_value is None else hash_value

    def __hash__(self):
        self.log.append(("hash", self.value))
        return self.hash_value

    def __eq__(self, other):
        self.log.append(("eq", self.value, other))
        if isinstance(other, _HashProbe):
            return self.value == other.value
        return self.value == other


_CASE_LOG = []
_CASE_A = _HashProbe("a", _CASE_LOG)
_CASE_B = _HashProbe("b", _CASE_LOG)


def test_custom_hash_and_equality_follow_real_dict_lookup_counts():
    source = '''def custom(x):
    with switch(x):
        if case(_CASE_A):
            return "a"
        if case(_CASE_B):
            return "b"
        if case():
            return "miss"
'''
    fn = _compile(source, extra={"_CASE_A": _CASE_A, "_CASE_B": _CASE_B})

    # Compare observable key/subject hash+eq event sequence with one ordinary
    # dict.get built from the same case objects.
    subject_log = []
    subject = _HashProbe("a", subject_log)
    _CASE_LOG.clear()
    table = {_CASE_A: "a", _CASE_B: "b"}
    _CASE_LOG.clear()
    subject_log.clear()
    expected = table.get(subject, "miss")
    expected_events = tuple(_CASE_LOG + subject_log)

    _CASE_LOG.clear()
    subject_log.clear()
    assert fn(subject) == expected
    actual_events = tuple(_CASE_LOG + subject_log)
    assert actual_events == expected_events


class _EqRaises:
    def __hash__(self):
        return 7

    def __eq__(self, other):
        raise RuntimeError("eq exploded")


_EQ_KEY = _EqRaises()


def test_user_equality_exception_propagates_without_default_conversion():
    source = '''def eq_failure(x):
    with switch(x):
        if case(_EQ_KEY):
            return 1
        if case():
            return 0
'''
    fn = _compile(source, extra={"_EQ_KEY": _EQ_KEY})

    class Subject:
        def __hash__(self):
            return 7

        def __eq__(self, other):
            return NotImplemented

    with pytest.raises(RuntimeError, match="eq exploded"):
        fn(Subject())


def test_nan_and_signed_zero_follow_python_dict_identity_rules():
    nan_key = float("nan")
    source = '''def special(x):
    with switch(x):
        if case(NAN_KEY):
            return "nan"
        if case(-0.0):
            return "zero"
        if case():
            return "miss"
'''
    fn = _compile(source, extra={"NAN_KEY": nan_key})
    assert fn(nan_key) == "nan"  # dict identity shortcut for the same NaN object
    assert fn(float("nan")) == "miss"
    assert fn(0.0) == "zero"
    assert fn(0) == "zero"


def test_typed_signed_zero_still_respects_same_type_hash_equality():
    source = '''def special(x):
    with switch(x):
        if case(-0.0):
            return "float-zero"
        if case(0):
            return "int-zero"
        if case():
            return "miss"
'''
    fn = _compile(source, case_key_mode="typed")
    assert fn(-0.0) == "float-zero"
    assert fn(0.0) == "float-zero"
    assert fn(0) == "int-zero"
    assert fn(False) == "miss"


def test_cross_numeric_fraction_collision_matches_python_dict():
    fraction_one = Fraction(1, 1)
    with pytest.raises(DuplicateCaseError):
        _compile(
            '''def duplicate(x):
    with switch(x):
        if case(1):
            return "int"
        if case(FRACTION_ONE):
            return "fraction"
        if case():
            return "miss"
''',
            extra={"FRACTION_ONE": fraction_one},
        )


def test_guard_order_side_effects_and_default_guard():
    source = '''def ordered(x, log):
    with switch(x):
        if case(1, when=(log.append("g1") or False)):
            return 1
        elif case(1, when=(log.append("g2") or True)):
            return 2
        elif case(1, when=(log.append("g3") or True)):
            return 3
        elif case(when=(log.append("gd") or True)):
            return 9
    return 10
'''
    fn = _compile(source)
    log = []
    assert fn(1, log) == 2
    assert log == ["g1", "g2"]
    log = []
    assert fn(5, log) == 9
    assert log == ["gd"]


def test_subject_alias_assignment_and_evaluation_happen_once():
    source = '''def alias(counter):
    with switch(counter.pop()) as chosen:
        if case(1):
            return chosen, tuple(counter)
        if case():
            return chosen, tuple(counter)
'''
    fn = _compile(source)
    data = [3, 2, 1]
    assert fn(data) == (1, (3, 2))
    assert data == [3, 2]


def test_try_finally_return_semantics_remain_in_original_frame():
    source = '''def protected(x, log):
    try:
        with switch(x):
            if case(1, when=True):
                log.append("case")
                return 10
            if case():
                log.append("default")
                return 20
    finally:
        log.append("finally")
'''
    fn = _compile(source)
    log = []
    assert fn(1, log) == 10
    assert log == ["case", "finally"]
    log = []
    assert fn(2, log) == 20
    assert log == ["default", "finally"]


def test_closure_nonlocal_state_and_recursion():
    namespace = {"switch": switch, "case": case}
    exec(
        '''def factory():
    total = 0
    def recursive(n, selector):
        nonlocal total
        with switch(selector):
            if case(1):
                total += 1
            if case():
                total += 10
        if n:
            return recursive(n - 1, selector), total
        return total
    return recursive
''',
        namespace,
    )
    original = namespace["factory"]()
    source = '''def recursive(n, selector):
    nonlocal total
    with switch(selector):
        if case(1):
            total += 1
        if case():
            total += 10
    if n:
        return recursive(n - 1, selector), total
    return total
'''
    fn = enable_switch(mode="portable", source=source)(original)
    result = fn(3, 1)
    # Nested return shape is less important than proving all four recursive
    # invocations shared and updated the original closure cell.
    cursor = result
    while isinstance(cursor, tuple):
        cursor = cursor[0]
    assert cursor == 4


def test_async_concurrent_calls_are_isolated():
    source = '''async def async_case(x):
    await asyncio.sleep(0)
    with switch(x):
        if case(0):
            await asyncio.sleep(0)
            return x + 10
        if case(1):
            await asyncio.sleep(0)
            return x + 20
        if case():
            await asyncio.sleep(0)
            return -1
'''
    fn = _compile(source, extra={"asyncio": asyncio})

    async def run():
        values = [0, 1, 2] * 300
        return await asyncio.gather(*(fn(value) for value in values))

    actual = asyncio.run(run())
    expected = [10 if v == 0 else 21 if v == 1 else -1 for v in [0, 1, 2] * 300]
    assert actual == expected


def test_threaded_custom_hash_subjects_do_not_share_state():
    source = '''def threaded(x):
    with switch(x):
        if case(1, when=True):
            return 11
        if case(2):
            return 22
        if case():
            return -1
'''
    fn = _compile(source)
    values = list(range(5)) * 4000
    expected = [11 if x == 1 else 22 if x == 2 else -1 for x in values]
    with ThreadPoolExecutor(max_workers=16) as pool:
        actual = list(pool.map(fn, values, chunksize=128))
    assert actual == expected


def test_nested_function_fallthrough_is_not_mistaken_for_outer_marker():
    source = '''def outer(x):
    def inner(y):
        fallthrough()
    with switch(x):
        if case(1):
            return 1
        if case():
            return 0
'''
    # The nested function is deliberately not transformed; merely containing a
    # runtime call named fallthrough must not invalidate the outer switch.
    fn = _compile(source)
    assert fn(1) == 1
    assert fn(2) == 0


def test_malformed_duplicate_case_with_mixed_equal_numerics_rejected():
    with pytest.raises(DuplicateCaseError):
        _compile(
            '''def bad(x, flag):
    with switch(x):
        if case(1, 1.0, when=flag):
            return 1
        if case():
            return 0
'''
        )


def test_specialized_assignment_temporaries_do_not_linger_after_switch():
    sources = [
        '''def direct(x):
    with switch(x):
        if case(1):
            y = 10
        if case(2):
            y = 20
        if case():
            y = 30
    return y, tuple(sorted(k for k in locals() if k.startswith("__pyswitch_")))
''',
        '''def expression(x):
    with switch(x):
        if case(1):
            y = x + 10
        if case(2):
            y = x + 20
        if case():
            y = x + 30
    return y, tuple(sorted(k for k in locals() if k.startswith("__pyswitch_")))
''',
        '''def statement(x):
    with switch(x):
        if case(1):
            y = x + 10
            z = y * 2
        if case(2):
            y = x + 20
            z = y * 3
        if case():
            y = x + 30
            z = y * 4
    return (y, z), tuple(sorted(k for k in locals() if k.startswith("__pyswitch_")))
''',
    ]
    expected_backends = {
        "direct": "portable-direct-value-v18",
        "expression": "portable-expression-template-v18",
        "statement": "portable-statement-template-v18",
    }
    for source in sources:
        fn = _compile(source)
        assert fn.__pyswitch_backend__ == expected_backends[fn.__name__]
        assert fn(1)[-1] == ()
        assert fn(9)[-1] == ()


def test_simple_alias_holds_complex_subject_without_compiler_subject_local():
    observed = []

    class Subject:
        def __hash__(self):
            caller = sys._getframe(1)
            observed.append(tuple(sorted(k for k in caller.f_locals if k.startswith("__pyswitch_subject_"))))
            return hash(1)

        def __eq__(self, other):
            return other == 1

    subject = Subject()
    source = '''def aliased(factory):
    with switch(factory()) as chosen:
        if case(1, when=True):
            return chosen
        if case():
            return None
'''
    fn = _compile(source)
    assert fn(lambda: subject) is subject
    assert observed == [()]


def test_nested_recursive_self_closure_portable_and_isolated_modes():
    factory_source = '''def factory():
    def recurse(n):
        with switch(n):
            if case(0):
                return 0
            if case():
                return 1 + recurse(n - 1)
    return recurse
'''
    body_source = '''def recurse(n):
    with switch(n):
        if case(0):
            return 0
        if case():
            return 1 + recurse(n - 1)
'''

    def original():
        ns = {"switch": switch, "case": case}
        exec(factory_source, ns)
        return ns["factory"]()

    for mode in ("portable", "isolated", "per_call"):
        fn = enable_switch(mode=mode, source=body_source)(original())
        assert fn(40) == 40


def test_nested_recursive_self_closure_does_not_mutate_original_cell():
    factory_source = '''def factory():
    def recurse(n):
        with switch(n):
            if case(0):
                return 0
            if case():
                return 1 + recurse(n - 1)
    return recurse
'''
    body_source = '''def recurse(n):
    with switch(n):
        if case(0):
            return 0
        if case():
            return 1 + recurse(n - 1)
'''
    ns = {"switch": switch, "case": case}
    exec(factory_source, ns)
    original = ns["factory"]()
    self_index = original.__code__.co_freevars.index("recurse")
    original_cell = original.__closure__[self_index]
    assert original_cell.cell_contents is original
    transformed = enable_switch(mode="portable", source=body_source)(original)
    assert transformed(8) == 8
    assert original_cell.cell_contents is original


def test_zero_argument_super_class_cell_is_preserved():
    class Base:
        def base_value(self):
            return 10

    class Child(Base):
        @enable_switch(mode="portable")
        def value(self, selector):
            with switch(selector):
                if case(1):
                    return super().base_value() + 1
                if case():
                    return super().base_value() + 2

    child = Child()
    assert child.value(1) == 11
    assert child.value(9) == 12


def test_pep695_type_parameter_cell_identity_is_preserved():
    namespace = {"switch": switch, "case": case}
    source = '''def generic[T](selector):
    with switch(selector):
        if case(1):
            chosen = T
        if case():
            chosen = T
    return chosen
'''
    exec(source, namespace)
    fn = enable_switch(mode="portable", source=source)(namespace["generic"])
    assert fn(1) is fn.__type_params__[0]
    assert fn(9) is fn.__type_params__[0]


def test_generator_send_value_survives_statement_template_lowering():
    source = '''def gen(selector):
    with switch(selector):
        if case(1):
            received = yield 10
            yield received + 1
        if case(2):
            received = yield 20
            yield received + 2
        if case():
            received = yield 30
            yield received + 3
'''
    fn = _compile(source)
    for selector, first, delta in ((1, 10, 1), (2, 20, 2), (9, 30, 3)):
        iterator = fn(selector)
        assert next(iterator) == first
        assert iterator.send(100) == 100 + delta
        with pytest.raises(StopIteration):
            next(iterator)


def test_double_decoration_is_semantically_stable():
    source = '''def twice(x):
    with switch(x):
        if case(1):
            return 10
        if case():
            return 20
'''
    ns = {"switch": switch, "case": case}
    exec(source, ns)
    once = enable_switch(mode="portable", source=source)(ns["twice"])
    twice = enable_switch(mode="portable", source=source)(once)
    assert once(1) == twice(1) == 10
    assert once(9) == twice(9) == 20


def test_nested_recursive_async_self_closure_stays_transformed():
    factory_source = '''def factory():
    async def recurse(n):
        await asyncio.sleep(0)
        with switch(n):
            if case(0):
                return 0
            if case():
                return 1 + await recurse(n - 1)
    return recurse
'''
    body_source = '''async def recurse(n):
    await asyncio.sleep(0)
    with switch(n):
        if case(0):
            return 0
        if case():
            return 1 + await recurse(n - 1)
'''
    ns = {"switch": switch, "case": case, "asyncio": asyncio}
    exec(factory_source, ns)
    original = ns["factory"]()
    transformed = enable_switch(mode="portable", source=body_source)(original)
    assert asyncio.run(transformed(24)) == 24


def test_recursive_self_cell_cycle_is_collectable():
    import gc
    import weakref

    factory_source = '''def factory():
    def recurse(n):
        with switch(n):
            if case(0):
                return 0
            if case():
                return 1 + recurse(n - 1)
    return recurse
'''
    body_source = '''def recurse(n):
    with switch(n):
        if case(0):
            return 0
        if case():
            return 1 + recurse(n - 1)
'''
    ns = {"switch": switch, "case": case}
    exec(factory_source, ns)
    transformed = enable_switch(mode="portable", source=body_source)(ns["factory"]())
    assert transformed(12) == 12
    reference = weakref.ref(transformed)
    del transformed
    gc.collect()
    gc.collect()
    assert reference() is None
