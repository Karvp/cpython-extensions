"""Production-oriented switch compilation for CPython 3.13.

The default ``mode="auto"`` uses the semantics-safe portable compiler and
never mutates executable bytecode.  Version 18 specializes common switches
without moving user code out of its original frame:

* canonicalized direct-value tables for literal return/assignment switches;
* expression-template tables when routes share one expression shape; and
* exact-dictionary lookup plus a balanced inline route tree for arbitrary
  bodies, guards, fallthrough, and control flow.

The direct-value path is one bound ``dict.get`` on the normal hot path and is
average O(1) regardless of case count.  Table payloads are rebound to the
compiled function's real constant objects, preserving observable CPython
constant identity.  Typed-key mode preserves exact runtime type identity.  When every key in a
portable plan has one ordinary-metaclass exact type, the compiler routes that
type directly to a raw per-type dictionary and avoids allocating the historical
``(type(subject), subject)`` tuple.  Mixed-type and custom-metaclass plans keep
the conservative tuple-key representation.

Native ``match`` and out-of-line handler lowering are deliberately excluded
from automatic portable selection: both can change observable behavior for
valid Python programs.  The general fallback therefore keeps route bodies in
the caller frame and uses a balanced O(log r) route tree after the average
O(1) dictionary lookup, where ``r`` is the number of distinct route bodies.

Explicit CPython 3.13 live modes retain the self-modifying jump-table research
backend for controlled workloads.  They are opt-in because they mutate raw
interpreter bytecode and have stricter re-entry/threading constraints.  The
portable backend is the production default and supports closures, methods,
generators, coroutines, async generators, guards, qualified constants,
fallthrough, source injection, recursion, and concurrent calls.
"""

from __future__ import annotations

import ast
import __future__
import copy
import ctypes
import dis
import inspect
import linecache
import os
import platform
import sys
import struct
import textwrap
import threading
import types
import uuid
import warnings
import functools
from collections.abc import Callable, Hashable
from typing import Any, TypeVar, overload

from ._core import attach_report, make_report, verify_code

from ._version import __version__

__all__ = [
    "SwitchError", "SwitchSyntaxError", "DuplicateCaseError",
    "DuplicateDefaultError", "UnsupportedRuntimeError", "SwitchRangeError",
    "enable_switch", "switch", "case", "fallthrough", "__version__",
]

F = TypeVar("F", bound=Callable[..., Any])
_WORD = 2
_MAX_ARG = 0xFFFF
_JUMP_OPNAME = "JUMP_FORWARD"
_EXTENDED_ARG = dis.opmap.get("EXTENDED_ARG", -1)
_JUMP_OPCODE = dis.opmap.get(_JUMP_OPNAME, -1)
_CELL_TYPES = {
    1: ctypes.c_uint16,
    2: ctypes.c_uint32,
    4: ctypes.c_uint64,
}
_CELL_DUMMIES = {
    width: (ctypes.c_ubyte * (width * 2))()
    for width in _CELL_TYPES
}
_CDATA_POINTER_OFFSET = object.__basicsize__
_MAX_ARGS = {1: 0xFF, 2: 0xFFFF, 4: 0xFFFFFFFF}


class SwitchError(Exception):
    pass


class SwitchSyntaxError(SwitchError, SyntaxError):
    pass


class DuplicateCaseError(SwitchSyntaxError):
    pass


class DuplicateDefaultError(SwitchSyntaxError):
    pass


class UnsupportedRuntimeError(SwitchError, RuntimeError):
    pass


class SwitchRangeError(SwitchError):
    pass


class _SyntaxOnlySwitch:
    def __enter__(self) -> None:
        raise RuntimeError("switch() requires @enable_switch")

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


def switch(value: Any) -> _SyntaxOnlySwitch:
    del value
    return _SyntaxOnlySwitch()


def case(*values: Any, when: Any = True) -> bool:
    del values, when
    raise RuntimeError("case() requires @enable_switch")


def fallthrough() -> None:
    raise RuntimeError("fallthrough() requires @enable_switch")


def _live_runtime_reason() -> str | None:
    if platform.python_implementation() != "CPython":
        return "live backend requires CPython"
    if sys.version_info[:2] != (3, 13):
        return f"live backend requires CPython 3.13.x, got {sys.version.split()[0]}"
    if _EXTENDED_ARG < 0 or _JUMP_OPCODE < 0:
        return "required CPython jump opcodes are unavailable"
    if hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled():
        return "live backend is disabled on free-threaded CPython; use mode='portable'"
    return None


_LIVE_SELF_TESTED = False
_LIVE_SELF_TEST_ERROR: str | None = None
_LIVE_SELF_TEST_LOCK = threading.Lock()


def _self_test_live_layout() -> None:
    global _LIVE_SELF_TESTED, _LIVE_SELF_TEST_ERROR
    if _LIVE_SELF_TESTED:
        if _LIVE_SELF_TEST_ERROR is not None:
            raise UnsupportedRuntimeError(_LIVE_SELF_TEST_ERROR)
        return
    with _LIVE_SELF_TEST_LOCK:
        if _LIVE_SELF_TESTED:
            if _LIVE_SELF_TEST_ERROR is not None:
                raise UnsupportedRuntimeError(_LIVE_SELF_TEST_ERROR)
            return
        try:
            reason = _live_runtime_reason()
            if reason is not None:
                raise UnsupportedRuntimeError(reason)

            def probe() -> object:
                value = 731_927
                return value

            code = probe.__code__
            address = id(code) + type(code).__basicsize__
            public = code.co_code
            live = ctypes.string_at(address, len(public))
            if live != public:
                raise UnsupportedRuntimeError(
                    "CPython code-object layout self-test failed: "
                    "co_code_adaptive does not begin at "
                    "id(code)+type(code).__basicsize__"
                )

            load = next(
                (
                    i
                    for i in dis.get_instructions(probe, adaptive=False)
                    if i.opname == "LOAD_CONST" and i.argval == 731_927
                ),
                None,
            )
            if load is None or load.arg is None or load.arg > 0xFF:
                raise UnsupportedRuntimeError("unable to locate probe LOAD_CONST")
            none_index = code.co_consts.index(None)
            operand_address = address + load.offset + 1
            operand = ctypes.c_ubyte.from_address(operand_address)
            old = operand.value
            try:
                operand.value = none_index
                if probe() is not None:
                    raise UnsupportedRuntimeError(
                        "live-bytecode write self-test did not affect execution"
                    )
            finally:
                operand.value = old
            if probe() != 731_927:
                raise UnsupportedRuntimeError(
                    "live-bytecode write self-test failed to restore code"
                )

            # Validate the ctypes scalar rebinding primitive and native-endian
            # packed stores before it is ever used on a code object.  STORE_ATTR
            # on a scalar's ``value`` is materially faster than generic
            # pointer[0] STORE_SUBSCR on CPython 3.13.
            for width in _CELL_TYPES:
                raw = (ctypes.c_ubyte * (width * 2))()
                cell = _new_rebindable_cell(width)
                if ctypes.c_void_p.from_address(
                    id(cell) + _CDATA_POINTER_OFFSET
                ).value != ctypes.addressof(cell):
                    raise UnsupportedRuntimeError(
                        "ctypes CData layout self-test failed"
                    )
                _bind_cell(cell, ctypes.addressof(raw))
                sample_arg = min(0x55AA, _MAX_ARGS[width])
                encoded = _encode_jump(sample_arg, width)
                cell.value = encoded
                expected = encoded.to_bytes(width * 2, sys.byteorder)
                if bytes(raw) != expected:
                    raise UnsupportedRuntimeError(
                        f"ctypes scalar-store self-test failed for width {width}"
                    )
        except Exception as exc:
            _LIVE_SELF_TEST_ERROR = str(exc)
            _LIVE_SELF_TESTED = True
            if isinstance(exc, UnsupportedRuntimeError):
                raise
            raise UnsupportedRuntimeError(_LIVE_SELF_TEST_ERROR) from exc
        else:
            _LIVE_SELF_TEST_ERROR = None
            _LIVE_SELF_TESTED = True


def _require_runtime() -> None:
    _self_test_live_layout()


def _live_address(code: types.CodeType, byte_offset: int = 0) -> int:
    return id(code) + type(code).__basicsize__ + byte_offset


def _new_rebindable_cell(width: int) -> Any:
    """Create a non-owning ctypes scalar whose backing address may be rebound."""
    scalar_type = _CELL_TYPES[width]
    return scalar_type.from_address(ctypes.addressof(_CELL_DUMMIES[width]))


def _bind_cell(cell: Any, address: int) -> None:
    """Point a non-owning ctypes scalar at a live jump gate.

    ``ctypes`` exposes the scalar data address but not a public rebinding API.
    On the verified CPython 3.13 CData layout, the backing pointer immediately
    follows ``PyObject_HEAD``.  The runtime self-test validates this before any
    decorated function uses the optimization.
    """
    ctypes.c_void_p.from_address(id(cell) + _CDATA_POINTER_OFFSET).value = address


def _encode_jump(oparg: int, width: int) -> int:
    """Pack a 1-, 2-, or 4-code-unit jump in native byte order.

    ``_Py_CODEUNIT`` stores the opcode byte followed by the argument byte on
    every architecture.  Converting the exact byte sequence with the host byte
    order makes the integer suitable for a ctypes uint16/uint32/uint64 store on
    both little- and big-endian systems.
    """
    if width not in _MAX_ARGS:
        raise ValueError(f"unsupported gate width: {width}")
    if not 0 <= oparg <= _MAX_ARGS[width]:
        raise SwitchRangeError(
            f"jump argument {oparg} exceeds the {width * 8}-bit inline gate"
        )
    arg_bytes = [
        (oparg >> shift) & 0xFF
        for shift in range((width - 1) * 8, -1, -8)
    ]
    raw = bytearray()
    for byte in arg_bytes[:-1]:
        raw.extend((_EXTENDED_ARG, byte))
    raw.extend((_JUMP_OPCODE, arg_bytes[-1]))
    return int.from_bytes(raw, sys.byteorder)


def _choose_gate_width(gate_prefix: int, targets: list[int]) -> int:
    for width in (1, 2, 4):
        args = [_relative_arg(gate_prefix, target, width) for target in targets]
        if max(args, default=0) <= _MAX_ARGS[width]:
            return width
    raise SwitchRangeError("switch exceeds the 32-bit forward-jump range")


class _Plan:
    __slots__ = (
        "table_marker", "getter_marker", "pointer_marker", "default_marker", "gate_marker_a", "gate_marker_b",
        "gate_temp_a", "gate_temp_b", "index_name", "key_groups", "has_default",
        "case_markers", "fallback_marker", "target_temp", "extra_constants",
    )

    def __init__(
        self,
        table_marker: bytes,
        getter_marker: bytes,
        pointer_marker: bytes,
        default_marker: bytes,
        gate_marker_a: bytes,
        gate_marker_b: bytes,
        gate_temp_a: str,
        gate_temp_b: str,
        index_name: str,
        key_groups: list[tuple[Hashable, ...]],
        has_default: bool,
        case_markers: list[bytes],
        fallback_marker: bytes,
        target_temp: str,
        extra_constants: dict[bytes, Any] | None = None,
    ) -> None:
        self.table_marker = table_marker
        self.getter_marker = getter_marker
        self.pointer_marker = pointer_marker
        self.default_marker = default_marker
        self.gate_marker_a = gate_marker_a
        self.gate_marker_b = gate_marker_b
        self.gate_temp_a = gate_temp_a
        self.gate_temp_b = gate_temp_b
        self.index_name = index_name
        self.key_groups = key_groups
        self.has_default = has_default
        self.case_markers = case_markers
        self.fallback_marker = fallback_marker
        self.target_temp = target_temp
        self.extra_constants = extra_constants or {}


def _name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


_MARKER_MISSING = object()


def _static_reference_value(node: ast.AST, environment: dict[str, Any]) -> Any:
    """Resolve marker references without invoking arbitrary attribute hooks.

    Names are read from the decoration-time globals/closure environment. Qualified
    references are followed only through real module dictionaries, so forms such as
    ``python_extensions.switch`` or ``pe.case`` can be recognized without executing
    user ``__getattr__`` / descriptor code.
    """

    if isinstance(node, ast.Name):
        return environment.get(node.id, _MARKER_MISSING)
    if isinstance(node, ast.Attribute):
        owner = _static_reference_value(node.value, environment)
        if isinstance(owner, types.ModuleType):
            return vars(owner).get(node.attr, _MARKER_MISSING)
    return _MARKER_MISSING


def _marker_name(node: ast.AST, environment: dict[str, Any]) -> str | None:
    """Return the exact switch marker represented by *node*, if any.

    Identity-based resolution adds safe support for imported aliases and qualified
    public API references while retaining the historical bare-name syntax when the
    marker is introduced inside the function body and therefore is not yet present in
    the decoration-time environment.
    """

    value = _static_reference_value(node, environment)
    if value is switch:
        return "switch"
    if value is case:
        return "case"
    if value is fallthrough:
        return "fallthrough"
    if value is _MARKER_MISSING and isinstance(node, ast.Name):
        if node.id in {"switch", "case", "fallthrough"}:
            return node.id
    return None


def _validate_case_key_mode(mode: str) -> str:
    if mode not in {"python", "typed"}:
        raise ValueError("case_key_mode must be 'python' or 'typed'")
    return mode


def _case_identity(value: Hashable, mode: str) -> Hashable:
    return value if mode == "python" else (type(value), value)


def _hash_is_disabled(value: Any) -> bool:
    """Return whether Python's special-method lookup resolves ``__hash__`` to None.

    This deliberately bypasses user metaclass ``__getattribute__`` hooks.  It
    is used only after dictionary lookup raised ``TypeError`` so intrinsically
    unhashable switch subjects can behave as an unmatched value without
    swallowing a ``TypeError`` raised by a real user ``__hash__`` method.
    """
    cls = type(value)
    mro = type.__getattribute__(cls, "__mro__")
    for base in mro:
        namespace = type.__getattribute__(base, "__dict__")
        if "__hash__" in namespace:
            return namespace["__hash__"] is None
    return False


def _make_case_getter(table: dict[Hashable, Any], mode: str):
    """Return the bound C-level lookup callable used by the live gate.

    Exact-type key construction and intrinsically-unhashable recovery are
    emitted directly into the live function's bytecode, so the common hashable
    path does not pay an extra Python helper frame.
    """
    return table.get


def _portable_lookup_key(
    subject: ast.expr, mode: str, type_marker: bytes | None = None
) -> ast.expr:
    """Build the conservative portable lookup key.

    Typed plans may bypass this tuple construction when every case key has one
    proven safe exact type.  Mixed-type and custom-metaclass plans retain this
    representation as their conservative semantic path.
    """
    if mode == "python":
        return copy.deepcopy(subject)
    if type_marker is None:
        raise SwitchError("typed portable lookup is missing its type marker")
    return ast.Tuple(
        [
            ast.Call(ast.Constant(type_marker), [copy.deepcopy(subject)], []),
            copy.deepcopy(subject),
        ],
        ast.Load(),
    )


def _typed_partition_specs(
    table: dict[Hashable, Any], token: str, mode: str
) -> tuple[tuple[type, bytes, bytes, Any], ...]:
    """Select semantics-preserving allocation-free exact-type partitions.

    Typed dispatch historically builds ``(type(subject), subject)`` for every
    lookup.  Once exact runtime type identity has selected a partition, the
    outer tuple is redundant: a raw per-type dictionary preserves the
    subject's ordinary hash/equality behavior without allocating that tuple.

    We partition only when **every** case-key type object uses the ordinary
    ``type`` metaclass.  This keeps custom-metaclass plans on the conservative
    tuple-key route rather than introducing a new observable metaclass
    hash/equality path.  Mixed plans of ordinary exact types are safe to split
    into independent dictionaries because typed identity can never compare
    values across distinct exact types.

    A runtime subject whose exact type is absent from all partitions cannot
    match.  The generated miss still executes the subject's real ``hash()``
    once, preserving intrinsic-unhashable handling and genuine user hash
    failures, then takes the default without running impossible equality or
    allocating an outer tuple.
    """
    if mode != "typed" or not table:
        return ()

    partition_items: dict[type, list[tuple[Hashable, Any]]] = {}
    partition_order: list[type] = []
    for identity, payload in table.items():
        if not (isinstance(identity, tuple) and len(identity) == 2):
            return ()
        identity_type, raw_key = identity
        if not isinstance(identity_type, type) or type(identity_type) is not type:
            # Custom metaclasses can make type-object hashing/equality
            # observable.  Keep the whole plan on historical tuple routing.
            return ()
        items = partition_items.get(identity_type)
        if items is None:
            items = []
            partition_items[identity_type] = items
            partition_order.append(identity_type)
        items.append((raw_key, payload))

    partitions: list[tuple[type, bytes, bytes, Any]] = []
    for index, case_type in enumerate(partition_order):
        type_marker = (
            f"__pyswitch_partition_type_{token}_{index}"
        ).encode("ascii")
        getter_marker = (
            f"__pyswitch_partition_getter_{token}_{index}"
        ).encode("ascii")
        partitions.append(
            (case_type, type_marker, getter_marker, tuple(partition_items[case_type]))
        )
    return tuple(partitions)



def _typed_router_markers(
    token: str, typed_partitions: tuple[tuple[type, bytes, bytes, Any], ...]
) -> tuple[bytes | None, bytes | None]:
    if len(typed_partitions) <= 1:
        return None, None
    return (
        (f"__pyswitch_partition_router_{token}").encode("ascii"),
        (f"__pyswitch_partition_router_miss_{token}").encode("ascii"),
    )

def _portable_lookup_call(
    *,
    subject: ast.expr,
    mode: str,
    getter_marker: bytes,
    default: ast.expr,
    type_marker: bytes | None,
    typed_partitions: tuple[
        tuple[type, bytes, bytes, Any], ...
    ] = (),
    typed_miss_hash_marker: bytes | None = None,
    typed_router_getter_marker: bytes | None = None,
    typed_router_miss_getter_marker: bytes | None = None,
) -> ast.expr:
    """Build a bound-dict lookup with allocation-free typed routing.

    Single exact-type plans retain the 0.18.4 identity branch: a hit performs
    ``type(subject) is T`` and a raw per-type dict lookup, while a miss hashes
    the subject once and defaults.

    Multi-type plans whose case types all have the ordinary ``type`` metaclass
    use a type-router dictionary.  ``router.get(type(subject), empty.get)``
    selects a bound per-type getter in O(1), then that getter hashes/equates the
    subject exactly as an ordinary dictionary would.  A type-router miss calls
    the bound empty-dict getter, which still performs the subject's real hash
    once before returning the lookup default.  This avoids the historical
    ``(type(subject), subject)`` tuple allocation without a Python helper frame
    or generated routing local.
    """
    if mode != "typed" or not typed_partitions:
        return ast.Call(
            ast.Constant(getter_marker),
            [
                _portable_lookup_key(subject, mode, type_marker),
                copy.deepcopy(default),
            ],
            [],
        )
    if type_marker is None:
        raise SwitchError("typed portable lookup is missing its type marker")

    if len(typed_partitions) > 1:
        if (
            typed_router_getter_marker is None
            or typed_router_miss_getter_marker is None
        ):
            raise SwitchError("multi-type partition is missing its router marker")
        selected_getter = ast.Call(
            ast.Constant(typed_router_getter_marker),
            [
                ast.Call(
                    ast.Constant(type_marker),
                    [copy.deepcopy(subject)],
                    [],
                ),
                ast.Constant(typed_router_miss_getter_marker),
            ],
            [],
        )
        return ast.Call(
            selected_getter,
            [copy.deepcopy(subject), copy.deepcopy(default)],
            [],
        )

    if typed_miss_hash_marker is None:
        raise SwitchError("single-type partition is missing its miss-hash marker")
    fallback: ast.expr = ast.IfExp(
        test=ast.Compare(
            left=ast.Call(
                ast.Constant(typed_miss_hash_marker),
                [copy.deepcopy(subject)],
                [],
            ),
            ops=[ast.IsNot()],
            comparators=[ast.Constant(None)],
        ),
        body=copy.deepcopy(default),
        orelse=copy.deepcopy(default),
    )
    _case_type, partition_type_marker, partition_getter_marker, _table = (
        typed_partitions[0]
    )
    return ast.IfExp(
        test=ast.Compare(
            left=ast.Call(
                ast.Constant(type_marker), [copy.deepcopy(subject)], []
            ),
            ops=[ast.Is()],
            comparators=[ast.Constant(partition_type_marker)],
        ),
        body=ast.Call(
            ast.Constant(partition_getter_marker),
            [copy.deepcopy(subject), copy.deepcopy(default)],
            [],
        ),
        orelse=fallback,
    )



