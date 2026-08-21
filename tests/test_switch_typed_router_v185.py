from __future__ import annotations

import sys

import pytest

from python_extensions import case, enable_switch, switch


def _compile(source: str, *, extra=None):
    ns = {"switch": switch, "case": case}
    if extra:
        ns.update(extra)
    exec(compile(source, "<pyswitch-typed-router-v185>", "exec"), ns)
    name = next(
        line.split("(", 1)[0].split()[1]
        for line in source.splitlines()
        if line.startswith("def ")
    )
    return enable_switch(
        mode="portable", case_key_mode="typed", source=source
    )(ns[name])


def test_mixed_builtin_types_use_one_multitype_router_plan():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(1.0): return "float"
        if case(True): return "bool"
        if case("1"): return "str"
        if case(): return "miss"
'''
    fn = _compile(source)
    assert fn.__pyswitch_typed_partition_plan_count__ == 1
    assert fn.__pyswitch_typed_partition_type_count__ == 4
    assert fn.__pyswitch_typed_router_plan_count__ == 1
    assert fn.__pyswitch_typed_router_type_count__ == 4
    assert fn(1) == "int"
    assert fn(1.0) == "float"
    assert fn(True) == "bool"
    assert fn("1") == "str"
    assert fn(False) == "miss"
    assert fn(b"1") == "miss"


def test_multitype_router_unknown_type_hashes_subject_once():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(1.5): return "float"
        if case(): return "miss"
'''
    fn = _compile(source)
    events = []

    class Other:
        def __hash__(self):
            events.append("hash")
            return 12345

        def __eq__(self, other):
            events.append(("eq", other))
            return False

    assert fn(Other()) == "miss"
    assert events == ["hash"]


def test_multitype_router_unknown_intrinsic_unhashable_is_default():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(1.5): return "float"
        if case(): return "miss"
'''
    fn = _compile(source)
    assert fn([]) == "miss"
    assert fn({}) == "miss"
    assert fn(set()) == "miss"


def test_multitype_router_unknown_user_hash_typeerror_propagates():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(1.5): return "float"
        if case(): return "miss"
'''
    fn = _compile(source)

    class BadHash:
        def __hash__(self):
            raise TypeError("router subject hash exploded")

    with pytest.raises(TypeError, match="router subject hash exploded"):
        fn(BadHash())


def test_multitype_router_custom_runtime_metaclass_hash_failure_propagates():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(1.5): return "float"
        if case(): return "miss"
'''
    fn = _compile(source)
    events = []

    class Meta(type):
        def __hash__(cls):
            events.append("type-hash")
            raise TypeError("router type hash exploded")

    class Other(metaclass=Meta):
        def __hash__(self):
            events.append("subject-hash")
            return 7

    with pytest.raises(TypeError, match="router type hash exploded"):
        fn(Other())
    assert events == ["type-hash"]


def test_multitype_partitions_prevent_cross_type_subject_equality():
    events = []

    class A:
        def __init__(self, value): self.value = value
        def __hash__(self):
            events.append(("hash-a", self.value))
            return 99
        def __eq__(self, other):
            if not isinstance(other, A):
                raise AssertionError("cross-type A equality must not run")
            events.append(("eq-a", self.value, other.value))
            return self.value == other.value

    class B:
        def __init__(self, value): self.value = value
        def __hash__(self):
            events.append(("hash-b", self.value))
            return 99
        def __eq__(self, other):
            if not isinstance(other, B):
                raise AssertionError("cross-type B equality must not run")
            events.append(("eq-b", self.value, other.value))
            return self.value == other.value

    a1, a2, b1, b2 = A(1), A(2), B(1), B(2)
    source = '''def classify(x):
    with switch(x):
        if case(A1): return "a1"
        if case(A2): return "a2"
        if case(B1): return "b1"
        if case(B2): return "b2"
        if case(): return "miss"
