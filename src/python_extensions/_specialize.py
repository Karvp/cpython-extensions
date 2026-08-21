from __future__ import annotations

import builtins
import dis
import functools
import inspect
import struct
import sys
import threading
import types
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, TypeVar, overload

from bytecode import Bytecode, Compare, Instr, Label, TryBegin, TryEnd

from ._core import attach_report, make_report, verify_code

F = TypeVar("F", bound=Callable[..., Any])


class SpecializationError(Exception):
    """Base exception for specialization failures."""


class SpecializationUnsupportedError(SpecializationError):
    """Raised when a requested specialization cannot preserve Python semantics."""


class SpecializationLimitError(SpecializationError):
    """Raised when configured variant limits are invalid or exceeded explicitly."""


@dataclass(frozen=True)
class SpecializationStats:
    calls: int
    variant_hits: int
    fallback_calls: int
    variants_created: int
    variants_rejected: int
    profiled_shapes: int
    profile_calls: int
    profile_evictions: int
    profile_budget_exhausted: bool
    profiling_active: bool
    runtime_metrics: bool


@dataclass(frozen=True)
class PartialStats:
    constants_bound: int
    constant_loads_rewritten: int
    type_predicates_folded: int
    constant_branches_folded: int
    constant_expressions_folded: int
    dead_instructions_pruned: int
    redundant_jumps_removed: int
    original_code_bytes: int
    final_code_bytes: int
    estimated_original_path_instructions: int | None
    estimated_specialized_path_instructions: int | None
    estimated_executed_instructions_removed: int | None


@dataclass(frozen=True)
class _ConstantGuard:
    name: str
    value: Any
    safe_key: Any | None

    def matches(self, current: Any) -> bool:
        if self.safe_key is None:
            return current is self.value
        key = _adaptive_constant_key(current)
        return key is not None and key == self.safe_key


@dataclass(frozen=True)
class _Variant:
    function: Callable[..., Any]
    constant_guards: tuple[_ConstantGuard, ...]
    type_guards: tuple[tuple[str, type[Any]], ...]
    key: tuple[Any, ...]
    details: tuple[tuple[str, object], ...]

    def matches(self, values: Mapping[str, Any]) -> bool:
        for guard in self.constant_guards:
            try:
                value = values[guard.name]
            except KeyError:
                return False
            if not guard.matches(value):
                return False
        for name, expected in self.type_guards:
            try:
                value = values[name]
            except KeyError:
                return False
            if type(value) is not expected:
                return False
        return True


_MISSING = object()
_LOAD_FAST_SIMPLE = frozenset({"LOAD_FAST", "LOAD_FAST_CHECK", "LOAD_FAST_BORROW"})
_LOAD_FAST_PAIRED = frozenset({"LOAD_FAST_LOAD_FAST", "LOAD_FAST_BORROW_LOAD_FAST_BORROW"})
_SAFE_SCALAR_TYPES = frozenset({type(None), bool, int, str, bytes})
_INLINE_VALUE_GUARD_TYPES = frozenset({type(None), bool, int, str, bytes})


def _unwrap_descriptor(value: Any) -> tuple[Callable[..., Any], type[staticmethod] | type[classmethod] | None]:
    if isinstance(value, staticmethod):
        return value.__func__, staticmethod
    if isinstance(value, classmethod):
        return value.__func__, classmethod
    if not isinstance(value, types.FunctionType):
        raise TypeError("python_extensions specialization decorators require a Python function")
    return value, None


def _rewrap_descriptor(function: F, descriptor: type[staticmethod] | type[classmethod] | None):
    if descriptor is staticmethod:
        return staticmethod(function)
    if descriptor is classmethod:
        return classmethod(function)
    return function


def _copy_function_metadata(source: Callable[..., Any], target: Callable[..., Any]) -> None:
    target.__annotations__ = dict(getattr(source, "__annotations__", {}))
    target.__dict__.update(getattr(source, "__dict__", {}))
    target.__module__ = source.__module__
    target.__qualname__ = source.__qualname__
    target.__doc__ = source.__doc__
    if hasattr(source, "__type_params__"):
        target.__type_params__ = source.__type_params__


def _variant_key(
    guards: Iterable[_ConstantGuard],
    type_guards: Mapping[str, type[Any]],
) -> tuple[Any, ...]:
    constant_parts = []
    for guard in guards:
        identity = guard.safe_key if guard.safe_key is not None else ("identity", id(guard.value))
        constant_parts.append((guard.name, identity))
    type_parts = [(name, expected) for name, expected in type_guards.items()]
    return (
        tuple(sorted(constant_parts, key=lambda item: item[0])),
        tuple(sorted(type_parts, key=lambda item: item[0])),
    )


def _adaptive_constant_key(value: Any) -> Any | None:
    """Return a side-effect-free hashable key for constants safe to profile.

    Adaptive profiling deliberately excludes arbitrary objects so shape discovery
    never invokes user ``__hash__``/``__eq__`` methods and never retains arbitrary
    application objects merely because they appeared at a call site.
    """

    kind = type(value)
    if kind in _SAFE_SCALAR_TYPES:
        return (kind, value)
    if kind is float:
        return (float, struct.pack("!d", value))
    if kind is complex:
        return (
            complex,
            struct.pack("!d", value.real),
            struct.pack("!d", value.imag),
        )
    if kind is tuple:
        parts = []
        for item in value:
            key = _adaptive_constant_key(item)
            if key is None:
                return None
            parts.append(key)
        return (tuple, tuple(parts))
    if kind is frozenset:
        parts = []
        for item in value:
            key = _adaptive_constant_key(item)
            if key is None:
                return None
            parts.append(key)
        return (frozenset, frozenset(parts))
    return None


def _constant_guard(name: str, value: Any, *, adaptive: bool) -> _ConstantGuard | None:
    key = _adaptive_constant_key(value)
    if adaptive and key is None:
        return None
    return _ConstantGuard(name=name, value=value, safe_key=key)


@dataclass(frozen=True)
class _KnownExactTypeValue:
    expected: type[Any]


_ABSTRACT_UNKNOWN = object()
_ABSTRACT_NULL = object()


def _safe_abstract_equal(left: Any, right: Any) -> bool | None:
    if isinstance(left, _KnownExactTypeValue) or isinstance(right, _KnownExactTypeValue):
        return None
    if _adaptive_constant_key(left) is None or _adaptive_constant_key(right) is None:
        return None
    try:
        return bool(left == right)
    except Exception:
        return None


def _safe_abstract_truth(value: Any) -> bool | None:
    if isinstance(value, _KnownExactTypeValue) or value is _ABSTRACT_UNKNOWN:
        return None
    if _adaptive_constant_key(value) is None:
        return None
    try:
        return bool(value)
    except Exception:
        return None


def _estimate_known_path_instructions(
    function: Callable[..., Any],
    *,
    constants: Mapping[str, Any] | None = None,
    exact_types: Mapping[str, type[Any]] | None = None,
) -> int | None:
    """Count one deterministically proven execution path without running user code.

    The interpreter understands only side-effect-free argument/literal/type tests.
    If a conditional outcome depends on anything else it returns ``None`` rather
    than guessing. This makes profitability conservative while still covering the
    large value/type dispatch chains that specialization is designed to collapse.
    """

    constants = {} if constants is None else constants
    exact_types = {} if exact_types is None else exact_types
    instructions = list(dis.get_instructions(function))
    if not instructions:
        return 0
    by_offset = {instruction.offset: index for index, instruction in enumerate(instructions)}
    stack: list[Any] = []
    index = 0
    executed = 0
    max_steps = max(32, len(instructions) * 4)

    for _ in range(max_steps):
        if not 0 <= index < len(instructions):
            return None
        instruction = instructions[index]
        opname = instruction.opname
        executed += 1
        advance = True

        if opname in {"RESUME", "EXTENDED_ARG", "NOP", "CACHE", "COPY_FREE_VARS"}:
            pass
        elif opname == "LOAD_FAST":
            name = instruction.argval
            if name in constants:
                stack.append(constants[name])
            elif name in exact_types:
                stack.append(_KnownExactTypeValue(exact_types[name]))
            else:
                stack.append(_ABSTRACT_UNKNOWN)
        elif opname == "LOAD_CONST":
            stack.append(instruction.argval)
        elif opname == "LOAD_GLOBAL":
            if "NULL +" in instruction.argrepr:
                stack.append(_ABSTRACT_NULL)
            stack.append(_global_value(function, str(instruction.argval)))
        elif opname == "PUSH_NULL":
            stack.append(_ABSTRACT_NULL)
        elif opname == "CALL":
            argc = int(instruction.arg or 0)
            if len(stack) < argc + 1:
                return None
            arguments = [stack.pop() for _ in range(argc)][::-1]
            callable_value = stack.pop()
            if stack and stack[-1] is _ABSTRACT_NULL:
                stack.pop()
            result: Any = _ABSTRACT_UNKNOWN
            if callable_value is builtins.type and len(arguments) == 1:
                argument = arguments[0]
                if isinstance(argument, _KnownExactTypeValue):
                    result = argument.expected
                elif argument is not _ABSTRACT_UNKNOWN:
                    result = type(argument)
            elif callable_value is builtins.isinstance and len(arguments) == 2:
                value, checked = arguments
                if (
                    isinstance(value, _KnownExactTypeValue)
                    and isinstance(checked, type)
                    and type(checked) is type
                ):
                    result = checked in type.__getattribute__(value.expected, "__mro__")
            stack.append(result)
        elif opname == "COMPARE_OP":
            if len(stack) < 2:
                return None
            right = stack.pop()
            left = stack.pop()
            result: Any = _ABSTRACT_UNKNOWN
            if instruction.argval in {"==", "!="}:
                equal = _safe_abstract_equal(left, right)
                if equal is not None:
                    result = equal if instruction.argval == "==" else not equal
            stack.append(result)
        elif opname == "IS_OP":
            if len(stack) < 2:
                return None
            right = stack.pop()
            left = stack.pop()
            if left is _ABSTRACT_UNKNOWN or right is _ABSTRACT_UNKNOWN:
                stack.append(_ABSTRACT_UNKNOWN)
            else:
                result = left is right
                stack.append(not result if instruction.arg else result)
        elif opname == "CONTAINS_OP":
            if len(stack) < 2:
                return None
            container = stack.pop()
            member = stack.pop()
            result: Any = _ABSTRACT_UNKNOWN
            if (
                _adaptive_constant_key(member) is not None
                and _adaptive_constant_key(container) is not None
                and type(container) in {tuple, frozenset, str, bytes}
            ):
                try:
                    result = member in container
                    if instruction.arg:
                        result = not result
                except Exception:
                    result = _ABSTRACT_UNKNOWN
            stack.append(result)
        elif opname in {"POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE"}:
            if not stack:
                return None
            truth = _safe_abstract_truth(stack.pop())
            if truth is None:
                return None
            take = (not truth) if opname.endswith("FALSE") else truth
            if take:
                target = by_offset.get(instruction.argval)
                if target is None:
                    return None
                index = target
                advance = False
        elif opname in {"POP_JUMP_IF_NONE", "POP_JUMP_IF_NOT_NONE"}:
            if not stack:
                return None
            value = stack.pop()
            if value is _ABSTRACT_UNKNOWN or isinstance(value, _KnownExactTypeValue):
                return None
            take = (value is None) if opname.endswith("IF_NONE") else (value is not None)
            if take:
                target = by_offset.get(instruction.argval)
                if target is None:
                    return None
                index = target
                advance = False
        elif opname.startswith("JUMP"):
            target = by_offset.get(instruction.argval)
            if target is None:
                return None
            index = target
            advance = False
        elif opname in {"RETURN_VALUE", "RETURN_CONST"}:
            return executed
        else:
            # Keep enough abstract stack shape to traverse straight-line result
            # computation, but never infer a branch value from an unknown opcode.
            try:
                effect = dis.stack_effect(instruction.opcode, instruction.arg)
            except (TypeError, ValueError):
                effect = 0
            if effect < 0:
                for _ in range(min(len(stack), -effect)):
                    stack.pop()
            elif effect > 0:
                stack.extend([_ABSTRACT_UNKNOWN] * effect)
            # Unknown instructions may reorder or transform more than the net
            # stack effect reveals. Forget every surviving abstract value so a
            # later branch can never be "proved" from stale stack knowledge.
            for stack_index in range(len(stack)):
                stack[stack_index] = _ABSTRACT_UNKNOWN

        if advance:
            index += 1
    return None