class _Transformer(ast.NodeTransformer):
    def __init__(self, func: Callable[..., Any], case_key_mode: str = "python") -> None:
        self.plans: list[_Plan] = []
        self.environment = _closure_environment(func)
        self.case_key_mode = _validate_case_key_mode(case_key_mode)
        self._root_seen = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if not self._root_seen:
            self._root_seen = True
            return self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        if not self._root_seen:
            self._root_seen = True
            return self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        return node

    def _visit_statements(self, statements: list[ast.stmt]) -> list[ast.stmt]:
        result: list[ast.stmt] = []
        for statement in statements:
            transformed = self.visit(statement)
            if transformed is None:
                continue
            if isinstance(transformed, list):
                result.extend(transformed)
            else:
                result.append(transformed)
        return result

    def visit_With(self, node: ast.With) -> ast.AST:
        if len(node.items) != 1:
            return self.generic_visit(node)
        call = node.items[0].context_expr
        if not (isinstance(call, ast.Call) and _marker_name(call.func, self.environment) == "switch"):
            return self.generic_visit(node)
        if (
            node.items[0].optional_vars is not None
            or len(call.args) != 1
            or call.keywords
        ):
            raise SwitchSyntaxError("use exactly: with switch(expression):")

        token = uuid.uuid4().hex
        table_marker = ("__pyswitch_table_" + token).encode("ascii")
        getter_marker = ("__pyswitch_getter_" + token).encode("ascii")
        pointer_marker = ("__pyswitch_pointer_" + token).encode("ascii")
        default_marker = ("__pyswitch_default_" + token).encode("ascii")
        gate_marker_a = ("__pyswitch_gate_a_" + token).encode("ascii")
        gate_marker_b = ("__pyswitch_gate_b_" + token).encode("ascii")
        gate_temp_a = f"__pyswitch_gate_a_{token}"
        gate_temp_b = f"__pyswitch_gate_b_{token}"
        index_name = f"__pyswitch_never_{token}"
        target_temp = f"__pyswitch_target_{token}"
        fallback_marker = ("__pyswitch_fallback_" + token).encode("ascii")
        type_marker = ("__pyswitch_type_" + token).encode("ascii")
        typeerror_marker = ("__pyswitch_typeerror_" + token).encode("ascii")
        unhashable_marker = ("__pyswitch_unhashable_" + token).encode("ascii")
        subject_temp = f"__pyswitch_subject_{token}"

        key_groups: list[tuple[Hashable, ...]] = []
        bodies: list[tuple[list[ast.stmt], ast.If]] = []
        seen: set[Hashable] = set()
        default_body: list[ast.stmt] | None = None

        for stmt in node.body:
            if not isinstance(stmt, ast.If) or stmt.orelse:
                raise SwitchSyntaxError(
                    "each direct switch member must be: if case(...):"
                )
            test = stmt.test
            if (
                not (isinstance(test, ast.Call) and _marker_name(test.func, self.environment) == "case")
                or test.keywords
            ):
                raise SwitchSyntaxError("invalid case syntax")
            if not test.args:
                if default_body is not None:
                    raise DuplicateDefaultError("duplicate default case")
                default_body = self._visit_statements(stmt.body) or [ast.Pass()]
                continue
            keys = tuple(_resolved_constant(arg, self.environment) for arg in test.args)
            for key in keys:
                identity = _case_identity(key, self.case_key_mode)
                if identity in seen:
                    raise DuplicateCaseError(f"duplicate case key: {key!r}")
                seen.add(identity)
            key_groups.append(keys)
            bodies.append(
                (self._visit_statements(stmt.body) or [ast.Pass()], stmt)
            )

        if not bodies and default_body is None:
            raise SwitchSyntaxError("empty switch")

        case_markers = [
            (f"__pyswitch_case_{token}_{i}").encode("ascii")
            for i in range(len(bodies))
        ]

        # Exact-dict fast path.  The subject is staged once so an exceptional
        # intrinsically-unhashable lookup can be classified without evaluating
        # the user's expression twice.  CPython 3.13 exception tables make the
        # normal path zero-cost; the getter itself remains bound ``dict.get``.
        pointer_target = ast.Attribute(
            value=ast.Constant(pointer_marker),
            attr="value",
            ctx=ast.Store(),
        )
        subject_expr = self.visit(call.args[0])
        subject_assign = ast.Assign(
            [ast.Name(subject_temp, ast.Store())],
            subject_expr,
        )
        ast.copy_location(subject_assign, node)

        subject_ref = ast.Name(subject_temp, ast.Load())
        if self.case_key_mode == "typed":
            lookup_key: ast.expr = ast.Tuple(
                [
                    ast.Call(
                        ast.Constant(type_marker),
                        [copy.deepcopy(subject_ref)],
                        [],
                    ),
                    copy.deepcopy(subject_ref),
                ],
                ast.Load(),
            )
        else:
            lookup_key = copy.deepcopy(subject_ref)

        patch_assign = ast.Assign(
            targets=[copy.deepcopy(pointer_target)],
            value=ast.Call(
                func=ast.Constant(getter_marker),
                args=[lookup_key, ast.Constant(default_marker)],
                keywords=[],
            ),
        )
        ast.copy_location(patch_assign, node)

        fallback_assign = ast.Assign(
            [copy.deepcopy(pointer_target)],
            ast.Constant(default_marker),
        )
        ast.copy_location(fallback_assign, node)
        cleanup_before_raise = ast.Delete([ast.Name(subject_temp, ast.Del())])
        ast.copy_location(cleanup_before_raise, node)
        unhashable_branch = ast.If(
            ast.Call(
                ast.Constant(unhashable_marker),
                [copy.deepcopy(subject_ref)],
                [],
            ),
            [fallback_assign],
            [cleanup_before_raise, ast.Raise(None, None)],
        )
        ast.copy_location(unhashable_branch, node)
        patch_stmt: ast.stmt = ast.Try(
            [patch_assign],
            [
                ast.ExceptHandler(
                    ast.Constant(typeerror_marker),
                    None,
                    [unhashable_branch],
                )
            ],
            [],
            [],
        )
        ast.copy_location(patch_stmt, node)
        cleanup_subject = ast.Delete([ast.Name(subject_temp, ast.Del())])
        ast.copy_location(cleanup_subject, node)

        # Reserve four consecutive code units without runtime padding:
        # LOAD_CONST/STORE_FAST + LOAD_CONST/STORE_FAST. Finalization overwrites
        # the leading 1, 2, or 4 units with a computed unconditional jump.
        gate_reserve = [
            ast.Assign(
                [ast.Name(gate_temp_a, ast.Store())],
                ast.Constant(gate_marker_a),
            ),
            ast.Assign(
                [ast.Name(gate_temp_b, ast.Store())],
                ast.Constant(gate_marker_b),
            ),
        ]
        for stmt in gate_reserve:
            ast.copy_location(stmt, node)

        # Flat compiler-only scaffold. Runtime dispatch jumps directly into a
        # selected body; MATCH avoids a recursively nested elif AST.
        match_cases: list[ast.match_case] = []
        for number, (body, original) in enumerate(bodies):
            marker_assign = ast.Assign(
                [ast.Name(target_temp, ast.Store())],
                ast.Constant(case_markers[number]),
            )
            ast.copy_location(marker_assign, original)
            match_cases.append(
                ast.match_case(
                    pattern=ast.MatchValue(ast.Constant(number)),
                    guard=None,
                    body=[marker_assign, *body],
                )
            )

        fallback_marker_assign = ast.Assign(
            [ast.Name(target_temp, ast.Store())],
            ast.Constant(fallback_marker),
        )
        ast.copy_location(fallback_marker_assign, node)
        fallback = [fallback_marker_assign, *(default_body or [ast.Pass()])]
        match_cases.append(ast.match_case(pattern=ast.MatchAs(), guard=None, body=fallback))
        scaffold = ast.Match(subject=ast.Name(index_name, ast.Load()), cases=match_cases)
        ast.copy_location(scaffold, node)

        self.plans.append(
            _Plan(
                table_marker,
                getter_marker,
                pointer_marker,
                default_marker,
                gate_marker_a,
                gate_marker_b,
                gate_temp_a,
                gate_temp_b,
                index_name,
                key_groups,
                default_body is not None,
                case_markers,
                fallback_marker,
                target_temp,
                extra_constants={
                    typeerror_marker: TypeError,
                    unhashable_marker: _hash_is_disabled,
                    **({type_marker: type} if self.case_key_mode == "typed" else {}),
                },
            )
        )
        return [subject_assign, patch_stmt, cleanup_subject, *gate_reserve, scaffold]


def _find_function(module: ast.Module, name: str):
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise SwitchError(f"cannot locate source AST for {name}")



def _contains_direct_self_call(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    class Finder(ast.NodeVisitor):
        found = False

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == fn_node.name:
                self.found = True
                return
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is fn_node:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is fn_node:
                self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    finder = Finder()
    finder.visit(fn_node)
    return finder.found

def _locate(
    code: types.CodeType, plan: _Plan
) -> tuple[int, list[int], int, int, int, int]:
    """Return gate prefix offset, body targets, fallback, and marker indexes."""
    ins = list(dis.get_instructions(code, show_caches=True, adaptive=False))
    getter_const_index = None
    pointer_const_index = None
    default_const_index = None
    gate_prefix = None

    for op in ins:
        if op.opname == "LOAD_CONST" and op.argval == plan.getter_marker:
            getter_const_index = op.arg
        elif op.opname == "LOAD_CONST" and op.argval == plan.pointer_marker:
            pointer_const_index = op.arg
        elif op.opname == "LOAD_CONST" and op.argval == plan.default_marker:
            default_const_index = op.arg

    for i, op in enumerate(ins):
        if op.opname != "LOAD_CONST" or op.argval != plan.gate_marker_a:
            continue
        if i + 3 >= len(ins):
            continue
        if (
            ins[i + 1].opname == "STORE_FAST"
            and ins[i + 1].argval == plan.gate_temp_a
            and ins[i + 2].opname == "LOAD_CONST"
            and ins[i + 2].argval == plan.gate_marker_b
            and ins[i + 3].opname == "STORE_FAST"
            and ins[i + 3].argval == plan.gate_temp_b
        ):
            gate_prefix = op.offset
            break

    if (
        gate_prefix is None
        or getter_const_index is None
        or pointer_const_index is None
        or default_const_index is None
    ):
        raise SwitchError(
            "failed to locate inline switch gate or constant markers"
        )

    marker_targets: dict[bytes, int] = {}
    wanted = {*plan.case_markers, plan.fallback_marker}
    for i, op in enumerate(ins):
        if op.opname != "LOAD_CONST" or op.argval not in wanted:
            continue
        j = i + 1
        while j < len(ins) and ins[j].opname == "CACHE":
            j += 1
        if (
            j >= len(ins)
            or ins[j].opname != "STORE_FAST"
            or ins[j].argval != plan.target_temp
        ):
            continue
        k = j + 1
        while k < len(ins) and ins[k].opname == "CACHE":
            k += 1
        if k >= len(ins):
            raise SwitchError("marker has no following case body")
        marker_targets[op.argval] = ins[k].offset

    missing = [m for m in [*plan.case_markers, plan.fallback_marker] if m not in marker_targets]
    if missing:
        raise SwitchError(f"failed to locate switch target markers: {len(missing)} missing")
    targets = [marker_targets[m] for m in plan.case_markers]
    fallback = marker_targets[plan.fallback_marker]
    return (
        gate_prefix,
        targets,
        fallback,
        getter_const_index,
        pointer_const_index,
        default_const_index,
    )


def _relative_arg(gate_prefix: int, target: int, width: int) -> int:
    # EXTENDED_ARG prefixes are part of the instruction; the relative base is
    # the code unit immediately after the final JUMP_FORWARD unit.
    arg = target // _WORD - (gate_prefix // _WORD + width)
    if arg < 0:
        raise SwitchError("switch target must be forward")
    return arg


def _source_for_function(func: Callable[..., Any], explicit_source: str | None = None) -> tuple[str, int]:
    """Return dedented source and its first physical line.

    ``explicit_source`` and ``__pyswitch_source__`` provide deterministic
    support for generated functions, notebooks, frozen modules, and custom
    loaders where ``inspect`` cannot recover source.
    """
    if explicit_source is None:
        explicit_source = getattr(func, "__pyswitch_source__", None)
    if explicit_source is not None:
        return textwrap.dedent(explicit_source), func.__code__.co_firstlineno
    try:
        lines, first_line = inspect.getsourcelines(func)
        return textwrap.dedent("".join(lines)), first_line
    except (OSError, IOError) as first_error:
        filename = func.__code__.co_filename
        cached = linecache.getlines(filename, func.__globals__)
        if cached:
            try:
                lines, first_line = inspect.findsource(func)
                block = inspect.getblock(lines[first_line:])
                return textwrap.dedent("".join(block)), first_line + 1
            except (OSError, IOError, IndexError):
                pass
        raise SwitchError(
            "@enable_switch requires retrievable source; pass source=... or "
            "set function.__pyswitch_source__ for generated/frozen code"
        ) from first_error


def _strip_definition_time_expressions(
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    """Avoid re-evaluating defaults and annotations while recompiling.

    They are restored from the original function object after compilation.
    This preserves object identity for mutable defaults and avoids repeating
    annotation side effects.
    """
    fn_node.decorator_list = []
    fn_node.returns = None
    fn_node.type_comment = None
    for arg in [
        *fn_node.args.posonlyargs,
        *fn_node.args.args,
        *fn_node.args.kwonlyargs,
    ]:
        arg.annotation = None
        arg.type_comment = None
    if fn_node.args.vararg is not None:
        fn_node.args.vararg.annotation = None
        fn_node.args.vararg.type_comment = None
    if fn_node.args.kwarg is not None:
        fn_node.args.kwarg.annotation = None
        fn_node.args.kwarg.type_comment = None
    fn_node.args.defaults = []
    fn_node.args.kw_defaults = [None] * len(fn_node.args.kwonlyargs)


def _future_flags(code: types.CodeType) -> int:
    flags = 0
    for feature_name in __future__.all_feature_names:
        flags |= code.co_flags & getattr(__future__, feature_name).compiler_flag
    return flags


def _execute_transformed_function(
    func: F,
    module: ast.Module,
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> F:
    """Compile a transformed definition while preserving original closure cells."""
    _strip_definition_time_expressions(fn_node)
    namespace = dict(func.__globals__)

    if func.__code__.co_freevars:
        factory_name = f"__pyswitch_factory_{uuid.uuid4().hex}"
        factory_args = ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg=name) for name in func.__code__.co_freevars],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        )
        factory = ast.FunctionDef(
            name=factory_name,
            args=factory_args,
            body=[fn_node, ast.Return(ast.Name(fn_node.name, ast.Load()))],
            decorator_list=[],
            returns=None,
            type_comment=None,
        )
        ast.copy_location(factory, fn_node)
        module.body = [factory]
    ast.fix_missing_locations(module)

    with warnings.catch_warnings():
        # Marker constants are replaced with runtime objects immediately after
        # compilation.  Some deliberately look invalid to the syntax-warning
        # pass (for example an identity comparison against a bytes marker), so
        # suppress SyntaxWarning only for this generated recompilation.  The
        # user's original definition has already been compiled independently.
        warnings.simplefilter("ignore", SyntaxWarning)
        compiled = compile(
            module,
            func.__code__.co_filename,
            "exec",
            flags=_future_flags(func.__code__),
            dont_inherit=True,
        )
    exec(compiled, namespace)

    if not func.__code__.co_freevars:
        generated = namespace[func.__name__]
    else:
        factory_func = namespace[factory_name]
        original_cells = dict(zip(func.__code__.co_freevars, func.__closure__ or ()))
        factory_values: list[Any] = []
        for cell in func.__closure__ or ():
            try:
                factory_values.append(cell.cell_contents)
            except ValueError:
                # Recursive nested functions have an intentionally empty self
                # cell while their decorator is running.  A placeholder is
                # sufficient to obtain the regenerated code object; the final
                # function receives the original cell itself.
                factory_values.append(None)
        temporary = factory_func(*factory_values)
        missing = set(temporary.__code__.co_freevars) - set(original_cells)
        if missing:
            raise SwitchError(
                "closure reconstruction failed; missing cells: "
                + ", ".join(sorted(missing))
            )
        closure_cells: list[Any] = []
        recursive_self_cells: list[Any] = []
        for name in temporary.__code__.co_freevars:
            cell = original_cells[name]
            try:
                contents = cell.cell_contents
            except ValueError:
                contents = None
            # Nested recursive functions capture their own binding in a closure
            # cell. Reusing that original cell makes recursive calls jump back
            # into the undecorated function. Give the transformed function a
            # private self cell instead; never mutate the user's original cell.
            if name == func.__name__ and contents is func:
                cell = types.CellType()
                recursive_self_cells.append(cell)
            closure_cells.append(cell)
        closure = tuple(closure_cells)
        generated = types.FunctionType(
            temporary.__code__,
            func.__globals__,
            func.__name__,
            func.__defaults__,
            closure,
        )
        for cell in recursive_self_cells:
            cell.cell_contents = generated
    generated.__defaults__ = func.__defaults__
    generated.__kwdefaults__ = (
        None if func.__kwdefaults__ is None else dict(func.__kwdefaults__)
    )
    return generated  # type: ignore[return-value]


def _rebind_recursive_self_closure(previous: F, replacement: F) -> None:
    """Point a private transformed self-cell at the newest callable layer.

    ``_execute_transformed_function`` creates a fresh cell for a nested
    function that closes over itself. Subsequent code-object replacement and
    wrapper construction share that private cell, so updating it here keeps
    recursive calls on the production-safe callable rather than bypassing the
    decorator/wrapper layer.
    """
    if previous.__closure__ is None:
        return
    for name, cell in zip(previous.__code__.co_freevars, previous.__closure__):
        if name != previous.__name__:
            continue
        try:
            contents = cell.cell_contents
        except ValueError:
            continue
        if contents is previous:
            cell.cell_contents = replacement




def _compile_portable_adaptive(
    func: F,
    *,
    explicit_source: str | None = None,
    match_threshold: int = 5,
    case_key_mode: str = "python",
    compact_routes: bool | str = False,
) -> F:
    source, _ = _source_for_function(func, explicit_source)
    case_key_mode = _validate_case_key_mode(case_key_mode)
    # Production semantics are defined by dictionary hash/equality behavior.
    # Native ``match`` uses equality without hashing and therefore disagrees for
    # custom/unhashable subjects, so the legacy native-match lowering was removed
    # from the production code path. Keep the threshold argument for API stability.
    del match_threshold
    return _compile_portable(
        func, explicit_source=source, case_key_mode=case_key_mode,
        compact_routes=compact_routes,
    )

class _PortablePlan:
    __slots__ = (
        "getter_marker", "table", "case_count", "kind", "extra_constants",
        "stack_payload_names", "typed_partitions",
        "typed_router_getter_marker", "typed_router_miss_getter_marker",
        "shared_continuation_statements", "auto_compact_estimated_bytes_saved",
        "auto_compact_used",
    )

    def __init__(
        self,
        getter_marker: bytes,
        table: dict[Hashable, Any],
        case_count: int,
        kind: str = "balanced",
        extra_constants: dict[bytes, Any] | None = None,
        stack_payload_names: tuple[str, ...] = (),
        typed_partitions: tuple[
            tuple[type, bytes, bytes, Any], ...
        ] = (),
        typed_router_getter_marker: bytes | None = None,
        typed_router_miss_getter_marker: bytes | None = None,
        shared_continuation_statements: int = 0,
        auto_compact_estimated_bytes_saved: int = 0,
        auto_compact_used: bool = False,
    ) -> None:
        self.getter_marker = getter_marker
        self.table = table
        self.case_count = case_count
        self.kind = kind
        self.extra_constants = extra_constants or {}
        self.stack_payload_names = stack_payload_names
        self.typed_partitions = typed_partitions
        self.typed_router_getter_marker = typed_router_getter_marker
        self.typed_router_miss_getter_marker = typed_router_miss_getter_marker
        self.shared_continuation_statements = shared_continuation_statements
        self.auto_compact_estimated_bytes_saved = auto_compact_estimated_bytes_saved
        self.auto_compact_used = auto_compact_used