'''
    fn = _compile(source, extra={"A1": a1, "A2": a2, "B1": b1, "B2": b2})
    assert fn.__pyswitch_typed_partition_type_count__ == 2
    events.clear()
    assert fn(A(2)) == "a2"
    assert fn(B(1)) == "b1"


def test_any_custom_metaclass_case_type_disables_whole_router():
    class Meta(type):
        def __hash__(cls): return type.__hash__(cls)

    class Custom(metaclass=Meta):
        def __init__(self, value): self.value = value
        def __hash__(self): return hash(self.value)
        def __eq__(self, other):
            return isinstance(other, Custom) and self.value == other.value

    key = Custom("x")
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(KEY): return "custom"
        if case(): return "miss"
'''
    fn = _compile(source, extra={"KEY": key})
    assert fn.__pyswitch_typed_partition_plan_count__ == 0
    assert fn(1) == "int"
    assert fn(Custom("x")) == "custom"


def test_multitype_expression_template_preserves_stack_payload_transparency():
    source = '''def classify(x):
    with switch(x):
        if case(1): return x + 10
        if case(1.5): return x + 11
        if case(True): return x + 12
        if case(): return x + -1
'''
    fn = _compile(source)
    assert fn.__pyswitch_backend__ == "portable-expression-template-v18"
    assert fn.__pyswitch_typed_partition_type_count__ == 3
    if sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 13):
        assert fn.__pyswitch_stack_payload_plan_count__ == 1
        assert not any(name.startswith("__pyswitch_payload_") for name in fn.__code__.co_varnames)
    assert fn(1) == 11
    assert fn(1.5) == 12.5
    assert fn(True) == 13
    assert fn(2) == 1


def test_multitype_statement_template_preserves_stack_payload_transparency():
    source = '''def classify(x):
    with switch(x):
        if case(1):
            y = x + 10
            y *= 2
            return y
        if case(1.5):
            y = x + 11
            y *= 2
            return y
        if case(True):
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
    assert fn.__pyswitch_typed_partition_type_count__ == 3
    if sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 13):
        assert fn.__pyswitch_stack_payload_plan_count__ == 1
    assert fn(1) == 22
    assert fn(1.5) == 25.0
    assert fn(True) == 26
    assert fn(2) == 2


def test_multitype_guarded_balanced_route_keeps_user_frame_clean():
    observed = []
    def probe():
        observed.append(tuple(
            name for name in sys._getframe(1).f_locals
            if name.startswith("__pyswitch_")
        ))
        return True

    source = '''def classify(x):
    with switch(x):
        if case(1, when=probe()): return "int"
        if case(1.5): return "float"
        if case(True): return "bool"
        if case(): return "miss"
'''
    fn = _compile(source, extra={"probe": probe})
    assert fn.__pyswitch_backend__ == "portable-balanced-v18"
    assert fn.__pyswitch_typed_partition_type_count__ == 3
    assert fn(1) == "int"
    assert observed == [()]
    assert fn(1.5) == "float"


def test_six_type_router_scales_without_tuple_fallback():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(1.5): return "float"
        if case(True): return "bool"
        if case("x"): return "str"
        if case(b"x"): return "bytes"
        if case((1,)): return "tuple"
        if case(): return "miss"
'''
    fn = _compile(source)
    assert fn.__pyswitch_typed_partition_plan_count__ == 1
    assert fn.__pyswitch_typed_partition_type_count__ == 6
    expected = ["int", "float", "bool", "str", "bytes", "tuple", "miss"]
    values = [1, 1.5, True, "x", b"x", (1,), None]
    assert [fn(value) for value in values] == expected


def test_multitype_router_counts_flow_through_public_report():
    source = '''def classify(x):
    with switch(x):
        if case(1): return "int"
        if case(1.5): return "float"
        if case(True): return "bool"
        if case(): return "miss"
'''
    fn = _compile(source)
    details = fn.__python_extensions_report__.as_dict()
    assert details["typed_partition_plans"] == 1
    assert details["typed_partition_types"] == 3
    assert details["typed_router_plans"] == 1
    assert details["typed_router_types"] == 3
