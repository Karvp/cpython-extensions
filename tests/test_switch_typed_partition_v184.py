from __future__ import annotations

import sys

import pytest

from python_extensions import case, enable_switch, switch


def _compile(source: str, *, extra=None):
    ns = {"switch": switch, "case": case}
    if extra:
        ns.update(extra)
    exec(compile(source, "<pyswitch-typed-partition-v184>", "exec"), ns)
    name = next(
        line.split("(", 1)[0].split()[1]
        for line in source.splitlines()
        if line.startswith("def ")
    )
    return enable_switch(
        mode="portable", case_key_mode="typed", source=source
    )(ns[name])


def test_single_exact_case_type_uses_partition_and_rejects_numeric_aliases():
    source = '''def classify(x):
    with switch(x):
        if case(0): return "zero"
        if case(1): return "one"
        if case(2): return "two"
        if case(): return "miss"
'''
    fn = _compile(source)
    assert fn.__pyswitch_typed_partition_plan_count__ == 1
    assert fn.__pyswitch_typed_partition_type_count__ == 1
    assert fn(0) == "zero"
    assert fn(2) == "two"
    assert fn(True) == "miss"
    assert fn(1.0) == "miss"


def test_exact_type_miss_still_executes_real_hash_once():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "one"
        if case(2): return "two"
        if case(): return "miss"
'''
    fn = _compile(source)
    events = []

    class Other:
        def __hash__(self):
            events.append("hash")
            return 1234567

        def __eq__(self, other):
            events.append("eq")
            return False

    assert fn(Other()) == "miss"
    assert events == ["hash"]


def test_exact_type_miss_intrinsic_unhashable_is_default_but_user_typeerror_propagates():
    source = '''def classify(x):
    with switch(x):
        if case(1): return 10
        if case(2): return 20
        if case(): return 30
'''
    fn = _compile(source)
    assert fn([]) == 30
    assert fn({}) == 30

    class RaisingHash:
        def __hash__(self):
            raise TypeError("typed partition hash exploded")

    with pytest.raises(TypeError, match="typed partition hash exploded"):
        fn(RaisingHash())


class _ProbeKey:
    def __init__(self, value, events):
        self.value = value
        self.events = events

    def __hash__(self):
        self.events.append(("hash", self.value))
        return 17

    def __eq__(self, other):
        self.events.append(("eq", self.value, getattr(other, "value", other)))
        return isinstance(other, _ProbeKey) and self.value == other.value


def test_matching_exact_type_preserves_raw_dict_hash_equality_event_sequence():
    case_events = []
    a = _ProbeKey("a", case_events)
    b = _ProbeKey("b", case_events)
    source = '''def classify(x):
    with switch(x):
        if case(A): return "a"
        if case(B): return "b"
        if case(): return "miss"
'''
    fn = _compile(source, extra={"A": a, "B": b})
    assert fn.__pyswitch_typed_partition_plan_count__ == 1

    expected_table = {a: "a", b: "b"}
    case_events.clear()
    expected_subject = _ProbeKey("b", case_events)
    expected = expected_table.get(expected_subject, "miss")
    expected_events = tuple(case_events)

    case_events.clear()
    actual_subject = _ProbeKey("b", case_events)
    actual = fn(actual_subject)
    assert actual == expected == "b"
    assert tuple(case_events) == expected_events


def test_subclass_is_exact_type_miss_and_does_not_run_case_equality():
    events = []

    class Base:
        def __init__(self, value):
            self.value = value

        def __hash__(self):
            events.append(("hash", self.value))
            return 11

        def __eq__(self, other):
            events.append(("eq", self.value, getattr(other, "value", other)))
            return isinstance(other, Base) and self.value == other.value

    class Sub(Base):
        pass

    a, b = Base("a"), Base("b")
    source = '''def classify(x):
    with switch(x):
        if case(A): return "a"
        if case(B): return "b"
        if case(): return "miss"
'''
    fn = _compile(source, extra={"A": a, "B": b})
    assert fn.__pyswitch_typed_partition_plan_count__ == 1
    events.clear()
    assert fn(Sub("a")) == "miss"
    assert events == [("hash", "a")]


def test_mixed_case_types_keep_exact_typed_semantics():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(1.0): return "float"
        if case(True): return "bool"
        if case(): return "miss"
'''
    fn = _compile(source)
    assert fn(1) == "int"
    assert fn(1.0) == "float"
    assert fn(True) == "bool"


def test_custom_metaclass_case_type_disables_partition():
    class Meta(type):
        def __hash__(cls):
            return type.__hash__(cls)

    class Key(metaclass=Meta):
        def __init__(self, value):
            self.value = value

        def __hash__(self):
            return hash(self.value)

        def __eq__(self, other):
            return isinstance(other, Key) and self.value == other.value

    a, b = Key("a"), Key("b")
    source = '''def classify(x):
    with switch(x):
        if case(A): return "a"
        if case(B): return "b"
        if case(): return "miss"
'''
    fn = _compile(source, extra={"A": a, "B": b})
    assert fn.__pyswitch_typed_partition_plan_count__ == 0
    assert fn(Key("a")) == "a"


def test_branchless_expression_template_keeps_stack_payload_transparency():
    source = '''def classify(x):
    with switch(x):
        if case(0): return x + 10
        if case(1): return x + 11
        if case(2): return x + 12
        if case(): return x + -1
'''
    fn = _compile(source)
    assert fn.__pyswitch_backend__ == "portable-expression-template-v18"
    assert fn.__pyswitch_typed_partition_plan_count__ == 1
    if sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 13):
        assert fn.__pyswitch_stack_payload_plan_count__ == 1
        assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
    assert fn(2) == 14
    assert fn(True) == 0
    assert fn(2.0) == 1.0


def test_branchless_statement_template_keeps_stack_payload_transparency():
    source = '''def classify(x):
    with switch(x):
        if case(0):
            y = x + 10
            y *= 2
            return y
        if case(1):
            y = x + 11
            y *= 2
            return y
        if case(2):
            y = x + 12
            y *= 2
            return y
        if case():
            y = x + -1
            y *= 2
            return y
'''
    fn = _compile(source)
    assert fn.__pyswitch_backend__ == "portable-statement-template-v18"
    assert fn.__pyswitch_typed_partition_plan_count__ == 1
    if sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 13):
        assert fn.__pyswitch_stack_payload_plan_count__ == 1
        assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
    assert fn(2) == 28
    assert fn(True) == 0
    assert fn(2.0) == 2.0


def test_balanced_guarded_plan_uses_same_type_partition_without_leaking_temporaries():
    observed = []

    def probe():
        frame_locals = sys._getframe(1).f_locals
        observed.append(tuple(name for name in frame_locals if name.startswith("__pyswitch_")))
        return True

    source = '''def classify(x):
    with switch(x):
        if case(1, when=probe()):
            return "one"
        if case(2):
            return "two"
        if case():
            return "miss"
'''
    fn = _compile(source, extra={"probe": probe})
    assert fn.__pyswitch_backend__ == "portable-balanced-v18"
    assert fn.__pyswitch_typed_partition_plan_count__ == 1
    assert fn(1) == "one"
    assert observed == [()]
    assert fn(True) == "miss"


def test_typed_partition_counts_are_exposed_in_transformation_report():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "one"
        if case(2): return "two"
        if case(): return "miss"
'''
    fn = _compile(source)
    details = fn.__python_extensions_report__.as_dict()
    assert details["typed_partition_plans"] == 1
    assert details["typed_partition_types"] == 1