def _shared_route_continuation(
    route_bodies: list[list[ast.stmt]],
) -> tuple[list[list[ast.stmt]], list[ast.stmt]]:
    """Extract the longest source-location-identical suffix shared by every route.

    General switch routes are frequently synthesized from lexical fallthrough.
    Several routes can therefore end in deep-copied copies of the *same* downstream
    statements, including identical source locations.  Hoisting that suffix after
    the generated route branch preserves caller-frame execution and trace locations
    while avoiding duplicated bytecode.  Requiring location-identical ASTs is
    intentionally stricter than ordinary route-body deduplication: separately
    written lookalike statements keep their own debugger/coverage lines.
    """

    if len(route_bodies) < 2 or any(not body for body in route_bodies):
        return route_bodies, []

    max_suffix = min(len(body) for body in route_bodies)
    count = 0
    for distance in range(1, max_suffix + 1):
        candidate = ast.dump(route_bodies[0][-distance], include_attributes=True)
        if not all(
            ast.dump(body[-distance], include_attributes=True) == candidate
            for body in route_bodies[1:]
        ):
            break
        count = distance

    if count == 0:
        return route_bodies, []

    suffix = copy.deepcopy(route_bodies[0][-count:])
    prefixes = [body[:-count] for body in route_bodies]
    return prefixes, suffix


def _shared_continuation_bytecode_size(shared: list[ast.stmt]) -> int | None:
    """Return a side-effect-free CPython bytecode-size proxy for a shared tail.

    The proxy compiles a synthetic function but never executes it, so case keys,
    calls, descriptors, and user protocols are not observed.  Context-dependent
    constructs whose code shape cannot be represented faithfully outside their
    original lexical container fail closed and keep the duplicated layout.
    """
    if not shared:
        return 0
    for statement in shared:
        for node in ast.walk(statement):
            if isinstance(
                node,
                (ast.Break, ast.Continue, ast.Await, ast.AsyncFor, ast.AsyncWith, ast.Nonlocal),
            ):
                return None
    fn = ast.FunctionDef(
        name="__pyswitch_compaction_probe",
        args=ast.arguments(
            posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[],
            kwarg=None, defaults=[]
        ),
        body=copy.deepcopy(shared),
        decorator_list=[],
        returns=None,
        type_comment=None,
    )
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    try:
        compiled = compile(module, "<pyswitch-compact-probe>", "exec", dont_inherit=True)
    except (SyntaxError, TypeError, ValueError):
        return None
    fn_code = next(
        (value for value in compiled.co_consts if isinstance(value, types.CodeType)), None
    )
    if fn_code is None:
        return None
    baseline_fn = ast.FunctionDef(
        name="__pyswitch_compaction_baseline",
        args=ast.arguments(
            posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[],
            kwarg=None, defaults=[]
        ),
        body=[ast.Pass()], decorator_list=[], returns=None, type_comment=None,
    )
    baseline_module = ast.Module(body=[baseline_fn], type_ignores=[])
    ast.fix_missing_locations(baseline_module)
    baseline_compiled = compile(
        baseline_module, "<pyswitch-compact-probe>", "exec", dont_inherit=True
    )
    baseline_code = next(
        value for value in baseline_compiled.co_consts if isinstance(value, types.CodeType)
    )
    return max(0, len(fn_code.co_code) - len(baseline_code.co_code))


def _shared_continuation_bytecode_gain(
    route_bodies: list[list[ast.stmt]], shared: list[ast.stmt]
) -> int | None:
    """Estimate duplicated code bytes removed by continuation compaction."""
    if len(route_bodies) < 2 or not shared:
        return 0
    body_bytes = _shared_continuation_bytecode_size(shared)
    if body_bytes is None:
        return None
    # Hoisting keeps one shared copy.  Reserve a small branch/join allowance so
    # auto mode activates only when density savings are material in machine code.
    duplicated = body_bytes * (len(route_bodies) - 1)
    join_allowance = 8 * max(1, len(route_bodies) - 1)
    return max(0, duplicated - join_allowance)


def _stackify_template_payloads(
    code: types.CodeType, payload_names: tuple[str, ...]
) -> tuple[types.CodeType, bool, str | None]:
    """Keep branchless template payloads on the CPython value stack.

    The AST compiler normally stages the selected per-route payload through a
    generated fast local so a narrow ``try`` can distinguish intrinsically
    unhashable subjects from user ``__hash__``/``__eq__`` ``TypeError``.  For
    straight-line branchless templates we can preserve that exact exception
    boundary while removing the observable fast local:

    * the successful ``dict.get`` result remains on the operand stack;
    * tuple payloads are unpacked once and remain stack-resident;
    * the intrinsic-unhashable fallback rotates all payloads below the active
      exception state before ``POP_EXCEPT``;
    * payload reads are scheduled at dynamic carrier depth: profitable final
      uses consume their carrier via short ``SWAP`` rotations, while deeper
      uses retain depth-correct ``COPY`` operations;
    * normal exits clean only carriers that remain after scheduling.

    The optimization is deliberately structural and fail-closed.  Branching,
    nested exception regions, suspension opcodes, cell/free-variable payloads,
    or an unexpected compiler shape retain the ordinary fast-local lowering.
    ``bytecode`` is imported lazily so the switch API remains usable from a
    source checkout where only the optional inline dependency is absent.
    """
    if not payload_names:
        return code, False, "no-payloads"
    # This transform emits CPython 3.13 operand-stack opcodes directly.  Keep
    # the public portable compiler cross-version by failing closed everywhere
    # else; the original fast-local template remains fully functional.
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 13):
        return code, False, "unsupported-stack-runtime"
    payload_set = set(payload_names)

    try:
        from bytecode import Bytecode, Instr, Label, TryBegin, TryEnd
    except ModuleNotFoundError as exc:
        if exc.name == "bytecode":
            return code, False, "bytecode-unavailable"
        raise

    try:
        bytecode = Bytecode.from_code(code)
    except Exception as exc:  # optimization-only: preserve the proven code
        return code, False, f"decode:{type(exc).__name__}"

    def instruction_payload_names(instruction: Any) -> tuple[str, ...]:
        if not isinstance(instruction, Instr):
            return ()
        argument = instruction.arg
        if isinstance(argument, str):
            return (argument,) if argument in payload_set else ()
        if isinstance(argument, tuple):
            return tuple(name for name in argument if name in payload_set)
        return ()

    def collect_store_groups() -> list[tuple[int, int, tuple[str, ...]]]:
        groups: list[tuple[int, int, tuple[str, ...]]] = []
        index = 0
        while index < len(bytecode):
            instruction = bytecode[index]
            names = instruction_payload_names(instruction)
            is_store = (
                isinstance(instruction, Instr)
                and instruction.name in {"STORE_FAST", "STORE_FAST_STORE_FAST"}
                and names
            )
            if not is_store:
                index += 1
                continue
            start = index
            collected: list[str] = []
            while index < len(bytecode):
                current = bytecode[index]
                current_names = instruction_payload_names(current)
                if not (
                    isinstance(current, Instr)
                    and current.name in {"STORE_FAST", "STORE_FAST_STORE_FAST"}
                    and current_names
                ):
                    break
                collected.extend(current_names)
                index += 1
            groups.append((start, index, tuple(collected)))
        return groups

    store_groups = collect_store_groups()
    if len(store_groups) != 2:
        return code, False, "store-shape"
    (normal_start, normal_end, normal_names), (fallback_start, fallback_end, fallback_names) = store_groups
    if normal_names != payload_names or fallback_names != payload_names:
        return code, False, "store-order"

    # The fallback STORE_FAST executes while an exception state is live.  Its
    # next runtime operations must be POP_EXCEPT and an unconditional jump to
    # the same join reached by the successful lookup.
    saw_pop_except = False
    join_label: Any = None
    for index in range(fallback_end, min(len(bytecode), fallback_end + 16)):
        instruction = bytecode[index]
        if isinstance(instruction, Label):
            break
        if isinstance(instruction, TryEnd):
            continue
        if not isinstance(instruction, Instr):
            continue
        if instruction.name == "POP_EXCEPT":
            saw_pop_except = True
            continue
        if saw_pop_except and instruction.is_uncond_jump():
            join_label = instruction.arg
            break
    if not isinstance(join_label, Label):
        return code, False, "fallback-join"
    join_index = bytecode.index(join_label)
    if not (normal_end <= join_index < fallback_start):
        return code, False, "join-order"

    # The only payload references outside the active template region must be
    # the two staging-store groups. Fused/cell instructions are intentionally
    # unsupported unless they are the recognized two-fast-local store fusion.
    store_indexes = set(range(normal_start, normal_end)) | set(range(fallback_start, fallback_end))
    for index, instruction in enumerate(bytecode):
        names = instruction_payload_names(instruction)
        if names and index not in store_indexes:
            if not (
                isinstance(instruction, Instr)
                and instruction.name in {"LOAD_FAST", "LOAD_FAST_LOAD_FAST", "DELETE_FAST"}
                and index > join_index
            ):
                return code, False, "unexpected-reference"

    stack_height = 0
    payload_replacements: dict[int, list[tuple[str, Any]]] = {}
    # Logical payload loads are collected first, then scheduled after the
    # straight-line stack shape is known.  A payload's final use can consume
    # the carrier itself instead of copying it and cleaning it later.
    # ``stack_height`` is the number of ordinary (non-carrier) values above
    # the carrier set immediately before that logical load.
    payload_load_events: list[tuple[int, int, str, int]] = []
    cleanup_names: list[str] = []
    active_try_begins: list[Any] = []
    terminal: tuple[str, int] | None = None
    forbidden = {
        "YIELD_VALUE", "YIELD_FROM", "SEND", "GET_AWAITABLE",
        "RETURN_GENERATOR", "ASYNC_GEN_WRAP",
    }
    for index in range(join_index + 1, len(bytecode)):
        instruction = bytecode[index]
        if isinstance(instruction, Label) or isinstance(instruction, TryEnd):
            continue
        if isinstance(instruction, TryBegin):
            active_try_begins.append(instruction)
            continue
        if not isinstance(instruction, Instr):
            continue
        if instruction.name in forbidden:
            return code, False, "suspension"
        if instruction.name == "LOAD_FAST" and instruction.arg in payload_set:
            # Defer the exact COPY depth until all last-use opportunities are
            # known: consuming an earlier carrier changes the physical depth of
            # every later carrier without changing the logical ordinary stack.
            payload_load_events.append((index, 0, instruction.arg, stack_height))
        elif instruction.name == "LOAD_FAST_LOAD_FAST":
            args = instruction.arg
            if not isinstance(args, tuple) or len(args) != 2:
                return code, False, "fused-load-shape"
            local_height = stack_height
            for subindex, name in enumerate(args):
                if name in payload_set:
                    payload_load_events.append(
                        (index, subindex, name, local_height)
                    )
                local_height += 1
            # Fused loads that contain payloads are expanded after scheduling;
            # payload-free fused instructions remain untouched.
            stack_height += 2
            continue
        elif instruction.name == "DELETE_FAST" and instruction.arg in payload_set:
            if stack_height != 0:
                return code, False, "cleanup-stack"
            cleanup_names.append(instruction.arg)
            if len(cleanup_names) == len(payload_names):
                if tuple(cleanup_names) != payload_names:
                    return code, False, "cleanup-order"
                terminal = ("delete", index)
                break
            continue
        elif instruction.name == "RETURN_VALUE":
            terminal = ("return", index)
            if stack_height != 1:
                return code, False, "return-stack"
            break
        elif instruction.is_final():
            return code, False, "final-opcode"
        if instruction.has_jump():
            return code, False, "branching-template"
        try:
            stack_height += instruction.stack_effect()
        except Exception:
            return code, False, "stack-effect"
        if stack_height < 0:
            return code, False, "negative-stack"

    if terminal is None:
        return code, False, "missing-cleanup"

    # Schedule payload reads against the *current* carrier stack.  The final
    # use of a carrier can be extracted in place with a rotation:
    #
    #     carrier, a, b   --SWAP 2; SWAP 3-->   a, b, carrier
    #
    # This has exactly the logical effect of LOAD_FAST/COPY from the template,
    # except the lower carrier is gone.  Restrict extraction to physical depth
    # 1 or 2: depth 1 costs no instruction, depth 2 costs one SWAP, and both
    # strictly beat one COPY plus the later cleanup.  Deeper rotations remain
    # on COPY because they are not a clear throughput win.  Consuming an early
    # carrier can make a later final use shallower, so schedule in execution
    # order rather than from the original static payload positions.
    last_event_for: dict[str, int] = {}
    for event_number, (_index, _subindex, name, _height) in enumerate(payload_load_events):
        last_event_for[name] = event_number

    remaining_payloads = list(payload_names)  # top carrier -> bottom carrier
    per_instruction_specs: dict[int, list[tuple[int, tuple[str, Any] | None]]] = {}
    for event_number, (index, subindex, name, logical_height) in enumerate(payload_load_events):
        try:
            carrier_position = remaining_payloads.index(name)
        except ValueError:
            return code, False, "consumed-payload-reused"
        depth = logical_height + carrier_position + 1
        is_last_use = last_event_for.get(name) == event_number
        specification: tuple[str, Any] | None
        max_profitable_depth = 2
        if terminal[0] == "return" and len(remaining_payloads) == 1:
            # Consuming the final return carrier also removes the shared
            # terminal SWAP, so depth three still saves one instruction:
            # two rotation SWAPs versus COPY + SWAP + POP.
            max_profitable_depth = 3
        if is_last_use and depth <= max_profitable_depth:
            # Move the carrier to the logical load result position while
            # preserving every intervening stack item.  Depth one is already
            # on top and therefore needs no opcode at all.
            if depth == 1:
                specification = None
            else:
                # SWAP 2, SWAP 3, ... rotates the selected carrier to TOS
                # while retaining the relative order of intervening values.
                specification = ("ROTATE_TO_TOP", depth)
            remaining_payloads.pop(carrier_position)
        else:
            specification = ("COPY", depth)
        per_instruction_specs.setdefault(index, []).append((subindex, specification))

    # Materialize replacement specifications.  A normal LOAD_FAST has one
    # logical component.  LOAD_FAST_LOAD_FAST is split in original argument
    # order, allowing either component to be a consumed carrier, an ordinary
    # local load, or a COPY.
    for index, entries in per_instruction_specs.items():
        instruction = bytecode[index]
        if not isinstance(instruction, Instr):
            return code, False, "payload-load-shape"
        if instruction.name == "LOAD_FAST":
            if len(entries) != 1 or entries[0][0] != 0:
                return code, False, "payload-load-count"
            spec = entries[0][1]
            if spec is None:
                payload_replacements[index] = []
            elif spec[0] == "ROTATE_TO_TOP":
                payload_replacements[index] = [
                    ("SWAP", depth) for depth in range(2, int(spec[1]) + 1)
                ]
            else:
                payload_replacements[index] = [spec]
            continue
        if instruction.name != "LOAD_FAST_LOAD_FAST":
            return code, False, "payload-fused-shape"
        args = instruction.arg
        if not isinstance(args, tuple) or len(args) != 2:
            return code, False, "fused-load-shape"
        by_subindex = {subindex: spec for subindex, spec in entries}
        replacement: list[tuple[str, Any]] = []
        for subindex, name in enumerate(args):
            if name in payload_set:
                spec = by_subindex.get(subindex)
                if subindex not in by_subindex:
                    return code, False, "fused-payload-missing"
                if spec is None:
                    pass
                elif spec[0] == "ROTATE_TO_TOP":
                    replacement.extend(
                        ("SWAP", depth)
                        for depth in range(2, int(spec[1]) + 1)
                    )
                else:
                    replacement.append(spec)
            else:
                replacement.append(("LOAD_FAST", name))
        payload_replacements[index] = replacement

    discard_all_unused_carriers = (
        not payload_load_events and len(remaining_payloads) == len(payload_names)
    )
    remaining_carrier_count = (
        0 if discard_all_unused_carriers else len(remaining_payloads)
    )

    # A surrounding ``try`` may begin exactly where the stack carrier becomes
    # live.  Its handler is safe when it is textually outside the template: an
    # exception unwinds to the original stack depth (dropping the carrier) and
    # control never needs the payload again.  An exception region whose handler
    # lands before the template cleanup could resume while the carrier is still
    # needed, so that more complex shape remains on the fast-local fallback.
    _, terminal_index = terminal
    for try_begin in active_try_begins:
        try:
            target_index = bytecode.index(try_begin.target)
        except ValueError:
            return code, False, "exception-target"
        if target_index <= terminal_index:
            return code, False, "nested-exception-region"

    # Replace the fallback store group before removing the earlier normal
    # group so indexes remain stable.  After UNPACK_SEQUENCE the exception
    # state is below N payloads. SWAP 2, SWAP 3, ... SWAP N+1 rotates that
    # exception state to the top without changing payload order; POP_EXCEPT
    # then exposes the same carrier stack as the successful lookup path.
    fallback_location = None
    for index in range(fallback_start, fallback_end):
        instruction = bytecode[index]
        if isinstance(instruction, Instr):
            fallback_location = instruction.location
            break
    swaps = [
        Instr("SWAP", depth, location=fallback_location)
        for depth in range(2, len(payload_names) + 2)
    ]
    bytecode[fallback_start:fallback_end] = swaps

    # The fallback replacement may change the textual indexes after it, but the
    # normal group is earlier than both the join and fallback handler. Remove it
    # next and compute original->current indexes from the two edit deltas.
    normal_removed = normal_end - normal_start
    del bytecode[normal_start:normal_end]
    fallback_delta = len(swaps) - (fallback_end - fallback_start)

    def shifted(index: int) -> int:
        actual = index
        if index >= normal_end:
            actual -= normal_removed
        if index >= fallback_end:
            actual += fallback_delta
        return actual

    terminal_kind, terminal_index = terminal
    actual_terminal = shifted(terminal_index)
    terminal_instruction = bytecode[actual_terminal]
    assert isinstance(terminal_instruction, Instr)
    if terminal_kind == "delete":
        # The terminal index points at the final DELETE_FAST. Replace the full
        # source-level cleanup sequence with pops only for carriers that were
        # not profitably consumed at their final use.
        first_cleanup_original = terminal_index - len(payload_names) + 1
        first_cleanup = shifted(first_cleanup_original)
        last_cleanup = shifted(terminal_index) + 1
        bytecode[first_cleanup:last_cleanup] = [
            Instr("POP_TOP", location=terminal_instruction.location)
            for _ in range(remaining_carrier_count)
        ]
    elif remaining_carrier_count:
        # RETURN_VALUE in CPython 3.13 requires the frame value stack to be
        # empty after popping the return value.  Move that value beneath the
        # carriers that remain, then discard them.  Fully consumed templates
        # need no terminal stack cleanup at all.
        cleanup_ops: list[Any] = [
            Instr(
                "SWAP",
                remaining_carrier_count + 1,
                location=terminal_instruction.location,
            )
        ]
        cleanup_ops.extend(
            Instr("POP_TOP", location=terminal_instruction.location)
            for _ in range(remaining_carrier_count)
        )
        bytecode[actual_terminal:actual_terminal] = cleanup_ops

    # Expand fused payload loads last and in reverse source order.  This keeps
    # all earlier original->current index calculations stable; replacements
    # before the already-rewritten terminal may grow by one instruction.
    for index, specification in sorted(payload_replacements.items(), reverse=True):
        actual = shifted(index)
        instruction = bytecode[actual]
        assert isinstance(instruction, Instr)
        replacement: list[Any] = []
        for opname, argument in specification:
            if opname in {"COPY", "SWAP"}:
                replacement.append(Instr(opname, argument, location=instruction.location))
            else:
                replacement.append(Instr("LOAD_FAST", argument, location=instruction.location))
        bytecode[actual:actual + 1] = replacement

    if discard_all_unused_carriers:
        # Identical route templates still perform the dictionary lookup so
        # custom hash/equality behavior remains observable, but their synthetic
        # identity payload is never read.  Drop such carriers immediately at
        # the join instead of carrying them through user code to RETURN_VALUE.
        current_join = bytecode.index(join_label)
        location = None
        for item in bytecode[current_join + 1:]:
            if isinstance(item, Instr):
                location = item.location
                break
        bytecode[current_join + 1:current_join + 1] = [
            Instr("POP_TOP", location=location) for _ in payload_names
        ]

    try:
        rewritten = bytecode.to_code()
    except Exception as exc:  # fail closed to the AST compiler result
        return code, False, f"encode:{type(exc).__name__}"
    if any(
        name in rewritten.co_varnames or name in rewritten.co_cellvars
        for name in payload_names
    ):
        return code, False, "payload-still-local"
    verification = verify_code(rewritten, raise_on_error=False)
    if not verification.valid:
        return code, False, "verify:" + (verification.errors[0] if verification.errors else "invalid")
    return rewritten, True, None