def _safe_type_qualname(value: type[Any]) -> str:
    # Read type metadata through the builtin descriptor implementation so a
    # custom metaclass cannot observe specialization bookkeeping.
    try:
        return type.__getattribute__(value, "__qualname__")
    except (AttributeError, TypeError):
        return "<type>"


def _global_value(function: Callable[..., Any], name: str) -> Any:
    globals_dict = function.__globals__
    if name in globals_dict:
        return globals_dict[name]
    builtins_obj = function.__builtins__
    if isinstance(builtins_obj, dict):
        return builtins_obj.get(name, _MISSING)
    try:
        return getattr(builtins_obj, name)
    except AttributeError:
        return _MISSING


def _instr_fast_names(item: Instr) -> tuple[str, ...]:
    if "FAST" not in item.name:
        return ()
    arg = item.arg
    if isinstance(arg, str):
        return (arg,)
    if isinstance(arg, tuple):
        return tuple(value for value in arg if isinstance(value, str))
    return ()


def _parameter_readonly(items: Iterable[Any], name: str) -> bool:
    for item in items:
        if not isinstance(item, Instr):
            continue
        names = _instr_fast_names(item)
        if name not in names:
            continue
        if item.name in _LOAD_FAST_SIMPLE or item.name in _LOAD_FAST_PAIRED:
            continue
        return False
    return True


def _replace_constant_loads(items: list[Any], constants: Mapping[str, Any]) -> tuple[list[Any], int]:
    result: list[Any] = []
    rewrites = 0
    for item in items:
        if not isinstance(item, Instr):
            result.append(item)
            continue
        if item.name in _LOAD_FAST_SIMPLE and isinstance(item.arg, str) and item.arg in constants:
            result.append(Instr("LOAD_CONST", constants[item.arg], location=item.location))
            rewrites += 1
            continue
        if item.name in _LOAD_FAST_PAIRED and isinstance(item.arg, tuple) and any(name in constants for name in item.arg):
            for name in item.arg:
                if name in constants:
                    result.append(Instr("LOAD_CONST", constants[name], location=item.location))
                    rewrites += 1
                else:
                    result.append(Instr("LOAD_FAST", name, location=item.location))
            continue
        result.append(item)
    return result, rewrites


def _resolved_global_name(item: Any) -> str | None:
    if not isinstance(item, Instr) or item.name != "LOAD_GLOBAL":
        return None
    arg = item.arg
    if isinstance(arg, tuple) and len(arg) == 2 and isinstance(arg[1], str):
        return arg[1]
    if isinstance(arg, str):
        return arg
    return None


def _fold_exact_type_predicates(
    function: Callable[..., Any],
    items: list[Any],
    exact_types: Mapping[str, type[Any]],
) -> tuple[list[Any], int]:
    """Fold side-effect-free exact-type predicates proven by a variant guard."""

    if not exact_types:
        return items, 0
    result = list(items)
    folds = 0
    i = 0
    while i < len(result):
        # type(x) is SomeClass
        if i + 4 < len(result):
            global_type = _resolved_global_name(result[i])
            arg = result[i + 1]
            call = result[i + 2]
            class_load = result[i + 3]
            is_op = result[i + 4]
            if (
                global_type == "type"
                and _global_value(function, "type") is builtins.type
                and isinstance(arg, Instr)
                and arg.name in _LOAD_FAST_SIMPLE
                and isinstance(arg.arg, str)
                and arg.arg in exact_types
                and isinstance(call, Instr)
                and call.name == "CALL"
                and call.arg == 1
                and isinstance(is_op, Instr)
                and is_op.name == "IS_OP"
                and is_op.arg in {0, 1}
            ):
                checked_name = _resolved_global_name(class_load)
                if checked_name is not None:
                    checked = _global_value(function, checked_name)
                    if isinstance(checked, type):
                        value = exact_types[arg.arg] is checked
                        if is_op.arg == 1:
                            value = not value
                        result[i : i + 5] = [Instr("LOAD_CONST", value, location=is_op.location)]
                        folds += 1
                        continue

        # isinstance(x, SomeClass). Only fold ordinary classes whose metaclass is
        # exactly type, avoiding custom __instancecheck__ semantics.
        if i + 3 < len(result):
            global_isinstance = _resolved_global_name(result[i])
            arg = result[i + 1]
            class_load = result[i + 2]
            call = result[i + 3]
            if (
                global_isinstance == "isinstance"
                and _global_value(function, "isinstance") is builtins.isinstance
                and isinstance(arg, Instr)
                and arg.name in _LOAD_FAST_SIMPLE
                and isinstance(arg.arg, str)
                and arg.arg in exact_types
                and isinstance(call, Instr)
                and call.name == "CALL"
                and call.arg == 2
            ):
                checked_name = _resolved_global_name(class_load)
                if checked_name is not None:
                    checked = _global_value(function, checked_name)
                    if isinstance(checked, type) and type(checked) is type:
                        expected = exact_types[arg.arg]
                        value = checked in type.__getattribute__(expected, "__mro__")
                        result[i : i + 4] = [Instr("LOAD_CONST", value, location=call.location)]
                        folds += 1
                        continue
        i += 1
    return result, folds



def _fold_special_constant_ops(items: list[Any]) -> tuple[list[Any], int]:
    """Fold constant identity/membership operations without invoking user code."""

    result = list(items)
    folds = 0
    index = 0
    while index + 2 < len(result):
        left, right, op = result[index : index + 3]
        if not (
            isinstance(left, Instr)
            and left.name == "LOAD_CONST"
            and isinstance(right, Instr)
            and right.name == "LOAD_CONST"
            and isinstance(op, Instr)
        ):
            index += 1
            continue
        value: bool | None = None
        if op.name == "IS_OP" and op.arg in {0, 1}:
            value = left.arg is right.arg
            if op.arg == 1:
                value = not value
        elif op.name == "CONTAINS_OP" and op.arg in {0, 1}:
            left_key = _adaptive_constant_key(left.arg)
            right_key = _adaptive_constant_key(right.arg)
            if left_key is not None and right_key is not None and type(right.arg) in {tuple, frozenset, str, bytes}:
                value = left.arg in right.arg
                if op.arg == 1:
                    value = not value
        if value is None:
            index += 1
            continue
        result[index : index + 3] = [Instr("LOAD_CONST", value, location=op.location)]
        folds += 1
        index += 1
    return result, folds


def _fold_special_constant_branches(items: list[Any]) -> tuple[list[Any], int]:
    result = list(items)
    folds = 0
    index = 0
    while index + 1 < len(result):
        value = result[index]
        branch = result[index + 1]
        if not (
            isinstance(value, Instr)
            and value.name == "LOAD_CONST"
            and isinstance(branch, Instr)
            and branch.name in {"POP_JUMP_IF_NONE", "POP_JUMP_IF_NOT_NONE"}
            and isinstance(branch.arg, Label)
        ):
            index += 1
            continue
        is_none = value.arg is None
        jump_taken = is_none if branch.name == "POP_JUMP_IF_NONE" else not is_none
        if jump_taken:
            try:
                target_index = result.index(branch.arg)
            except ValueError:
                index += 1
                continue
            opname = "JUMP_FORWARD" if target_index > index + 1 else "JUMP_BACKWARD"
            replacement: list[Any] = [Instr(opname, branch.arg, location=branch.location)]
        else:
            replacement = []
        result[index : index + 2] = replacement
        folds += 1
        index += len(replacement)
    return result, folds