def _strip_synthetic_line_zero(
    code: types.CodeType,
) -> tuple[types.CodeType, int, str | None]:
    """Normalize compiler-synthetic locations preceding ``co_firstlineno``.

    CPython's AST location fixer can assign generated child nodes a synthetic
    source position one line before the decorated function.  That is line 0
    for a function starting at line 1, but line 47 for one starting at line 48.
    Reordering AST location repair can change CPython optimizer folding and the
    proven switch CFG shape, so preserve compilation and normalize only the
    resulting line table.
    """
    try:
        from bytecode import Bytecode, Instr
        from bytecode.instr import InstrLocation
    except ModuleNotFoundError as exc:
        if exc.name == "bytecode":
            return code, 0, "bytecode-unavailable"
        raise
    try:
        bytecode = Bytecode.from_code(code)
    except Exception as exc:
        return code, 0, f"decode:{type(exc).__name__}"
    removed = 0
    for instruction in bytecode:
        if not isinstance(instruction, Instr):
            continue
        location = instruction.location
        if not (
            location is not None
            and location.lineno is not None
            and location.lineno < code.co_firstlineno
        ):
            continue
        instruction.location = InstrLocation(
            code.co_firstlineno,
            code.co_firstlineno,
            None,
            None,
        )
        removed += 1
    if not removed:
        return code, 0, None
    try:
        rewritten = bytecode.to_code()
    except Exception as exc:
        return code, 0, f"encode:{type(exc).__name__}"
    verification = verify_code(rewritten, raise_on_error=False)
    if not verification.valid:
        # ``bytecode`` is an optional optimization aid here, never an authority
        # over CPython's already-valid compiler output.  Some exception-table
        # shapes (notably try/finally returns on 3.13) cannot be round-tripped by
        # all supported bytecode releases without corrupting unwind depths.
        # Preserve the original executable code rather than accepting an invalid
        # line-table cleanup rewrite.
        reason = verification.errors[0] if verification.errors else "invalid"
        return code, 0, f"verify:{reason}"
    return rewritten, removed, None


def _closure_environment(func: Callable[..., Any]) -> dict[str, Any]:
    result = dict(func.__globals__)
    if func.__closure__:
        for name, cell in zip(func.__code__.co_freevars, func.__closure__):
            try:
                result[name] = cell.cell_contents
            except ValueError:
                pass
    return result


def _is_name_or_attribute(node: ast.AST) -> bool:
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _resolved_constant(node: ast.expr, environment: dict[str, Any]) -> Hashable:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        if not _is_name_or_attribute(node):
            raise SwitchSyntaxError(
                "case keys must be literals or qualified constant names"
            ) from None
        expression = ast.Expression(copy.deepcopy(node))
        ast.fix_missing_locations(expression)
        try:
            value = eval(compile(expression, "<pyswitch-case>", "eval"), environment)
        except Exception as exc:
            raise SwitchSyntaxError("unable to resolve qualified case key") from exc
    try:
        hash(value)
    except TypeError as exc:
        raise SwitchSyntaxError(f"unhashable case key: {value!r}") from exc
    return value


def _is_fallthrough_statement(
    statement: ast.stmt, environment: dict[str, Any]
) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and _marker_name(statement.value.func, environment) == "fallthrough"
        and not statement.value.args
        and not statement.value.keywords
    )


def _invalid_fallthrough_in_body(
    statements: list[ast.stmt], environment: dict[str, Any]
) -> bool:
    """Return whether a case body contains unsupported fallthrough syntax.

    ``fallthrough()`` is a compiler marker, not an ordinary runtime call.  It
    is valid only as the final direct statement of a case body.  Rejecting
    buried/argument-bearing uses at decoration time prevents a surprising
    ``RuntimeError`` only on the rarely executed route.  Nested switch blocks
    own their fallthrough markers and are intentionally skipped here.
    """

    class Finder(ast.NodeVisitor):
        found = False

        def visit_Call(self, node: ast.Call) -> None:
            if _marker_name(node.func, environment) == "fallthrough":
                self.found = True
                return
            self.generic_visit(node)

        def visit_With(self, node: ast.With) -> None:
            # A nested switch is transformed independently and may contain its
            # own valid trailing fallthrough marker.
            if any(
                isinstance(item.context_expr, ast.Call)
                and _marker_name(item.context_expr.func, environment) == "switch"
                for item in node.items
            ):
                return
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    last = len(statements) - 1
    for index, statement in enumerate(statements):
        if index == last and _is_fallthrough_statement(statement, environment):
            continue
        finder = Finder()
        finder.visit(statement)
        if finder.found:
            return True
    return False


def _delete_locals(names: list[str]) -> ast.stmt | None:
    """Build one deletion statement for bound compiler temporaries."""
    if not names:
        return None
    return ast.Delete([ast.Name(name, ast.Del()) for name in names])


_NO_STABLE_LITERAL = object()
_CONSTANT_MISSING = object()


def _literal_constant_key(value: Any) -> tuple[Any, ...] | None:
    """Return a CPython-like identity key for compiler-safe literals.

    Equality alone is insufficient for constant pooling: ``1`` and ``True``
    are distinct constants, as are positive and negative floating zero.
    """
    value_type = type(value)
    if value is None:
        return (type(None),)
    if value is Ellipsis:
        return (type(Ellipsis),)
    if value_type in {bool, int, str, bytes}:
        return (value_type, value)
    if value_type is float:
        return (float, struct.pack("!d", value))
    if value_type is complex:
        return (
            complex,
            struct.pack("!d", value.real),
            struct.pack("!d", value.imag),
        )
    if value_type is tuple:
        parts: list[tuple[Any, ...]] = []
        for item in value:
            key = _literal_constant_key(item)
            if key is None:
                return None
            parts.append(key)
        return (tuple, tuple(parts))
    return None


def _register_compiler_constant(
    pool: dict[tuple[Any, ...], Any], value: Any
) -> None:
    key = _literal_constant_key(value)
    if key is None:
        return
    pool.setdefault(key, value)
    if type(value) is tuple:
        for item in value:
            _register_compiler_constant(pool, item)


def _canonicalize_compiler_literal(
    pool: dict[tuple[Any, ...], Any], value: Any
) -> Any:
    key = _literal_constant_key(value)
    if key is None:
        return value
    existing = pool.get(key, _CONSTANT_MISSING)
    if existing is not _CONSTANT_MISSING:
        return existing
    if type(value) is tuple:
        value = tuple(_canonicalize_compiler_literal(pool, item) for item in value)
        key = _literal_constant_key(value)
        assert key is not None
        existing = pool.get(key, _CONSTANT_MISSING)
        if existing is not _CONSTANT_MISSING:
            return existing
    pool[key] = value
    return value


def _stable_literal_value(node: ast.expr | None) -> Any:
    """Return a definition-time-stable literal, or a private failure marker.

    Only values that CPython itself may safely retain in ``co_consts`` are
    accepted.  Mutable literals such as lists, sets, and dictionaries must be
    rebuilt on every execution and therefore cannot participate in the direct
    value-table optimization.
    """
    if node is None:
        return None
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return _NO_STABLE_LITERAL

    def immutable(item: Any) -> bool:
        if item is None or item is Ellipsis:
            return True
        if isinstance(item, (bool, int, float, complex, str, bytes)):
            return True
        return isinstance(item, tuple) and all(immutable(part) for part in item)

    return value if immutable(value) else _NO_STABLE_LITERAL


_FRAME_SENSITIVE_HANDLER_CALLS = {
    "locals", "globals", "vars", "dir", "eval", "exec", "super",
    "breakpoint", "compile",
}


def _handler_expression_references(
    expression: ast.expr | None,
    *,
    allowed_locals: set[str],
    all_function_locals: set[str],
) -> set[str] | None:
    """Return caller locals needed by a safe out-of-line return expression.

    Handler-table dispatch is used only for terminal return expressions.  The
    selected expression executes in a tiny helper function, so constructs that
    observe or mutate the current frame are conservatively rejected.
    """
    if expression is None:
        return set()

    class Inspector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.references: set[str] = set()
            self.valid = True

        def visit_Name(self, node: ast.Name) -> None:
            if not self.valid:
                return
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.valid = False
                return
            if node.id in all_function_locals:
                if node.id not in allowed_locals:
                    self.valid = False
                    return
                self.references.add(node.id)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id in _FRAME_SENSITIVE_HANDLER_CALLS:
                self.valid = False
                return
            self.generic_visit(node)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            self.valid = False

        def visit_Await(self, node: ast.Await) -> None:
            self.valid = False

        def visit_Yield(self, node: ast.Yield) -> None:
            self.valid = False

        def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
            self.valid = False

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self.valid = False

        def visit_ListComp(self, node: ast.ListComp) -> None:
            self.valid = False

        def visit_SetComp(self, node: ast.SetComp) -> None:
            self.valid = False

        def visit_DictComp(self, node: ast.DictComp) -> None:
            self.valid = False

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
            self.valid = False

    inspector = Inspector()
    inspector.visit(expression)
    return inspector.references if inspector.valid else None


def _compile_return_handler(
    func: Callable[..., Any], expression: ast.expr | None, argument_names: list[str]
) -> Callable[..., Any]:
    body = ast.Constant(None) if expression is None else copy.deepcopy(expression)
    lambda_node = ast.Lambda(
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(name) for name in argument_names],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        ),
        body=body,
    )
    ast.copy_location(lambda_node, expression or ast.Constant(None))
    module = ast.Expression(lambda_node)
    ast.fix_missing_locations(module)
    code = compile(
        module,
        func.__code__.co_filename,
        "eval",
        flags=_future_flags(func.__code__),
        dont_inherit=True,
    )
    return eval(code, func.__globals__)


def _normalize_template_expression(expression: ast.expr | None) -> ast.expr:
    return ast.Constant(None) if expression is None else copy.deepcopy(expression)


def _unify_expression_template(
    expressions: list[ast.expr | None], token: str
) -> tuple[ast.expr, list[str], list[Any]] | None:
    """Build one caller-frame expression plus per-route literal payloads."""
    normalized = [_normalize_template_expression(expr) for expr in expressions]
    slot_names: list[str] = []
    slot_columns: list[list[Any]] = []

    class CannotUnify(Exception):
        pass

    def unify_values(values: list[Any]) -> Any:
        first = values[0]
        if all(value == first for value in values[1:]):
            return copy.deepcopy(first)
        raise CannotUnify

    def unify_nodes(nodes: list[ast.AST]) -> ast.AST:
        first = nodes[0]
        if all(
            ast.dump(node, include_attributes=False)
            == ast.dump(first, include_attributes=False)
            for node in nodes[1:]
        ):
            return copy.deepcopy(first)

        literal_values = [
            _stable_literal_value(node) if isinstance(node, ast.expr) else _NO_STABLE_LITERAL
            for node in nodes
        ]
        if all(value is not _NO_STABLE_LITERAL for value in literal_values):
            name = f"__pyswitch_payload_{token}_{len(slot_names)}"
            slot_names.append(name)
            slot_columns.append(literal_values)
            return ast.Name(name, ast.Load())

        if all(isinstance(node, ast.BinOp) for node in nodes):
            operators = [node.op for node in nodes]
            if all(isinstance(op, (ast.Add, ast.Sub)) for op in operators):
                left = unify_nodes([node.left for node in nodes])
                right = unify_nodes([node.right for node in nodes])
                if len({type(op) for op in operators}) == 1:
                    return ast.BinOp(left, copy.deepcopy(operators[0]), right)
                name = f"__pyswitch_payload_{token}_{len(slot_names)}"
                slot_names.append(name)
                slot_columns.append([isinstance(op, ast.Sub) for op in operators])
                test = ast.Name(name, ast.Load())
                return ast.IfExp(
                    test,
                    ast.BinOp(copy.deepcopy(left), ast.Sub(), copy.deepcopy(right)),
                    ast.BinOp(left, ast.Add(), right),
                )

        if any(type(node) is not type(first) for node in nodes[1:]):
            raise CannotUnify
        result = copy.deepcopy(first)
        for field, first_value in ast.iter_fields(first):
            field_values = [getattr(node, field) for node in nodes]
            if isinstance(first_value, ast.AST):
                setattr(result, field, unify_nodes(field_values))
            elif isinstance(first_value, list):
                if any(len(value) != len(first_value) for value in field_values[1:]):
                    raise CannotUnify
                combined: list[Any] = []
                for index, item in enumerate(first_value):
                    column = [value[index] for value in field_values]
                    if isinstance(item, ast.AST):
                        combined.append(unify_nodes(column))
                    else:
                        combined.append(unify_values(column))
                setattr(result, field, combined)
            else:
                setattr(result, field, unify_values(field_values))
        return result

    try:
        template = unify_nodes(normalized)
    except CannotUnify:
        return None
    assert isinstance(template, ast.expr)

    payloads: list[Any] = []
    if not slot_names:
        # Retain the lookup even when every expression is identical so custom
        # hashing/equality behavior is not silently skipped.
        slot_names.append(f"__pyswitch_payload_{token}_0")
        payloads = [None] * len(expressions)
    elif len(slot_names) == 1:
        payloads = list(slot_columns[0])
    else:
        payloads = [tuple(column[row] for column in slot_columns) for row in range(len(expressions))]
    return template, slot_names, payloads


def _unify_statement_sequence(
    bodies: list[list[ast.stmt]], token: str
) -> tuple[list[ast.stmt], list[str], list[Any]] | None:
    """Unify straight-line route bodies while varying only expression literals.

    This deliberately accepts a small, easy-to-audit statement vocabulary.
    Arbitrary control flow continues through the balanced compiler.  Identical
    statements are copied unchanged; differing Return/Expr/Assign/AugAssign
    values reuse the expression-template unifier.
    """
    if not bodies:
        return None
    width = len(bodies[0])
    if any(len(body) != width for body in bodies[1:]):
        return None

    template_body: list[ast.stmt] = []
    slot_names: list[str] = []
    row_values: list[list[Any]] = [[] for _ in bodies]

    def add_payloads(names: list[str], payloads: list[Any]) -> None:
        slot_names.extend(names)
        if len(names) == 1:
            for row, payload in zip(row_values, payloads):
                row.append(payload)
        else:
            for row, payload in zip(row_values, payloads):
                if not isinstance(payload, tuple) or len(payload) != len(names):
                    raise SwitchError("invalid statement-template payload shape")
                row.extend(payload)

    for position in range(width):
        statements = [body[position] for body in bodies]
        first = statements[0]
        first_dump = ast.dump(first, include_attributes=False)
        if all(
            ast.dump(statement, include_attributes=False) == first_dump
            for statement in statements[1:]
        ):
            # Keep statement templates genuinely straight-line.  Earlier
            # versions let arbitrary identical statements (for example try,
            # with, loops, or nested conditionals) bypass the documented
            # vocabulary through this equality shortcut.  Those constructs can
            # carry exception/control-flow edges across the payload lifetime,
            # so preserve them through the balanced caller-frame backend.
            if not isinstance(
                first, (ast.Return, ast.Expr, ast.Assign, ast.AugAssign, ast.Pass)
            ):
                return None
            template_body.append(copy.deepcopy(first))
            continue

        value_expressions: list[ast.expr | None]
        rebuilt = copy.deepcopy(first)
        if all(isinstance(statement, ast.Return) for statement in statements):
            value_expressions = [statement.value for statement in statements]
            unified = _unify_expression_template(
                value_expressions, f"{token}_stmt_{position}"
            )
            if unified is None:
                return None
            expression, names, payloads = unified
            assert isinstance(rebuilt, ast.Return)
            rebuilt.value = expression
        elif all(isinstance(statement, ast.Expr) for statement in statements):
            value_expressions = [statement.value for statement in statements]
            unified = _unify_expression_template(
                value_expressions, f"{token}_stmt_{position}"
            )
            if unified is None:
                return None
            expression, names, payloads = unified
            assert isinstance(rebuilt, ast.Expr)
            rebuilt.value = expression
        elif isinstance(first, ast.Assign) and all(
            isinstance(statement, ast.Assign)
            and len(statement.targets) == len(first.targets)
            for statement in statements
        ):
            target_shape = [
                ast.dump(target, include_attributes=False) for target in first.targets
            ]
            if any(
                [ast.dump(target, include_attributes=False) for target in statement.targets]
                != target_shape
                or statement.type_comment != first.type_comment
                for statement in statements[1:]
            ):
                return None
            value_expressions = [statement.value for statement in statements]
            unified = _unify_expression_template(
                value_expressions, f"{token}_stmt_{position}"
            )
            if unified is None:
                return None
            expression, names, payloads = unified
            rebuilt.value = expression
        elif all(isinstance(statement, ast.AugAssign) for statement in statements) and isinstance(first, ast.AugAssign):
            target_shape = ast.dump(first.target, include_attributes=False)
            operator_type = type(first.op)
            if any(
                ast.dump(statement.target, include_attributes=False) != target_shape
                or type(statement.op) is not operator_type
                for statement in statements[1:]
            ):
                return None
            value_expressions = [statement.value for statement in statements]
            unified = _unify_expression_template(
                value_expressions, f"{token}_stmt_{position}"
            )
            if unified is None:
                return None
            expression, names, payloads = unified
            rebuilt.value = expression
        else:
            return None

        add_payloads(names, payloads)
        template_body.append(rebuilt)

    if not slot_names:
        # Keep an actual hash/equality lookup even when all route bodies are
        # identical; custom key behavior remains observable.
        slot_names = [f"__pyswitch_payload_{token}_stmt_identity"]
        payloads = [None] * len(bodies)
    elif len(slot_names) == 1:
        payloads = [row[0] for row in row_values]
    else:
        payloads = [tuple(row) for row in row_values]
    return template_body, slot_names, payloads


class _PortableTransformer(ast.NodeTransformer):
    """Semantics-first switch compiler with no executable-memory mutation.

    It performs an average-O(1) dictionary lookup followed by a balanced
    O(log n) integer dispatch tree.  It is thread-safe, recursion-safe,
    portable, and supports guards/fallthrough/closures/suspendable functions.
    """

    def __init__(
        self, func: Callable[..., Any], case_key_mode: str = "python",
        *, compact_routes: bool | str = False,
    ) -> None:
        self.func = func
        self.case_key_mode = _validate_case_key_mode(case_key_mode)
        self.compact_routes = compact_routes
        self.plans: list[_PortablePlan] = []
        self.environment = _closure_environment(func)
        self.fast_local_names = set(func.__code__.co_varnames) - set(
            func.__code__.co_cellvars
        )
        self.parameter_names = list(inspect.signature(func).parameters)
        self.all_function_locals = (
            set(func.__code__.co_varnames)
            | set(func.__code__.co_cellvars)
            | set(func.__code__.co_freevars)
        )
        self._root_seen = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if not self._root_seen:
            self._root_seen = True
            return self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        if not self._root_seen:
            self._root_seen = True
            return self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        return node

    def _visit_statements(self, statements: list[ast.stmt]) -> list[ast.stmt]:
        output: list[ast.stmt] = []
        for statement in statements:
            transformed = self.visit(statement)
            if transformed is None:
                continue
            if isinstance(transformed, list):
                output.extend(transformed)
            else:
                output.append(transformed)
        return output

    def _prepare_subject(
        self,
        node: ast.With,
        context: ast.withitem,
        subject: ast.expr,
        subject_name: str,
    ) -> tuple[list[ast.stmt], ast.expr, str | None]:
        """Evaluate a switch subject once with the smallest possible footprint.

        A fast local can be read directly. If the user requested a simple
        ``as name`` alias, that user-visible binding itself can hold a complex
        subject expression and avoids an otherwise unnecessary compiler local.
        Complex alias targets still require a temporary so the subject can be
        reused for dispatch without re-evaluation.
        """
        if isinstance(subject, ast.Name) and subject.id in self.fast_local_names:
            subject_ref: ast.expr = ast.Name(subject.id, ast.Load())
            output: list[ast.stmt] = []
            if context.optional_vars is not None:
                alias_assign = ast.Assign(
                    [self.visit(copy.deepcopy(context.optional_vars))],
                    copy.deepcopy(subject_ref),
                )
                ast.copy_location(alias_assign, node)
                output.append(alias_assign)
            return output, subject_ref, None

        if isinstance(context.optional_vars, ast.Name):
            alias_target = self.visit(copy.deepcopy(context.optional_vars))
            alias_assign = ast.Assign([alias_target], copy.deepcopy(subject))
            ast.copy_location(alias_assign, node)
            return [alias_assign], ast.Name(context.optional_vars.id, ast.Load()), None

        subject_assign = ast.Assign(
            [ast.Name(subject_name, ast.Store())], copy.deepcopy(subject)
        )
        ast.copy_location(subject_assign, node)
        output = [subject_assign]
        subject_ref = ast.Name(subject_name, ast.Load())
        if context.optional_vars is not None:
            alias_assign = ast.Assign(
                [self.visit(copy.deepcopy(context.optional_vars))],
                copy.deepcopy(subject_ref),
            )
            ast.copy_location(alias_assign, node)
            output.append(alias_assign)
        return output, subject_ref, subject_name

    def _flatten_member_chain(
        self, statement: ast.If
    ) -> tuple[list[ast.If], list[ast.stmt] | None]:
        clauses: list[ast.If] = []
        current = statement
        while True:
            clauses.append(current)
            if not current.orelse:
                return clauses, None
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                current = current.orelse[0]
                continue
            return clauses, current.orelse

    def _balanced_dispatch(
        self, index_name: str, bodies: list[list[ast.stmt]], lo: int, hi: int
    ) -> list[ast.stmt]:
        if hi - lo == 1:
            return bodies[lo]
        mid = (lo + hi) // 2
        return [
            ast.If(
                test=ast.Compare(
                    left=ast.Name(index_name, ast.Load()),
                    ops=[ast.Lt()],
                    comparators=[ast.Constant(mid)],
                ),
                body=self._balanced_dispatch(index_name, bodies, lo, mid),
                orelse=self._balanced_dispatch(index_name, bodies, mid, hi),
            )
        ]

    def _direct_value_dispatch(
        self,
        *,
        node: ast.With,
        context: ast.withitem,
        subject: ast.expr,
        raw_clauses: list[
            tuple[tuple[Hashable, ...] | None, ast.expr | None, list[ast.stmt], bool]
        ],
        expanded_bodies: list[list[ast.stmt]],
        explicit_default: list[ast.stmt] | None,
        default_guard: ast.expr | None,
    ) -> list[ast.stmt] | None:
        """Collapse literal terminal actions to one canonicalized dict lookup.

        Route payload objects are canonicalized after compilation against the
        function's real constant pool. This preserves CPython constant identity
        while retaining the one-``dict.get`` hot path of the original fast
        implementation. Assignment targets execute outside the lookup ``try``
        so a user descriptor/subscript ``TypeError`` is never misclassified as
        an unhashable switch subject.
        """
        if explicit_default is None or default_guard is not None:
            return None
        if any(guard is not None or fall for _keys, guard, _body, fall in raw_clauses):
            return None
        if len(explicit_default) != 1 or any(len(body) != 1 for body in expanded_bodies):
            return None

        default_statement = explicit_default[0]
        action_kind: str
        assignment_target: ast.expr | None = None
        if isinstance(default_statement, ast.Return):
            action_kind = "return"
            default_value = _stable_literal_value(default_statement.value)
        elif isinstance(default_statement, ast.Assign) and len(default_statement.targets) == 1:
            action_kind = "assign"
            assignment_target = default_statement.targets[0]
            default_value = _stable_literal_value(default_statement.value)
        else:
            return None
        if default_value is _NO_STABLE_LITERAL:
            return None

        route_values: list[Any] = []
        target_dump = (
            ast.dump(assignment_target, include_attributes=False)
            if assignment_target is not None
            else None
        )
        for body in expanded_bodies:
            statement = body[0]
            if action_kind == "return":
                if not isinstance(statement, ast.Return):
                    return None
                value = _stable_literal_value(statement.value)
            else:
                if not (
                    isinstance(statement, ast.Assign)
                    and len(statement.targets) == 1
                    and ast.dump(statement.targets[0], include_attributes=False) == target_dump
                ):
                    return None
                value = _stable_literal_value(statement.value)
            if value is _NO_STABLE_LITERAL:
                return None
            route_values.append(value)

        table: dict[Hashable, Any] = {}
        seen: set[Hashable] = set()
        for (keys, _guard, _body, _fall), value in zip(raw_clauses, route_values):
            assert keys is not None
            for key in keys:
                identity = _case_identity(key, self.case_key_mode)
                if identity in seen:
                    raise DuplicateCaseError(f"unreachable duplicate case key: {key!r}")
                seen.add(identity)
                table[identity] = value

        token = uuid.uuid4().hex
        subject_name = f"__pyswitch_subject_{token}"
        payload_name = f"__pyswitch_payload_{token}"
        getter_marker = (f"__pyswitch_direct_getter_{token}").encode("ascii")
        type_marker = (f"__pyswitch_type_{token}").encode("ascii")
        typeerror_marker = (f"__pyswitch_typeerror_{token}").encode("ascii")
        statements, subject_ref, subject_temp_name = self._prepare_subject(
            node, context, subject, subject_name
        )
        typed_partitions = _typed_partition_specs(
            table, token, self.case_key_mode
        )
        typed_miss_hash_marker = (
            (f"__pyswitch_partition_hash_{token}").encode("ascii")
            if len(typed_partitions) == 1 else None
        )
        typed_router_getter_marker, typed_router_miss_getter_marker = (
            _typed_router_markers(token, typed_partitions)
        )

        lookup = _portable_lookup_call(
            subject=subject_ref,
            mode=self.case_key_mode,
            getter_marker=getter_marker,
            default=ast.Constant(default_value),
            type_marker=type_marker,
            typed_partitions=typed_partitions,
            typed_miss_hash_marker=typed_miss_hash_marker,
            typed_router_getter_marker=typed_router_getter_marker,
            typed_router_miss_getter_marker=typed_router_miss_getter_marker,
        )
        direct_local_assignment = (
            action_kind == "assign" and isinstance(assignment_target, ast.Name)
        )
        if action_kind == "return":
            fast_lookup: ast.stmt = ast.Return(lookup)
            unhashable_action: ast.stmt = ast.Return(ast.Constant(default_value))
            trailing_action: ast.stmt | None = None
        elif direct_local_assignment:
            # A simple fast-local STORE_FAST cannot raise.  Assign the lookup
            # result directly so the common assignment form needs neither a
            # compiler payload local nor its DELETE_FAST cleanup.  Attribute,
            # subscript, and destructuring targets stay staged outside the
            # lookup try because their user code can raise TypeError/ValueError.
            assert assignment_target is not None
            fast_lookup = ast.Assign([copy.deepcopy(assignment_target)], lookup)
            unhashable_action = ast.Assign(
                [copy.deepcopy(assignment_target)], ast.Constant(default_value)
            )
            trailing_action = None
        else:
            fast_lookup = ast.Assign(
                [ast.Name(payload_name, ast.Store())], lookup
            )
            unhashable_action = ast.Assign(
                [ast.Name(payload_name, ast.Store())], ast.Constant(default_value)
            )
            assert assignment_target is not None
            trailing_action = ast.Assign(
                [copy.deepcopy(assignment_target)], ast.Name(payload_name, ast.Load())
            )

        unhashable_marker = (f"__pyswitch_unhashable_{token}").encode("ascii")
        unhashable = ast.If(
            ast.Call(
                ast.Constant(unhashable_marker),
                [copy.deepcopy(subject_ref)],
                [],
            ),
            [unhashable_action],
            [ast.Raise(None, None)],
        )
        try_lookup = ast.Try(
            [fast_lookup],
            [ast.ExceptHandler(ast.Constant(typeerror_marker), None, [unhashable])],
            [],
            [],
        )
        ast.copy_location(try_lookup, node)
        statements.append(try_lookup)
        if action_kind == "assign" and subject_temp_name is not None:
            cleanup_subject = _delete_locals([subject_temp_name])
            assert cleanup_subject is not None
            ast.copy_location(cleanup_subject, node)
            statements.append(cleanup_subject)
        if trailing_action is not None:
            statements.append(trailing_action)
            cleanup_payload = _delete_locals([payload_name])
            assert cleanup_payload is not None
            ast.copy_location(cleanup_payload, node)
            statements.append(cleanup_payload)
        for statement in statements:
            ast.copy_location(statement, node)

        self.plans.append(
            _PortablePlan(
                getter_marker,
                table,
                len(table),
                kind="direct-value",
                extra_constants={
                    **({type_marker: type} if self.case_key_mode == "typed" else {}),
                    **({typed_miss_hash_marker: hash} if typed_miss_hash_marker is not None else {}),
                    typeerror_marker: TypeError,
                    unhashable_marker: _hash_is_disabled,
                },
                typed_partitions=typed_partitions,
                typed_router_getter_marker=typed_router_getter_marker,
                typed_router_miss_getter_marker=typed_router_miss_getter_marker,
            )
        )
        return statements

    def _return_handler_dispatch(
        self,
        *,
        node: ast.With,
        context: ast.withitem,
        subject: ast.expr,
        raw_clauses: list[
            tuple[tuple[Hashable, ...] | None, ast.expr | None, list[ast.stmt], bool]
        ],
        expanded_bodies: list[list[ast.stmt]],
        explicit_default: list[ast.stmt] | None,
        default_guard: ast.expr | None,
    ) -> list[ast.stmt] | None:
        """Use an O(1) handler table for safe terminal return expressions."""
        if (
            inspect.isgeneratorfunction(self.func)
            or inspect.iscoroutinefunction(self.func)
            or inspect.isasyncgenfunction(self.func)
            or explicit_default is None
            or default_guard is not None
            or any(
                guard is not None or fall
                for _keys, guard, _body, fall in raw_clauses
            )
            or len(explicit_default) != 1
            or any(len(body) != 1 for body in expanded_bodies)
        ):
            return None

        statements_to_compile = [body[0] for body in expanded_bodies]
        default_statement = explicit_default[0]
        if not isinstance(default_statement, ast.Return) or not all(
            isinstance(statement, ast.Return)
            for statement in statements_to_compile
        ):
            return None

        alias_name: str | None = None
        if context.optional_vars is not None:
            if not isinstance(context.optional_vars, ast.Name):
                return None
            alias_name = context.optional_vars.id

        allowed_locals = set(self.parameter_names)
        all_locals = set(self.all_function_locals)
        if alias_name is not None:
            allowed_locals.add(alias_name)
            all_locals.add(alias_name)

        expressions = [
            statement.value for statement in statements_to_compile
        ] + [default_statement.value]
        references: set[str] = set()
        for expression in expressions:
            needed = _handler_expression_references(
                expression,
                allowed_locals=allowed_locals,
                all_function_locals=all_locals,
            )
            if needed is None:
                return None
            references.update(needed)

        argument_names = [
            name for name in self.parameter_names if name in references
        ]
        if alias_name is not None and alias_name in references:
            argument_names.append(alias_name)

        handler_cache: dict[str, Callable[..., Any]] = {}

        def handler_for(expression: ast.expr | None) -> Callable[..., Any]:
            key = "<none>" if expression is None else ast.dump(
                expression, include_attributes=False
            )
            handler = handler_cache.get(key)
            if handler is None:
                handler = _compile_return_handler(
                    self.func, expression, argument_names
                )
                handler_cache[key] = handler
            return handler

        route_handlers = [
            handler_for(statement.value) for statement in statements_to_compile
        ]
        default_handler = handler_for(default_statement.value)

        table: dict[Hashable, Callable[..., Any]] = {}
        seen: set[Hashable] = set()
        for (keys, _guard, _body, _fall), handler in zip(
            raw_clauses, route_handlers
        ):
            assert keys is not None
            for key in keys:
                identity = _case_identity(key, self.case_key_mode)
                if identity in seen:
                    raise DuplicateCaseError(
                        f"unreachable duplicate case key: {key!r}"
                    )
                seen.add(identity)
                table[identity] = handler

        token = uuid.uuid4().hex
        subject_name = f"__pyswitch_subject_{token}"
        getter_marker = (f"__pyswitch_handler_getter_{token}").encode("ascii")
        default_marker = (f"__pyswitch_default_handler_{token}").encode("ascii")

        output, subject_ref, subject_temp_name = self._prepare_subject(
            node, context, subject, subject_name
        )

        handler_lookup = ast.Call(
            ast.Constant(getter_marker),
            [copy.deepcopy(subject_ref), ast.Constant(default_marker)],
            [],
        )
        call_arguments = [ast.Name(name, ast.Load()) for name in argument_names]
        fast_return = ast.Return(ast.Call(handler_lookup, call_arguments, []))
        fallback_return = ast.Return(
            ast.Call(ast.Constant(default_marker), copy.deepcopy(call_arguments), [])
        )
        unhashable = ast.If(
            ast.Compare(
                ast.Attribute(
                    ast.Call(
                        ast.Constant(type_marker),
                        [copy.deepcopy(subject_ref)],
                        [],
                    ),
                    "__hash__",
                    ast.Load(),
                ),
                [ast.Is()],
                [ast.Constant(None)],
            ),
            [fallback_return],
            [ast.Raise(None, None)],
        )
        lookup = ast.Try(
            [fast_return],
            [ast.ExceptHandler(ast.Constant(typeerror_marker), None, [unhashable])],
            [],
            [],
        )
        ast.copy_location(lookup, node)
        output.append(lookup)
        for statement in output:
            ast.copy_location(statement, node)

        self.plans.append(
            _PortablePlan(
                getter_marker,
                table,
                len(table),
                kind="return-handler",
                extra_constants={default_marker: default_handler},
            )
        )
        return output

    def _return_template_dispatch(
        self,
        *,
        node: ast.With,
        context: ast.withitem,
        subject: ast.expr,
        raw_clauses: list[
            tuple[tuple[Hashable, ...] | None, ast.expr | None, list[ast.stmt], bool]
        ],
        expanded_bodies: list[list[ast.stmt]],
        explicit_default: list[ast.stmt] | None,
        default_guard: ast.expr | None,
    ) -> list[ast.stmt] | None:
        """Lower same-shape route expressions to one O(1) payload lookup.

        The fastest form also unifies the default and is branchless after the
        table lookup.  If the default has a different shape (or is absent), a
        private miss sentinel selects the original default body while matched
        routes still execute the shared expression in the caller's frame.
        """
        if (
            default_guard is not None
            or any(
                guard is not None or fall
                for _keys, guard, _body, fall in raw_clauses
            )
            or not expanded_bodies
            or any(len(body) != 1 for body in expanded_bodies)
        ):
            return None

        token = uuid.uuid4().hex
        route_statements = [body[0] for body in expanded_bodies]
        assignment_target: ast.expr | None = None
        if all(isinstance(statement, ast.Return) for statement in route_statements):
            action_kind = "return"
            route_expressions = [statement.value for statement in route_statements]
        elif all(
            isinstance(statement, ast.Assign) and len(statement.targets) == 1
            for statement in route_statements
        ):
            target_dump = ast.dump(
                route_statements[0].targets[0], include_attributes=False
            )
            if any(
                ast.dump(statement.targets[0], include_attributes=False)
                != target_dump
                for statement in route_statements[1:]
            ):
                return None
            action_kind = "assign"
            assignment_target = route_statements[0].targets[0]
            route_expressions = [statement.value for statement in route_statements]
        else:
            return None

        # Prefer the branchless form when the default is a compatible single
        # action.  Otherwise unify only the matched routes and retain the exact
        # original default body behind a private miss sentinel.
        unified: tuple[ast.expr, list[str], list[Any]] | None = None
        branchless_default = False
        if explicit_default is not None and len(explicit_default) == 1:
            default_statement = explicit_default[0]
            if action_kind == "return" and isinstance(default_statement, ast.Return):
                unified = _unify_expression_template(
                    route_expressions + [default_statement.value], token
                )
            elif (
                action_kind == "assign"
                and isinstance(default_statement, ast.Assign)
                and len(default_statement.targets) == 1
                and assignment_target is not None
                and ast.dump(default_statement.targets[0], include_attributes=False)
                == ast.dump(assignment_target, include_attributes=False)
            ):
                unified = _unify_expression_template(
                    route_expressions + [default_statement.value], token
                )
            branchless_default = unified is not None

        if unified is None:
            unified = _unify_expression_template(route_expressions, token)
        if unified is None:
            return None
        template, slot_names, payloads = unified

        if branchless_default:
            route_payloads = payloads[:-1]
            default_payload = payloads[-1]
        else:
            route_payloads = payloads
            default_payload = None

        table: dict[Hashable, Any] = {}
        seen: set[Hashable] = set()
        for (keys, _guard, _body, _fall), payload in zip(
            raw_clauses, route_payloads
        ):
            assert keys is not None
            for key in keys:
                identity = _case_identity(key, self.case_key_mode)
                if identity in seen:
                    raise DuplicateCaseError(
                        f"unreachable duplicate case key: {key!r}"
                    )
                seen.add(identity)
                table[identity] = payload

        subject_name = f"__pyswitch_subject_{token}"
        payload_name = f"__pyswitch_template_result_{token}"
        getter_marker = (f"__pyswitch_template_getter_{token}").encode("ascii")
        type_marker = (f"__pyswitch_type_{token}").encode("ascii")
        typeerror_marker = (f"__pyswitch_typeerror_{token}").encode("ascii")
        miss_marker = (f"__pyswitch_template_miss_{token}").encode("ascii")
        miss_sentinel = object()
        output, subject_ref, subject_temp_name = self._prepare_subject(
            node, context, subject, subject_name
        )
        typed_partitions = _typed_partition_specs(
            table, token, self.case_key_mode
        )
        typed_miss_hash_marker = (
            (f"__pyswitch_partition_hash_{token}").encode("ascii")
            if len(typed_partitions) == 1 else None
        )
        typed_router_getter_marker, typed_router_miss_getter_marker = (
            _typed_router_markers(token, typed_partitions)
        )

        if len(slot_names) == 1:
            payload_target: ast.expr = ast.Name(slot_names[0], ast.Store())
        else:
            payload_target = ast.Tuple(
                [ast.Name(name, ast.Store()) for name in slot_names],
                ast.Store(),
            )

        lookup_default: Any = default_payload if branchless_default else miss_marker
        partial_result_name = (
            slot_names[0] if not branchless_default and len(slot_names) == 1
            else payload_name
        )
        if branchless_default:
            fast_target = copy.deepcopy(payload_target)
            fallback_target = copy.deepcopy(payload_target)
        else:
            fast_target = ast.Name(partial_result_name, ast.Store())
            fallback_target = ast.Name(partial_result_name, ast.Store())

        fast_assignment = ast.Assign(
            [fast_target],
            _portable_lookup_call(
                subject=subject_ref,
                mode=self.case_key_mode,
                getter_marker=getter_marker,
                default=ast.Constant(lookup_default),
                type_marker=type_marker,
                typed_partitions=typed_partitions,
                typed_miss_hash_marker=typed_miss_hash_marker,
                typed_router_getter_marker=typed_router_getter_marker,
                typed_router_miss_getter_marker=typed_router_miss_getter_marker,
            ),
        )
        fallback_assignment = ast.Assign(
            [fallback_target], ast.Constant(lookup_default)
        )
        unhashable_marker = (f"__pyswitch_unhashable_{token}").encode("ascii")
        unhashable = ast.If(
            ast.Call(
                ast.Constant(unhashable_marker),
                [copy.deepcopy(subject_ref)],
                [],
            ),
            [fallback_assignment],
            [ast.Raise(None, None)],
        )
        lookup = ast.Try(
            [fast_assignment],
            [ast.ExceptHandler(ast.Constant(typeerror_marker), None, [unhashable])],
            [],
            [],
        )
        ast.copy_location(lookup, node)
        output.append(lookup)
        if subject_temp_name is not None:
            cleanup_subject = _delete_locals([subject_temp_name])
            assert cleanup_subject is not None
            ast.copy_location(cleanup_subject, node)
            output.append(cleanup_subject)

        if action_kind == "return":
            final_action: ast.stmt = ast.Return(template)
        else:
            assert assignment_target is not None
            final_action = ast.Assign([copy.deepcopy(assignment_target)], template)

        extra_constants: dict[bytes, Any] = {
            typeerror_marker: TypeError,
            unhashable_marker: _hash_is_disabled,
        }
        if self.case_key_mode == "typed":
            extra_constants[type_marker] = type
        if typed_miss_hash_marker is not None:
            extra_constants[typed_miss_hash_marker] = hash
        if branchless_default:
            output.append(final_action)
            if action_kind == "assign":
                cleanup_slots = _delete_locals(list(slot_names))
                assert cleanup_slots is not None
                ast.copy_location(cleanup_slots, node)
                output.append(cleanup_slots)
        else:
            if len(slot_names) == 1:
                matched_body = [final_action]
                if action_kind == "assign":
                    cleanup_slots = _delete_locals(list(slot_names))
                    assert cleanup_slots is not None
                    matched_body.append(cleanup_slots)
            else:
                bind_payload = ast.Assign(
                    [copy.deepcopy(payload_target)], ast.Name(payload_name, ast.Load())
                )
                cleanup_tuple = _delete_locals([payload_name])
                assert cleanup_tuple is not None
                matched_body = [bind_payload, cleanup_tuple, final_action]
                if action_kind == "assign":
                    cleanup_slots = _delete_locals(list(slot_names))
                    assert cleanup_slots is not None
                    matched_body.append(cleanup_slots)
            miss_cleanup = _delete_locals([partial_result_name])
            assert miss_cleanup is not None
            miss_body = [miss_cleanup, *(copy.deepcopy(explicit_default) if explicit_default else [ast.Pass()])]
            dispatch = ast.If(
                ast.Compare(
                    ast.Name(partial_result_name, ast.Load()),
                    [ast.Is()],
                    [ast.Constant(miss_marker)],
                ),
                miss_body,
                matched_body,
            )
            output.append(dispatch)
            extra_constants[miss_marker] = miss_sentinel

        for statement in output:
            ast.copy_location(statement, node)

        self.plans.append(
            _PortablePlan(
                getter_marker,
                table,
                len(table),
                kind="expression-template",
                extra_constants=extra_constants,
                stack_payload_names=(tuple(slot_names) if branchless_default else ()),
                typed_partitions=typed_partitions,
                typed_router_getter_marker=typed_router_getter_marker,
                typed_router_miss_getter_marker=typed_router_miss_getter_marker,
            )
        )
        return output

    def _statement_template_dispatch(
        self,
        *,
        node: ast.With,
        context: ast.withitem,
        subject: ast.expr,
        raw_clauses: list[
            tuple[tuple[Hashable, ...] | None, ast.expr | None, list[ast.stmt], bool]
        ],
        expanded_bodies: list[list[ast.stmt]],
        explicit_default: list[ast.stmt] | None,
        default_guard: ast.expr | None,
    ) -> list[ast.stmt] | None:
        """Compile same-shape straight-line route bodies through one table."""
        if (
            default_guard is not None
            or any(
                guard is not None or fall
                for _keys, guard, _body, fall in raw_clauses
            )
            or not expanded_bodies
        ):
            return None

        token = uuid.uuid4().hex
        unified = None
        branchless_default = False
        if explicit_default is not None:
            unified = _unify_statement_sequence(
                expanded_bodies + [explicit_default], token
            )
            branchless_default = unified is not None
        if unified is None:
            unified = _unify_statement_sequence(expanded_bodies, token)
        if unified is None:
            return None
        template_body, slot_names, payloads = unified

        if branchless_default:
            route_payloads = payloads[:-1]
            default_payload = payloads[-1]
        else:
            route_payloads = payloads
            default_payload = None

        table: dict[Hashable, Any] = {}
        seen: set[Hashable] = set()
        for (keys, _guard, _body, _fall), payload in zip(
            raw_clauses, route_payloads
        ):
            assert keys is not None
            for key in keys:
                identity = _case_identity(key, self.case_key_mode)
                if identity in seen:
                    raise DuplicateCaseError(
                        f"unreachable duplicate case key: {key!r}"
                    )
                seen.add(identity)
                table[identity] = payload

        subject_name = f"__pyswitch_subject_{token}"
        payload_name = f"__pyswitch_statement_result_{token}"
        getter_marker = (f"__pyswitch_statement_getter_{token}").encode("ascii")
        type_marker = (f"__pyswitch_type_{token}").encode("ascii")
        typeerror_marker = (f"__pyswitch_typeerror_{token}").encode("ascii")
        unhashable_marker = (f"__pyswitch_unhashable_{token}").encode("ascii")
        miss_marker = (f"__pyswitch_statement_miss_{token}").encode("ascii")
        miss_sentinel = object()

        output, subject_ref, subject_temp_name = self._prepare_subject(
            node, context, subject, subject_name
        )
        typed_partitions = _typed_partition_specs(
            table, token, self.case_key_mode
        )
        typed_miss_hash_marker = (
            (f"__pyswitch_partition_hash_{token}").encode("ascii")
            if len(typed_partitions) == 1 else None
        )
        typed_router_getter_marker, typed_router_miss_getter_marker = (
            _typed_router_markers(token, typed_partitions)
        )

        if len(slot_names) == 1:
            payload_target: ast.expr = ast.Name(slot_names[0], ast.Store())
        else:
            payload_target = ast.Tuple(
                [ast.Name(name, ast.Store()) for name in slot_names],
                ast.Store(),
            )

        lookup_default: Any = default_payload if branchless_default else miss_marker
        partial_result_name = (
            slot_names[0] if not branchless_default and len(slot_names) == 1
            else payload_name
        )
        if branchless_default:
            fast_target = copy.deepcopy(payload_target)
            fallback_target = copy.deepcopy(payload_target)
        else:
            fast_target = ast.Name(partial_result_name, ast.Store())
            fallback_target = ast.Name(partial_result_name, ast.Store())

        lookup = ast.Try(
            [
                ast.Assign(
                    [fast_target],
                    _portable_lookup_call(
                        subject=subject_ref,
                        mode=self.case_key_mode,
                        getter_marker=getter_marker,
                        default=ast.Constant(lookup_default),
                        type_marker=type_marker,
                        typed_partitions=typed_partitions,
                        typed_miss_hash_marker=typed_miss_hash_marker,
                        typed_router_getter_marker=typed_router_getter_marker,
                        typed_router_miss_getter_marker=typed_router_miss_getter_marker,
                    ),
                )
            ],
            [
                ast.ExceptHandler(
                    ast.Constant(typeerror_marker),
                    None,
                    [
                        ast.If(
                            ast.Call(
                                ast.Constant(unhashable_marker),
                                [copy.deepcopy(subject_ref)],
                                [],
                            ),
                            [
                                ast.Assign(
                                    [fallback_target],
                                    ast.Constant(lookup_default),
                                )
                            ],
                            [ast.Raise(None, None)],
                        )
                    ],
                )
            ],
            [],
            [],
        )
        ast.copy_location(lookup, node)
        output.append(lookup)
        if subject_temp_name is not None:
            cleanup_subject = _delete_locals([subject_temp_name])
            assert cleanup_subject is not None
            ast.copy_location(cleanup_subject, node)
            output.append(cleanup_subject)

        extra_constants: dict[bytes, Any] = {
            typeerror_marker: TypeError,
            unhashable_marker: _hash_is_disabled,
        }
        if self.case_key_mode == "typed":
            extra_constants[type_marker] = type
        if typed_miss_hash_marker is not None:
            extra_constants[typed_miss_hash_marker] = hash

        if branchless_default:
            output.extend(copy.deepcopy(template_body))
            cleanup_slots = _delete_locals(list(slot_names))
            assert cleanup_slots is not None
            ast.copy_location(cleanup_slots, node)
            output.append(cleanup_slots)
        else:
            if len(slot_names) == 1:
                matched_body = copy.deepcopy(template_body)
                cleanup_slots = _delete_locals(list(slot_names))
                assert cleanup_slots is not None
                matched_body.append(cleanup_slots)
            else:
                bind_payload = ast.Assign(
                    [copy.deepcopy(payload_target)], ast.Name(payload_name, ast.Load())
                )
                cleanup_tuple = _delete_locals([payload_name])
                assert cleanup_tuple is not None
                matched_body = [
                    bind_payload, cleanup_tuple, *copy.deepcopy(template_body)
                ]
                cleanup_slots = _delete_locals(list(slot_names))
                assert cleanup_slots is not None
                matched_body.append(cleanup_slots)
            miss_cleanup = _delete_locals([partial_result_name])
            assert miss_cleanup is not None
            miss_body = [miss_cleanup, *(copy.deepcopy(explicit_default) if explicit_default else [ast.Pass()])]
            output.append(
                ast.If(
                    ast.Compare(
                        ast.Name(partial_result_name, ast.Load()),
                        [ast.Is()],
                        [ast.Constant(miss_marker)],
                    ),
                    miss_body,
                    matched_body,
                )
            )
            extra_constants[miss_marker] = miss_sentinel

        for statement in output:
            ast.copy_location(statement, node)

        self.plans.append(
            _PortablePlan(
                getter_marker,
                table,
                len(table),
                kind="statement-template",
                extra_constants=extra_constants,
                stack_payload_names=(tuple(slot_names) if branchless_default else ()),
                typed_partitions=typed_partitions,
                typed_router_getter_marker=typed_router_getter_marker,
                typed_router_miss_getter_marker=typed_router_miss_getter_marker,
            )
        )
        return output

    def visit_With(self, node: ast.With) -> ast.AST:
        if len(node.items) != 1:
            return self.generic_visit(node)
        context = node.items[0]
        call = context.context_expr
        if not (isinstance(call, ast.Call) and _marker_name(call.func, self.environment) == "switch"):
            return self.generic_visit(node)
        if len(call.args) != 1 or call.keywords:
            raise SwitchSyntaxError("use exactly: with switch(expression):")

        raw_clauses: list[tuple[tuple[Hashable, ...] | None, ast.expr | None, list[ast.stmt], bool]] = []
        explicit_default: list[ast.stmt] | None = None
        default_guard: ast.expr | None = None
        saw_default = False

        for member_position, member in enumerate(node.body):
            if not isinstance(member, ast.If):
                raise SwitchSyntaxError(
                    "each direct switch member must be if/elif case(...); else is the default"
                )
            chain, else_body = self._flatten_member_chain(member)
            for clause in chain:
                test = clause.test
                if not (isinstance(test, ast.Call) and _marker_name(test.func, self.environment) == "case"):
                    raise SwitchSyntaxError("invalid case syntax")
                unknown = [kw.arg for kw in test.keywords if kw.arg != "when"]
                if unknown or any(kw.arg is None for kw in test.keywords):
                    raise SwitchSyntaxError("case() accepts only the keyword 'when'")
                when_nodes = [kw.value for kw in test.keywords if kw.arg == "when"]
                if len(when_nodes) > 1:
                    raise SwitchSyntaxError("duplicate case when= guard")
                guard = when_nodes[0] if when_nodes else None
                if _invalid_fallthrough_in_body(clause.body, self.environment):
                    raise SwitchSyntaxError(
                        "fallthrough() must be the final direct statement of a case"
                    )
                body = self._visit_statements(clause.body) or [ast.Pass()]
                do_fallthrough = bool(body and _is_fallthrough_statement(body[-1], self.environment))
                if do_fallthrough:
                    body = body[:-1]
                if not test.args:
                    if saw_default:
                        raise DuplicateDefaultError("duplicate default case")
                    saw_default = True
                    if do_fallthrough:
                        raise SwitchSyntaxError("default case cannot fall through")
                    explicit_default = body or [ast.Pass()]
                    default_guard = guard
                    continue
                if saw_default:
                    raise SwitchSyntaxError("the default case must be final")
                keys = tuple(_resolved_constant(arg, self.environment) for arg in test.args)
                raw_clauses.append((keys, guard, body, do_fallthrough))
            if else_body is not None:
                if saw_default:
                    raise DuplicateDefaultError("duplicate default case")
                saw_default = True
                explicit_default = self._visit_statements(else_body) or [ast.Pass()]
            if else_body is not None and member_position != len(node.body) - 1:
                raise SwitchSyntaxError("an else/default case must be the final switch member")

        if not raw_clauses and explicit_default is None:
            raise SwitchSyntaxError("empty switch")

        # Expand a trailing fallthrough marker by appending the following case
        # body.  This preserves return/break/continue semantics because no loop
        # or helper function is introduced.
        expanded_bodies: list[list[ast.stmt]] = [list(item[2]) for item in raw_clauses]
        for index in range(len(raw_clauses) - 1, -1, -1):
            if raw_clauses[index][3]:
                next_body = (
                    expanded_bodies[index + 1]
                    if index + 1 < len(expanded_bodies)
                    else (explicit_default or [ast.Pass()])
                )
                expanded_bodies[index].extend(copy.deepcopy(next_body))

        default_body = explicit_default or [ast.Pass()]
        if default_guard is not None:
            default_body = [
                ast.If(
                    test=self.visit(default_guard),
                    body=default_body,
                    orelse=[ast.Pass()],
                )
            ]

        direct = self._direct_value_dispatch(
            node=node,
            context=context,
            subject=self.visit(call.args[0]),
            raw_clauses=raw_clauses,
            expanded_bodies=expanded_bodies,
            explicit_default=explicit_default,
            default_guard=default_guard,
        )
        if direct is not None:
            return direct

        template_dispatch = self._return_template_dispatch(
            node=node,
            context=context,
            subject=self.visit(call.args[0]),
            raw_clauses=raw_clauses,
            expanded_bodies=expanded_bodies,
            explicit_default=explicit_default,
            default_guard=default_guard,
        )
        if template_dispatch is not None:
            return template_dispatch

        statement_template = self._statement_template_dispatch(
            node=node,
            context=context,
            subject=self.visit(call.args[0]),
            raw_clauses=raw_clauses,
            expanded_bodies=expanded_bodies,
            explicit_default=explicit_default,
            default_guard=default_guard,
        )
        if statement_template is not None:
            return statement_template

        # Do not out-line arbitrary return expressions into helper functions.
        # A callee may observe its caller frame (directly or indirectly), and
        # executing it in a synthetic handler changes trace/profile/exception
        # behavior.  Expression-template lowering above stays in this frame;
        # heterogeneous bodies fall through to the general route compiler.

        key_to_clauses: dict[Hashable, list[int]] = {}
        unconditional_seen: set[Hashable] = set()
        for index, (keys, guard, _body, _fall) in enumerate(raw_clauses):
            assert keys is not None
            clause_seen: set[Hashable] = set()
            for key in keys:
                identity = _case_identity(key, self.case_key_mode)
                if identity in clause_seen:
                    raise DuplicateCaseError(
                        f"duplicate key within one case clause: {key!r}"
                    )
                clause_seen.add(identity)
                if identity in unconditional_seen:
                    raise DuplicateCaseError(f"unreachable duplicate case key: {key!r}")
                key_to_clauses.setdefault(identity, []).append(index)
                if guard is None:
                    unconditional_seen.add(identity)

        route_map: dict[tuple[int, ...], int] = {}
        body_map: dict[tuple[str, ...], int] = {}
        route_bodies: list[list[ast.stmt]] = []
        table: dict[Hashable, int] = {}

        def body_key(body: list[ast.stmt]) -> tuple[str, ...]:
            return tuple(
                ast.dump(statement, include_attributes=False)
                for statement in body
            )

        def make_route(indices: tuple[int, ...]) -> list[ast.stmt]:
            def build(position: int) -> list[ast.stmt]:
                if position >= len(indices):
                    return copy.deepcopy(default_body)
                clause_index = indices[position]
                guard = raw_clauses[clause_index][1]
                body = copy.deepcopy(expanded_bodies[clause_index]) or [ast.Pass()]
                if guard is None:
                    return body
                return [
                    ast.If(
                        test=self.visit(copy.deepcopy(guard)),
                        body=body,
                        orelse=build(position + 1),
                    )
                ]
            return build(0)

        for case_key, clause_indices in key_to_clauses.items():
            signature = tuple(clause_indices)
            route_index = route_map.get(signature)
            if route_index is None:
                route_body = make_route(signature)
                route_body_key = body_key(route_body)
                route_index = body_map.get(route_body_key)
                if route_index is None:
                    route_index = len(route_bodies)
                    route_bodies.append(route_body)
                    body_map[route_body_key] = route_index
                route_map[signature] = route_index
            table[case_key] = route_index

        copied_default = copy.deepcopy(default_body)
        default_key = body_key(copied_default)
        default_index = body_map.get(default_key)
        if default_index is None:
            default_index = len(route_bodies)
            route_bodies.append(copied_default)
            body_map[default_key] = default_index

        # Fallthrough expansion can duplicate an identical downstream continuation
        # into several heterogeneous routes.  Keep route-specific prefixes inside
        # the dispatch tree and execute the source-identical suffix once afterward.
        # Empty prefixes are fine because compiler-local cleanup is prepended below.
        auto_compact_estimated_bytes_saved = 0
        auto_compact_used = False
        if self.compact_routes:
            candidate_bodies, candidate_continuation = _shared_route_continuation(route_bodies)
            if self.compact_routes == "auto":
                # Use compiled CPython bytecode as the density proxy rather than AST
                # node count.  This better reflects expensive calls/try blocks and
                # fails closed for tails whose lexical context cannot be proxied.
                gain = _shared_continuation_bytecode_gain(
                    route_bodies, candidate_continuation
                )
                if gain is not None:
                    auto_compact_estimated_bytes_saved = gain
                if gain is not None and gain >= 64:
                    route_bodies, shared_continuation = candidate_bodies, candidate_continuation
                    auto_compact_used = True
                else:
                    shared_continuation = []
            else:
                route_bodies, shared_continuation = candidate_bodies, candidate_continuation
        else:
            shared_continuation = []

        # A two-route general plan needs no integer dispatch tree.  Canonicalize
        # its lookup payload to bool so CPython can branch directly on the table
        # result.  This is especially useful for control-heavy bodies that are
        # intentionally excluded from expression/statement template lowering.
        binary_route = len(route_bodies) == 2
        binary_true_body: list[ast.stmt] | None = None
        binary_false_body: list[ast.stmt] | None = None
        if binary_route:
            matched_index = 1 - default_index
            binary_true_body = route_bodies[matched_index]
            binary_false_body = route_bodies[default_index]
            table = {key: value != default_index for key, value in table.items()}

        token = uuid.uuid4().hex
        subject_name = f"__pyswitch_subject_{token}"
        index_name = f"__pyswitch_index_{token}"
        getter_marker = (f"__pyswitch_portable_getter_{token}").encode("ascii")
        type_marker = (f"__pyswitch_type_{token}").encode("ascii")
        typeerror_marker = (f"__pyswitch_typeerror_{token}").encode("ascii")

        subject_expr = self.visit(call.args[0])
        statements, subject_ref, subject_temp_name = self._prepare_subject(
            node, context, subject_expr, subject_name
        )
        typed_partitions = _typed_partition_specs(
            table, token, self.case_key_mode
        )
        typed_miss_hash_marker = (
            (f"__pyswitch_partition_hash_{token}").encode("ascii")
            if len(typed_partitions) == 1 else None
        )
        typed_router_getter_marker, typed_router_miss_getter_marker = (
            _typed_router_markers(token, typed_partitions)
        )

        # Compiler dispatch temporaries must not leak into user guards/bodies or
        # remain visible through locals()/frame inspection after the switch.
        # The index is needed only while traversing the generated balanced tree;
        # the subject temporary is needed only through lookup/error handling.
        cleanup_names = [index_name]
        if subject_temp_name is not None:
            cleanup_names.append(subject_temp_name)
        cleanup = _delete_locals(cleanup_names)
        if cleanup is not None:
            for route_index, route_body in enumerate(route_bodies):
                route_cleanup = copy.deepcopy(cleanup)
                ast.copy_location(route_cleanup, node)
                route_bodies[route_index] = [route_cleanup, *route_body]
        if binary_route:
            # Refresh references after compiler-local cleanup has been prepended.
            binary_true_body = route_bodies[1 - default_index]
            binary_false_body = route_bodies[default_index]

        lookup_default = False if binary_route else default_index
        index_assign = ast.Assign(
            targets=[ast.Name(index_name, ast.Store())],
            value=_portable_lookup_call(
                subject=subject_ref,
                mode=self.case_key_mode,
                getter_marker=getter_marker,
                default=ast.Constant(lookup_default),
                type_marker=type_marker,
                typed_partitions=typed_partitions,
                typed_miss_hash_marker=typed_miss_hash_marker,
                typed_router_getter_marker=typed_router_getter_marker,
                typed_router_miss_getter_marker=typed_router_miss_getter_marker,
            ),
        )
        ast.copy_location(index_assign, node)
        unhashable_marker = (f"__pyswitch_unhashable_{token}").encode("ascii")
        unhashable_fallback = ast.If(
            test=ast.Call(
                func=ast.Constant(unhashable_marker),
                args=[copy.deepcopy(subject_ref)],
                keywords=[],
            ),
            body=[
                ast.Assign(
                    targets=[ast.Name(index_name, ast.Store())],
                    value=ast.Constant(lookup_default),
                )
            ],
            orelse=[ast.Raise(exc=None, cause=None)],
        )
        lookup = ast.Try(
            body=[index_assign],
            handlers=[
                ast.ExceptHandler(
                    type=ast.Constant(typeerror_marker),
                    name=None,
                    body=[unhashable_fallback],
                )
            ],
            orelse=[],
            finalbody=[],
        )
        ast.copy_location(lookup, node)
        statements.append(lookup)
        if binary_route:
            assert binary_true_body is not None and binary_false_body is not None
            statements.append(
                ast.If(
                    test=ast.Name(index_name, ast.Load()),
                    body=binary_true_body,
                    orelse=binary_false_body,
                )
            )
        else:
            statements.extend(
                self._balanced_dispatch(index_name, route_bodies, 0, len(route_bodies))
            )
        statements.extend(shared_continuation)
        for statement in statements:
            ast.copy_location(statement, node)

        self.plans.append(
            _PortablePlan(
                getter_marker,
                table,
                len(key_to_clauses),
                kind=("binary-route" if binary_route else "balanced"),
                extra_constants={
                    **({type_marker: type} if self.case_key_mode == "typed" else {}),
                    **({typed_miss_hash_marker: hash} if typed_miss_hash_marker is not None else {}),
                    typeerror_marker: TypeError,
                    unhashable_marker: _hash_is_disabled,
                },
                typed_partitions=typed_partitions,
                typed_router_getter_marker=typed_router_getter_marker,
                typed_router_miss_getter_marker=typed_router_miss_getter_marker,
                shared_continuation_statements=len(shared_continuation),
                auto_compact_estimated_bytes_saved=auto_compact_estimated_bytes_saved,
                auto_compact_used=auto_compact_used,
            )
        )
        return statements