def _run_constant_optimizer(items: list[Any]) -> tuple[list[Any], int, int, int, int]:
    # Reuse the already-hardened, side-effect-free constant/dataflow passes from
    # the inliner. Keeping one implementation prevents semantic drift between
    # inlining and specialization.
    from .inline import (
        _fold_constant_branches,
        _fold_constant_expression_fixpoint,
        _prune_unreachable_items,
        _remove_redundant_jumps,
    )

    result = list(items)
    expression_folds = branch_folds = dead_pruned = jumps_removed = 0
    while True:
        before = len(result)
        result, special = _fold_special_constant_ops(result)
        result, unary, binary, comparisons = _fold_constant_expression_fixpoint(result)
        expression_folds += special + unary + binary + comparisons
        result, special_branches = _fold_special_constant_branches(result)
        result, branches = _fold_constant_branches(result)
        branch_folds += special_branches + branches
        result, dead = _prune_unreachable_items(result)
        dead_pruned += dead
        result, jumps = _remove_redundant_jumps(result)
        jumps_removed += jumps
        if len(result) == before and special + unary + binary + comparisons + special_branches + branches + dead + jumps == 0:
            break
    return result, expression_folds, branch_folds, dead_pruned, jumps_removed


def _signature_parts(function: Callable[..., Any]) -> tuple[inspect.Signature, list[inspect.Parameter]]:
    try:
        signature = inspect.signature(function, follow_wrapped=False)
    except (TypeError, ValueError) as exc:
        raise SpecializationUnsupportedError(
            f"{function.__qualname__}: cannot inspect function signature"
        ) from exc
    return signature, list(signature.parameters.values())


def _validate_names(function: Callable[..., Any], names: Iterable[str]) -> tuple[str, ...]:
    signature, _ = _signature_parts(function)
    result: list[str] = []
    for name in names:
        if not isinstance(name, str):
            raise TypeError("specialization parameter names must be strings")
        parameter = signature.parameters.get(name)
        if parameter is None:
            raise SpecializationUnsupportedError(
                f"{function.__qualname__}: unknown parameter {name!r}"
            )
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            raise SpecializationUnsupportedError(
                f"{function.__qualname__}: cannot specialize variadic parameter {name!r}"
            )
        if name not in result:
            result.append(name)
    return tuple(result)


def _defaults_for_remaining(parameters: list[inspect.Parameter]) -> tuple[Any, ...] | None:
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    defaults = [parameter.default for parameter in positional if parameter.default is not inspect._empty]
    return tuple(defaults) if defaults else None


def _kwdefaults_for_remaining(parameters: list[inspect.Parameter]) -> dict[str, Any] | None:
    result = {
        parameter.name: parameter.default
        for parameter in parameters
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is not inspect._empty
    }
    return result or None


def _insert_bound_local_initializers(items: list[Any], constants: Mapping[str, Any]) -> list[Any]:
    if not constants:
        return items
    result = list(items)
    insert_at = 0
    location = None
    for index, item in enumerate(result):
        if isinstance(item, Instr) and item.name == "RESUME":
            insert_at = index + 1
            location = item.location
            break
    init: list[Instr] = []
    for name, value in constants.items():
        init.append(Instr("LOAD_CONST", value, location=location))
        init.append(Instr("STORE_FAST", name, location=location))
    result[insert_at:insert_at] = init
    return result