def _compile_portable(
    func: F,
    *,
    explicit_source: str | None = None,
    case_key_mode: str = "python",
    compact_routes: bool | str = False,
) -> F:
    source, first_line = _source_for_function(func, explicit_source)
    module = ast.parse(source, func.__code__.co_filename)
    fn_node = _find_function(module, func.__name__)
    transformer = _PortableTransformer(
        func, case_key_mode, compact_routes=compact_routes
    )
    transformer.visit(fn_node)
    if not transformer.plans:
        raise SwitchSyntaxError("function contains no switch block")
    ast.increment_lineno(module, first_line - 1)
    ast.fix_missing_locations(module)
    generated = _execute_transformed_function(func, module, fn_node)

    constants = list(generated.__code__.co_consts)

    # Literal payload tables are built from AST values before CPython has had
    # a chance to pool/deduplicate constants.  Rebind those payloads to the
    # generated function's real constant objects before installing the bound
    # dict.get callable.  This keeps the direct/table fast path without making
    # observable ``is`` relationships between literals differ from ordinary
    # CPython compilation.
    constant_pool: dict[tuple[Any, ...], Any] = {}
    for value in constants:
        _register_compiler_constant(constant_pool, value)
    for plan in transformer.plans:
        canonical_payloads = plan.kind in {
            "direct-value", "expression-template", "statement-template"
        }
        if canonical_payloads and not plan.typed_partitions:
            plan.table = {
                key: _canonicalize_compiler_literal(constant_pool, payload)
                for key, payload in plan.table.items()
            }
        if plan.typed_partitions:
            canonical_partitions = []
            for case_type, type_marker, getter_marker, partition_items in plan.typed_partitions:
                canonical_subtable = {
                    key: (
                        _canonicalize_compiler_literal(constant_pool, payload)
                        if canonical_payloads else payload
                    )
                    for key, payload in partition_items
                }
                canonical_partitions.append(
                    (case_type, type_marker, getter_marker, canonical_subtable)
                )
            plan.typed_partitions = tuple(canonical_partitions)

    replacements: dict[bytes, Any] = {}
    for plan in transformer.plans:
        # A fully partitioned single-type typed plan has no tuple-key fallback
        # call in its generated code; its historical full-table getter marker is
        # therefore intentionally absent.
        if not plan.typed_partitions:
            replacements[plan.getter_marker] = plan.table.get
        if len(plan.typed_partitions) == 1:
            case_type, type_marker, getter_marker, subtable = plan.typed_partitions[0]
            replacements[type_marker] = case_type
            replacements[getter_marker] = subtable.get
        if plan.typed_router_getter_marker is not None:
            router = {
                case_type: subtable.get
                for case_type, _type_marker, _getter_marker, subtable in plan.typed_partitions
            }
            replacements[plan.typed_router_getter_marker] = router.get
            assert plan.typed_router_miss_getter_marker is not None
            replacements[plan.typed_router_miss_getter_marker] = {}.get
        replacements.update(plan.extra_constants)
    found_markers: set[bytes] = set()
    for index, value in enumerate(constants):
        if isinstance(value, bytes) and value in replacements:
            constants[index] = replacements[value]
            found_markers.add(value)
    missing_markers = replacements.keys() - found_markers
    if missing_markers:
        raise SwitchError("failed to locate portable jump-table constants")
    code = generated.__code__.replace(co_consts=tuple(constants))
    stack_payload_successes = 0
    stack_payload_failures: list[str] = []
    for plan in transformer.plans:
        if not plan.stack_payload_names:
            continue
        code, optimized, reason = _stackify_template_payloads(
            code, plan.stack_payload_names
        )
        if optimized:
            stack_payload_successes += 1
        elif reason is not None:
            stack_payload_failures.append(reason)

    code, stripped_line_locations, line_location_fallback = (
        _strip_synthetic_line_zero(code)
    )

    result = types.FunctionType(
        code,
        func.__globals__,
        func.__name__,
        func.__defaults__,
        generated.__closure__,
    )
    _rebind_recursive_self_closure(generated, result)
    _copy_function_metadata(func, result)
    kinds = {plan.kind for plan in transformer.plans}
    if kinds == {"direct-value"}:
        backend = "portable-direct-value-v18"
    elif kinds == {"expression-template"}:
        backend = "portable-expression-template-v18"
    elif kinds == {"statement-template"}:
        backend = "portable-statement-template-v18"
    elif kinds <= {"direct-value", "expression-template", "statement-template"}:
        backend = "portable-hybrid-v18"
    elif kinds == {"binary-route"}:
        # Retain the public balanced-backend label for compatibility; binary
        # routing is a specialization inside the general portable backend.
        backend = "portable-balanced-v18"
    else:
        backend = "portable-balanced-v18" if kinds == {"balanced"} else "portable-hybrid-v18"
    result.__pyswitch_backend__ = backend
    result.__pyswitch_mode__ = "portable"
    result.__pyswitch_case_key_mode__ = case_key_mode
    result.__pyswitch_typed_partition_plan_count__ = sum(
        bool(plan.typed_partitions) for plan in transformer.plans
    )
    result.__pyswitch_typed_partition_type_count__ = sum(
        len(plan.typed_partitions) for plan in transformer.plans
    )
    result.__pyswitch_typed_router_plan_count__ = sum(
        len(plan.typed_partitions) > 1 for plan in transformer.plans
    )
    result.__pyswitch_typed_router_type_count__ = sum(
        len(plan.typed_partitions)
        for plan in transformer.plans
        if len(plan.typed_partitions) > 1
    )
    result.__pyswitch_case_count__ = sum(plan.case_count for plan in transformer.plans)
    result.__pyswitch_switch_count__ = len(transformer.plans)
    result.__pyswitch_direct_plan_count__ = sum(
        plan.kind == "direct-value" for plan in transformer.plans
    )
    result.__pyswitch_handler_plan_count__ = 0
    result.__pyswitch_template_plan_count__ = sum(
        plan.kind == "expression-template" for plan in transformer.plans
    )
    result.__pyswitch_statement_template_plan_count__ = sum(
        plan.kind == "statement-template" for plan in transformer.plans
    )
    result.__pyswitch_binary_route_plan_count__ = sum(
        plan.kind == "binary-route" for plan in transformer.plans
    )
    result.__pyswitch_balanced_plan_count__ = sum(
        plan.kind == "balanced" for plan in transformer.plans
    )
    result.__pyswitch_shared_continuation_plan_count__ = sum(
        plan.shared_continuation_statements > 0 for plan in transformer.plans
    )
    result.__pyswitch_shared_continuation_statement_count__ = sum(
        plan.shared_continuation_statements for plan in transformer.plans
    )
    result.__pyswitch_auto_compact_plan_count__ = sum(
        plan.auto_compact_used for plan in transformer.plans
    )
    result.__pyswitch_auto_compact_estimated_bytes_saved__ = sum(
        plan.auto_compact_estimated_bytes_saved for plan in transformer.plans
    )
    result.__pyswitch_stack_payload_plan_count__ = stack_payload_successes
    result.__pyswitch_stack_payload_fallbacks__ = tuple(stack_payload_failures)
    result.__pyswitch_synthetic_line_locations_removed__ = stripped_line_locations
    result.__pyswitch_line_location_fallback__ = line_location_fallback
    return result