def _specialize_code(
    function: F,
    *,
    constants: Mapping[str, Any] | None = None,
    exact_types: Mapping[str, type[Any]] | None = None,
    remove_bound_parameters: bool = False,
) -> tuple[F, PartialStats]:
    constants = dict(constants or {})
    exact_types = dict(exact_types or {})
    _validate_names(function, (*constants.keys(), *exact_types.keys()))
    for name, expected in exact_types.items():
        if not isinstance(expected, type):
            raise TypeError(f"exact type specialization for {name!r} must be a type")

    original_code = function.__code__
    bytecode = Bytecode.from_code(original_code)
    items = list(bytecode)

    if remove_bound_parameters:
        cell_names = set(original_code.co_cellvars)
        blocked = cell_names.intersection(constants)
        if blocked:
            names = ", ".join(sorted(blocked))
            raise SpecializationUnsupportedError(
                f"{function.__qualname__}: bound cell parameter(s) are not yet supported: {names}"
            )

    readonly_constants = {
        name: value
        for name, value in constants.items()
        if _parameter_readonly(items, name)
    }
    readonly_types = {
        name: value
        for name, value in exact_types.items()
        if _parameter_readonly(items, name)
    }
    estimated_original_path = _estimate_known_path_instructions(
        function,
        constants=readonly_constants,
        exact_types=readonly_types,
    )

    if remove_bound_parameters:
        # Preserve the original parameter-local observability (locals(), tracing,
        # and later STORE_FAST assignments) even though the public signature no
        # longer accepts the bound parameter.
        items = _insert_bound_local_initializers(items, constants)

    items, load_rewrites = _replace_constant_loads(items, readonly_constants)
    items, type_folds = _fold_exact_type_predicates(function, items, readonly_types)
    items, expression_folds, branch_folds, dead_pruned, jumps_removed = _run_constant_optimizer(items)

    if remove_bound_parameters:
        signature, parameters = _signature_parts(function)
        remaining = [parameter for parameter in parameters if parameter.name not in constants]
        positional = [
            parameter
            for parameter in remaining
            if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        posonly = sum(parameter.kind is inspect.Parameter.POSITIONAL_ONLY for parameter in remaining)
        kwonly = sum(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in remaining)
        vararg = next((parameter for parameter in remaining if parameter.kind is inspect.Parameter.VAR_POSITIONAL), None)
        varkw = next((parameter for parameter in remaining if parameter.kind is inspect.Parameter.VAR_KEYWORD), None)
        argnames = [parameter.name for parameter in positional]
        argnames.extend(parameter.name for parameter in remaining if parameter.kind is inspect.Parameter.KEYWORD_ONLY)
        if vararg is not None:
            argnames.append(vararg.name)
        if varkw is not None:
            argnames.append(varkw.name)
        bytecode.argnames = argnames
        bytecode.argcount = len(positional)
        bytecode.posonlyargcount = posonly
        bytecode.kwonlyargcount = kwonly

    bytecode.clear()
    bytecode.extend(items)
    try:
        new_code = bytecode.to_code()
    except Exception as exc:
        raise SpecializationUnsupportedError(
            f"{function.__qualname__}: specialized bytecode could not be assembled"
        ) from exc

    if remove_bound_parameters:
        _, parameters = _signature_parts(function)
        remaining = [parameter for parameter in parameters if parameter.name not in constants]
        defaults = _defaults_for_remaining(remaining)
        kwdefaults = _kwdefaults_for_remaining(remaining)
    else:
        defaults = function.__defaults__
        kwdefaults = None if function.__kwdefaults__ is None else dict(function.__kwdefaults__)

    rebuilt = types.FunctionType(
        new_code,
        function.__globals__,
        function.__name__,
        defaults,
        function.__closure__,
    )
    rebuilt.__kwdefaults__ = kwdefaults
    _copy_function_metadata(function, rebuilt)
    if remove_bound_parameters:
        annotations = dict(getattr(function, "__annotations__", {}))
        for name in constants:
            annotations.pop(name, None)
        rebuilt.__annotations__ = annotations
    verify_code(rebuilt.__code__)
    estimated_specialized_path = _estimate_known_path_instructions(rebuilt)
    if estimated_original_path is not None and estimated_specialized_path is not None:
        estimated_removed = max(0, estimated_original_path - estimated_specialized_path)
    else:
        estimated_removed = None

    stats = PartialStats(
        constants_bound=len(constants),
        constant_loads_rewritten=load_rewrites,
        type_predicates_folded=type_folds,
        constant_branches_folded=branch_folds,
        constant_expressions_folded=expression_folds,
        dead_instructions_pruned=dead_pruned,
        redundant_jumps_removed=jumps_removed,
        original_code_bytes=len(original_code.co_code),
        final_code_bytes=len(rebuilt.__code__.co_code),
        estimated_original_path_instructions=estimated_original_path,
        estimated_specialized_path_instructions=estimated_specialized_path,
        estimated_executed_instructions_removed=estimated_removed,
    )
    details = (
        ("constants_bound", stats.constants_bound),
        ("constant_loads_rewritten", stats.constant_loads_rewritten),
        ("type_predicates_folded", stats.type_predicates_folded),
        ("constant_branches_folded", stats.constant_branches_folded),
        ("constant_expressions_folded", stats.constant_expressions_folded),
        ("dead_instructions_pruned", stats.dead_instructions_pruned),
        ("redundant_jumps_removed", stats.redundant_jumps_removed),
        ("estimated_original_path_instructions", stats.estimated_original_path_instructions),
        ("estimated_specialized_path_instructions", stats.estimated_specialized_path_instructions),
        ("estimated_executed_instructions_removed", stats.estimated_executed_instructions_removed),
    )
    attach_report(rebuilt, make_report("partial" if remove_bound_parameters else "specialize", original_code, rebuilt.__code__, details=details))
    rebuilt.__python_extensions_partial_stats__ = stats
    return rebuilt, stats  # type: ignore[return-value]


def _bound_constants(function: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    signature, _ = _signature_parts(function)
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError as exc:
        raise SpecializationUnsupportedError(str(exc)) from exc
    constants = dict(bound.arguments)
    for name in tuple(constants):
        parameter = signature.parameters[name]
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            raise SpecializationUnsupportedError(
                f"{function.__qualname__}: partial binding of *args/**kwargs is not supported"
            )
    return constants


def _partial_impl(function: F, args: tuple[Any, ...], kwargs: dict[str, Any]) -> F:
    constants = _bound_constants(function, args, kwargs)
    signature, parameters = _signature_parts(function)
    if constants and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
        raise SpecializationUnsupportedError(
            f"{function.__qualname__}: partial binding with **kwargs is not yet supported; "
            "a removed bound name could otherwise be captured by the variadic mapping"
        )
    if not constants:
        raise SpecializationUnsupportedError(
            f"{function.__qualname__}: partial requires at least one bound argument"
        )
    specialized, _ = _specialize_code(
        function,
        constants=constants,
        remove_bound_parameters=True,
    )
    signature = inspect.signature(function, follow_wrapped=False)
    specialized.__signature__ = signature.replace(
        parameters=[p for p in signature.parameters.values() if p.name not in constants]
    )
    specialized.__python_extensions_partial_constants__ = dict(constants)
    return specialized


@overload
def partial(function: F, /, *args: Any, **kwargs: Any) -> F: ...


@overload
def partial(*args: Any, **kwargs: Any) -> Callable[[F], F]: ...


def partial(function: F | None = None, /, *args: Any, **kwargs: Any):
    """Partially evaluate a Python function by freezing selected arguments.

    Functional form mirrors the useful part of ``functools.partial`` while
    producing a real transformed Python function with the bound parameters removed
    from its call signature::

        fast = partial(parse, mode="fast")

    Decorator form is also supported when binding by keyword::

        @partial(mode="fast")
        def parse(data, mode="safe"): ...

    Bound parameters are initialized as locals in the transformed frame, preserving
    ``locals()`` visibility and later local reassignment. Read-only bound parameters
    are additionally propagated as constants and may remove dead branches.
    """

    if function is None:
        if args:
            raise TypeError("decorator-form partial accepts bound arguments by keyword")
        def decorate(target: F) -> F:
            base, descriptor = _unwrap_descriptor(target)
            result = _partial_impl(base, (), dict(kwargs))
            return _rewrap_descriptor(result, descriptor)  # type: ignore[return-value]
        return decorate
    base, descriptor = _unwrap_descriptor(function)
    result = _partial_impl(base, args, dict(kwargs))
    return _rewrap_descriptor(result, descriptor)


class _ArgumentExtractor:
    __slots__ = ("parameters", "needed", "positional_names", "posonly_count")

    def __init__(self, function: Callable[..., Any], needed: Iterable[str]) -> None:
        signature, parameters = _signature_parts(function)
        self.parameters = tuple(parameters)
        self.needed = frozenset(needed)
        self.positional_names = tuple(
            p.name for p in parameters
            if p.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        self.posonly_count = sum(p.kind is inspect.Parameter.POSITIONAL_ONLY for p in parameters)

    def extract(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any] | None:
        if len(args) > len(self.positional_names) and not any(
            p.kind is inspect.Parameter.VAR_POSITIONAL for p in self.parameters
        ):
            return None
        result: dict[str, Any] = {}
        positional_index = 0
        for parameter in self.parameters:
            name = parameter.name
            kind = parameter.kind
            if kind is inspect.Parameter.POSITIONAL_ONLY:
                if positional_index < len(args):
                    value = args[positional_index]
                elif parameter.default is not inspect._empty:
                    value = parameter.default
                else:
                    return None
                positional_index += 1
            elif kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
                if positional_index < len(args):
                    value = args[positional_index]
                elif name in kwargs:
                    value = kwargs[name]
                elif parameter.default is not inspect._empty:
                    value = parameter.default
                else:
                    return None
                positional_index += 1
            elif kind is inspect.Parameter.KEYWORD_ONLY:
                if name in kwargs:
                    value = kwargs[name]
                elif parameter.default is not inspect._empty:
                    value = parameter.default
                else:
                    return None
            elif kind is inspect.Parameter.VAR_POSITIONAL:
                if name in self.needed:
                    return None
                continue
            else:  # VAR_KEYWORD
                if name in self.needed:
                    return None
                continue
            if name in self.needed:
                result[name] = value
        return result


def _normalize_type_constraints(function: Callable[..., Any], value: Mapping[str, type[Any]] | None) -> dict[str, type[Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("types= must be a mapping of parameter name to exact type")
    names = _validate_names(function, value.keys())
    result: dict[str, type[Any]] = {}
    for name in names:
        expected = value[name]
        if not isinstance(expected, type):
            raise TypeError(f"types[{name!r}] must be a type")
        result[name] = expected
    return result


def _normalize_constant_constraints(function: Callable[..., Any], value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("constants= must be a mapping of parameter name to value")
    names = _validate_names(function, value.keys())
    return {name: value[name] for name in names}


def _variant_profitable(stats: PartialStats, policy: str, execution_mode: str) -> bool:
    if policy == "always":
        return True
    if policy != "speed":
        raise ValueError("policy must be 'speed' or 'always'")
    removed = stats.estimated_executed_instructions_removed
    if removed is not None:
        # In-frame variants pay a guard but no extra Python call; wrapper variants
        # pay both a guard and an additional call boundary. Use conservative
        # instruction-equivalent crossover points calibrated by the benchmark
        # harness rather than code-size deletion.
        return removed >= (8 if execution_mode == "inline" else 16)
    # If path estimation cannot prove a deterministic route, retain a conservative
    # fallback score based only on side-effect-free transformations.
    runtime_savings_score = (
        stats.type_predicates_folded * 4
        + stats.constant_branches_folded * 3
        + stats.constant_expressions_folded
    )
    return runtime_savings_score >= (32 if execution_mode == "inline" else 48)


class _SpecializationDispatcher:
    def __init__(
        self,
        function: F,
        *,
        policy: str,
        profile_type_names: tuple[str, ...] = (),
        profile_constant_names: tuple[str, ...] = (),
        threshold: int = 64,
        max_variants: int = 4,
        max_profiled_shapes: int = 64,
        profile_budget: int | None = None,
        runtime_metrics: bool = True,
        execution_mode: str = "wrapper",
    ) -> None:
        if policy not in {"speed", "always"}:
            raise ValueError("policy must be 'speed' or 'always'")
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if max_variants < 1:
            raise ValueError("max_variants must be >= 1")
        if max_profiled_shapes < 1:
            raise ValueError("max_profiled_shapes must be >= 1")
        if profile_budget is not None and profile_budget < 1:
            raise ValueError("profile_budget must be >= 1 or None")
        if execution_mode not in {"wrapper", "inline"}:
            raise ValueError("execution_mode must be 'wrapper' or 'inline'")
        self.function = function
        self.policy = policy
        self.execution_mode = execution_mode
        self.runtime_metrics = bool(runtime_metrics)
        self._token = uuid.uuid4().hex
        self._dispatch_globals: dict[str, Any] = {"__builtins__": function.__builtins__}
        self.wrapper: Callable[..., Any] | None = None
        self.threshold = threshold
        self.max_variants = max_variants
        self.max_profiled_shapes = max_profiled_shapes
        self.profile_budget = (
            profile_budget
            if profile_budget is not None
            else max(1024, threshold * max_profiled_shapes * 8)
        )
        self.profile_type_names = profile_type_names
        self.profile_constant_names = profile_constant_names
        self._profile_extractor = _ArgumentExtractor(
            function, (*profile_type_names, *profile_constant_names)
        ) if (profile_type_names or profile_constant_names) else None
        self._variants: tuple[_Variant, ...] = ()
        self._lock = threading.RLock()
        self._shape_counts: Counter[Any] = Counter()
        self._blocked_shapes: set[Any] = set()
        self._calls = 0
        self._variant_hits = 0
        self._fallback_calls = 0
        self._variants_rejected = 0
        self._profile_calls = 0
        self._profile_evictions = 0
        self._profile_budget_exhausted = False
        self._profiling_active = self._profile_extractor is not None

    @property
    def variants(self) -> tuple[_Variant, ...]:
        return self._variants

    def stats(self) -> SpecializationStats:
        return SpecializationStats(
            calls=self._calls,
            variant_hits=self._variant_hits,
            fallback_calls=self._fallback_calls,
            variants_created=len(self._variants),
            variants_rejected=self._variants_rejected,
            profiled_shapes=len(self._shape_counts) + len(self._blocked_shapes),
            profile_calls=self._profile_calls,
            profile_evictions=self._profile_evictions,
            profile_budget_exhausted=self._profile_budget_exhausted,
            profiling_active=self._profiling_active,
            runtime_metrics=self.execution_mode == "wrapper" and self.runtime_metrics,
        )

    def register(
        self,
        *,
        constants: Mapping[str, Any] | None = None,
        types: Mapping[str, type[Any]] | None = None,
        adaptive: bool = False,
    ) -> Callable[..., Any] | None:
        constants_map = _normalize_constant_constraints(self.function, constants)
        types_map = _normalize_type_constraints(self.function, types)
        for name in constants_map:
            types_map.pop(name, None)
        guards: list[_ConstantGuard] = []
        for name, value in constants_map.items():
            guard = _constant_guard(name, value, adaptive=adaptive)
            if guard is None:
                return None
            guards.append(guard)
        if self.execution_mode == "inline" and not all(_inline_guard_supported(guard) for guard in guards):
            raise SpecializationUnsupportedError(
                f"{self.function.__qualname__}: requested value guard requires wrapper dispatch"
            )
        key = _variant_key(guards, types_map)
        with self._lock:
            for existing in self._variants:
                if existing.key == key:
                    return existing.function
            if len(self._variants) >= self.max_variants:
                raise SpecializationLimitError(
                    f"{self.function.__qualname__}: maximum specialization variants reached"
                )
        needed = (*constants_map.keys(), *types_map.keys())
        extractor = _ArgumentExtractor(self.function, needed)
        specialized, stats = _specialize_code(
            self.function,
            constants=constants_map,
            exact_types=types_map,
            remove_bound_parameters=False,
        )
        if not _variant_profitable(stats, self.policy, self.execution_mode):
            self._variants_rejected += 1
            return None
        details = (
            ("constants", tuple(constants_map)),
            ("exact_types", tuple((name, _safe_type_qualname(expected)) for name, expected in types_map.items())),
            ("code_bytes", (stats.original_code_bytes, stats.final_code_bytes)),
        )
        variant = _Variant(
            function=specialized,
            constant_guards=tuple(guards),
            type_guards=tuple(types_map.items()),
            key=key,
            details=details,
        )
        # Extractor is intentionally attached to the internal function rather than
        # kept as another public object. The wrapper uses the union extractor below.
        specialized.__python_extensions_variant_extractor__ = extractor
        with self._lock:
            for existing in self._variants:
                if existing.key == key:
                    return existing.function
            if len(self._variants) >= self.max_variants:
                raise SpecializationLimitError(
                    f"{self.function.__qualname__}: maximum specialization variants reached"
                )
            self._variants = (*self._variants, variant)
            if len(self._variants) >= self.max_variants:
                self._profiling_active = False
            _refresh_dispatch_wrapper(self)
        return specialized

    def _extract_for_variant(self, variant: _Variant, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> dict[str, Any] | None:
        extractor = getattr(variant.function, "__python_extensions_variant_extractor__")
        return extractor.extract(args, kwargs)

    def _shape(self, values: Mapping[str, Any]) -> tuple[Any, dict[str, Any], dict[str, type[Any]]] | None:
        constants: dict[str, Any] = {}
        types: dict[str, type[Any]] = {}
        key_parts: list[Any] = []
        for name in self.profile_constant_names:
            value = values[name]
            key = _adaptive_constant_key(value)
            if key is None:
                return None
            constants[name] = value
            key_parts.append(("c", name, key))
        for name in self.profile_type_names:
            if name in constants:
                continue
            value = values[name]
            expected = type(value)
            types[name] = expected
            key_parts.append(("t", name, expected))
        return tuple(key_parts), constants, types

    def _record_profile_observation(self, key: Any) -> int | None:
        """Record one bounded shape observation while holding ``self._lock``."""

        self._profile_calls += 1
        if self._profile_calls >= self.profile_budget:
            self._profile_budget_exhausted = True
            self._profiling_active = False
            _refresh_dispatch_wrapper(self)
            return None
        if key not in self._shape_counts and len(self._shape_counts) >= self.max_profiled_shapes:
            # Evict the least-observed unpromoted shape. Counter preserves
            # insertion order, making ties deterministic without comparing
            # heterogeneous shape keys. This bounds memory on megamorphic sites.
            victim = min(self._shape_counts, key=self._shape_counts.get)
            del self._shape_counts[victim]
            self._profile_evictions += 1
        count = self._shape_counts[key] + 1
        self._shape_counts[key] = count
        return count

    def _promote_observed_shape(
        self,
        key: Any,
        constants: Mapping[str, Any],
        types_map: Mapping[str, type[Any]],
    ) -> None:
        if key in self._blocked_shapes:
            return
        with self._lock:
            if not self._profiling_active or key in self._blocked_shapes:
                return
            count = self._record_profile_observation(key)
            if count is None or count < self.threshold:
                return
            self._shape_counts.pop(key, None)
            self._blocked_shapes.add(key)
            try:
                created = self.register(constants=constants, types=types_map, adaptive=True)
            except (SpecializationError, ValueError, TypeError):
                self._variants_rejected += 1
                created = None
            if created is None:
                if len(self._variants) >= self.max_variants:
                    self._profiling_active = False
                elif (
                    self.policy == "speed"
                    and not self._variants
                    and not self._shape_counts
                    and len(self._blocked_shapes) == 1
                ):
                    # One complete monomorphic threshold window was proven
                    # unprofitable. Stop observing it rather than taxing every
                    # future call for a specialization we have already declined.
                    self._profiling_active = False
                if not self._profiling_active:
                    _refresh_dispatch_wrapper(self)

    def _profile_bound(self, constant_values: tuple[Any, ...], type_values: tuple[Any, ...]) -> None:
        if not self._profiling_active:
            return
        constants: dict[str, Any] = {}
        types_map: dict[str, type[Any]] = {}
        key_parts: list[Any] = []
        for name, value in zip(self.profile_constant_names, constant_values):
            key = _adaptive_constant_key(value)
            if key is None:
                return
            constants[name] = value
            key_parts.append(("c", name, key))
        for name, value in zip(self.profile_type_names, type_values):
            if name in constants:
                continue
            expected = type(value)
            types_map[name] = expected
            key_parts.append(("t", name, expected))
        self._promote_observed_shape(tuple(key_parts), constants, types_map)

    def _profile(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> None:
        if not self._profiling_active or self._profile_extractor is None:
            return
        values = self._profile_extractor.extract(args, kwargs)
        if values is None:
            return
        shape = self._shape(values)
        if shape is None:
            return
        key, constants, types_map = shape
        self._promote_observed_shape(key, constants, types_map)

    def _profile_locals(self, local_values: Mapping[str, Any]) -> None:
        if not self._profiling_active:
            return
        constants: dict[str, Any] = {}
        types_map: dict[str, type[Any]] = {}
        key_parts: list[Any] = []
        for name in self.profile_constant_names:
            if name not in local_values:
                return
            value = local_values[name]
            key = _adaptive_constant_key(value)
            if key is None:
                return
            constants[name] = value
            key_parts.append(("c", name, key))
        for name in self.profile_type_names:
            if name in constants:
                continue
            if name not in local_values:
                return
            expected = type(local_values[name])
            types_map[name] = expected
            key_parts.append(("t", name, expected))
        self._promote_observed_shape(tuple(key_parts), constants, types_map)

    def invoke(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if self.runtime_metrics:
            self._calls += 1
        variants = self._variants
        for variant in variants:
            values = self._extract_for_variant(variant, args, kwargs)
            if values is not None and variant.matches(values):
                if self.runtime_metrics:
                    self._variant_hits += 1
                return variant.function(*args, **kwargs)
        if self.runtime_metrics:
            self._fallback_calls += 1
        result = self.function(*args, **kwargs)
        self._profile(args, kwargs)
        return result



_MONITORING_TOOL_NAME = "python_extensions.hotpath"


class _MonitoringHotpathManager:
    """CPython 3.13 local-call profiler for one-variant adaptive hotpaths."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[types.CodeType, tuple[types.FunctionType, _SpecializationDispatcher]] = {}
        self._claimed = False
        self._tool_id: int | None = None

    def _monitoring(self):
        return getattr(sys, "monitoring", None)

    def _candidate_tool_ids(self) -> tuple[int, ...]:
        monitoring = self._monitoring()
        if monitoring is None:
            return ()
        # IDs 3 and 4 have no standard debugger/coverage/profiler assignment in
        # CPython 3.13. Prefer them so the public OPTIMIZER_ID remains available
        # to other optimizer tests/tools whenever possible.
        return (4, 3, monitoring.OPTIMIZER_ID)

    def available(self) -> bool:
        monitoring = self._monitoring()
        if monitoring is None:
            return False
        if self._claimed and self._tool_id is not None:
            return monitoring.get_tool(self._tool_id) == _MONITORING_TOOL_NAME
        return any(
            monitoring.get_tool(tool_id) in {None, _MONITORING_TOOL_NAME}
            for tool_id in self._candidate_tool_ids()
        )

    def _claim(self) -> bool:
        monitoring = self._monitoring()
        if monitoring is None:
            return False
        with self._lock:
            if self._claimed and self._tool_id is not None:
                return monitoring.get_tool(self._tool_id) == _MONITORING_TOOL_NAME
            for tool_id in self._candidate_tool_ids():
                owner = monitoring.get_tool(tool_id)
                if owner not in {None, _MONITORING_TOOL_NAME}:
                    continue
                if owner is None:
                    try:
                        monitoring.use_tool_id(tool_id, _MONITORING_TOOL_NAME)
                    except ValueError:
                        continue
                monitoring.register_callback(
                    tool_id,
                    monitoring.events.PY_START,
                    self._callback,
                )
                self._tool_id = tool_id
                self._claimed = True
                return True
            return False

    def register(
        self,
        target: types.FunctionType,
        dispatcher: _SpecializationDispatcher,
    ) -> bool:
        if not self._claim():
            return False
        monitoring = self._monitoring()
        assert monitoring is not None
        code = target.__code__
        with self._lock:
            tool_id = self._tool_id
            if tool_id is None:
                return False
            self._entries[code] = (target, dispatcher)
            monitoring.set_local_events(tool_id, code, monitoring.events.PY_START)
        return True

    def finish(
        self,
        target: types.FunctionType,
        dispatcher: _SpecializationDispatcher,
        observed_code: types.CodeType,
    ) -> None:
        monitoring = self._monitoring()
        if monitoring is None or not self._claimed:
            return
        with self._lock:
            self._entries.pop(observed_code, None)
            tool_id = self._tool_id
            if tool_id is not None:
                try:
                    monitoring.set_local_events(tool_id, observed_code, 0)
                except ValueError:
                    pass
            if not self._entries and tool_id is not None:
                # Releasing from a PY_START callback is supported by CPython and
                # avoids reserving one of the six process-global monitoring IDs.
                try:
                    monitoring.register_callback(tool_id, monitoring.events.PY_START, None)
                    monitoring.free_tool_id(tool_id)
                finally:
                    self._claimed = False
                    self._tool_id = None
        if not dispatcher.variants:
            # Rejection/budget exhaustion should become an exact no-op after the
            # bounded warm-up rather than leaving transformed scaffolding behind.
            target.__code__ = dispatcher.function.__code__
            target.__defaults__ = dispatcher.function.__defaults__
            target.__kwdefaults__ = (
                None
                if dispatcher.function.__kwdefaults__ is None
                else dict(dispatcher.function.__kwdefaults__)
            )
            target.__python_extensions_dispatch_mode__ = "passthrough"
        else:
            target.__python_extensions_dispatch_mode__ = "monitoring-inline"

    def _callback(self, code: types.CodeType, instruction_offset: int) -> None:
        del instruction_offset
        with self._lock:
            entry = self._entries.get(code)
        if entry is None:
            return
        target, dispatcher = entry
        try:
            frame = sys._getframe(1)
            if frame.f_code is not code:
                return
            dispatcher._profile_locals(frame.f_locals)
        except Exception:
            # Monitoring must never make the application call fail. A failed
            # optimizer observation simply disables this adaptive site.
            dispatcher._profiling_active = False
        if not dispatcher._profiling_active:
            self.finish(target, dispatcher, code)


_MONITORING_HOTPATHS = _MonitoringHotpathManager()


def _attach_monitoring_hotpath(
    target: F,
    dispatcher: _SpecializationDispatcher,
) -> F:
    target.__python_extensions_specialization__ = dispatcher
    target.specialization_stats = dispatcher.stats
    target.specialization_variants = lambda: tuple(variant.function for variant in dispatcher.variants)
    target.__python_extensions_dispatch_mode__ = "monitoring"
    dispatcher.wrapper = target

    def register_specialization(
        *,
        constants: Mapping[str, Any] | None = None,
        types: Mapping[str, type[Any]] | None = None,
        adaptive: bool = False,
    ):
        observed_code = target.__code__
        result = dispatcher.register(constants=constants, types=types, adaptive=adaptive)
        if not dispatcher._profiling_active or target.__code__ is not observed_code:
            _MONITORING_HOTPATHS.finish(target, dispatcher, observed_code)
            if dispatcher.variants:
                target.__python_extensions_dispatch_mode__ = "monitoring-inline"
            elif not dispatcher._profiling_active:
                target.__python_extensions_dispatch_mode__ = "passthrough"
        return result

    target.register_specialization = register_specialization
    return target


def _render_signature(function: Callable[..., Any], token: str) -> tuple[str, dict[str, Any]]:
    signature, parameters = _signature_parts(function)
    globals_to_add: dict[str, Any] = {}
    rendered: list[str] = []
    posonly_count = sum(p.kind is inspect.Parameter.POSITIONAL_ONLY for p in parameters)
    inserted_kw_separator = False

    def render_parameter(parameter: inspect.Parameter) -> str:
        if parameter.default is inspect._empty:
            return parameter.name
        default_name = f"__pex_default_{token}_{len(globals_to_add)}"
        globals_to_add[default_name] = parameter.default
        return f"{parameter.name}={default_name}"

    for index, parameter in enumerate(parameters):
        kind = parameter.kind
        if kind is inspect.Parameter.POSITIONAL_ONLY:
            rendered.append(render_parameter(parameter))
            if index + 1 == posonly_count:
                rendered.append("/")
        elif kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
            rendered.append(render_parameter(parameter))
        elif kind is inspect.Parameter.VAR_POSITIONAL:
            rendered.append(f"*{parameter.name}")
            inserted_kw_separator = True
        elif kind is inspect.Parameter.KEYWORD_ONLY:
            if not inserted_kw_separator:
                rendered.append("*")
                inserted_kw_separator = True
            rendered.append(render_parameter(parameter))
        elif kind is inspect.Parameter.VAR_KEYWORD:
            rendered.append(f"**{parameter.name}")
    return ", ".join(rendered), globals_to_add


def _canonical_call_arguments(function: Callable[..., Any]) -> str:
    _, parameters = _signature_parts(function)
    pieces: list[str] = []
    for parameter in parameters:
        kind = parameter.kind
        if kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
            pieces.append(parameter.name)
        elif kind is inspect.Parameter.VAR_POSITIONAL:
            pieces.append(f"*{parameter.name}")
        elif kind is inspect.Parameter.KEYWORD_ONLY:
            pieces.append(f"{parameter.name}={parameter.name}")
        elif kind is inspect.Parameter.VAR_KEYWORD:
            pieces.append(f"**{parameter.name}")
    return ", ".join(pieces)


def _guard_expression(
    dispatcher: "_SpecializationDispatcher",
    variant: _Variant,
    index: int,
    globals_dict: dict[str, Any],
) -> str:
    token = dispatcher._token
    checks: list[str] = []
    for guard_index, guard in enumerate(variant.constant_guards):
        value_name = f"__pex_guard_value_{token}_{index}_{guard_index}"
        globals_dict[value_name] = guard.value
        if guard.safe_key is None:
            checks.append(f"{guard.name} is {value_name}")
        elif type(guard.value) in _INLINE_VALUE_GUARD_TYPES:
            type_name = f"__pex_guard_type_{token}_{index}_{guard_index}"
            globals_dict[type_name] = type(guard.value)
            checks.append(f"type({guard.name}) is {type_name} and {guard.name} == {value_name}")
        else:
            # Float/complex/container profiling uses canonical side-effect-free
            # keys. Reuse exactly that matcher here so NaN and signed zero do
            # not disagree with the shape cache.
            matcher_name = f"__pex_guard_matcher_{token}_{index}_{guard_index}"
            globals_dict[matcher_name] = guard.matches
            checks.append(f"{matcher_name}({guard.name})")
    type_offset = len(variant.constant_guards)
    for guard_index, (name, expected) in enumerate(variant.type_guards, start=type_offset):
        type_name = f"__pex_guard_type_{token}_{index}_{guard_index}"
        globals_dict[type_name] = expected
        checks.append(f"type({name}) is {type_name}")
    return " and ".join(f"({check})" for check in checks) or "True"


def _build_dispatch_code(dispatcher: "_SpecializationDispatcher") -> types.FunctionType:
    function = dispatcher.function
    token = dispatcher._token
    globals_dict = dispatcher._dispatch_globals
    signature_source, defaults = _render_signature(function, token)
    globals_dict.update(defaults)
    state_name = f"__pex_state_{token}"
    original_name = f"__pex_original_{token}"
    globals_dict[state_name] = dispatcher
    globals_dict[original_name] = function
    call_args = _canonical_call_arguments(function)

    lines = [f"def __pex_wrapper({signature_source}):"]
    indent = "    "
    if dispatcher.runtime_metrics:
        lines.append(f"{indent}{state_name}._calls += 1")
    for index, variant in enumerate(dispatcher._variants):
        variant_name = f"__pex_variant_{token}_{index}"
        globals_dict[variant_name] = variant.function
        guard = _guard_expression(dispatcher, variant, index, globals_dict)
        lines.append(f"{indent}if {guard}:")
        if dispatcher.runtime_metrics:
            lines.append(f"{indent}    {state_name}._variant_hits += 1")
        lines.append(f"{indent}    return {variant_name}({call_args})")
    if dispatcher.runtime_metrics:
        lines.append(f"{indent}{state_name}._fallback_calls += 1")
    lines.append(f"{indent}__pex_result = {original_name}({call_args})")
    if dispatcher._profiling_active and dispatcher._profile_extractor is not None:
        constant_values = ", ".join(dispatcher.profile_constant_names)
        type_values = ", ".join(dispatcher.profile_type_names)
        if len(dispatcher.profile_constant_names) == 1:
            constant_values += ","
        if len(dispatcher.profile_type_names) == 1:
            type_values += ","
        lines.append(
            f"{indent}{state_name}._profile_bound(({constant_values}), ({type_values}))"
        )
    lines.append(f"{indent}return __pex_result")

    source = "\n".join(lines)
    namespace: dict[str, Any] = {}
    exec(compile(source, function.__code__.co_filename, "exec"), globals_dict, namespace)
    built = namespace["__pex_wrapper"]
    built.__python_extensions_dispatch_source__ = source
    return built


def _build_async_dispatch_code(dispatcher: "_SpecializationDispatcher") -> types.FunctionType:
    function = dispatcher.function
    token = dispatcher._token
    globals_dict = dispatcher._dispatch_globals
    signature_source, defaults = _render_signature(function, token)
    globals_dict.update(defaults)
    state_name = f"__pex_state_{token}"
    original_name = f"__pex_original_{token}"
    globals_dict[state_name] = dispatcher
    globals_dict[original_name] = function
    call_args = _canonical_call_arguments(function)

    lines = [f"async def __pex_wrapper({signature_source}):"]
    indent = "    "
    if dispatcher.runtime_metrics:
        lines.append(f"{indent}{state_name}._calls += 1")
    for index, variant in enumerate(dispatcher._variants):
        variant_name = f"__pex_variant_{token}_{index}"
        globals_dict[variant_name] = variant.function
        guard = _guard_expression(dispatcher, variant, index, globals_dict)
        lines.append(f"{indent}if {guard}:")
        if dispatcher.runtime_metrics:
            lines.append(f"{indent}    {state_name}._variant_hits += 1")
        lines.append(f"{indent}    return await {variant_name}({call_args})")
    if dispatcher.runtime_metrics:
        lines.append(f"{indent}{state_name}._fallback_calls += 1")
    lines.append(f"{indent}__pex_result = await {original_name}({call_args})")
    if dispatcher._profiling_active and dispatcher._profile_extractor is not None:
        constant_values = ", ".join(dispatcher.profile_constant_names)
        type_values = ", ".join(dispatcher.profile_type_names)
        if len(dispatcher.profile_constant_names) == 1:
            constant_values += ","
        if len(dispatcher.profile_type_names) == 1:
            type_values += ","
        lines.append(
            f"{indent}{state_name}._profile_bound(({constant_values}), ({type_values}))"
        )
    lines.append(f"{indent}return __pex_result")

    source = "\n".join(lines)
    namespace: dict[str, Any] = {}
    exec(compile(source, function.__code__.co_filename, "exec"), globals_dict, namespace)
    built = namespace["__pex_wrapper"]
    built.__python_extensions_dispatch_source__ = source
    return built




def _inline_guard_supported(guard: _ConstantGuard) -> bool:
    # Identity guards and canonical-key guards can both be emitted in-frame.
    # Complex safe constants use the same side-effect-free matcher as wrapper
    # dispatch rather than invoking application equality/hash protocols.
    return True


def _emit_type_guard(name: str, expected: type[Any], fail: Label, location: Any) -> list[Instr]:
    return [
        Instr("LOAD_CONST", builtins.type, location=location),
        Instr("PUSH_NULL", location=location),
        Instr("LOAD_FAST", name, location=location),
        Instr("CALL", 1, location=location),
        Instr("LOAD_CONST", expected, location=location),
        Instr("IS_OP", 0, location=location),
        Instr("POP_JUMP_IF_FALSE", fail, location=location),
    ]


def _emit_constant_guard(guard: _ConstantGuard, fail: Label, location: Any) -> list[Instr]:
    value = guard.value
    if guard.safe_key is None or type(value) in {type(None), bool}:
        return [
            Instr("LOAD_FAST", guard.name, location=location),
            Instr("LOAD_CONST", value, location=location),
            Instr("IS_OP", 0, location=location),
            Instr("POP_JUMP_IF_FALSE", fail, location=location),
        ]
    if type(value) in _INLINE_VALUE_GUARD_TYPES:
        return [
            *_emit_type_guard(guard.name, type(value), fail, location),
            Instr("LOAD_FAST", guard.name, location=location),
            Instr("LOAD_CONST", value, location=location),
            Instr("COMPARE_OP", Compare.EQ_CAST, location=location),
            Instr("POP_JUMP_IF_FALSE", fail, location=location),
        ]
    return [
        Instr("LOAD_CONST", guard.matches, location=location),
        Instr("PUSH_NULL", location=location),
        Instr("LOAD_FAST", guard.name, location=location),
        Instr("CALL", 1, location=location),
        Instr("POP_JUMP_IF_FALSE", fail, location=location),
    ]


def _split_prologue_and_body(code: types.CodeType) -> tuple[list[Any], list[Any], Bytecode]:
    bytecode = Bytecode.from_code(code)
    items = list(bytecode)
    for index, item in enumerate(items):
        if isinstance(item, Instr) and item.name == "RESUME":
            return items[: index + 1], items[index + 1 :], bytecode
    raise SpecializationUnsupportedError("specialization requires a normal RESUME-based CPython function")


def _build_inline_dispatch_code(dispatcher: "_SpecializationDispatcher") -> types.CodeType:
    function = dispatcher.function
    if inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
        raise SpecializationUnsupportedError("in-frame specialize currently supports ordinary functions only")
    prefix, fallback_body, bytecode = _split_prologue_and_body(function.__code__)
    location = next(
        (item.location for item in reversed(prefix) if isinstance(item, Instr)),
        None,
    )
    combined: list[Any] = list(prefix)
    for variant in dispatcher._variants:
        if not all(_inline_guard_supported(guard) for guard in variant.constant_guards):
            raise SpecializationUnsupportedError(
                f"{function.__qualname__}: one specialization guard requires wrapper dispatch"
            )
        fail = Label()
        for guard in variant.constant_guards:
            combined.extend(_emit_constant_guard(guard, fail, location))
        for name, expected in variant.type_guards:
            combined.extend(_emit_type_guard(name, expected, fail, location))
        _, variant_body, _ = _split_prologue_and_body(variant.function.__code__)
        combined.extend(variant_body)
        combined.append(fail)
    combined.extend(fallback_body)
    bytecode.clear()
    bytecode.extend(combined)
    try:
        code = bytecode.to_code()
        verify_code(code)
    except Exception as exc:
        raise SpecializationUnsupportedError(
            f"{function.__qualname__}: in-frame specialization dispatcher failed verification"
        ) from exc
    return code


def _clone_function(function: F) -> F:
    clone = types.FunctionType(
        function.__code__,
        function.__globals__,
        function.__name__,
        function.__defaults__,
        function.__closure__,
    )
    clone.__kwdefaults__ = None if function.__kwdefaults__ is None else dict(function.__kwdefaults__)
    _copy_function_metadata(function, clone)
    clone.__wrapped__ = function
    return clone  # type: ignore[return-value]


def _refresh_inline_dispatch_function(dispatcher: "_SpecializationDispatcher") -> None:
    target = dispatcher.wrapper
    if target is None:
        return
    target.__code__ = _build_inline_dispatch_code(dispatcher)
    _sync_wrapper_reports(dispatcher)


def _inline_dispatch_target(dispatcher: "_SpecializationDispatcher", function: F) -> F:
    target = _clone_function(function)
    target.__python_extensions_specialization__ = dispatcher
    target.specialization_stats = dispatcher.stats
    target.specialization_variants = lambda: tuple(variant.function for variant in dispatcher.variants)
    target.register_specialization = dispatcher.register
    target.__python_extensions_dispatch_mode__ = "inline"
    dispatcher.wrapper = target
    _refresh_inline_dispatch_function(dispatcher)
    return target

def _sync_wrapper_reports(dispatcher: "_SpecializationDispatcher") -> None:
    wrapper = getattr(dispatcher, "wrapper", None)
    if wrapper is None:
        return
    base_reports = tuple(getattr(dispatcher.function, "__python_extensions_reports__", ()))
    variant_reports = []
    for variant in dispatcher._variants:
        report = getattr(variant.function, "__python_extensions_report__", None)
        if report is not None:
            variant_reports.append(report)
    reports: tuple[Any, ...] = (*base_reports, *variant_reports)
    if dispatcher.execution_mode == "inline":
        dispatch_report = make_report(
            "specialize-dispatch",
            dispatcher.function.__code__,
            wrapper.__code__,
            details=(
                ("variants", len(dispatcher._variants)),
                ("frame_transparent", True),
                ("runtime_metrics", False),
            ),
        )
        reports = (*reports, dispatch_report)
    if reports:
        wrapper.__python_extensions_reports__ = reports
        wrapper.__python_extensions_report__ = reports[-1]

def _refresh_dispatch_wrapper(dispatcher: "_SpecializationDispatcher") -> None:
    wrapper = getattr(dispatcher, "wrapper", None)
    if wrapper is None:
        return
    if dispatcher.execution_mode == "inline":
        _refresh_inline_dispatch_function(dispatcher)
        return
    if inspect.iscoroutinefunction(dispatcher.function):
        built = _build_async_dispatch_code(dispatcher)
    else:
        built = _build_dispatch_code(dispatcher)
    if built.__code__.co_freevars:
        raise SpecializationUnsupportedError("generated specialization dispatcher unexpectedly captured closure state")
    wrapper.__code__ = built.__code__
    wrapper.__defaults__ = built.__defaults__
    wrapper.__kwdefaults__ = built.__kwdefaults__
    wrapper.__python_extensions_dispatch_source__ = built.__python_extensions_dispatch_source__
    _sync_wrapper_reports(dispatcher)


def _dispatcher_wrapper(dispatcher: _SpecializationDispatcher, function: F) -> F:
    if inspect.isasyncgenfunction(function) or inspect.isgeneratorfunction(function):
        raise SpecializationUnsupportedError(
            f"{function.__qualname__}: specialize/hotpath currently require a non-generator function"
        )

    if inspect.iscoroutinefunction(function):
        built = _build_async_dispatch_code(dispatcher)
    else:
        built = _build_dispatch_code(dispatcher)
    functools.update_wrapper(built, function)
    built.__python_extensions_specialization__ = dispatcher
    built.specialization_stats = dispatcher.stats
    built.specialization_variants = lambda: tuple(variant.function for variant in dispatcher.variants)
    built.register_specialization = dispatcher.register
    built.__python_extensions_dispatch_mode__ = "wrapper"
    dispatcher.wrapper = built
    _sync_wrapper_reports(dispatcher)
    return built  # type: ignore[return-value]


@overload
def specialize(function: F, /) -> F: ...


@overload
def specialize(
    *,
    constants: Mapping[str, Any] | None = None,
    types: Mapping[str, type[Any]] | None = None,
    policy: str = "always",
    max_variants: int = 4,
    dispatch: str = "auto",
) -> Callable[[F], F]: ...


def specialize(
    function: F | None = None,
    /,
    *,
    constants: Mapping[str, Any] | None = None,
    types: Mapping[str, type[Any]] | None = None,
    policy: str = "always",
    max_variants: int = 4,
    dispatch: str = "auto",
):
    """Create a guarded specialization dispatcher for a Python function.

    ``constants`` fixes branch-driving argument values for one fast variant while
    preserving the original call signature and generic fallback. ``types`` uses
    exact runtime types and can eliminate safe ``type(x) is T`` and ordinary
    ``isinstance(x, T)`` branches. Guard misses always call the original function.

    Bare ``@specialize`` creates an initially generic dispatcher; add explicit
    variants with ``fn.register_specialization(...)``.
    """

    def decorate(target: F) -> F:
        base, descriptor = _unwrap_descriptor(target)
        if dispatch not in {"auto", "inline", "wrapper"}:
            raise ValueError("dispatch must be 'auto', 'inline', or 'wrapper'")
        requested_constraints = bool(constants or types)
        execution_mode = "wrapper"
        if dispatch == "inline":
            execution_mode = "inline"
        elif (
            dispatch == "auto"
            and requested_constraints
            and not inspect.iscoroutinefunction(base)
            and not inspect.isgeneratorfunction(base)
            and not inspect.isasyncgenfunction(base)
        ):
            # Explicit ordinary-function specializations use a frame-transparent
            # in-code guard when all guards support that lowering. Bare dynamic
            # registries stay wrapper-based because future guard types are unknown.
            candidate_constants = _normalize_constant_constraints(base, constants)
            candidate_guards = [
                _constant_guard(name, value, adaptive=False)
                for name, value in candidate_constants.items()
            ]
            if all(guard is not None and _inline_guard_supported(guard) for guard in candidate_guards):
                execution_mode = "inline"
        dispatcher = _SpecializationDispatcher(
            base,
            policy=policy,
            max_variants=max_variants,
            execution_mode=execution_mode,
        )
        if constants or types:
            dispatcher.register(constants=constants, types=types)
        if execution_mode == "inline":
            wrapped = _inline_dispatch_target(dispatcher, base)
        else:
            wrapped = _dispatcher_wrapper(dispatcher, base)
            wrapped.__python_extensions_dispatch_mode__ = "wrapper"
        return _rewrap_descriptor(wrapped, descriptor)  # type: ignore[return-value]

    return decorate if function is None else decorate(function)


def _infer_hotpath_candidates(function: Callable[..., Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Infer branch-driving constant and exact-type parameters from bytecode."""

    bytecode = Bytecode.from_code(function.__code__)
    items = list(bytecode)
    parameter_names = set(inspect.signature(function, follow_wrapped=False).parameters)
    constant_names: list[str] = []
    type_names: list[str] = []

    def add_unique(target: list[str], name: str) -> None:
        if name in parameter_names and name not in target:
            target.append(name)

    for i, item in enumerate(items):
        # Parameter directly compared with an immutable literal.
        if isinstance(item, Instr) and item.name in _LOAD_FAST_SIMPLE and isinstance(item.arg, str):
            if i + 1 < len(items):
                branch = items[i + 1]
                if isinstance(branch, Instr) and branch.name in {"POP_JUMP_IF_NONE", "POP_JUMP_IF_NOT_NONE"}:
                    add_unique(constant_names, item.arg)
            if i + 2 < len(items):
                second, third = items[i + 1], items[i + 2]
                if (
                    isinstance(second, Instr)
                    and second.name == "LOAD_CONST"
                    and isinstance(third, Instr)
                    and third.name in {"COMPARE_OP", "IS_OP", "CONTAINS_OP"}
                    and _adaptive_constant_key(second.arg) is not None
                ):
                    add_unique(constant_names, item.arg)
            # type(parameter) is T
            if i >= 1 and i + 3 < len(items):
                if (
                    _resolved_global_name(items[i - 1]) == "type"
                    and _global_value(function, "type") is builtins.type
                    and isinstance(items[i + 1], Instr) and items[i + 1].name == "CALL" and items[i + 1].arg == 1
                    and _resolved_global_name(items[i + 2]) is not None
                    and isinstance(items[i + 3], Instr) and items[i + 3].name == "IS_OP"
                ):
                    add_unique(type_names, item.arg)
            # isinstance(parameter, T)
            if i >= 1 and i + 2 < len(items):
                if (
                    _resolved_global_name(items[i - 1]) == "isinstance"
                    and _global_value(function, "isinstance") is builtins.isinstance
                    and _resolved_global_name(items[i + 1]) is not None
                    and isinstance(items[i + 2], Instr) and items[i + 2].name == "CALL" and items[i + 2].arg == 2
                ):
                    add_unique(type_names, item.arg)
        # Reversed literal comparisons such as ``"fast" == mode``.
        if (
            isinstance(item, Instr)
            and item.name == "LOAD_CONST"
            and _adaptive_constant_key(item.arg) is not None
            and i + 2 < len(items)
            and isinstance(items[i + 1], Instr)
            and items[i + 1].name in _LOAD_FAST_SIMPLE
            and isinstance(items[i + 1].arg, str)
            and isinstance(items[i + 2], Instr)
            and items[i + 2].name in {"COMPARE_OP", "IS_OP", "CONTAINS_OP"}
        ):
            add_unique(constant_names, items[i + 1].arg)
    return tuple(type_names), tuple(constant_names)


@overload
def hotpath(function: F, /) -> F: ...


@overload
def hotpath(
    *,
    threshold: int = 64,
    max_variants: int = 1,
    types: bool | Iterable[str] = True,
    constants: str | Iterable[str] = "auto",
    policy: str = "speed",
    max_profiled_shapes: int = 64,
    profile_budget: int | None = None,
    metrics: bool = False,
    backend: str = "auto",
) -> Callable[[F], F]: ...


def hotpath(
    function: F | None = None,
    /,
    *,
    threshold: int = 64,
    max_variants: int = 1,
    types: bool | Iterable[str] = True,
    constants: str | Iterable[str] = "auto",
    policy: str = "speed",
    max_profiled_shapes: int = 64,
    profile_budget: int | None = None,
    metrics: bool = False,
    backend: str = "auto",
):
    """Adaptively promote hot argument shapes to guarded bytecode variants.

    The profiler is intentionally bounded and local to calls of the decorated
    function. By default it profiles exact types for parameters that participate in
    type predicates and immutable values for parameters compared with literals.
    Only variants that the static partial evaluator can simplify are retained under
    ``policy='speed'``. Discovery is memory-bounded by ``max_profiled_shapes``
    and time-bounded by ``profile_budget`` (an automatically sized finite budget
    by default), so megamorphic call sites cannot accumulate unbounded profiling
    state or pay profiling overhead forever. Per-call hit/fallback counters are
    disabled by default because they materially tax warmed hot paths; pass
    ``metrics=True`` when detailed runtime hit accounting is more important than
    minimum dispatch overhead. Promotion/rejection/profile counts remain available.

    ``backend='auto'`` prefers CPython 3.13 ``sys.monitoring`` for ordinary
    one-variant hotpaths. That path observes the original frame during a bounded
    warm-up and then installs a verified in-frame dispatcher with no permanent
    Python wrapper. Polymorphic, coroutine, metrics-enabled, or monitoring-slot
    conflicts use the portable wrapper backend instead.
    """

    def decorate(target: F) -> F:
        base, descriptor = _unwrap_descriptor(target)
        inferred_types, inferred_constants = _infer_hotpath_candidates(base)

        if types is True:
            type_names = inferred_types
        elif types is False:
            type_names = ()
        else:
            type_names = _validate_names(base, types)

        if constants == "auto":
            constant_names = inferred_constants
        elif constants in {False, None}:  # type: ignore[comparison-overlap]
            constant_names = ()
        elif isinstance(constants, str):
            raise ValueError("constants must be 'auto', False, or an iterable of parameter names")
        else:
            constant_names = _validate_names(base, constants)

        if backend not in {"auto", "monitoring", "wrapper"}:
            raise ValueError("backend must be 'auto', 'monitoring', or 'wrapper'")
        if backend == "monitoring" and max_variants != 1:
            raise SpecializationUnsupportedError(
                "monitoring hotpath currently requires max_variants=1"
            )
        if backend == "monitoring" and metrics:
            raise SpecializationUnsupportedError(
                "monitoring hotpath does not provide per-call runtime metrics"
            )
        if not type_names and not constant_names:
            # There is nothing to profile. Preserve a real function object and
            # registration API, but install no callback or permanent wrapper.
            baseline = _clone_function(base)
            dispatcher = _SpecializationDispatcher(
                base,
                policy=policy,
                threshold=threshold,
                max_variants=max_variants,
                max_profiled_shapes=max_profiled_shapes,
                profile_budget=profile_budget,
                runtime_metrics=False,
                execution_mode="inline",
            )
            wrapped = _attach_monitoring_hotpath(baseline, dispatcher)
            wrapped.__python_extensions_dispatch_mode__ = "passthrough"
            wrapped.__python_extensions_hotpath_candidates__ = {
                "types": (),
                "constants": (),
            }
            return _rewrap_descriptor(wrapped, descriptor)  # type: ignore[return-value]
        monitoring_eligible = (
            max_variants == 1
            and not metrics
            and not inspect.iscoroutinefunction(base)
            and not inspect.isgeneratorfunction(base)
            and not inspect.isasyncgenfunction(base)
            and _MONITORING_HOTPATHS.available()
        )
        if backend == "monitoring" and not monitoring_eligible:
            raise SpecializationUnsupportedError(
                "monitoring hotpath is unavailable for this function/process"
            )
        use_monitoring = monitoring_eligible and backend != "wrapper"

        if use_monitoring:
            # Monitor a distinct function object with the same code/globals/closure.
            # Functional decorator use therefore never mutates a retained alias to
            # the caller's original function.
            monitored_target = _clone_function(base)
            dispatcher = _SpecializationDispatcher(
                base,
                policy=policy,
                profile_type_names=type_names,
                profile_constant_names=constant_names,
                threshold=threshold,
                max_variants=1,
                max_profiled_shapes=max_profiled_shapes,
                profile_budget=profile_budget,
                runtime_metrics=False,
                execution_mode="inline",
            )
            wrapped = _attach_monitoring_hotpath(monitored_target, dispatcher)
            if not _MONITORING_HOTPATHS.register(wrapped, dispatcher):
                if backend == "monitoring":
                    raise SpecializationUnsupportedError(
                        "CPython optimizer monitoring slot is already in use"
                    )
                # The slot raced with another optimizer after availability was
                # checked. Fall back to the wrapper without changing semantics.
                dispatcher.wrapper = None
                dispatcher = _SpecializationDispatcher(
                    base,
                    policy=policy,
                    profile_type_names=type_names,
                    profile_constant_names=constant_names,
                    threshold=threshold,
                    max_variants=max_variants,
                    max_profiled_shapes=max_profiled_shapes,
                    profile_budget=profile_budget,
                    runtime_metrics=metrics,
                )
                wrapped = _dispatcher_wrapper(dispatcher, base)
        else:
            dispatcher = _SpecializationDispatcher(
                base,
                policy=policy,
                profile_type_names=type_names,
                profile_constant_names=constant_names,
                threshold=threshold,
                max_variants=max_variants,
                max_profiled_shapes=max_profiled_shapes,
                profile_budget=profile_budget,
                runtime_metrics=metrics,
            )
            wrapped = _dispatcher_wrapper(dispatcher, base)

        wrapped.__python_extensions_hotpath_candidates__ = {
            "types": type_names,
            "constants": constant_names,
        }
        return _rewrap_descriptor(wrapped, descriptor)  # type: ignore[return-value]

    return decorate if function is None else decorate(function)


__all__ = [
    "PartialStats",
    "SpecializationError",
    "SpecializationLimitError",
    "SpecializationStats",
    "SpecializationUnsupportedError",
    "hotpath",
    "partial",
    "specialize",
]