def _compile(
    func: F,
    *,
    allow_direct_recursion: bool = False,
    explicit_source: str | None = None,
    case_key_mode: str = "python",
) -> F:
    _require_runtime()
    source, first_line = _source_for_function(func, explicit_source)
    module = ast.parse(source, func.__code__.co_filename)
    fn_node = _find_function(module, func.__name__)
    if _contains_direct_self_call(fn_node) and not allow_direct_recursion:
        raise SwitchSyntaxError(
            "direct recursion requires enable_switch(mode='isolated')"
        )
    fn_node.decorator_list = []
    transformer = _Transformer(func, case_key_mode)
    transformer.visit(fn_node)
    if not transformer.plans:
        raise SwitchSyntaxError("function contains no switch block")
    ast.increment_lineno(module, first_line - 1)
    ast.fix_missing_locations(module)
    # Preserve the live compiler's proven line-0 CFG shape (CPython's peephole
    # optimizer can use source positions when deciding folds), but make the
    # synthetic positions structurally valid for bytecode decoding.  The code
    # line table is sanitized immediately after compilation, before any live
    # gate offsets are discovered.
    for location_node in ast.walk(module):
        lineno = getattr(location_node, "lineno", None)
        end_lineno = getattr(location_node, "end_lineno", None)
        if lineno is not None and lineno <= 0 and (end_lineno is None or end_lineno <= 0):
            location_node.end_lineno = 1
    generated = _execute_transformed_function(func, module, fn_node)

    live_code, stripped_line_locations, line_location_fallback = (
        _strip_synthetic_line_zero(generated.__code__)
    )
    if line_location_fallback is not None:
        raise SwitchError(
            f"failed to normalize live switch source locations: {line_location_fallback}"
        )
    if live_code is not generated.__code__:
        previous_generated = generated
        generated = types.FunctionType(
            live_code,
            func.__globals__,
            func.__name__,
            func.__defaults__,
            previous_generated.__closure__,
        )
        _rebind_recursive_self_closure(previous_generated, generated)

    constants = list(generated.__code__.co_consts)
    installs: list[tuple[_Plan, int, Any, int]] = []
    metadata_tables: list[dict[Hashable, int]] = []
    gate_offsets: list[int] = []
    gate_widths: list[int] = []
    pointer_indexes: list[int] = []

    for plan in transformer.plans:
        (
            gate_prefix, body_targets, fallback_target,
            getter_index, pointer_index, default_index,
        ) = _locate(generated.__code__, plan)
        if len(body_targets) != len(plan.key_groups):
            raise SwitchError("case target discovery mismatch")
        all_targets = [*body_targets, fallback_target]
        gate_width = _choose_gate_width(gate_prefix, all_targets)
        default_jump = _encode_jump(
            _relative_arg(gate_prefix, fallback_target, gate_width), gate_width
        )
        table: dict[Hashable, int] = {}
        for keys, target in zip(plan.key_groups, body_targets):
            encoded = _encode_jump(
                _relative_arg(gate_prefix, target, gate_width), gate_width
            )
            for key in keys:
                table[_case_identity(key, case_key_mode)] = encoded
        pointer = _new_rebindable_cell(gate_width)
        for index, marker, value, label in (
            (getter_index, plan.getter_marker, _make_case_getter(table, case_key_mode), "jump-getter"),
            (pointer_index, plan.pointer_marker, pointer, "live-pointer"),
            (default_index, plan.default_marker, default_jump, "default-jump"),
        ):
            if constants[index] != marker:
                raise SwitchError(f"{label} marker mismatch")
            constants[index] = value
        for marker, replacement in plan.extra_constants.items():
            replaced = False
            for constant_index, constant_value in enumerate(constants):
                if constant_value == marker:
                    constants[constant_index] = replacement
                    replaced = True
            if not replaced:
                raise SwitchError("failed to locate live switch helper constant")

        installs.append((plan, gate_prefix, pointer, gate_width))
        metadata_tables.append(table)
        gate_offsets.append(gate_prefix)
        gate_widths.append(gate_width)
        pointer_indexes.append(pointer_index)

    final_code = generated.__code__.replace(co_consts=tuple(constants))
    machine = platform.machine().lower()
    unaligned_safe = machine in {"x86_64", "amd64", "i386", "i686", "x86"}
    for _plan, gate_prefix, pointer, width in installs:
        address = _live_address(final_code, gate_prefix)
        byte_width = width * 2
        if not unaligned_safe and address % byte_width:
            raise UnsupportedRuntimeError(
                f"live gate at {address:#x} is not {byte_width}-byte aligned on {machine}; "
                "use mode='portable'"
            )
        _bind_cell(pointer, address)

    previous_generated = generated
    generated = types.FunctionType(
        final_code,
        func.__globals__,
        func.__name__,
        func.__defaults__,
        previous_generated.__closure__,
    )
    _rebind_recursive_self_closure(previous_generated, generated)
    generated.__signature__ = inspect.signature(func)
    generated.__kwdefaults__ = func.__kwdefaults__
    generated.__annotations__ = dict(getattr(func, "__annotations__", {}))
    generated.__dict__.update(func.__dict__)
    generated.__module__ = func.__module__
    generated.__qualname__ = func.__qualname__
    generated.__doc__ = func.__doc__
    if hasattr(func, "__type_params__"):
        generated.__type_params__ = func.__type_params__
    generated.__pyswitch_backend__ = "cpython313-live-inline-v18"
    generated.__pyswitch_gate_offsets__ = tuple(gate_offsets)
    generated.__pyswitch_case_count__ = sum(
        sum(len(group) for group in plan.key_groups) for plan in transformer.plans
    )
    generated.__pyswitch_gate_units__ = tuple(gate_widths)
    generated.__pyswitch_jump_tables__ = tuple(metadata_tables)
    generated.__pyswitch_pointer_indexes__ = tuple(pointer_indexes)
    generated.__pyswitch_synthetic_line_locations_removed__ = stripped_line_locations
    generated.__pyswitch_line_location_fallback__ = line_location_fallback
    generated.__pyswitch_clone_descriptors__ = tuple(
        zip(pointer_indexes, gate_offsets, gate_widths)
    )
    if len(gate_offsets) == 1:
        generated.__pyswitch_gate_offset__ = gate_offsets[0]
        generated.__pyswitch_jump_table__ = metadata_tables[0]
    return generated


def _copy_function_metadata(
    source: F, target: F, *, expose_wrapped: bool = True
) -> F:
    """Copy function metadata without accidentally exposing an unsafe template."""
    for attribute in functools.WRAPPER_ASSIGNMENTS:
        try:
            setattr(target, attribute, getattr(source, attribute))
        except AttributeError:
            pass
    for key, value in source.__dict__.items():
        if not key.startswith("__pyswitch_") or key == "__pyswitch_source__":
            target.__dict__[key] = value
    if expose_wrapped:
        target.__wrapped__ = source
    else:
        target.__dict__.pop("__wrapped__", None)
    target.__signature__ = inspect.signature(source)
    target.__kwdefaults__ = (
        None if source.__kwdefaults__ is None else dict(source.__kwdefaults__)
    )
    target.__annotations__ = dict(getattr(source, "__annotations__", {}))
    if hasattr(source, "__type_params__"):
        target.__type_params__ = source.__type_params__
    return target


def _clone_isolated_instance(template: F) -> F:
    """Clone a decorated function and rebind every live gate to its own code object.

    A shallow FunctionType clone is insufficient because it would share the same
    code object and the same ctypes pointer constants.  This routine creates a
    distinct code object, replaces each pointer constant, then binds the new
    pointers to the clone's adaptive instruction buffer.
    """
    descriptors = getattr(template, "__pyswitch_clone_descriptors__", None)
    if not descriptors:
        raise SwitchError("missing switch clone descriptors")

    constants = list(template.__code__.co_consts)
    bindings: list[tuple[Any, int]] = []
    for pointer_index, gate_offset, gate_width in descriptors:
        pointer = _new_rebindable_cell(gate_width)
        constants[pointer_index] = pointer
        bindings.append((pointer, gate_offset))

    cloned_code = template.__code__.replace(co_consts=tuple(constants))
    for pointer, gate_offset in bindings:
        _bind_cell(pointer, _live_address(cloned_code, gate_offset))

    clone = types.FunctionType(
        cloned_code,
        template.__globals__,
        template.__name__,
        template.__defaults__,
        template.__closure__,
    )
    _copy_function_metadata(template, clone, expose_wrapped=False)
    clone.__pyswitch_backend__ = "cpython313-isolated-instance-v18"
    return clone



def _signature_ast(func: Callable[..., Any]) -> tuple[ast.arguments, list[ast.expr], list[ast.keyword]]:
    signature = inspect.signature(func)
    posonly: list[ast.arg] = []
    positional: list[ast.arg] = []
    kwonly: list[ast.arg] = []
    vararg: ast.arg | None = None
    kwarg: ast.arg | None = None
    call_args: list[ast.expr] = []
    call_keywords: list[ast.keyword] = []

    for parameter in signature.parameters.values():
        argument = ast.arg(parameter.name)
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            posonly.append(argument)
            call_args.append(ast.Name(parameter.name, ast.Load()))
        elif parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
            positional.append(argument)
            call_args.append(ast.Name(parameter.name, ast.Load()))
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            vararg = argument
            call_args.append(ast.Starred(ast.Name(parameter.name, ast.Load()), ast.Load()))
        elif parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            kwonly.append(argument)
            call_keywords.append(ast.keyword(parameter.name, ast.Name(parameter.name, ast.Load())))
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            kwarg = argument
            call_keywords.append(ast.keyword(None, ast.Name(parameter.name, ast.Load())))

    positional_parameters = [
        p for p in signature.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    positional_default_count = sum(
        p.default is not inspect.Parameter.empty for p in positional_parameters
    )
    defaults = [ast.Constant(None)] * positional_default_count
    kw_defaults = [
        ast.Constant(None) if p.default is not inspect.Parameter.empty else None
        for p in signature.parameters.values()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    ]
    return (
        ast.arguments(
            posonlyargs=posonly,
            args=positional,
            vararg=vararg,
            kwonlyargs=kwonly,
            kw_defaults=kw_defaults,
            kwarg=kwarg,
            defaults=defaults,
        ),
        call_args,
        call_keywords,
    )


def _build_specialized_sync_wrapper(
    template: F,
    state: threading.local,
    *,
    per_depth: bool,
    max_cached_depth: int,
) -> F:
    """Compile a vectorcall-friendly wrapper with the original call signature."""
    arguments, call_args, call_keywords = _signature_ast(template)
    pool_name = "__pool"
    depth_name = "__depth"
    instance_name = "__instance"

    initialize = ast.Try(
        body=[
            ast.Assign(
                [ast.Name(pool_name, ast.Store())],
                ast.Attribute(ast.Name("__state", ast.Load()), "pool", ast.Load()),
            )
        ],
        handlers=[
            ast.ExceptHandler(
                ast.Name("AttributeError", ast.Load()),
                None,
                [
                    ast.Assign([ast.Name(pool_name, ast.Store())], ast.List([], ast.Load())),
                    ast.Assign(
                        [ast.Attribute(ast.Name("__state", ast.Load()), "pool", ast.Store())],
                        ast.Name(pool_name, ast.Load()),
                    ),
                    ast.Assign(
                        [ast.Attribute(ast.Name("__state", ast.Load()), "depth", ast.Store())],
                        ast.Constant(0),
                    ),
                ],
            )
        ],
        orelse=[],
        finalbody=[],
    )

    body: list[ast.stmt] = [initialize]
    if per_depth:
        body.append(
            ast.Assign(
                [ast.Name(depth_name, ast.Store())],
                ast.Attribute(ast.Name("__state", ast.Load()), "depth", ast.Load()),
            )
        )
        cached_branch = ast.If(
            ast.Compare(
                ast.Name(depth_name, ast.Load()),
                [ast.Lt()],
                [ast.Name("__limit", ast.Load())],
            ),
            [
                ast.If(
                    ast.Compare(
                        ast.Name(depth_name, ast.Load()),
                        [ast.Eq()],
                        [ast.Call(ast.Name("len", ast.Load()), [ast.Name(pool_name, ast.Load())], [])],
                    ),
                    [
                        ast.Expr(
                            ast.Call(
                                ast.Attribute(ast.Name(pool_name, ast.Load()), "append", ast.Load()),
                                [
                                    ast.Call(
                                        ast.Name("__clone", ast.Load()),
                                        [ast.Name("__template", ast.Load())],
                                        [],
                                    )
                                ],
                                [],
                            )
                        )
                    ],
                    [],
                ),
                ast.Assign(
                    [ast.Name(instance_name, ast.Store())],
                    ast.Subscript(
                        ast.Name(pool_name, ast.Load()),
                        ast.Name(depth_name, ast.Load()),
                        ast.Load(),
                    ),
                ),
            ],
            [
                ast.Assign(
                    [ast.Name(instance_name, ast.Store())],
                    ast.Call(
                        ast.Name("__clone", ast.Load()),
                        [ast.Name("__template", ast.Load())],
                        [],
                    ),
                )
            ],
        )
        body.append(cached_branch)
        body.append(
            ast.Assign(
                [ast.Attribute(ast.Name("__state", ast.Load()), "depth", ast.Store())],
                ast.BinOp(ast.Name(depth_name, ast.Load()), ast.Add(), ast.Constant(1)),
            )
        )
        body.append(
            ast.Try(
                body=[
                    ast.Return(
                        ast.Call(ast.Name(instance_name, ast.Load()), call_args, call_keywords)
                    )
                ],
                handlers=[],
                orelse=[],
                finalbody=[
                    ast.Assign(
                        [ast.Attribute(ast.Name("__state", ast.Load()), "depth", ast.Store())],
                        ast.Name(depth_name, ast.Load()),
                    )
                ],
            )
        )
    else:
        if max_cached_depth == 0:
            body.append(
                ast.Assign(
                    [ast.Name(instance_name, ast.Store())],
                    ast.Call(ast.Name("__clone", ast.Load()), [ast.Name("__template", ast.Load())], []),
                )
            )
        else:
            body.append(
                ast.If(
                    ast.UnaryOp(ast.Not(), ast.Name(pool_name, ast.Load())),
                    [
                        ast.Expr(
                            ast.Call(
                                ast.Attribute(ast.Name(pool_name, ast.Load()), "append", ast.Load()),
                                [
                                    ast.Call(
                                        ast.Name("__clone", ast.Load()),
                                        [ast.Name("__template", ast.Load())],
                                        [],
                                    )
                                ],
                                [],
                            )
                        )
                    ],
                    [],
                )
            )
            body.append(
                ast.Assign(
                    [ast.Name(instance_name, ast.Store())],
                    ast.Subscript(ast.Name(pool_name, ast.Load()), ast.Constant(0), ast.Load()),
                )
            )
        body.append(ast.Return(ast.Call(ast.Name(instance_name, ast.Load()), call_args, call_keywords)))

    definition = ast.FunctionDef(
        name=template.__name__,
        args=arguments,
        body=body,
        decorator_list=[],
        returns=None,
        type_comment=None,
    )
    factory_name = f"__pyswitch_wrapper_factory_{uuid.uuid4().hex}"
    factory = ast.FunctionDef(
        name=factory_name,
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg("__state"), ast.arg("__template"), ast.arg("__clone"), ast.arg("__limit")],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        ),
        body=[definition, ast.Return(ast.Name(template.__name__, ast.Load()))],
        decorator_list=[],
        returns=None,
        type_comment=None,
    )
    module = ast.Module([factory], [])
    ast.fix_missing_locations(module)
    ast.increment_lineno(module, template.__code__.co_firstlineno - 1)
    namespace: dict[str, Any] = {}
    exec(compile(module, template.__code__.co_filename, "exec"), namespace)
    temporary = namespace[factory_name](
        state, template, _clone_isolated_instance, max_cached_depth
    )
    wrapper = types.FunctionType(
        temporary.__code__,
        template.__globals__,
        template.__name__,
        template.__defaults__,
        temporary.__closure__,
    )
    wrapper.__kwdefaults__ = (
        None if template.__kwdefaults__ is None else dict(template.__kwdefaults__)
    )
    return wrapper  # type: ignore[return-value]

def _make_thread_local_wrapper(
    template: F,
    *,
    per_depth: bool,
    max_cached_depth: int = 16,
    expose_debug: bool = False,
) -> F:
    """Route calls to independent code objects with a bounded clone cache."""
    if max_cached_depth < 0:
        raise ValueError("max_cached_depth must be non-negative")
    state = threading.local()

    def reset_state() -> None:
        state.pool = []
        state.depth = 0

    wrapper = _build_specialized_sync_wrapper(
        template,
        state,
        per_depth=per_depth,
        max_cached_depth=max_cached_depth,
    )

    _copy_function_metadata(template, wrapper, expose_wrapped=False)
    wrapper.__pyswitch_mode__ = "isolated" if per_depth else "thread_local"
    wrapper.__pyswitch_backend__ = (
        "cpython313-thread-depth-isolated-v18"
        if per_depth
        else "cpython313-thread-local-v18"
    )
    wrapper.__pyswitch_case_count__ = template.__pyswitch_case_count__
    wrapper.__pyswitch_clear_cache__ = reset_state
    wrapper.__pyswitch_cache_limit__ = max_cached_depth
    wrapper.__pyswitch_cache_info__ = lambda: {
        "cached_clones": len(getattr(state, "pool", ())),
        "active_depth": getattr(state, "depth", 0),
        "limit": max_cached_depth,
    }
    wrapper.__pyswitch_debug__ = {
        "mode": wrapper.__pyswitch_mode__,
        "gate_offsets": getattr(template, "__pyswitch_gate_offsets__", ()),
        "gate_units": getattr(template, "__pyswitch_gate_units__", ()),
    }
    if expose_debug:
        # Deliberately opt-in because directly calling this template bypasses
        # isolation.  It is never exposed by default.
        wrapper.__pyswitch_unsafe_template__ = template
        wrapper.__pyswitch_clone_for_current_thread__ = lambda: (
            getattr(state, "pool", [None])[0]
            if getattr(state, "pool", None)
            else None
        )
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=reset_state)
    _rebind_recursive_self_closure(template, wrapper)
    return wrapper  # type: ignore[return-value]



def _build_specialized_per_call_wrapper(template: F) -> F:
    arguments, call_args, call_keywords = _signature_ast(template)
    clone_assign = ast.Assign(
        [ast.Name("__instance", ast.Store())],
        ast.Call(ast.Name("__clone", ast.Load()), [ast.Name("__template", ast.Load())], []),
    )
    invocation = ast.Call(ast.Name("__instance", ast.Load()), call_args, call_keywords)
    if inspect.iscoroutinefunction(template):
        definition: ast.FunctionDef | ast.AsyncFunctionDef = ast.AsyncFunctionDef(
            template.__name__, arguments, [clone_assign, ast.Return(ast.Await(invocation))], [], None, None
        )
    elif inspect.isgeneratorfunction(template):
        definition = ast.FunctionDef(
            template.__name__, arguments,
            [clone_assign, ast.Return(ast.YieldFrom(invocation))], [], None, None
        )
    else:
        definition = ast.FunctionDef(
            template.__name__, arguments, [clone_assign, ast.Return(invocation)], [], None, None
        )
    factory_name = f"__pyswitch_per_call_factory_{uuid.uuid4().hex}"
    factory = ast.FunctionDef(
        factory_name,
        ast.arguments([], [ast.arg("__template"), ast.arg("__clone")], None, [], [], None, []),
        [definition, ast.Return(ast.Name(template.__name__, ast.Load()))],
        [], None, None,
    )
    module = ast.Module([factory], [])
    ast.fix_missing_locations(module)
    ast.increment_lineno(module, template.__code__.co_firstlineno - 1)
    namespace: dict[str, Any] = {}
    exec(compile(module, template.__code__.co_filename, "exec"), namespace)
    temporary = namespace[factory_name](template, _clone_isolated_instance)
    wrapper = types.FunctionType(
        temporary.__code__, template.__globals__, template.__name__,
        template.__defaults__, temporary.__closure__
    )
    wrapper.__kwdefaults__ = (
        None if template.__kwdefaults__ is None else dict(template.__kwdefaults__)
    )
    return wrapper  # type: ignore[return-value]

def _make_per_call_wrapper(template: F, *, expose_debug: bool = False) -> F:
    """Give every invocation a fresh code object.

    This is the correct live backend for generators and coroutines because the
    frame may remain suspended after the wrapper itself has returned.
    """
    wrapper = _build_specialized_per_call_wrapper(template)

    _copy_function_metadata(template, wrapper, expose_wrapped=False)
    wrapper.__pyswitch_mode__ = "per_call"
    wrapper.__pyswitch_backend__ = "cpython313-per-invocation-isolated-v18"
    wrapper.__pyswitch_case_count__ = template.__pyswitch_case_count__
    if expose_debug:
        wrapper.__pyswitch_unsafe_template__ = template
    _rebind_recursive_self_closure(template, wrapper)
    return wrapper  # type: ignore[return-value]


def _count_case_keys(
    source: str, filename: str, function_name: str | None = None,
    environment: dict[str, Any] | None = None,
) -> int:
    """Count case keys in the decorated lexical function only.

    When an environment is supplied, imported aliases and qualified package/module
    marker references are counted by exact identity as well as historical bare names.
    """
    tree = ast.parse(source, filename)
    marker_environment = environment or {}
    if function_name is None:
        return sum(
            len(node.args)
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Call)
                and _marker_name(node.func, marker_environment) == "case"
            )
        )

    root = _find_function(tree, function_name)

    class Counter(ast.NodeVisitor):
        def __init__(self) -> None:
            self.count = 0
            self.root_seen = False

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if not self.root_seen:
                self.root_seen = True
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if not self.root_seen:
                self.root_seen = True
                self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Call(self, node: ast.Call) -> None:
            if _marker_name(node.func, marker_environment) == "case":
                self.count += len(node.args)
            self.generic_visit(node)

    counter = Counter()
    counter.visit(root)
    return counter.count


@overload
def enable_switch(func: F, /) -> F: ...


@overload
def enable_switch(
    *,
    mode: str = "auto",
    unsafe_shared_slot: bool | None = None,
    source: str | None = None,
    live_threshold: int | None = None,
    portable_match_threshold: int = 5,
    max_cached_depth: int = 16,
    expose_debug: bool = False,
    case_key_mode: str = "python",
    compact_routes: bool | str = False,
) -> Callable[[F], F]: ...


def enable_switch(
    func: F | None = None,
    /,
    *,
    mode: str = "auto",
    unsafe_shared_slot: bool | None = None,
    source: str | None = None,
    live_threshold: int | None = None,
    portable_match_threshold: int = 5,
    max_cached_depth: int = 16,
    expose_debug: bool = False,
    case_key_mode: str = "python",
    compact_routes: bool | str = False,
):
    """Enable switch syntax with portable and O(1)-style live backends.

    Modes:
        auto (default): always select the fastest semantics-safe portable
            backend.  It never mutates executable bytecode.  Supplying an
            explicit live_threshold opts into isolated live dispatch only for
            switches at or above that size.
        portable: no raw memory mutation.  Thread/recursion safe; supports
            closures, generators, coroutines, async generators, guards,
            elif/else defaults, qualified constants, ``with ... as ...``, and
            trailing ``fallthrough()``.  It preserves dictionary hash/equality
            semantics and selects direct-value, expression-template, or
            balanced-route lowering. Complexity ranges from average O(1) for
            specialized table paths to O(log n) for the fully general route.
        isolated: live dictionary jump table with one cached code object per
            thread and active depth.  Sync functions only use the bounded pool;
            generators/coroutines automatically use a fresh clone per call.
        per_call: fresh live code object for every invocation.
        thread_local: one live clone per thread; not re-entry safe.
        fast: one shared live code object; only for proven single-active-call
            execution.

    ``case_key_mode="python"`` uses ordinary Python equality and hashing, so
    ``1``, ``1.0``, and ``True`` collide. ``case_key_mode="typed"`` includes
    the exact runtime type in key identity, keeping those cases distinct.  A
    portable typed plan whose case types all use the ordinary ``type`` metaclass
    is split into raw per-type dictionaries.  Single-type plans use a direct
    exact-type identity fast lane; multi-type plans use an allocation-free O(1)
    type router whose selected getter performs the subject's ordinary dictionary
    hash/equality lookup.  A router miss still hashes the subject once before
    defaulting, so intrinsic unhashables remain misses and genuine user hash
    failures propagate.  Any case type with a custom metaclass keeps the whole
    plan on the conservative historical tuple-key backend.

    ``compact_routes=True`` enables source-location-preserving shared-continuation
    hoisting for fallthrough-heavy general routes. ``compact_routes="auto"`` is an
    opt-in density heuristic that compacts only when the duplicated suffix is large
    enough to justify a join edge. ``False`` remains the speed-first default.

    ``source=`` fixes source retrieval for generated/frozen/notebook functions.
    ``max_cached_depth`` bounds retained isolated clones; deeper calls use
    ephemeral clones. ``portable_match_threshold`` is retained for source/API
    compatibility; the production portable path no longer selects native
    ``match`` because match does not preserve dictionary hashing semantics for
    arbitrary subjects. Unsafe templates are hidden unless ``expose_debug=True``.
    """
    if not isinstance(mode, str):
        raise TypeError("mode must be a string")
    if unsafe_shared_slot is not None and not isinstance(unsafe_shared_slot, bool):
        raise TypeError("unsafe_shared_slot must be bool or None")
    if source is not None and not isinstance(source, str):
        raise TypeError("source must be str or None")
    for parameter_name, value, allow_none in (
        ("live_threshold", live_threshold, True),
        ("portable_match_threshold", portable_match_threshold, False),
        ("max_cached_depth", max_cached_depth, False),
    ):
        if value is None and allow_none:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            suffix = " or None" if allow_none else ""
            raise TypeError(f"{parameter_name} must be an int{suffix}")
        if value < 0:
            suffix = " or None" if allow_none else ""
            raise ValueError(f"{parameter_name} must be non-negative{suffix}")
    if not isinstance(expose_debug, bool):
        raise TypeError("expose_debug must be bool")
    if not isinstance(compact_routes, (bool, str)):
        raise TypeError("compact_routes must be bool or 'auto'")
    if isinstance(compact_routes, str) and compact_routes != "auto":
        raise ValueError("compact_routes string mode must be 'auto'")

    if unsafe_shared_slot is not None:
        if mode != "auto":
            raise TypeError("pass either mode or unsafe_shared_slot, not both")
        mode = "fast" if unsafe_shared_slot else "isolated"

    valid_modes = {"auto", "portable", "fast", "thread_local", "isolated", "per_call"}
    if mode not in valid_modes:
        raise ValueError(f"mode must be one of {sorted(valid_modes)!r}")
    if not isinstance(case_key_mode, str):
        raise TypeError("case_key_mode must be a string")
    case_key_mode = _validate_case_key_mode(case_key_mode)

    def decorate(target: F) -> F:
        if isinstance(target, staticmethod):
            return staticmethod(decorate(target.__func__))  # type: ignore[return-value]
        if isinstance(target, classmethod):
            return classmethod(decorate(target.__func__))  # type: ignore[return-value]
        if not isinstance(target, types.FunctionType):
            raise TypeError("@enable_switch requires a Python function")

        original_code = target.__code__

        def finalize(result: F) -> F:
            actual_mode = getattr(result, "__pyswitch_mode__", mode)
            result.__pyswitch_version__ = __version__
            result.__pyswitch_semantics__ = (
                "python-hash-equality"
                if case_key_mode == "python"
                else "exact-type-hash-equality"
            )
            result.__pyswitch_memory_mutating__ = actual_mode != "portable"
            result.__pyswitch_thread_safe__ = actual_mode != "fast"
            result.__pyswitch_reentrant__ = actual_mode in {
                "portable", "isolated", "per_call"
            }
            report = make_report(
                "switch",
                original_code,
                result.__code__,
                details=(("mode", actual_mode),
                         ("backend", getattr(result, "__pyswitch_backend__", "unknown")),
                         ("case_count", getattr(result, "__pyswitch_case_count__", None)),
                         ("case_key_mode", case_key_mode),
                         ("typed_partition_plans", getattr(
                             result, "__pyswitch_typed_partition_plan_count__", 0
                         )),
                         ("typed_partition_types", getattr(
                             result, "__pyswitch_typed_partition_type_count__", 0
                         )),
                         ("typed_router_plans", getattr(
                             result, "__pyswitch_typed_router_plan_count__", 0
                         )),
                         ("typed_router_types", getattr(
                             result, "__pyswitch_typed_router_type_count__", 0
                         )),
                         ("portable_binary_route_plans", getattr(
                             result, "__pyswitch_binary_route_plan_count__", 0
                         )),
                         ("portable_balanced_plans", getattr(
                             result, "__pyswitch_balanced_plan_count__", 0
                         )),
                         ("shared_continuation_plans", getattr(
                             result, "__pyswitch_shared_continuation_plan_count__", 0
                         )),
                         ("shared_continuation_statements", getattr(
                             result, "__pyswitch_shared_continuation_statement_count__", 0
                         )),
                         ("auto_compact_plans", getattr(
                             result, "__pyswitch_auto_compact_plan_count__", 0
                         )),
                         ("auto_compact_estimated_bytes_saved", getattr(
                             result, "__pyswitch_auto_compact_estimated_bytes_saved__", 0
                         )),
                         ("compact_routes", compact_routes),
                         ("semantics", result.__pyswitch_semantics__),
                         ("memory_mutating", result.__pyswitch_memory_mutating__)),
            )
            attach_report(result, report)
            return result

        explicit_source = source
        recovered_source, _first_line = _source_for_function(target, explicit_source)

        selected_mode = mode
        if selected_mode == "auto":
            case_count = _count_case_keys(
                recovered_source, target.__code__.co_filename, target.__name__,
                _closure_environment(target),
            )
            live_opt_in = (
                live_threshold is not None
                and case_count >= live_threshold
                and _live_runtime_reason() is None
                and not inspect.isasyncgenfunction(target)
            )
            selected_mode = "isolated" if live_opt_in else "portable"

        if selected_mode == "portable":
            return finalize(_compile_portable_adaptive(
                target,
                explicit_source=recovered_source,
                match_threshold=portable_match_threshold,
                case_key_mode=case_key_mode,
                compact_routes=compact_routes,
            ))

        if inspect.isasyncgenfunction(target):
            raise SwitchSyntaxError(
                "async generators require mode='portable'; their asend/athrow "
                "protocol cannot be transparently proxied by the live backend"
            )
        if inspect.isgeneratorfunction(target) or inspect.iscoroutinefunction(target):
            if selected_mode not in {"isolated", "per_call"}:
                raise SwitchSyntaxError(
                    "suspendable functions require mode='isolated', 'per_call', or 'portable'"
                )

        try:
            template = _compile(
                target,
                allow_direct_recursion=(selected_mode in {"isolated", "per_call"}),
                explicit_source=recovered_source,
                case_key_mode=case_key_mode,
            )
        except SwitchError:
            if mode == "auto":
                return finalize(_compile_portable_adaptive(
                    target,
                    explicit_source=recovered_source,
                    match_threshold=portable_match_threshold,
                    case_key_mode=case_key_mode,
                    compact_routes=compact_routes,
                ))
            raise

        template.__pyswitch_mode__ = "fast"
        template.__pyswitch_case_key_mode__ = case_key_mode
        if selected_mode == "fast":
            return finalize(template)
        if selected_mode == "per_call" or inspect.isgeneratorfunction(target) or inspect.iscoroutinefunction(target):
            return finalize(_make_per_call_wrapper(template, expose_debug=expose_debug))
        return finalize(_make_thread_local_wrapper(
            template,
            per_depth=(selected_mode == "isolated"),
            max_cached_depth=max_cached_depth,
            expose_debug=expose_debug,
        ))

    return decorate if func is None else decorate(func)

