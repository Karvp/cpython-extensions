from __future__ import annotations

import functools
import inspect
import itertools
import platform
import sys
import threading
import types
import weakref
from dataclasses import dataclass
from typing import Any, Callable, TypeVar, overload

from bytecode import BinaryOp, Bytecode, Compare, FreeVar, Instr, Intrinsic1Op, Label, TryBegin, TryEnd, UNSET

from ._core import (
    BytecodeVerificationError,
    analyze_fast_locals,
    attach_report,
    make_report,
    verify_code,
)

F = TypeVar("F", bound=Callable[..., Any])

from ._version import __version__


class InlineError(Exception):
    """Base exception for bytecode inlining failures."""


class InlineUnsupportedError(InlineError):
    """Raised when a function uses semantics the inliner cannot preserve."""


class InlineCallSiteError(InlineError):
    """Raised when a call site cannot be proven safe to merge."""


class InlineRecursionError(InlineError):
    """Raised when recursive or mutually-recursive inlining is detected."""


class InlineExpansionError(InlineError):
    """Raised when configured expansion or code-growth limits are exceeded."""


@dataclass(frozen=True)
class InlineStats:
    calls_inlined: int
    original_code_bytes: int
    final_code_bytes: int
    calls_skipped_unprofitable: int = 0
    calls_skipped_unsupported: int = 0
    calls_shared: int = 0
    shared_regions: int = 0
    reused_local_groups: int = 0
    constant_branches_folded: int = 0
    dead_instructions_pruned: int = 0
    late_stack_forwards: int = 0
    redundant_jumps_removed: int = 0
    caller_parameter_aliases: int = 0
    caller_local_aliases: int = 0
    constant_comparisons_folded: int = 0
    constant_binary_ops_folded: int = 0
    protected_shared_regions: int = 0
    synthetic_roundtrips_elided: int = 0
    synthetic_copies_propagated: int = 0
    synthetic_constants_propagated: int = 0
    coalesced_local_slots: int = 0
    stack_resident_values: int = 0
    stack_scheduler_candidates: int = 0
    stack_spilled_values: int = 0
    stack_crossing_conflicts: int = 0
    stack_max_copy_depth: int = 0
    stack_instruction_savings: int = 0
    stack_dependency_edges: int = 0
    stack_peak_resident_values: int = 0
    stack_split_values: int = 0
    stack_split_reads: int = 0
    stack_split_instruction_cost: int = 0
    stack_middle_splits: int = 0
    segmented_local_lifetimes: int = 0
    fused_result_handoffs: int = 0
    constant_result_handoffs: int = 0
    aggressive_result_handoffs: int = 0
    region_dataflow_rounds: int = 0
    region_constant_propagations: int = 0
    region_copy_propagations: int = 0
    region_branches_folded: int = 0
    region_dead_instructions_pruned: int = 0
    region_redundant_jumps_removed: int = 0
    cfg_dataflow_rounds: int = 0
    cfg_merge_facts: int = 0
    cfg_constant_propagations: int = 0
    cfg_copy_propagations: int = 0
    cfg_branches_folded: int = 0
    cfg_dead_instructions_pruned: int = 0
    cfg_redundant_jumps_removed: int = 0
    cfg_loop_headers: int = 0
    cfg_loop_invariant_facts: int = 0
    cfg_loop_variant_kills: int = 0
    cfg_affine_recurrences: int = 0
    cfg_recurrence_folds: int = 0
    cfg_strength_reduced_values: int = 0
    cfg_strength_reduced_uses: int = 0
    cfg_strength_reduction_updates: int = 0
    cfg_strength_lazy_values: int = 0
    cfg_strength_lazy_uses: int = 0
    cfg_strength_lazy_materializations: int = 0
    constant_unary_ops_folded: int = 0
    guarded_closure_calls: int = 0
    guarded_closure_speed_accepted: int = 0
    guarded_closure_body_credit: int = 0


@dataclass(frozen=True)
class _BindingPlan:
    # Positional values assigned directly to declared positional parameters.
    positional_targets: tuple[str, ...]
    # Number of surplus positional values collected into *args.
    extra_positional_count: int
    # One target for each explicit keyword value, in source order. None means **kwargs.
    keyword_targets: tuple[str | None, ...]
    # Original keyword names, used to build the **kwargs dictionary.
    keyword_names: tuple[str, ...]
    # Parameters omitted at the call site and filled from the callee defaults.
    defaults: tuple[tuple[str, Any], ...]
    vararg_name: str | None = None
    varkw_name: str | None = None

    @property
    def provided_targets(self) -> tuple[str, ...]:
        return self.positional_targets + tuple(
            target for target in self.keyword_targets if target is not None
        )


@dataclass(frozen=True)
class _CallSite:
    start: int
    callable_end: int
    call_index: int
    kw_names_index: int | None
    callee: Callable[..., Any]
    binding: _BindingPlan
    implicit_values: tuple[Any, ...] = ()
    implicit_keywords: tuple[tuple[str, Any], ...] = ()
    guarded_closure: bool = False
    guarded_identity: Any | None = None
    # A normalized loader that evaluates the callable exactly once and leaves one
    # callable object on the stack.  Guarded binding mode uses it both to validate
    # the target and to execute the ordinary CALL fallback without a second lookup.
    guard_loader: tuple[Instr, ...] = ()


class _CallableGuardState:
    """Identity-only snapshot of the callable state relevant to one inline site.

    Values are deliberately compared with ``is`` rather than ``==``.  This avoids
    invoking user equality methods from a guard and still observes every mutation
    that can change the function-call binding used by an already-expanded body.
    Mutable default *objects* may change in place: the inline body holds the same
    object, so those mutations remain visible without deoptimizing.
    """

    __slots__ = (
        "kind",
        "identity",
        "function",
        "receiver",
        "code",
        "defaults",
        "check_defaults",
        "kwdefaults",
        "closure_values",
        "partial_args",
        "partial_keywords",
        "callable_type",
    )

    def __init__(
        self,
        *,
        kind: str,
        identity: Any,
        function: types.FunctionType,
        receiver: Any = None,
        partial_args: tuple[Any, ...] = (),
        partial_keywords: tuple[tuple[str, Any], ...] = (),
        callable_type: type[Any] | None = None,
        check_defaults: bool = False,
        kwdefault_names: tuple[str, ...] = (),
    ) -> None:
        self.kind = kind
        self.identity = identity
        self.function = function
        self.receiver = receiver
        self.code = function.__code__
        self.defaults = function.__defaults__
        self.check_defaults = bool(check_defaults)
        kwdefaults = function.__kwdefaults__ or {}
        self.kwdefaults = tuple(
            (name, kwdefaults[name]) for name in kwdefault_names if name in kwdefaults
        )
        closure = function.__closure__
        if closure is None:
            self.closure_values = None
        else:
            values: list[tuple[bool, Any]] = []
            for cell in closure:
                try:
                    values.append((True, cell.cell_contents))
                except ValueError:
                    values.append((False, None))
            self.closure_values = tuple(values)
        self.partial_args = partial_args
        self.partial_keywords = partial_keywords
        self.callable_type = callable_type


def _same_identity_mapping(
    current: dict[str, Any] | None,
    snapshot: tuple[tuple[str, Any], ...] | None,
) -> bool:
    if snapshot is None:
        return current is None
    if current is None or len(current) != len(snapshot):
        return False
    marker = object()
    for name, expected in snapshot:
        if current.get(name, marker) is not expected:
            return False
    return True


def _same_function_state(
    function: Any, state: _CallableGuardState
) -> bool:
    if function is not state.function:
        return False
    if function.__code__ is not state.code:
        return False
    if state.check_defaults and function.__defaults__ is not state.defaults:
        return False
    if state.kwdefaults:
        current_kwdefaults = function.__kwdefaults__
        if current_kwdefaults is None:
            return False
        marker = object()
        for name, expected in state.kwdefaults:
            if current_kwdefaults.get(name, marker) is not expected:
                return False
    closure = function.__closure__
    snapshot = state.closure_values
    if snapshot is None:
        return closure is None
    if closure is None or len(closure) != len(snapshot):
        return False
    for cell, (was_bound, expected) in zip(closure, snapshot):
        try:
            current = cell.cell_contents
        except ValueError:
            if was_bound:
                return False
        else:
            if not was_bound or current is not expected:
                return False
    return True


def _callable_guard_state(
    value: Any, callee: Callable[..., Any], binding: _BindingPlan
) -> _CallableGuardState | None:
    """Build a side-effect-free guard snapshot for a resolved inline target."""

    positional_names = set(callee.__code__.co_varnames[: callee.__code__.co_argcount])
    default_names = tuple(name for name, _ in binding.defaults)
    check_defaults = any(name in positional_names for name in default_names)
    kwdefault_names = tuple(name for name in default_names if name not in positional_names)

    if isinstance(value, types.FunctionType):
        if value is not callee:
            return None
        return _CallableGuardState(
            kind="function",
            identity=value,
            function=value,
            check_defaults=check_defaults,
            kwdefault_names=kwdefault_names,
        )

    if isinstance(value, types.MethodType):
        if value.__func__ is not callee:
            return None
        return _CallableGuardState(
            kind="method",
            identity=None,
            function=value.__func__,
            receiver=value.__self__,
            check_defaults=check_defaults,
            kwdefault_names=kwdefault_names,
        )

    if isinstance(value, functools.partial):
        base = value.func
        receiver = None
        if isinstance(base, types.MethodType):
            function = base.__func__
            receiver = base.__self__
        elif isinstance(base, types.FunctionType):
            function = base
        else:
            return None
        if function is not callee:
            return None
        return _CallableGuardState(
            kind="partial",
            identity=value,
            function=function,
            receiver=receiver,
            partial_args=tuple(value.args),
            partial_keywords=tuple((value.keywords or {}).items()),
            check_defaults=check_defaults,
            kwdefault_names=kwdefault_names,
        )

    if not isinstance(value, type):
        call_impl = getattr(type(value), "__call__", None)
        if isinstance(call_impl, types.FunctionType) and call_impl is callee:
            return _CallableGuardState(
                kind="callable",
                identity=value,
                function=call_impl,
                receiver=value,
                callable_type=type(value),
                check_defaults=check_defaults,
                kwdefault_names=kwdefault_names,
            )
    return None


def _inline_callable_guard_matches(value: Any, state: _CallableGuardState) -> bool:
    """Return whether *value* still denotes the exact semantics that were cloned.

    The helper intentionally performs only exact builtin type/identity checks.  A
    failed check means "deopt to the original CALL"; it never invokes user code.
    """

    kind = state.kind
    if kind == "function":
        if value is not state.identity:
            return False
        function = value
    elif kind == "method":
        if not isinstance(value, types.MethodType):
            return False
        if value.__self__ is not state.receiver or value.__func__ is not state.function:
            return False
        function = value.__func__
    elif kind == "partial":
        if not isinstance(value, functools.partial) or value is not state.identity:
            return False
        base = value.func
        if state.receiver is None:
            if base is not state.function:
                return False
        else:
            if not isinstance(base, types.MethodType):
                return False
            if base.__self__ is not state.receiver or base.__func__ is not state.function:
                return False
        if len(value.args) != len(state.partial_args):
            return False
        if any(current is not expected for current, expected in zip(value.args, state.partial_args)):
            return False
        if not _same_identity_mapping(value.keywords, state.partial_keywords):
            return False
        function = state.function
    elif kind == "callable":
        if value is not state.identity or type(value) is not state.callable_type:
            return False
        function = getattr(state.callable_type, "__call__", None)
        if function is not state.function:
            return False
    else:
        return False
    return _same_function_state(function, state)


def _guarded_site_replacement(
    site: _CallSite,
    items: list[Any],
    success: list[Any],
) -> list[Any] | None:
    """Wrap an inline expansion in a one-lookup runtime deoptimization guard."""

    if not site.guard_loader or site.guarded_identity is None:
        return None
    state = _callable_guard_state(site.guarded_identity, site.callee, site.binding)
    if state is None:
        return None

    fallback = Label()
    done = Label()
    location = site.guard_loader[0].location
    guard: list[Any] = [*site.guard_loader]

    if (
        state.kind == "function"
        and state.closure_values is None
        and not state.kwdefaults
    ):
        # The overwhelmingly common case can be guarded entirely in bytecode.
        # Identity is checked first, so subsequent exact function attributes are
        # safe to read without invoking any user-defined descriptor behavior.
        guard.extend(
            [
                Instr("COPY", 1, location=location),
                Instr("LOAD_CONST", state.identity, location=location),
                Instr("IS_OP", 0, location=location),
                Instr("POP_JUMP_IF_FALSE", fallback, location=location),
                Instr("COPY", 1, location=location),
                Instr("LOAD_ATTR", (False, "__code__"), location=location),
                Instr("LOAD_CONST", state.code, location=location),
                Instr("IS_OP", 0, location=location),
                Instr("POP_JUMP_IF_FALSE", fallback, location=location),
            ]
        )
        if state.check_defaults:
            guard.extend(
                [
                    Instr("COPY", 1, location=location),
                    Instr("LOAD_ATTR", (False, "__defaults__"), location=location),
                    Instr("LOAD_CONST", state.defaults, location=location),
                    Instr("IS_OP", 0, location=location),
                    Instr("POP_JUMP_IF_FALSE", fallback, location=location),
                ]
            )
    else:
        # Bound methods, partials, callable objects, closure-frozen functions, and
        # kw-only defaults need compound state validation.  The helper performs only
        # exact builtin type/identity checks and leaves the loaded callable untouched.
        guard.extend(
            [
                Instr("LOAD_CONST", _inline_callable_guard_matches, location=location),
                Instr("PUSH_NULL", location=location),
                Instr("COPY", 3, location=location),
                Instr("LOAD_CONST", state, location=location),
                Instr("CALL", 2, location=location),
                Instr("POP_JUMP_IF_FALSE", fallback, location=location),
            ]
        )

    guard.extend(
        [
            Instr("POP_TOP", location=location),
            *success,
            Instr("JUMP_FORWARD", done, location=location),
            fallback,
            Instr("PUSH_NULL", location=location),
            *items[site.callable_end : site.call_index + 1],
            done,
        ]
    )
    return guard


# Registry is isolated by globals dictionary to avoid collisions between modules that
# happen to define inlineable functions with the same global name. Weak references avoid
# retaining locally-created registered functions forever. Identity refcounts keep alias
# resolution exact when a registration is replaced, rolled back, or removed.
_RegistryKey = tuple[int, str, int | None]
_registry: dict[_RegistryKey, weakref.ReferenceType[Callable[..., Any]]] = {}
_registry_identities: dict[
    _RegistryKey, tuple[weakref.ReferenceType[Callable[..., Any]], ...]
] = {}
_registered_identity_counts: weakref.WeakKeyDictionary[Callable[..., Any], int] = (
    weakref.WeakKeyDictionary()
)
_registry_lock = threading.RLock()
_counter = itertools.count()


def _identity_increment(func: Callable[..., Any]) -> None:
    _registered_identity_counts[func] = _registered_identity_counts.get(func, 0) + 1


def _identity_decrement(func: Callable[..., Any]) -> None:
    count = _registered_identity_counts.get(func, 0)
    if count <= 1:
        _registered_identity_counts.pop(func, None)
    else:
        _registered_identity_counts[func] = count - 1


def _remove_registry_entry(
    key: _RegistryKey,
) -> tuple[Callable[..., Any] | None, tuple[Callable[..., Any], ...]]:
    ref = _registry.pop(key, None)
    current = ref() if ref is not None else None
    identities: list[Callable[..., Any]] = []
    for identity_ref in _registry_identities.pop(key, ()):
        identity = identity_ref()
        if identity is not None:
            identities.append(identity)
            _identity_decrement(identity)
    return current, tuple(identities)


def _install_registry_entry(
    key: _RegistryKey,
    current: Callable[..., Any],
    identities: tuple[Callable[..., Any], ...],
) -> None:
    # Keep the helper robust even if a future caller forgets the explicit remove.
    if key in _registry:
        _remove_registry_entry(key)

    # Deduplicate by identity because a no-op transformation can make ``current``
    # and ``original`` the same function object.
    unique: list[Callable[..., Any]] = []
    seen: set[int] = set()
    for identity in identities:
        marker = id(identity)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(identity)
        _identity_increment(identity)

    def cleanup(ref: weakref.ReferenceType[Callable[..., Any]]) -> None:
        # Weakref callbacks can run long after registration. Only remove the slot if
        # it still belongs to this exact ref; a newer replacement must survive.
        with _registry_lock:
            if _registry.get(key) is ref:
                _remove_registry_entry(key)

    _registry[key] = weakref.ref(current, cleanup)
    _registry_identities[key] = tuple(weakref.ref(identity) for identity in unique)


def _next_counter() -> int:
    """Return a process-unique synthetic-name token for concurrent decorators."""

    with _registry_lock:
        return next(_counter)

_DEFAULT_MAX_EXPANSIONS = 10_000
_DEFAULT_MAX_GROWTH_FACTOR = 256
_DEFAULT_MAX_CODE_BYTES = 16 * 1024 * 1024


def _require_runtime() -> None:
    if platform.python_implementation() != "CPython":
        raise InlineUnsupportedError("@inline_function currently requires CPython")
    if sys.version_info[:2] != (3, 13):
        raise InlineUnsupportedError(
            f"@inline_function targets CPython 3.13; found "
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )


def _registry_key_for_global(globals_dict: dict[str, Any], name: str) -> _RegistryKey:
    # Direct global functions have ``qualname == name`` and therefore use the
    # stable lexical key with no identity discriminator.
    return id(globals_dict), name, None


def _registry_key_for_function(func: Callable[..., Any]) -> _RegistryKey:
    original = getattr(func, "__inline_original__", None)
    lexical = original if isinstance(original, types.FunctionType) else func
    qualname = lexical.__qualname__
    # Distinct executions of one local/factory function share a qualname but may
    # have different defaults or closure cells. Keep those registrations separate.
    discriminator = id(lexical) if "<locals>" in qualname else None
    return id(lexical.__globals__), qualname, discriminator


def _resolve_registered(caller: Callable[..., Any], name: str) -> Callable[..., Any] | None:
    key = _registry_key_for_global(caller.__globals__, name)
    with _registry_lock:
        ref = _registry.get(key)
        if ref is None:
            return None
        callee = ref()
        if callee is None:
            _remove_registry_entry(key)
            return None

        # Do not silently hardwire a stale registration after the caller module has
        # rebound the global. The decorator is compile-time, but it must at least inline
        # the function that the global resolves to at decoration time.
        current = caller.__globals__.get(name)
        if current is callee:
            return callee
        if getattr(current, "__inline_original__", None) is callee:
            return current
        if getattr(callee, "__inline_original__", None) is current:
            return callee
        return None


def _validate_callee(
    func: Callable[..., Any],
    *,
    freeze_closures: bool = False,
    freeze_globals: bool = False,
) -> None:
    if not isinstance(func, types.FunctionType):
        raise InlineUnsupportedError("only ordinary Python functions can be inlined")

    code = func.__code__
    if inspect.isgeneratorfunction(func) or inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func):
        raise InlineUnsupportedError(f"{func.__qualname__}: generators/coroutines are not inlineable")
    if code.co_cellvars:
        raise InlineUnsupportedError(f"{func.__qualname__}: cell variables are not inlineable")
    if code.co_freevars and not freeze_closures:
        raise InlineUnsupportedError(
            f"{func.__qualname__}: read-only closures require freeze_closures=True"
        )

    forbidden = {
        "YIELD_VALUE",
        "YIELD_FROM",
        "RETURN_GENERATOR",
        "SEND",
    }
    frame_sensitive_globals = {"locals", "vars", "eval", "exec", "dir"}
    frame_sensitive_attributes = {"_getframe", "currentframe"}
    for item in Bytecode.from_code(code):
        if not isinstance(item, Instr):
            continue
        if freeze_globals and item.name in {"STORE_GLOBAL", "DELETE_GLOBAL"}:
            raise InlineUnsupportedError(
                f"{func.__qualname__}: mutating globals is incompatible with freeze_globals=True"
            )
        if item.name in {"STORE_DEREF", "DELETE_DEREF"}:
            raise InlineUnsupportedError(
                f"{func.__qualname__}: mutating closure cells is not inlineable"
            )
        if item.name in forbidden or item.name == "LOAD_LOCALS":
            raise InlineUnsupportedError(f"{func.__qualname__}: unsupported opcode {item.name}")
        if item.name == "LOAD_GLOBAL":
            _, name = item.arg
            if name in frame_sensitive_globals:
                raise InlineUnsupportedError(
                    f"{func.__qualname__}: frame-sensitive global {name}() is not inlineable"
                )
        if item.name in {"LOAD_ATTR", "LOAD_METHOD"}:
            value = item.arg[1] if isinstance(item.arg, tuple) else item.arg
            if value in frame_sensitive_attributes:
                raise InlineUnsupportedError(
                    f"{func.__qualname__}: frame-sensitive attribute {value} is not inlineable"
                )


def _is_control_flow_instruction(instr: Instr) -> bool:
    name = instr.name
    return (
        "JUMP" in name
        or name in {
            "FOR_ITER",
            "END_FOR",
            "RETURN_VALUE",
            "RETURN_CONST",
            "RAISE_VARARGS",
            "RERAISE",
        }
    )


def _call_region_start(items: list[Any], call_index: int) -> int | None:
    """Find the straight-line expression region consumed by CALL/CALL_KW.

    The reverse stack calculation uses CPython/bytecode stack effects, so nested calls
    and arbitrary straight-line argument expressions are handled without guessing for
    the nearest LOAD_GLOBAL. Labels, exception markers, and branches delimit a region
    and make the call site conservatively non-inlineable.
    """

    call = items[call_index]
    if not isinstance(call, Instr) or call.name not in {"CALL", "CALL_KW"}:
        return None

    needed = 1 - call.stack_effect()  # stack entries consumed by the call
    index = call_index - 1
    while needed > 0:
        if index < 0:
            return None
        item = items[index]
        if not isinstance(item, Instr):
            return None
        if _is_control_flow_instruction(item):
            return None
        needed -= item.stack_effect()
        if needed < 0:
            # The start would be inside a multi-output superinstruction.
            return None
        index -= 1
    return index + 1


def _registered_function(value: Any) -> Callable[..., Any] | None:
    # Bound methods delegate arbitrary attributes to ``__func__``. Restrict this
    # resolver to actual function objects so a bound method is not mistaken for its
    # underlying function without preserving the implicit receiver.
    if not isinstance(value, types.FunctionType):
        return None
    with _registry_lock:
        if _registered_identity_counts.get(value, 0):
            return value
        original = getattr(value, "__inline_original__", None)
        if (
            isinstance(original, types.FunctionType)
            and _registered_identity_counts.get(original, 0)
        ):
            return value
        return None


def _registered_bound_callable(
    value: Any,
) -> tuple[Callable[..., Any], tuple[Any, ...], tuple[tuple[str, Any], ...]] | None:
    """Resolve exact bound/partial/callable-object targets frozen at decoration time.

    Only positional ``functools.partial`` bindings are accepted. Keyword-bearing
    partials require reordering caller keyword values and are deliberately left as
    ordinary calls until that transformation can preserve every insertion-order edge.
    """

    if isinstance(value, types.MethodType):
        registered = _registered_function(value.__func__)
        if registered is not None:
            return registered, (value.__self__,), ()

    if isinstance(value, functools.partial):
        base = value.func
        keyword_items = tuple((value.keywords or {}).items())
        if isinstance(base, types.MethodType):
            registered = _registered_function(base.__func__)
            if registered is not None:
                return registered, (base.__self__, *value.args), keyword_items
        registered = _registered_function(base)
        if registered is not None:
            return registered, tuple(value.args), keyword_items

    if not isinstance(value, (types.FunctionType, type)):
        call_impl = getattr(type(value), "__call__", None)
        registered = _registered_function(call_impl)
        if registered is not None:
            return registered, (value,), ()

    return None


def _registered_guarded_closure_callable(
    value: Any,
) -> tuple[Callable[..., Any], tuple[Any, ...], tuple[tuple[str, Any], ...]] | None:
    """Resolve closure-held callables whose behavior is stable under identity guard.

    Exact functions and bound methods are stable once the loaded object identity is
    proven.  Positional-only ``functools.partial`` objects are also safe: their bound
    positional tuple is immutable and the base callable is fixed by the partial
    object.  Keyword-bearing partials are deliberately excluded because the public
    ``partial.keywords`` dictionary can mutate without changing object identity.
    Generic callable instances are excluded because their type's ``__call__`` may be
    replaced while the instance identity stays unchanged.
    """
    registered = _registered_function(value)
    if registered is not None:
        return registered, (), ()
    if isinstance(value, types.MethodType):
        registered = _registered_function(value.__func__)
        if registered is not None:
            return registered, (value.__self__,), ()
    if isinstance(value, functools.partial) and not (value.keywords or {}):
        base = value.func
        if isinstance(base, types.MethodType):
            registered = _registered_function(base.__func__)
            if registered is not None:
                return registered, (base.__self__, *value.args), ()
        registered = _registered_function(base)
        if registered is not None:
            return registered, tuple(value.args), ()
    return None


def _parse_callable_prefix(
    caller: Callable[..., Any], items: list[Any], start: int, call_index: int
) -> tuple[
    Callable[..., Any],
    int,
    tuple[Any, ...],
    tuple[tuple[str, Any], ...],
    bool,
    Any | None,
    tuple[Instr, ...],
] | None:
    """Resolve a provably direct registered callable prefix.

    The resolver records enough information for either frozen compilation or
    guarded runtime binding. Closure-held targets retain their evaluate-once load
    shape so guarded fallback can reuse the exact callable object without a second
    cell read. Global/attribute targets record normalized one-object loaders for the
    same reason.
    """

    first = items[start]
    if not isinstance(first, Instr):
        return None

    if first.name == "LOAD_DEREF" and isinstance(first.arg, FreeVar):
        name = first.arg.name
        try:
            closure_index = caller.__code__.co_freevars.index(name)
            cell = (caller.__closure__ or ())[closure_index]
            current = cell.cell_contents
        except (ValueError, IndexError):
            return None
        guarded = _registered_guarded_closure_callable(current)
        if guarded is None:
            return None
        callee, implicit_values, implicit_keywords = guarded
        if start + 1 < call_index:
            second = items[start + 1]
            if isinstance(second, Instr) and second.name == "PUSH_NULL":
                return (
                    callee,
                    start + 2,
                    implicit_values,
                    implicit_keywords,
                    True,
                    current,
                    (first,),
                )
        return None

    if first.name != "LOAD_GLOBAL":
        return None
    push_null, name = first.arg

    current_global = caller.__globals__.get(name)
    callee = _resolve_registered(caller, name)
    implicit_values: tuple[Any, ...] = ()
    implicit_keywords: tuple[tuple[str, Any], ...] = ()
    if callee is None:
        callee = _registered_function(current_global)
    if callee is None:
        bound = _registered_bound_callable(current_global)
        if bound is not None:
            callee, implicit_values, implicit_keywords = bound
    if callee is not None:
        guard_loader = (
            Instr("LOAD_GLOBAL", (False, name), location=first.location),
        )
        if push_null:
            return (
                callee,
                start + 1,
                implicit_values,
                implicit_keywords,
                False,
                current_global,
                guard_loader,
            )
        if start + 1 < call_index:
            second = items[start + 1]
            if isinstance(second, Instr) and second.name == "PUSH_NULL":
                return (
                    callee,
                    start + 2,
                    implicit_values,
                    implicit_keywords,
                    False,
                    current_global,
                    guard_loader,
                )

    if start + 1 < call_index:
        attr = items[start + 1]
        if isinstance(attr, Instr) and attr.name == "LOAD_ATTR":
            method_flag, attr_name = attr.arg
            if method_flag:
                owner = caller.__globals__.get(name, UNSET)
                if owner is not UNSET:
                    try:
                        resolved = getattr(owner, attr_name)
                    except Exception:
                        resolved = None
                    if isinstance(resolved, types.MethodType):
                        registered = _registered_function(resolved.__func__)
                        if registered is not None:
                            return (
                                registered,
                                start + 2,
                                (resolved.__self__,),
                                (),
                                False,
                                resolved,
                                (
                                    Instr(
                                        "LOAD_GLOBAL",
                                        (False, name),
                                        location=first.location,
                                    ),
                                    Instr(
                                        "LOAD_ATTR",
                                        (False, attr_name),
                                        location=attr.location,
                                    ),
                                ),
                            )
                    else:
                        registered = _registered_function(resolved)
                        if registered is not None:
                            return (
                                registered,
                                start + 2,
                                (),
                                (),
                                False,
                                resolved,
                                (
                                    Instr(
                                        "LOAD_GLOBAL",
                                        (False, name),
                                        location=first.location,
                                    ),
                                    Instr(
                                        "LOAD_ATTR",
                                        (False, attr_name),
                                        location=attr.location,
                                    ),
                                ),
                            )
    return None

def _bind_call(callee: Callable[..., Any], nargs: int, kw_names: tuple[str, ...]) -> _BindingPlan:
    code = callee.__code__
    positional_count = code.co_argcount
    posonly_count = code.co_posonlyargcount
    kwonly_count = code.co_kwonlyargcount
    has_varargs = bool(code.co_flags & inspect.CO_VARARGS)
    has_varkw = bool(code.co_flags & inspect.CO_VARKEYWORDS)

    positional_names = list(code.co_varnames[:positional_count])
    posonly_names = set(positional_names[:posonly_count])
    kwonly_names = list(code.co_varnames[positional_count : positional_count + kwonly_count])
    next_index = positional_count + kwonly_count
    vararg_name = code.co_varnames[next_index] if has_varargs else None
    if has_varargs:
        next_index += 1
    varkw_name = code.co_varnames[next_index] if has_varkw else None

    explicit_keyword_count = len(kw_names)
    positional_supplied = nargs - explicit_keyword_count
    if positional_supplied < 0:
        raise InlineCallSiteError(f"{callee.__qualname__}: malformed CALL_KW argument count")
    if positional_supplied > positional_count and not has_varargs:
        raise InlineCallSiteError(
            f"{callee.__qualname__}: {positional_supplied} positional arguments supplied; "
            f"at most {positional_count} accepted"
        )

    direct_positional_count = min(positional_supplied, positional_count)
    positional_targets = positional_names[:direct_positional_count]
    extra_positional_count = positional_supplied - direct_positional_count
    provided = set(positional_targets)
    keyword_targets: list[str | None] = []

    keyword_eligible = set(positional_names[posonly_count:]) | set(kwonly_names)
    for name in kw_names:
        if name in posonly_names:
            if has_varkw:
                keyword_targets.append(None)
                continue
            raise InlineCallSiteError(
                f"{callee.__qualname__}: positional-only parameter {name!r} passed by keyword"
            )
        if name not in keyword_eligible:
            if has_varkw:
                keyword_targets.append(None)
                continue
            raise InlineCallSiteError(
                f"{callee.__qualname__}: unexpected keyword argument {name!r}"
            )
        if name in provided:
            raise InlineCallSiteError(
                f"{callee.__qualname__}: multiple values for argument {name!r}"
            )
        keyword_targets.append(name)
        provided.add(name)

    defaults: list[tuple[str, Any]] = []
    positional_defaults = callee.__defaults__ or ()
    first_default = positional_count - len(positional_defaults)
    for index, name in enumerate(positional_names):
        if name in provided:
            continue
        if index < first_default:
            raise InlineCallSiteError(
                f"{callee.__qualname__}: missing required argument {name!r}"
            )
        defaults.append((name, positional_defaults[index - first_default]))

    kw_defaults = callee.__kwdefaults__ or {}
    for name in kwonly_names:
        if name in provided:
            continue
        if name not in kw_defaults:
            raise InlineCallSiteError(
                f"{callee.__qualname__}: missing required keyword-only argument {name!r}"
            )
        defaults.append((name, kw_defaults[name]))

    return _BindingPlan(
        tuple(positional_targets),
        extra_positional_count,
        tuple(keyword_targets),
        tuple(kw_names),
        tuple(defaults),
        vararg_name,
        varkw_name,
    )


def _find_direct_call(
    bytecode: Bytecode,
    caller: Callable[..., Any],
    excluded_call_indices: set[int] | None = None,
) -> _CallSite | None:
    """Find the earliest innermost provably-direct registered call."""

    excluded_call_indices = excluded_call_indices or set()
    items = list(bytecode)
    for call_index, item in enumerate(items):
        if call_index in excluded_call_indices:
            continue
        if not isinstance(item, Instr) or item.name not in {"CALL", "CALL_KW"}:
            continue

        start = _call_region_start(items, call_index)
        if start is None:
            continue
        resolved = _parse_callable_prefix(caller, items, start, call_index)
        if resolved is None:
            continue
        (
            callee,
            callable_end,
            implicit_values,
            implicit_keywords,
            guarded_closure,
            guarded_identity,
            guard_loader,
        ) = resolved

        nargs = int(item.arg) + len(implicit_values) + len(implicit_keywords)
        kw_names_index: int | None = None
        kw_names: tuple[str, ...] = ()
        if item.name == "CALL_KW":
            kw_names_index = call_index - 1
            kw_item = items[kw_names_index]
            if not (
                isinstance(kw_item, Instr)
                and kw_item.name == "LOAD_CONST"
                and isinstance(kw_item.arg, tuple)
                and all(isinstance(name, str) for name in kw_item.arg)
            ):
                raise InlineCallSiteError("CALL_KW is not preceded by a constant keyword-name tuple")
            kw_names = kw_item.arg
            if len(kw_names) > int(item.arg):
                raise InlineCallSiteError("CALL_KW keyword count exceeds argument count")
            if implicit_keywords:
                # Correct partial-keyword overriding requires storing and reordering
                # caller keyword expressions. Keep this overlap form ordinary for now.
                continue

        if implicit_keywords:
            kw_names = tuple(name for name, _ in implicit_keywords)

        binding = _bind_call(callee, nargs, kw_names)
        return _CallSite(
            start,
            callable_end,
            call_index,
            kw_names_index,
            callee,
            binding,
            implicit_values,
            implicit_keywords,
            guarded_closure,
            guarded_identity,
            guard_loader,
        )
    return None


def _map_local_arg(arg: Any, local_map: dict[str, str]) -> Any:
    if isinstance(arg, str):
        return local_map.get(arg, arg)
    if isinstance(arg, tuple):
        return tuple(local_map.get(value, value) if isinstance(value, str) else value for value in arg)
    return arg


def _clone_exception_markers(source: Bytecode, label_map: dict[Label, Label]) -> tuple[dict[TryBegin, TryBegin], dict[TryEnd, TryEnd]]:
    begin_map: dict[TryBegin, TryBegin] = {}
    end_map: dict[TryEnd, TryEnd] = {}

    for item in source:
        if isinstance(item, TryBegin):
            begin_map[item] = TryBegin(
                label_map[item.target],
                item.push_lasti,
                # Let bytecode recompute the merged absolute stack depth. This is
                # required when the call appears inside a larger caller expression.
                UNSET,
            )
    for item in source:
        if isinstance(item, TryEnd):
            end_map[item] = TryEnd(begin_map[item.entry])
    return begin_map, end_map


def _fast_written_names(source: Bytecode) -> set[str]:
    written: set[str] = set()
    for item in source:
        if not isinstance(item, Instr):
            continue
        if item.name in {"STORE_FAST", "STORE_FAST_MAYBE_NULL", "DELETE_FAST", "LOAD_FAST_AND_CLEAR"}:
            if isinstance(item.arg, str):
                written.add(item.arg)
        elif item.name in {"STORE_FAST_STORE_FAST", "STORE_FAST_LOAD_FAST"}:
            values = item.arg if isinstance(item.arg, tuple) else (item.arg,)
            if values and isinstance(values[0], str):
                written.add(values[0])
            if item.name == "STORE_FAST_STORE_FAST" and len(values) > 1 and isinstance(values[1], str):
                written.add(values[1])
    return written


def _forwardable_prefix_rewrites(
    source: Bytecode,
    parameters: tuple[str, ...],
    constant_parameters: dict[str, Any],
) -> dict[int, list[Instr]] | None:
    """Plan leading argument-load elimination with interleaved frozen defaults.

    Caller argument values already occupy the stack in ``parameters`` order. Loads
    for those parameters may therefore disappear only before any emitted load changes
    their relative order. Once all forwarded values are consumed, read-only frozen
    parameters may be emitted as constants.
    """

    if not parameters:
        return {}

    pure_loads = {
        "LOAD_FAST",
        "LOAD_FAST_CHECK",
        "LOAD_FAST_BORROW",
        "LOAD_FAST_LOAD_FAST",
        "LOAD_FAST_BORROW_LOAD_FAST_BORROW",
    }
    expected = list(parameters)
    consumed = 0
    rewrites: dict[int, list[Instr]] = {}
    source_items = list(source)

    for index, item in enumerate(source_items):
        if not isinstance(item, Instr) or item.name in {"RESUME", "COPY_FREE_VARS"}:
            if isinstance(item, (Label, TryBegin, TryEnd)) and consumed < len(expected):
                return None
            continue
        if consumed >= len(expected):
            break
        if item.name not in pure_loads:
            return None
        values = item.arg if isinstance(item.arg, tuple) else (item.arg,)
        emitted: list[Instr] = []
        for value in values:
            if not isinstance(value, str):
                return None
            if consumed < len(expected) and value == expected[consumed]:
                consumed += 1
                continue
            if value in constant_parameters and consumed == len(expected):
                emitted.append(Instr("LOAD_CONST", constant_parameters[value], location=item.location))
                continue
            return None
        rewrites[index] = emitted

    if consumed != len(expected):
        return None

    forwarded_set = set(parameters)
    for index, item in enumerate(source_items):
        if index in rewrites or not isinstance(item, Instr) or "FAST" not in item.name:
            continue
        values = item.arg if isinstance(item.arg, tuple) else (item.arg,)
        if forwarded_set.intersection(value for value in values if isinstance(value, str)):
            return None
    return rewrites


def _replace_constant_fast_load(
    item: Instr,
    constant_parameters: dict[str, Any],
    local_map: dict[str, str],
) -> list[Instr] | None:
    """Expand FAST load superinstructions when any operand is frozen."""

    simple = {"LOAD_FAST", "LOAD_FAST_CHECK", "LOAD_FAST_BORROW"}
    paired = {"LOAD_FAST_LOAD_FAST", "LOAD_FAST_BORROW_LOAD_FAST_BORROW"}
    if item.name in simple and isinstance(item.arg, str) and item.arg in constant_parameters:
        return [Instr("LOAD_CONST", constant_parameters[item.arg], location=item.location)]
    if item.name in paired and isinstance(item.arg, tuple):
        if not any(name in constant_parameters for name in item.arg):
            return None
        result: list[Instr] = []
        for name in item.arg:
            if name in constant_parameters:
                result.append(Instr("LOAD_CONST", constant_parameters[name], location=item.location))
            else:
                result.append(Instr("LOAD_FAST", local_map.get(name, name), location=item.location))
        return result
    return None



def _resolve_frozen_global(func: Callable[..., Any], name: str) -> Any:
    if name in func.__globals__:
        return func.__globals__[name]
    builtins_obj = func.__builtins__
    if isinstance(builtins_obj, dict):
        if name in builtins_obj:
            return builtins_obj[name]
    else:
        try:
            return getattr(builtins_obj, name)
        except AttributeError:
            pass
    raise InlineUnsupportedError(
        f"{func.__qualname__}: global name {name!r} cannot be frozen"
    )


def _constant_truth(value: Any) -> bool | None:
    """Return side-effect-free truth for exact immutable builtin constants."""

    if value is None:
        return False
    if type(value) in {bool, int, float, complex, str, bytes, tuple, frozenset}:
        return bool(value)
    return None


def _small_folded_constant(value: Any) -> bool:
    if type(value) is int:
        return value.bit_length() <= 4096
    if type(value) in {str, bytes, tuple}:
        return len(value) <= 4096
    return type(value) in {bool, float, complex}


def _fold_constant_unary_ops(items: list[Any]) -> tuple[list[Any], int]:
    """Fold unary operations over exact immutable builtin constants.

    The pass deliberately excludes arbitrary objects so decoration never invokes
    user ``__neg__``, ``__invert__``, ``__bool__``, or ``__len__`` methods.
    """

    import operator

    result = list(items)
    folds = 0
    index = 0
    while index < len(result):
        first = result[index]
        if not (isinstance(first, Instr) and first.name == "LOAD_CONST"):
            index += 1
            continue

        if index + 1 < len(result) and isinstance(result[index + 1], Instr):
            unary = result[index + 1]
            operation = None
            allowed: set[type[Any]] = set()
            if unary.name == "UNARY_NEGATIVE":
                operation = operator.neg
                allowed = {bool, int, float, complex}
            elif unary.name == "UNARY_INVERT":
                operation = operator.invert
                allowed = {bool, int}
            elif (
                unary.name == "CALL_INTRINSIC_1"
                and unary.arg == Intrinsic1Op.INTRINSIC_UNARY_POSITIVE
            ):
                operation = operator.pos
                allowed = {bool, int, float, complex}
            if operation is not None and type(first.arg) in allowed:
                try:
                    value = operation(first.arg)
                except Exception:
                    value = UNSET
                if value is not UNSET and _small_folded_constant(value):
                    result[index:index + 2] = [
                        Instr("LOAD_CONST", value, location=unary.location)
                    ]
                    folds += 1
                    index = max(0, index - 1)
                    continue
            if unary.name == "UNARY_NOT":
                truth = _constant_truth(first.arg)
                if truth is not None:
                    result[index:index + 2] = [
                        Instr("LOAD_CONST", not truth, location=unary.location)
                    ]
                    folds += 1
                    index = max(0, index - 1)
                    continue

        # CPython 3.13 commonly lowers ``not x`` as TO_BOOL + UNARY_NOT.  A
        # preceding constant fold can also expose LOAD_CONST + UNARY_NOT directly.
        # Exact builtin constants are side-effect-free under _constant_truth().
        # constants have side-effect-free truth testing under _constant_truth().
        if index + 2 < len(result):
            to_bool, unary_not = result[index + 1:index + 3]
            if (
                isinstance(to_bool, Instr)
                and to_bool.name == "TO_BOOL"
                and isinstance(unary_not, Instr)
                and unary_not.name == "UNARY_NOT"
            ):
                truth = _constant_truth(first.arg)
                if truth is not None:
                    result[index:index + 3] = [
                        Instr("LOAD_CONST", not truth, location=unary_not.location)
                    ]
                    folds += 1
                    index = max(0, index - 1)
                    continue
        index += 1
    return result, folds


def _fold_constant_binary_ops(items: list[Any]) -> tuple[list[Any], int]:
    """Fold pure binary operations over exact builtin constants.

    In-place operators and user-defined objects are excluded. Runtime exceptions
    (for example division by zero) stay runtime exceptions: a failed fold is simply
    left untouched. Result-size limits prevent decoration-time constant explosions.
    """

    import operator

    operations = {
        BinaryOp.ADD: operator.add,
        BinaryOp.AND: operator.and_,
        BinaryOp.FLOOR_DIVIDE: operator.floordiv,
        BinaryOp.LSHIFT: operator.lshift,
        BinaryOp.MULTIPLY: operator.mul,
        BinaryOp.REMAINDER: operator.mod,
        BinaryOp.OR: operator.or_,
        BinaryOp.POWER: operator.pow,
        BinaryOp.RSHIFT: operator.rshift,
        BinaryOp.SUBTRACT: operator.sub,
        BinaryOp.TRUE_DIVIDE: operator.truediv,
        BinaryOp.XOR: operator.xor,
    }
    allowed = {bool, int, float, complex, str, bytes, tuple}
    result = list(items)
    folds = 0
    index = 0
    while index + 2 < len(result):
        left, right, operation = result[index : index + 3]
        if not (
            isinstance(left, Instr)
            and left.name == "LOAD_CONST"
            and isinstance(right, Instr)
            and right.name == "LOAD_CONST"
            and isinstance(operation, Instr)
            and operation.name == "BINARY_OP"
            and operation.arg in operations
            and type(left.arg) in allowed
            and type(right.arg) in allowed
        ):
            index += 1
            continue
        try:
            value = operations[operation.arg](left.arg, right.arg)
        except Exception:
            index += 1
            continue
        if not _small_folded_constant(value):
            index += 1
            continue
        result[index : index + 3] = [
            Instr("LOAD_CONST", value, location=operation.location)
        ]
        folds += 1
        # Revisit one position earlier so chains of constants collapse.
        index = max(0, index - 1)
    return result, folds


def _fold_constant_comparisons(items: list[Any]) -> tuple[list[Any], int]:
    """Fold side-effect-free comparisons between exact primitive constants."""

    allowed = {type(None), bool, int, float, complex, str, bytes}
    result = list(items)
    folds = 0
    index = 0
    compare_ops = {
        Compare.LT: lambda a, b: a < b,
        Compare.LE: lambda a, b: a <= b,
        Compare.EQ: lambda a, b: a == b,
        Compare.NE: lambda a, b: a != b,
        Compare.GT: lambda a, b: a > b,
        Compare.GE: lambda a, b: a >= b,
        Compare.LT_CAST: lambda a, b: a < b,
        Compare.LE_CAST: lambda a, b: a <= b,
        Compare.EQ_CAST: lambda a, b: a == b,
        Compare.NE_CAST: lambda a, b: a != b,
        Compare.GT_CAST: lambda a, b: a > b,
        Compare.GE_CAST: lambda a, b: a >= b,
    }
    while index + 2 < len(result):
        left, right, compare = result[index : index + 3]
        if not (
            isinstance(left, Instr)
            and left.name == "LOAD_CONST"
            and isinstance(right, Instr)
            and right.name == "LOAD_CONST"
            and isinstance(compare, Instr)
            and compare.name == "COMPARE_OP"
            and compare.arg in compare_ops
            and type(left.arg) in allowed
            and type(right.arg) in allowed
        ):
            index += 1
            continue
        try:
            value = bool(compare_ops[compare.arg](left.arg, right.arg))
        except Exception:
            # Preserve runtime exceptions such as ordering complex numbers.
            index += 1
            continue
        result[index : index + 3] = [
            Instr("LOAD_CONST", value, location=compare.location)
        ]
        folds += 1
        index += 1
    return result, folds


def _fold_constant_expression_fixpoint(
    items: list[Any],
) -> tuple[list[Any], int, int, int]:
    """Fold unary/binary/comparison constant chains to a true fixed point.

    CPython emits nested expressions in evaluation order, so one fold can expose
    another operation that appears *earlier* in the pass order.  For example,
    ``-(1 + 2)`` exposes ``UNARY_NEGATIVE`` only after the binary fold, while
    ``not (1 == 2)`` exposes ``UNARY_NOT`` only after the comparison fold.  Every
    successful fold strictly reduces the instruction stream, so iterating until no
    pass changes it is finite and keeps decoration-time behavior side-effect free.
    """

    result = list(items)
    unary_total = binary_total = comparison_total = 0
    while True:
        result, unary = _fold_constant_unary_ops(result)
        result, binary = _fold_constant_binary_ops(result)
        result, comparison = _fold_constant_comparisons(result)
        unary_total += unary
        binary_total += binary
        comparison_total += comparison
        if unary + binary + comparison == 0:
            break
    return result, unary_total, binary_total, comparison_total


def _fold_constant_branches(items: list[Any]) -> tuple[list[Any], int]:
    """Fold constant truth tests introduced by frozen/default propagation.

    This intentionally handles only exact immutable builtin constants.  It never
    calls user-defined ``__bool__``/``__len__`` at decoration time.
    """

    result = list(items)
    folds = 0
    index = 0
    while index < len(result):
        first = result[index]
        if not isinstance(first, Instr) or first.name != "LOAD_CONST":
            index += 1
            continue
        truth = _constant_truth(first.arg)
        if truth is None:
            index += 1
            continue
        branch_index = index + 1
        if (
            branch_index < len(result)
            and isinstance(result[branch_index], Instr)
            and result[branch_index].name == "TO_BOOL"
        ):
            branch_index += 1
        if branch_index >= len(result):
            index += 1
            continue
        branch = result[branch_index]
        if not isinstance(branch, Instr) or branch.name not in {
            "POP_JUMP_IF_FALSE",
            "POP_JUMP_IF_TRUE",
        }:
            index += 1
            continue
        target = branch.arg
        if not isinstance(target, Label):
            index += 1
            continue
        jump_taken = truth if branch.name == "POP_JUMP_IF_TRUE" else not truth
        if jump_taken:
            try:
                target_index = result.index(target)
            except ValueError:
                index += 1
                continue
            opname = "JUMP_FORWARD" if target_index > branch_index else "JUMP_BACKWARD"
            replacement: list[Any] = [Instr(opname, target, location=branch.location)]
        else:
            replacement = []
        result[index : branch_index + 1] = replacement
        folds += 1
        index += len(replacement)
    return result, folds


def _prune_unreachable_items(items: list[Any]) -> tuple[list[Any], int]:
    """Remove unreachable ordinary bytecode blocks after constant folding.

    Exception-marked bodies are left untouched because exception-table reachability
    is not represented by ordinary jump edges in this compact pass.
    """

    if any(isinstance(item, (TryBegin, TryEnd)) for item in items):
        return items, 0
    if not items:
        return items, 0

    label_indexes = {item: i for i, item in enumerate(items) if isinstance(item, Label)}
    terminal = {"RETURN_VALUE", "RETURN_CONST", "RAISE_VARARGS", "RERAISE"}

    def successors(index: int) -> tuple[int, ...]:
        item = items[index]
        next_index = index + 1 if index + 1 < len(items) else None
        if not isinstance(item, Instr):
            return () if next_index is None else (next_index,)
        if item.name in terminal:
            return ()
        target_index = label_indexes.get(item.arg) if isinstance(item.arg, Label) else None
        if target_index is not None:
            unconditional = item.name.startswith("JUMP_FORWARD") or item.name.startswith("JUMP_BACKWARD") or item.name in {
                "JUMP", "JUMP_NO_INTERRUPT"
            }
            if unconditional:
                return (target_index,)
            if next_index is None:
                return (target_index,)
            return (target_index, next_index)
        return () if next_index is None else (next_index,)

    reachable: set[int] = set()
    pending = [0]
    while pending:
        index = pending.pop()
        if index in reachable or not 0 <= index < len(items):
            continue
        reachable.add(index)
        pending.extend(successors(index))

    pruned = [item for i, item in enumerate(items) if i in reachable]
    removed = sum(
        1 for i, item in enumerate(items) if i not in reachable and isinstance(item, Instr)
    )
    return pruned, removed


def _remove_redundant_jumps(items: list[Any]) -> tuple[list[Any], int]:
    result = list(items)
    removed = 0
    index = 0
    while index + 1 < len(result):
        item = result[index]
        if (
            isinstance(item, Instr)
            and item.name in {"JUMP_FORWARD", "JUMP_BACKWARD"}
            and isinstance(item.arg, Label)
            and result[index + 1] is item.arg
        ):
            del result[index]
            removed += 1
            continue
        index += 1
    return result, removed


def _fast_reference_counts(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, Instr) or "FAST" not in item.name:
            continue
        values = item.arg if isinstance(item.arg, tuple) else (item.arg,)
        for value in values:
            if isinstance(value, str):
                counts[value] = counts.get(value, 0) + 1
    return counts


def _fast_accesses(item: Any) -> tuple[tuple[str, str], ...]:
    """Return (local-name, access-kind) pairs for supported FAST opcodes."""

    if not isinstance(item, Instr):
        return ()
    arg = item.arg
    if item.name in {"LOAD_FAST", "LOAD_FAST_CHECK", "LOAD_FAST_BORROW"}:
        return ((arg, "read"),) if isinstance(arg, str) else ()
    if item.name in {"STORE_FAST", "STORE_FAST_MAYBE_NULL"}:
        return ((arg, "write"),) if isinstance(arg, str) else ()
    if item.name == "DELETE_FAST":
        return ((arg, "delete"),) if isinstance(arg, str) else ()
    if item.name in {"LOAD_FAST_LOAD_FAST", "LOAD_FAST_BORROW_LOAD_FAST_BORROW"}:
        if isinstance(arg, tuple) and len(arg) == 2:
            return tuple((name, "read") for name in arg if isinstance(name, str))
    if item.name == "STORE_FAST_LOAD_FAST":
        if isinstance(arg, tuple) and len(arg) == 2:
            output: list[tuple[str, str]] = []
            if isinstance(arg[0], str):
                output.append((arg[0], "write"))
            if isinstance(arg[1], str):
                output.append((arg[1], "read"))
            return tuple(output)
    if item.name == "STORE_FAST_STORE_FAST":
        if isinstance(arg, tuple) and len(arg) == 2:
            return tuple((name, "write") for name in arg if isinstance(name, str))
    if item.name == "LOAD_FAST_AND_CLEAR":
        return ((arg, "read_delete"),) if isinstance(arg, str) else ()
    return ()


def _replacement_loads_for_value(
    item: Instr, dest: str, source_kind: str, source_value: Any
) -> list[Instr] | None:
    """Rewrite a pure FAST load containing ``dest`` to the propagated value."""

    def load(location: Any) -> Instr:
        if source_kind == "const":
            return Instr("LOAD_CONST", source_value, location=location)
        return Instr("LOAD_FAST", source_value, location=location)

    if item.name == "LOAD_FAST" and item.arg == dest:
        return [load(item.location)]
    if item.name == "LOAD_FAST_LOAD_FAST" and isinstance(item.arg, tuple):
        if dest not in item.arg:
            return None
        if source_kind == "fast":
            # Preserve CPython 3.13's compact paired-load superinstruction.  The
            # bytecode assembler automatically expands it when local indexes cannot
            # be represented by the packed form.
            values = tuple(source_value if name == dest else name for name in item.arg)
            return [Instr("LOAD_FAST_LOAD_FAST", values, location=item.location)]
        output: list[Instr] = []
        for name in item.arg:
            if name == dest:
                output.append(load(item.location))
            else:
                output.append(Instr("LOAD_FAST", name, location=item.location))
        return output
    return None


def _propagate_single_assignment_synthetic_locals(
    items: list[Any], synthetic_names: set[str]
) -> tuple[list[Any], int, int]:
    """SSA-like copy/constant propagation for straight-line synthetic locals.

    Only a single assignment from ``LOAD_FAST`` or ``LOAD_CONST`` is considered.
    The complete assignment/use span must contain no labels, exception markers, or
    control-flow instructions, and every later reference must be a pure load.  For a
    fast-local source, the source itself must not be written in the span.  These
    restrictions make replacing the compiler-generated temporary with its source an
    exact local substitution rather than a speculative optimization.
    """

    result = list(items)
    copy_count = 0
    const_count = 0
    changed = True
    while changed:
        changed = False
        for producer_index in range(len(result) - 1):
            producer = result[producer_index]
            store = result[producer_index + 1]
            if not isinstance(producer, Instr) or producer.name not in {"LOAD_FAST", "LOAD_CONST"}:
                continue
            if producer.name == "LOAD_FAST" and not isinstance(producer.arg, str):
                continue

            fused = False
            dest: str | None = None
            if isinstance(store, Instr) and store.name == "STORE_FAST" and isinstance(store.arg, str):
                dest = store.arg
            elif (
                isinstance(store, Instr)
                and store.name == "STORE_FAST_LOAD_FAST"
                and isinstance(store.arg, tuple)
                and len(store.arg) == 2
                and store.arg[0] == store.arg[1]
                and isinstance(store.arg[0], str)
            ):
                dest = store.arg[0]
                fused = True
            if dest is None or dest not in synthetic_names:
                continue

            accesses: list[tuple[int, str]] = []
            unsupported = False
            for index, item in enumerate(result):
                for name, kind in _fast_accesses(item):
                    if name == dest:
                        accesses.append((index, kind))
                        if kind not in {"read", "write"}:
                            unsupported = True
            writes = [index for index, kind in accesses if kind == "write"]
            if unsupported or writes != [producer_index + 1]:
                continue
            later_reads = [index for index, kind in accesses if kind == "read" and index > producer_index + 1]
            last_index = max(later_reads, default=producer_index + 1)

            span = result[producer_index : last_index + 1]
            if any(isinstance(item, (Label, TryBegin, TryEnd)) for item in span):
                continue
            if any(isinstance(item, Instr) and _is_control_flow_instruction(item) for item in span):
                continue

            source_kind = "const" if producer.name == "LOAD_CONST" else "fast"
            source_value = producer.arg
            if source_kind == "fast":
                if source_value == dest:
                    continue
                # Re-reading the source later is equivalent only when no write can
                # change its value during the propagated lifetime.
                if any(
                    name == source_value and kind in {"write", "delete", "read_delete"}
                    for item in span[1:]
                    for name, kind in _fast_accesses(item)
                ):
                    continue

            rewrites: dict[int, list[Instr]] = {}
            ok = True
            for read_index in later_reads:
                rewritten = _replacement_loads_for_value(
                    result[read_index], dest, source_kind, source_value
                )
                if rewritten is None:
                    ok = False
                    break
                rewrites[read_index] = rewritten
            if not ok:
                continue

            rebuilt: list[Any] = []
            for index, item in enumerate(result):
                if index in rewrites:
                    rebuilt.extend(rewrites[index])
                    continue
                if index == producer_index:
                    # A fused store/load needs the producer's value to remain on TOS
                    # for the immediate load semantics.  Plain STORE_FAST is net-zero,
                    # so its pure producer can disappear with the store.
                    if fused:
                        rebuilt.append(item)
                    continue
                if index == producer_index + 1:
                    continue
                rebuilt.append(item)
            result = rebuilt
            if source_kind == "const":
                const_count += 1
            else:
                copy_count += 1
            changed = True
            break
    return result, copy_count, const_count


def _elide_synthetic_stack_roundtrips(
    items: list[Any], synthetic_names: set[str]
) -> tuple[list[Any], int]:
    """Keep transient inline values on the operand stack when provably safe.

    CPython 3.13 commonly fuses ``STORE_FAST x; LOAD_FAST x`` into
    ``STORE_FAST_LOAD_FAST(x, x)``.  For compiler-generated inline locals whose
    complete lifetime is exactly that store/reload, the instruction only writes a
    synthetic local and reconstructs the value already on TOS.  Removing it preserves
    the operand stack and deletes the local slot entirely.

    A second common form stores/reloads once and immediately loads the same value a
    second time (for ``x*x``).  ``COPY 1`` duplicates the still-live TOS without ever
    materializing the synthetic local.  The proof is deliberately based on whole-body
    reference counts, so a temporary with any later use is never rewritten.
    """

    result = list(items)
    eliminated = 0
    changed = True
    while changed:
        changed = False
        counts = _fast_reference_counts(result)
        index = 0
        while index < len(result):
            item = result[index]

            # CPython 3.13 fused identity: TOS -> local -> TOS.
            if (
                isinstance(item, Instr)
                and item.name == "STORE_FAST_LOAD_FAST"
                and isinstance(item.arg, tuple)
                and len(item.arg) == 2
                and item.arg[0] == item.arg[1]
                and item.arg[0] in synthetic_names
            ):
                name = item.arg[0]
                if counts.get(name) == 2:
                    del result[index]
                    eliminated += 1
                    changed = True
                    break
                if (
                    counts.get(name) == 3
                    and index + 1 < len(result)
                    and isinstance(result[index + 1], Instr)
                    and result[index + 1].name == "LOAD_FAST"
                    and result[index + 1].arg == name
                ):
                    location = result[index + 1].location
                    result[index : index + 2] = [Instr("COPY", 1, location=location)]
                    eliminated += 1
                    changed = True
                    break

            # Unfused equivalent retained by some control-flow shapes.
            if (
                isinstance(item, Instr)
                and item.name == "STORE_FAST"
                and isinstance(item.arg, str)
                and item.arg in synthetic_names
                and index + 1 < len(result)
            ):
                name = item.arg
                nxt = result[index + 1]
                if (
                    counts.get(name) == 2
                    and isinstance(nxt, Instr)
                    and nxt.name == "LOAD_FAST"
                    and nxt.arg == name
                ):
                    del result[index : index + 2]
                    eliminated += 1
                    changed = True
                    break
                if (
                    counts.get(name) == 3
                    and isinstance(nxt, Instr)
                    and nxt.name == "LOAD_FAST_LOAD_FAST"
                    and nxt.arg == (name, name)
                ):
                    result[index : index + 2] = [Instr("COPY", 1, location=nxt.location)]
                    eliminated += 1
                    changed = True
                    break
            index += 1
    return result, eliminated


_STACK_RESIDENT_FINAL_CONSUMERS = {
    "BINARY_OP",
}


@dataclass(frozen=True)
class _StackResidentCandidate:
    """One synthetic local considered for stack residency.

    ``score`` is deliberately dominated by the number of values we can keep out of
    fast locals.  Smaller live ranges and COPY depths break conflicts between
    otherwise equivalent candidates.  The conflict solver then selects a maximum
    weight non-crossing subset; every unselected value is an explicit spill.
    """

    name: str
    start: int
    end: int
    reads: int
    max_copy_depth: int
    instruction_savings: int
    score: int


@dataclass(frozen=True)
class _StackScheduleStats:
    candidates: int = 0
    scheduled: int = 0
    spilled: int = 0
    conflicts: int = 0
    max_copy_depth: int = 0
    instruction_savings: int = 0
    dependency_edges: int = 0
    peak_resident_values: int = 0
    split_values: int = 0
    split_reads: int = 0
    split_instruction_cost: int = 0
    middle_splits: int = 0


@dataclass(frozen=True)
class _StackSplitCandidate:
    """One proven stack-resident segment of an otherwise spilled local.

    ``kind`` identifies a prefix, suffix, or middle segment.  Segment boundaries are
    anchored to verified instruction objects so later rewrites can relocate stores
    or insert a reload without relying on stale numeric offsets.  Prefix segments
    retain the value before a conflict, suffix segments reload after one, and middle
    segments temporarily leave the fast-local backing store between two conflict
    clusters.
    """

    name: str
    kind: str
    seed_index: int
    anchor: Any
    start: int
    end: int
    reads: int
    score: int
    stop_anchor: Any | None = None


def _stack_resident_candidate(
    items: list[Any], synthetic_names: set[str], name: str
) -> tuple[int, int] | None:
    """Return the STORE/final-read interval for one safe stack-resident local.

    A candidate has one definite write, at least two ordinary reads, no protected or
    branching control flow in its live range, and a final read consumed by a binary
    operator.  Those restrictions let stack residency preserve exact Python operand
    ordering without speculative recovery paths.
    """

    accesses: list[tuple[int, str]] = []
    for index, item in enumerate(items):
        for accessed, kind in _fast_accesses(item):
            if accessed == name:
                accesses.append((index, kind))
    writes = [index for index, kind in accesses if kind == "write"]
    reads = [index for index, kind in accesses if kind == "read"]
    if len(writes) != 1 or len(reads) < 2:
        return None
    if any(kind not in {"write", "read"} for _, kind in accesses):
        return None

    store_index = writes[0]
    store = items[store_index]
    store_is_plain = (
        isinstance(store, Instr)
        and store.name == "STORE_FAST"
        and store.arg == name
    )
    store_is_fused_load = (
        isinstance(store, Instr)
        and store.name == "STORE_FAST_LOAD_FAST"
        and isinstance(store.arg, tuple)
        and len(store.arg) == 2
        and store.arg[0] == name
        and store.arg[1] != name
    )
    if not (store_is_plain or store_is_fused_load):
        return None
    if min(reads) <= store_index:
        return None

    final_read = max(reads)
    final_item = items[final_read]
    if not (
        isinstance(final_item, Instr)
        and final_item.name == "LOAD_FAST"
        and final_item.arg == name
    ):
        return None
    if final_read + 1 >= len(items):
        return None
    consumer = items[final_read + 1]
    if not (
        isinstance(consumer, Instr)
        and consumer.name in _STACK_RESIDENT_FINAL_CONSUMERS
    ):
        return None
    try:
        if consumer.stack_effect() != -1:
            return None
    except Exception:
        return None

    span = items[store_index + 1 : final_read + 2]
    if any(isinstance(item, (Label, TryBegin, TryEnd)) for item in span):
        return None
    if any(
        isinstance(item, Instr) and _is_control_flow_instruction(item)
        for item in span
    ):
        return None
    return store_index, final_read


def _stack_intervals_cross(
    left: tuple[int, int], right: tuple[int, int]
) -> bool:
    """Whether two live intervals overlap without being properly nested."""

    ls, le = left
    rs, re = right
    return (ls < rs <= le < re) or (rs < ls <= re < le)


def _schedule_one_stack_resident_value_rotate(
    items: list[Any], name: str, *, max_copy_depth: int = 0xFF
) -> tuple[list[Any], bool, int, int]:
    """Schedule one selected local using the *current* bytecode shape.

    Returns ``(items, changed, deepest_copy, instruction_savings)``.  Nested values
    are lowered innermost-first, so an outer pass observes the real COPY/SWAP traffic
    introduced by inner values and derives its depths from the actual operand stack.
    """

    result = list(items)
    accesses: list[tuple[int, str]] = []
    for index, item in enumerate(result):
        for accessed, kind in _fast_accesses(item):
            if accessed == name:
                accesses.append((index, kind))
    writes = [index for index, kind in accesses if kind == "write"]
    reads = [index for index, kind in accesses if kind == "read"]
    if len(writes) != 1 or len(reads) < 2:
        return result, False, 0, 0
    store_index = writes[0]
    store = result[store_index]
    store_is_plain = (
        isinstance(store, Instr)
        and store.name == "STORE_FAST"
        and store.arg == name
    )
    store_is_fused_load = (
        isinstance(store, Instr)
        and store.name == "STORE_FAST_LOAD_FAST"
        and isinstance(store.arg, tuple)
        and len(store.arg) == 2
        and store.arg[0] == name
        and store.arg[1] != name
    )
    if not (store_is_plain or store_is_fused_load):
        return result, False, 0, 0
    if any(kind not in {"write", "read"} for _, kind in accesses):
        return result, False, 0, 0
    if min(reads) <= store_index:
        return result, False, 0, 0

    final_read = max(reads)
    final_read_item = result[final_read]
    if not (
        isinstance(final_read_item, Instr)
        and final_read_item.name == "LOAD_FAST"
        and final_read_item.arg == name
    ):
        return result, False, 0, 0
    if final_read + 1 >= len(result):
        return result, False, 0, 0
    final_consumer = result[final_read + 1]
    if not (
        isinstance(final_consumer, Instr)
        and final_consumer.name in _STACK_RESIDENT_FINAL_CONSUMERS
    ):
        return result, False, 0, 0

    span = result[store_index + 1 : final_read + 2]
    if any(isinstance(item, (Label, TryBegin, TryEnd)) for item in span):
        return result, False, 0, 0
    if any(
        isinstance(item, Instr) and _is_control_flow_instruction(item)
        for item in span
    ):
        return result, False, 0, 0

    before_instructions = _instruction_count(result)
    above = 1 if store_is_fused_load else 0
    replacements: dict[int, Instr | list[Instr]] = {}
    valid = True
    deepest_copy = 0
    read_indexes = set(reads)
    for index in range(store_index + 1, final_read + 1):
        item = result[index]
        if index in read_indexes:
            if index == final_read:
                # Rotate the retained value from beneath ``above`` live operands to
                # TOS while preserving their relative order.  For [R, A, B],
                # SWAP 2; SWAP 3 yields [A, B, R].  The following binary consumer
                # then observes exactly the same operand order as LOAD_FAST R.
                if above < 1 or above + 1 > max_copy_depth:
                    valid = False
                    break
                deepest_copy = max(deepest_copy, above + 1)
                replacements[index] = [
                    Instr("SWAP", depth, location=item.location)
                    for depth in range(2, above + 2)
                ]
                continue

            if not isinstance(item, Instr):
                valid = False
                break
            if item.name == "LOAD_FAST" and item.arg == name:
                depth = above + 1
                if depth > max_copy_depth:
                    valid = False
                    break
                deepest_copy = max(deepest_copy, depth)
                replacements[index] = Instr("COPY", depth, location=item.location)
                above += 1
                continue
            if (
                item.name in {
                    "LOAD_FAST_LOAD_FAST",
                    "LOAD_FAST_BORROW_LOAD_FAST_BORROW",
                }
                and isinstance(item.arg, tuple)
                and name in item.arg
            ):
                expanded: list[Instr] = []
                for operand in item.arg:
                    if operand == name:
                        depth = above + 1
                        if depth > max_copy_depth:
                            valid = False
                            break
                        deepest_copy = max(deepest_copy, depth)
                        expanded.append(Instr("COPY", depth, location=item.location))
                    else:
                        expanded.append(
                            Instr("LOAD_FAST", operand, location=item.location)
                        )
                    above += 1
                if not valid:
                    break
                replacements[index] = expanded
                continue
            valid = False
            break

        if not isinstance(item, Instr):
            valid = False
            break
        try:
            above += item.stack_effect()
        except Exception:
            valid = False
            break
        if above < 0:
            valid = False
            break

    if not valid:
        return result, False, 0, 0
    try:
        if final_consumer.stack_effect() != -1:
            return result, False, 0, 0
    except Exception:
        return result, False, 0, 0

    rebuilt: list[Any] = []
    for index, item in enumerate(result):
        if index == store_index:
            if store_is_fused_load:
                rebuilt.append(
                    Instr("LOAD_FAST", store.arg[1], location=store.location)
                )
            continue
        replacement = replacements.get(index)
        if isinstance(replacement, list):
            rebuilt.extend(replacement)
        else:
            rebuilt.append(replacement if replacement is not None else item)
    return (
        rebuilt,
        True,
        deepest_copy,
        before_instructions - _instruction_count(rebuilt),
    )



def _schedule_one_stack_resident_value_deferred(
    items: list[Any], name: str, *, max_copy_depth: int = 0xFF
) -> tuple[list[Any], bool, int, int]:
    """Keep a value resident through its final use, then pop it at zero stack.

    Deep final uses would otherwise require a SWAP rotation proportional to the
    number of live operands above the retained value.  This alternative serves the
    final read with COPY like every earlier read, lets the surrounding expression
    finish, and removes the retained original at the first exact original zero-stack
    boundary.  It therefore has a fixed one-instruction cleanup cost independent of
    final expression depth.
    """

    result = list(items)
    accesses: list[tuple[int, str]] = []
    for index, item in enumerate(result):
        for accessed, kind in _fast_accesses(item):
            if accessed == name:
                accesses.append((index, kind))
    writes = [index for index, kind in accesses if kind == "write"]
    reads = [index for index, kind in accesses if kind == "read"]
    if len(writes) != 1 or len(reads) < 2:
        return result, False, 0, 0
    if any(kind not in {"write", "read"} for _, kind in accesses):
        return result, False, 0, 0

    store_index = writes[0]
    store = result[store_index]
    store_is_plain = (
        isinstance(store, Instr) and store.name == "STORE_FAST" and store.arg == name
    )
    store_is_fused_load = (
        isinstance(store, Instr)
        and store.name == "STORE_FAST_LOAD_FAST"
        and isinstance(store.arg, tuple)
        and len(store.arg) == 2
        and store.arg[0] == name
        and store.arg[1] != name
    )
    if not (store_is_plain or store_is_fused_load):
        return result, False, 0, 0
    if min(reads) <= store_index:
        return result, False, 0, 0

    final_read = max(reads)
    span = result[store_index + 1 : final_read + 2]
    if any(isinstance(item, (Label, TryBegin, TryEnd)) for item in span):
        return result, False, 0, 0
    if any(
        isinstance(item, Instr) and _is_control_flow_instruction(item) for item in span
    ):
        return result, False, 0, 0

    before_instructions = _instruction_count(result)
    above = 1 if store_is_fused_load else 0
    deepest_copy = 0
    read_indexes = set(reads)
    replacements: dict[int, Instr | list[Instr]] = {}
    valid = True
    cleanup_after: int | None = None
    cleanup_before_return: int | None = None
    cleanup_at_end = False

    # Continue beyond the final read until the original expression stack returns to
    # zero.  At that point the retained value is the only transformation-added item.
    for index in range(store_index + 1, len(result)):
        item = result[index]
        if isinstance(item, (Label, TryBegin, TryEnd)):
            valid = False
            break
        if not isinstance(item, Instr):
            valid = False
            break
        if _is_control_flow_instruction(item):
            # A terminal RETURN_VALUE is a useful cleanup boundary: with one
            # original result above the retained value, SWAP 2; POP_TOP removes
            # the retained object while leaving the return value on TOS.
            if index > final_read and item.name == "RETURN_VALUE" and above == 1:
                cleanup_before_return = index
                break
            valid = False
            break

        if index in read_indexes:
            if item.name == "LOAD_FAST" and item.arg == name:
                depth = above + 1
                if depth > max_copy_depth:
                    valid = False
                    break
                deepest_copy = max(deepest_copy, depth)
                replacements[index] = Instr("COPY", depth, location=item.location)
                above += 1
            elif (
                item.name
                in {"LOAD_FAST_LOAD_FAST", "LOAD_FAST_BORROW_LOAD_FAST_BORROW"}
                and isinstance(item.arg, tuple)
                and name in item.arg
            ):
                expanded: list[Instr] = []
                for operand in item.arg:
                    if operand == name:
                        depth = above + 1
                        if depth > max_copy_depth:
                            valid = False
                            break
                        deepest_copy = max(deepest_copy, depth)
                        expanded.append(Instr("COPY", depth, location=item.location))
                    else:
                        expanded.append(Instr("LOAD_FAST", operand, location=item.location))
                    above += 1
                if not valid:
                    break
                replacements[index] = expanded
            else:
                valid = False
                break
        else:
            try:
                above += item.stack_effect()
            except Exception:
                valid = False
                break
            if above < 0:
                valid = False
                break

        if index >= final_read and above == 0:
            cleanup_after = index
            break

    # A cloned terminal-return callee intentionally has no RETURN_VALUE in its body;
    # its result is left on TOS for the caller.  If that result is the sole original
    # value above the retained object, clean up at body end with SWAP 2; POP_TOP.
    if valid and cleanup_after is None and cleanup_before_return is None and above == 1:
        cleanup_at_end = True

    if not valid or (
        cleanup_after is None and cleanup_before_return is None and not cleanup_at_end
    ):
        return result, False, 0, 0

    rebuilt: list[Any] = []
    for index, item in enumerate(result):
        if index == store_index:
            if store_is_fused_load:
                rebuilt.append(Instr("LOAD_FAST", store.arg[1], location=store.location))
            continue
        if index == cleanup_before_return:
            location = getattr(item, "location", None)
            rebuilt.append(Instr("SWAP", 2, location=location))
            rebuilt.append(Instr("POP_TOP", location=location))
        replacement = replacements.get(index)
        if isinstance(replacement, list):
            rebuilt.extend(replacement)
        else:
            rebuilt.append(replacement if replacement is not None else item)
        if index == cleanup_after:
            rebuilt.append(Instr("POP_TOP", location=getattr(item, "location", None)))
    if cleanup_at_end:
        location = getattr(result[-1], "location", None) if result else None
        rebuilt.append(Instr("SWAP", 2, location=location))
        rebuilt.append(Instr("POP_TOP", location=location))

    return (
        rebuilt,
        True,
        deepest_copy,
        before_instructions - _instruction_count(rebuilt),
    )


def _schedule_one_stack_resident_value(
    items: list[Any], name: str, *, max_copy_depth: int = 0xFF
) -> tuple[list[Any], bool, int, int]:
    """Choose the cheaper proven whole-lifetime stack schedule for ``name``."""

    rotated = _schedule_one_stack_resident_value_rotate(
        items, name, max_copy_depth=max_copy_depth
    )
    deferred = _schedule_one_stack_resident_value_deferred(
        items, name, max_copy_depth=max_copy_depth
    )
    options = [option for option in (rotated, deferred) if option[1]]
    if not options:
        return list(items), False, 0, 0
    # Prefer fewer emitted instructions, then shallower stack addressing.  The
    # rotation form naturally wins shallow final uses; deferred cleanup wins deep
    # expressions where a long SWAP chain would otherwise be required.
    return max(options, key=lambda option: (option[3], -option[2]))



def _linear_stack_depths(items: list[Any], end: int) -> list[int] | None:
    """Return exact before-instruction depths for a straight-line prefix.

    Split seeding is deliberately limited to a region reachable from function entry
    without labels, exception markers, or control-flow instructions.  Starting from
    the function-frame depth of zero then gives an exact stack depth, not merely a
    relative estimate.
    """

    depth = 0
    depths: list[int] = []
    for index, item in enumerate(items):
        depths.append(depth)
        if index > end:
            break
        if isinstance(item, (Label, TryBegin, TryEnd)):
            return None
        if not isinstance(item, Instr) or _is_control_flow_instruction(item):
            return None
        try:
            depth += item.stack_effect()
        except Exception:
            return None
        if depth < 0:
            return None
    return depths


def _schedule_one_stack_resident_suffix(
    items: list[Any],
    name: str,
    anchor: Any,
    *,
    max_copy_depth: int = 0xFF,
) -> tuple[list[Any], bool, int, int, int]:
    """Reload a spilled value once and keep the remaining suffix resident.

    ``anchor`` marks an original zero-stack instruction boundary after all crossing
    resident lifetimes have died.  A LOAD_FAST inserted immediately before it seeds
    the retained value *under* all subsequent expression operands.  Later reads are
    COPYs and the final binary operand consumes the retained original with SWAP.
    """

    result = list(items)
    try:
        seed_index = next(index for index, item in enumerate(result) if item is anchor)
    except StopIteration:
        return result, False, 0, 0, 0

    accesses: list[tuple[int, str]] = []
    for index, item in enumerate(result):
        for accessed, kind in _fast_accesses(item):
            if accessed == name:
                accesses.append((index, kind))
    writes = [index for index, kind in accesses if kind == "write"]
    reads = [index for index, kind in accesses if kind == "read"]
    if len(writes) != 1 or len(reads) < 2:
        return result, False, 0, 0, 0
    if any(kind not in {"write", "read"} for _, kind in accesses):
        return result, False, 0, 0, 0
    suffix_reads = [index for index in reads if index >= seed_index]
    if len(suffix_reads) < 3:
        return result, False, 0, 0, 0
    if writes[0] >= seed_index:
        return result, False, 0, 0, 0

    final_read = reads[-1]
    if suffix_reads[-1] != final_read:
        return result, False, 0, 0, 0
    final_item = result[final_read]
    if not (
        isinstance(final_item, Instr)
        and final_item.name == "LOAD_FAST"
        and final_item.arg == name
    ):
        return result, False, 0, 0, 0
    if final_read + 1 >= len(result):
        return result, False, 0, 0, 0
    final_consumer = result[final_read + 1]
    if not (
        isinstance(final_consumer, Instr)
        and final_consumer.name in _STACK_RESIDENT_FINAL_CONSUMERS
    ):
        return result, False, 0, 0, 0
    try:
        if final_consumer.stack_effect() != -1:
            return result, False, 0, 0, 0
    except Exception:
        return result, False, 0, 0, 0

    span = result[seed_index : final_read + 2]
    if any(isinstance(item, (Label, TryBegin, TryEnd)) for item in span):
        return result, False, 0, 0, 0
    if any(
        isinstance(item, Instr) and _is_control_flow_instruction(item)
        for item in span
    ):
        return result, False, 0, 0, 0

    before_instructions = _instruction_count(result)
    replacements: dict[int, Instr | list[Instr]] = {}
    # The inserted retained value is initially the only extra stack item and all
    # original expression operands are above it.
    above = 0
    deepest_copy = 0
    read_indexes = set(suffix_reads)
    valid = True
    for index in range(seed_index, final_read + 1):
        item = result[index]
        if index in read_indexes:
            if index == final_read:
                # Rotate the retained value from beneath ``above`` live operands to
                # TOS while preserving their relative order.  For [R, A, B],
                # SWAP 2; SWAP 3 yields [A, B, R].  The following binary consumer
                # then observes exactly the same operand order as LOAD_FAST R.
                if above < 1 or above + 1 > max_copy_depth:
                    valid = False
                    break
                deepest_copy = max(deepest_copy, above + 1)
                replacements[index] = [
                    Instr("SWAP", depth, location=item.location)
                    for depth in range(2, above + 2)
                ]
                continue
            if not isinstance(item, Instr):
                valid = False
                break
            if item.name == "LOAD_FAST" and item.arg == name:
                depth = above + 1
                if depth > max_copy_depth:
                    valid = False
                    break
                deepest_copy = max(deepest_copy, depth)
                replacements[index] = Instr("COPY", depth, location=item.location)
                above += 1
                continue
            if (
                item.name in {
                    "LOAD_FAST_LOAD_FAST",
                    "LOAD_FAST_BORROW_LOAD_FAST_BORROW",
                }
                and isinstance(item.arg, tuple)
                and name in item.arg
            ):
                expanded: list[Instr] = []
                for operand in item.arg:
                    if operand == name:
                        depth = above + 1
                        if depth > max_copy_depth:
                            valid = False
                            break
                        deepest_copy = max(deepest_copy, depth)
                        expanded.append(Instr("COPY", depth, location=item.location))
                    else:
                        expanded.append(Instr("LOAD_FAST", operand, location=item.location))
                    above += 1
                if not valid:
                    break
                replacements[index] = expanded
                continue
            valid = False
            break

        if not isinstance(item, Instr):
            valid = False
            break
        try:
            above += item.stack_effect()
        except Exception:
            valid = False
            break
        if above < 0:
            valid = False
            break

    if not valid:
        return result, False, 0, 0, 0

    seed_location = getattr(anchor, "location", None)
    rebuilt: list[Any] = []
    for index, item in enumerate(result):
        if index == seed_index:
            rebuilt.append(Instr("LOAD_FAST", name, location=seed_location))
        replacement = replacements.get(index)
        if isinstance(replacement, list):
            rebuilt.extend(replacement)
        else:
            rebuilt.append(replacement if replacement is not None else item)
    suffix_read_count = len(suffix_reads)
    return (
        rebuilt,
        True,
        deepest_copy,
        before_instructions - _instruction_count(rebuilt),
        suffix_read_count,
    )


def _stack_split_candidate_plan(
    items: list[Any],
    candidate: _StackResidentCandidate,
    selected: list[_StackResidentCandidate],
) -> _StackSplitCandidate | None:
    """Plan a zero-stack suffix reload after crossing residents have died."""

    crossing = [
        other
        for other in selected
        if _stack_intervals_cross(
            (candidate.start, candidate.end), (other.start, other.end)
        )
    ]
    if not crossing or any(other.end >= candidate.end for other in crossing):
        return None
    cutoff = max(other.end for other in crossing)

    depths = _linear_stack_depths(items, candidate.end)
    if depths is None or len(depths) <= candidate.end:
        return None
    accesses = [
        (index, kind)
        for index, item in enumerate(items)
        for accessed, kind in _fast_accesses(item)
        if accessed == candidate.name
    ]
    reads = [index for index, kind in accesses if kind == "read"]
    writes = [index for index, kind in accesses if kind == "write"]
    if len(writes) != 1 or len(reads) < 3:
        return None

    # Seed only at an exact empty operand-stack boundary.  This guarantees the
    # reloaded local sits below every subsequent expression operand rather than
    # accidentally interposing itself inside an already-active expression.
    seed_index = None
    for index in range(max(cutoff + 1, writes[0] + 1), candidate.end):
        if depths[index] != 0:
            continue
        future_reads = [read for read in reads if read >= index]
        if len(future_reads) < 3:
            continue
        item = items[index]
        if isinstance(item, (Label, TryBegin, TryEnd)):
            continue
        if not isinstance(item, Instr) or _is_control_flow_instruction(item):
            continue
        seed_index = index
        break
    if seed_index is None:
        return None

    future_reads = [read for read in reads if read >= seed_index]
    score = 100_000 + len(future_reads) * 2_000 - (candidate.end - seed_index)
    return _StackSplitCandidate(
        name=candidate.name,
        kind="suffix",
        seed_index=seed_index,
        anchor=items[seed_index],
        start=seed_index,
        end=candidate.end,
        reads=len(future_reads),
        score=score,
    )


def _schedule_one_stack_resident_prefix(
    items: list[Any],
    name: str,
    anchor: Any,
    *,
    max_copy_depth: int = 0xFF,
) -> tuple[list[Any], bool, int, int, int]:
    """Keep a spilled value resident only until ``anchor``, then store it.

    This is the zero-extra-seed counterpart to suffix splitting: the original store
    is removed, early reads are served with COPY, and the same STORE_FAST is emitted
    later at an exact zero-expression-stack boundary before the conflicting resident
    lifetime begins.
    """

    result = list(items)
    try:
        seed_index = next(index for index, item in enumerate(result) if item is anchor)
    except StopIteration:
        return result, False, 0, 0, 0

    accesses: list[tuple[int, str]] = []
    for index, item in enumerate(result):
        for accessed, kind in _fast_accesses(item):
            if accessed == name:
                accesses.append((index, kind))
    writes = [index for index, kind in accesses if kind == "write"]
    reads = [index for index, kind in accesses if kind == "read"]
    if len(writes) != 1 or len(reads) < 2:
        return result, False, 0, 0, 0
    if any(kind not in {"write", "read"} for _, kind in accesses):
        return result, False, 0, 0, 0
    store_index = writes[0]
    if not store_index < seed_index:
        return result, False, 0, 0, 0
    prefix_reads = [index for index in reads if store_index < index < seed_index]
    if len(prefix_reads) < 2:
        return result, False, 0, 0, 0

    store = result[store_index]
    store_is_plain = (
        isinstance(store, Instr)
        and store.name == "STORE_FAST"
        and store.arg == name
    )
    store_is_fused_load = (
        isinstance(store, Instr)
        and store.name == "STORE_FAST_LOAD_FAST"
        and isinstance(store.arg, tuple)
        and len(store.arg) == 2
        and store.arg[0] == name
        and store.arg[1] != name
    )
    if not (store_is_plain or store_is_fused_load):
        return result, False, 0, 0, 0

    span = result[store_index + 1 : seed_index]
    if any(isinstance(item, (Label, TryBegin, TryEnd)) for item in span):
        return result, False, 0, 0, 0
    if any(
        isinstance(item, Instr) and _is_control_flow_instruction(item)
        for item in span
    ):
        return result, False, 0, 0, 0

    before_instructions = _instruction_count(result)
    above = 1 if store_is_fused_load else 0
    deepest_copy = 0
    read_indexes = set(prefix_reads)
    replacements: dict[int, Instr | list[Instr]] = {}
    valid = True
    for index in range(store_index + 1, seed_index):
        item = result[index]
        if index in read_indexes:
            if not isinstance(item, Instr):
                valid = False
                break
            if item.name == "LOAD_FAST" and item.arg == name:
                depth = above + 1
                if depth > max_copy_depth:
                    valid = False
                    break
                deepest_copy = max(deepest_copy, depth)
                replacements[index] = Instr("COPY", depth, location=item.location)
                above += 1
                continue
            if (
                item.name in {
                    "LOAD_FAST_LOAD_FAST",
                    "LOAD_FAST_BORROW_LOAD_FAST_BORROW",
                }
                and isinstance(item.arg, tuple)
                and name in item.arg
            ):
                expanded: list[Instr] = []
                for operand in item.arg:
                    if operand == name:
                        depth = above + 1
                        if depth > max_copy_depth:
                            valid = False
                            break
                        deepest_copy = max(deepest_copy, depth)
                        expanded.append(Instr("COPY", depth, location=item.location))
                    else:
                        expanded.append(Instr("LOAD_FAST", operand, location=item.location))
                    above += 1
                if not valid:
                    break
                replacements[index] = expanded
                continue
            valid = False
            break
        if not isinstance(item, Instr):
            valid = False
            break
        try:
            above += item.stack_effect()
        except Exception:
            valid = False
            break
        if above < 0:
            valid = False
            break

    # At the chosen original zero-stack boundary, the retained value must be the
    # only transformation-added item.  STORE_FAST then restores the exact original
    # stack shape before normal local-backed execution resumes.
    if not valid or above != 0:
        return result, False, 0, 0, 0

    seed_location = getattr(anchor, "location", None)
    rebuilt: list[Any] = []
    for index, item in enumerate(result):
        if index == store_index:
            if store_is_fused_load:
                rebuilt.append(Instr("LOAD_FAST", store.arg[1], location=store.location))
            continue
        if index == seed_index:
            rebuilt.append(Instr("STORE_FAST", name, location=seed_location))
        replacement = replacements.get(index)
        if isinstance(replacement, list):
            rebuilt.extend(replacement)
        else:
            rebuilt.append(replacement if replacement is not None else item)
    return (
        rebuilt,
        True,
        deepest_copy,
        before_instructions - _instruction_count(rebuilt),
        len(prefix_reads),
    )


def _stack_prefix_split_candidate_plan(
    items: list[Any],
    candidate: _StackResidentCandidate,
    selected: list[_StackResidentCandidate],
) -> _StackSplitCandidate | None:
    """Plan a resident prefix before a younger crossing resident begins."""

    crossing = [
        other
        for other in selected
        if _stack_intervals_cross(
            (candidate.start, candidate.end), (other.start, other.end)
        )
    ]
    if not crossing:
        return None
    # Prefix splitting applies only when this spilled lifetime starts first and dies
    # first.  The store is moved to the last safe zero-stack boundary before the
    # earliest younger resident's definition becomes live.
    if any(
        not (candidate.start < other.start <= candidate.end < other.end)
        for other in crossing
    ):
        return None
    cutoff = min(other.start for other in crossing)
    depths = _linear_stack_depths(items, cutoff)
    if depths is None or len(depths) <= cutoff:
        return None
    accesses = [
        (index, kind)
        for index, item in enumerate(items)
        for accessed, kind in _fast_accesses(item)
        if accessed == candidate.name
    ]
    reads = [index for index, kind in accesses if kind == "read"]
    writes = [index for index, kind in accesses if kind == "write"]
    if len(writes) != 1 or len(reads) < 2:
        return None
    store_index = writes[0]

    seed_index = None
    for index in range(cutoff - 1, store_index, -1):
        if depths[index] != 0:
            continue
        prefix_reads = [read for read in reads if store_index < read < index]
        if len(prefix_reads) < 2:
            continue
        item = items[index]
        if isinstance(item, (Label, TryBegin, TryEnd)):
            continue
        if not isinstance(item, Instr) or _is_control_flow_instruction(item):
            continue
        seed_index = index
        break
    if seed_index is None:
        return None
    prefix_reads = [read for read in reads if store_index < read < seed_index]
    score = 100_000 + len(prefix_reads) * 2_000 - (seed_index - candidate.start)
    return _StackSplitCandidate(
        name=candidate.name,
        kind="prefix",
        seed_index=seed_index,
        anchor=items[seed_index],
        start=candidate.start,
        end=seed_index,
        reads=len(prefix_reads),
        score=score,
    )



def _stack_middle_split_candidate_plan(
    items: list[Any],
    candidate: _StackResidentCandidate,
    selected: list[_StackResidentCandidate],
) -> _StackSplitCandidate | None:
    """Plan one resident middle segment between older and younger conflicts.

    For a non-crossing selected resident set, all crossing intervals on the left
    form one nested cluster and all crossing intervals on the right form another.
    Therefore the only split form missing from prefix/suffix scheduling is:

        local-backed -> stack-resident -> local-backed

    The segment is seeded and spilled only at exact zero-expression-stack
    boundaries, so it never interposes the retained value inside a live Python
    expression.
    """

    older = [
        other
        for other in selected
        if other.start < candidate.start <= other.end < candidate.end
    ]
    younger = [
        other
        for other in selected
        if candidate.start < other.start <= candidate.end < other.end
    ]
    if not older or not younger:
        return None

    left_cutoff = max(other.end for other in older)
    right_cutoff = min(other.start for other in younger)
    if left_cutoff + 1 >= right_cutoff:
        return None

    depths = _linear_stack_depths(items, right_cutoff)
    if depths is None or len(depths) <= right_cutoff:
        return None

    accesses = [
        (index, kind)
        for index, item in enumerate(items)
        for accessed, kind in _fast_accesses(item)
        if accessed == candidate.name
    ]
    writes = [index for index, kind in accesses if kind == "write"]
    reads = [index for index, kind in accesses if kind == "read"]
    if len(writes) != 1 or len(reads) < 2:
        return None

    # Find the earliest safe seed after the left conflict cluster.
    seed_index: int | None = None
    for index in range(max(left_cutoff + 1, writes[0] + 1), right_cutoff):
        if depths[index] != 0:
            continue
        item = items[index]
        if isinstance(item, (Label, TryBegin, TryEnd)):
            continue
        if not isinstance(item, Instr) or _is_control_flow_instruction(item):
            continue
        seed_index = index
        break
    if seed_index is None:
        return None

    # Spill at the latest safe zero-stack boundary before the right conflict.
    stop_index: int | None = None
    for index in range(right_cutoff - 1, seed_index, -1):
        if depths[index] != 0:
            continue
        middle_reads = [read for read in reads if seed_index <= read < index]
        if len(middle_reads) < 2:
            continue
        item = items[index]
        if isinstance(item, (Label, TryBegin, TryEnd)):
            continue
        if not isinstance(item, Instr) or _is_control_flow_instruction(item):
            continue
        stop_index = index
        break
    if stop_index is None:
        return None

    middle_reads = [read for read in reads if seed_index <= read < stop_index]

    # A middle segment costs one LOAD_FAST seed and one STORE_FAST spill.  Take that
    # density trade only when the resulting local-backed hole can be occupied by a
    # different synthetic lifetime.  Without such a reuse opportunity the split
    # merely adds bytecode while leaving frame size unchanged.
    selected_names = {other.name for other in selected}
    reusable_hole = False
    refs: dict[str, list[tuple[int, str]]] = {}
    for index, item in enumerate(items):
        for accessed, kind in _fast_accesses(item):
            if (
                isinstance(accessed, str)
                and accessed.startswith("__inl_")
                and accessed != candidate.name
                and accessed not in selected_names
            ):
                refs.setdefault(accessed, []).append((index, kind))
    for other_accesses in refs.values():
        if any(kind not in {"write", "read"} for _, kind in other_accesses):
            continue
        ordered = sorted(other_accesses)
        if not ordered or ordered[0][1] != "write":
            continue
        if seed_index <= ordered[0][0] and ordered[-1][0] < stop_index:
            reusable_hole = True
            break
    if not reusable_hole:
        return None

    score = (
        120_000
        + len(middle_reads) * 2_000
        + (stop_index - seed_index)
        - (right_cutoff - left_cutoff)
    )
    return _StackSplitCandidate(
        name=candidate.name,
        kind="middle",
        seed_index=seed_index,
        anchor=items[seed_index],
        start=seed_index,
        end=stop_index,
        reads=len(middle_reads),
        score=score,
        stop_anchor=items[stop_index],
    )


def _schedule_one_stack_resident_middle(
    items: list[Any],
    name: str,
    anchor: Any,
    stop_anchor: Any,
    *,
    max_copy_depth: int = 0xFF,
) -> tuple[list[Any], bool, int, int, int]:
    """Keep a local-backed value resident for one zero-stack middle segment.

    ``LOAD_FAST name`` seeds the resident copy at ``anchor``.  Reads inside the
    segment become ``COPY`` operations, and ``STORE_FAST name`` at ``stop_anchor``
    consumes the retained original and restores the exact pre-segment stack shape.
    The fast-local value may be considered dead during the middle interval by the
    segmented slot allocator.
    """

    result = list(items)
    try:
        seed_index = next(index for index, item in enumerate(result) if item is anchor)
        stop_index = next(index for index, item in enumerate(result) if item is stop_anchor)
    except StopIteration:
        return result, False, 0, 0, 0
    if seed_index >= stop_index:
        return result, False, 0, 0, 0

    accesses: list[tuple[int, str]] = []
    for index, item in enumerate(result):
        for accessed, kind in _fast_accesses(item):
            if accessed == name:
                accesses.append((index, kind))
    if any(kind not in {"write", "read"} for _, kind in accesses):
        return result, False, 0, 0, 0
    reads = [index for index, kind in accesses if kind == "read"]
    segment_reads = [index for index in reads if seed_index <= index < stop_index]
    if len(segment_reads) < 2:
        return result, False, 0, 0, 0

    span = result[seed_index:stop_index]
    if any(isinstance(item, (Label, TryBegin, TryEnd)) for item in span):
        return result, False, 0, 0, 0
    if any(
        isinstance(item, Instr) and _is_control_flow_instruction(item)
        for item in span
    ):
        return result, False, 0, 0, 0

    before_instructions = _instruction_count(result)
    replacements: dict[int, Instr | list[Instr]] = {}
    read_indexes = set(segment_reads)
    above = 0
    deepest_copy = 0
    valid = True
    for index in range(seed_index, stop_index):
        item = result[index]
        if index in read_indexes:
            if not isinstance(item, Instr):
                valid = False
                break
            if item.name == "LOAD_FAST" and item.arg == name:
                depth = above + 1
                if depth > max_copy_depth:
                    valid = False
                    break
                deepest_copy = max(deepest_copy, depth)
                replacements[index] = Instr("COPY", depth, location=item.location)
                above += 1
                continue
            if (
                item.name
                in {"LOAD_FAST_LOAD_FAST", "LOAD_FAST_BORROW_LOAD_FAST_BORROW"}
                and isinstance(item.arg, tuple)
                and name in item.arg
            ):
                expanded: list[Instr] = []
                for operand in item.arg:
                    if operand == name:
                        depth = above + 1
                        if depth > max_copy_depth:
                            valid = False
                            break
                        deepest_copy = max(deepest_copy, depth)
                        expanded.append(Instr("COPY", depth, location=item.location))
                    else:
                        expanded.append(Instr("LOAD_FAST", operand, location=item.location))
                    above += 1
                if not valid:
                    break
                replacements[index] = expanded
                continue
            valid = False
            break

        if not isinstance(item, Instr):
            valid = False
            break
        try:
            above += item.stack_effect()
        except Exception:
            valid = False
            break
        if above < 0:
            valid = False
            break

    # The original expression stack must be empty at the chosen spill boundary;
    # the retained value is the sole transformation-added item.
    if not valid or above != 0:
        return result, False, 0, 0, 0

    seed_location = getattr(anchor, "location", None)
    stop_location = getattr(stop_anchor, "location", None)
    rebuilt: list[Any] = []
    for index, item in enumerate(result):
        if index == seed_index:
            rebuilt.append(Instr("LOAD_FAST", name, location=seed_location))
        if index == stop_index:
            rebuilt.append(Instr("STORE_FAST", name, location=stop_location))
        replacement = replacements.get(index)
        if isinstance(replacement, list):
            rebuilt.extend(replacement)
        else:
            rebuilt.append(replacement if replacement is not None else item)

    return (
        rebuilt,
        True,
        deepest_copy,
        before_instructions - _instruction_count(rebuilt),
        len(segment_reads),
    )


def _select_split_candidates(
    plans: list[_StackSplitCandidate],
) -> list[_StackSplitCandidate]:
    """Choose a deterministic non-crossing subset of suffix splits."""

    selected: list[_StackSplitCandidate] = []
    for plan in sorted(plans, key=lambda value: (-value.score, value.start, value.end)):
        if any(other.name == plan.name for other in selected):
            continue
        if any(
            _stack_intervals_cross((plan.start, plan.end), (other.start, other.end))
            for other in selected
        ):
            continue
        selected.append(plan)
    selected.sort(key=lambda value: (value.end - value.start, -value.start))
    return selected


def _stack_candidate_plan(
    items: list[Any], synthetic_names: set[str], name: str
) -> _StackResidentCandidate | None:
    interval = _stack_resident_candidate(items, synthetic_names, name)
    if interval is None:
        return None
    transformed, changed, deepest_copy, instruction_savings = (
        _schedule_one_stack_resident_value(items, name)
    )
    if not changed:
        return None
    del transformed
    read_count = sum(
        kind == "read"
        for item in items
        for accessed, kind in _fast_accesses(item)
        if accessed == name
    )
    start, end = interval
    span = end - start
    # Cardinality/local elimination dominates.  Instruction deletion is the next
    # strongest signal; shorter live ranges and shallower COPYs break ties.  Keeping
    # these components integral makes selection deterministic across platforms.
    score = (
        1_000_000
        + instruction_savings * 20_000
        + min(read_count, 32) * 250
        - deepest_copy * 64
        - span
    )
    return _StackResidentCandidate(
        name=name,
        start=start,
        end=end,
        reads=read_count,
        max_copy_depth=deepest_copy,
        instruction_savings=instruction_savings,
        score=score,
    )


def _conflict_components(
    candidates: list[_StackResidentCandidate],
) -> tuple[list[list[int]], list[int]]:
    """Return crossing-conflict components and per-node conflict bitmasks."""

    count = len(candidates)
    masks = [0] * count
    for left in range(count):
        for right in range(left + 1, count):
            if _stack_intervals_cross(
                (candidates[left].start, candidates[left].end),
                (candidates[right].start, candidates[right].end),
            ):
                masks[left] |= 1 << right
                masks[right] |= 1 << left

    components: list[list[int]] = []
    unseen = set(range(count))
    while unseen:
        root = unseen.pop()
        component = [root]
        pending = [root]
        while pending:
            node = pending.pop()
            neighbors = [
                index for index in list(unseen) if masks[node] & (1 << index)
            ]
            for index in neighbors:
                unseen.remove(index)
                pending.append(index)
                component.append(index)
        components.append(component)
    return components, masks


def _select_stack_candidates(
    candidates: list[_StackResidentCandidate], *, strategy: str = "density"
) -> tuple[list[_StackResidentCandidate], int]:
    """Choose a maximum-weight non-crossing set; unchosen nodes are spills.

    Crossing intervals form the only incompatibility.  Small connected conflict
    components are solved exactly with memoized branch-and-bound.  Large components
    use a deterministic weighted-degree greedy fallback so decoration time remains
    bounded even for machine-generated callers with hundreds of temporaries.
    """

    if not candidates:
        return [], 0
    if strategy not in {"speed", "density"}:
        raise ValueError("stack strategy must be 'speed' or 'density'")
    components, masks = _conflict_components(candidates)
    conflict_edges = sum(mask.bit_count() for mask in masks) // 2
    selected_indexes: set[int] = set()

    if strategy == "speed":
        # Preserve the latency-calibrated 0.6 behavior for the default path: retain
        # the earliest compatible lifetime and spill later crossing values.  Exact
        # maximum-density selection is intentionally opt-in because fewer locals do
        # not universally mean fewer nanoseconds on CPython 3.13.
        selected: list[int] = []
        for index in sorted(
            range(len(candidates)),
            key=lambda i: (candidates[i].start, candidates[i].end),
        ):
            if any(masks[index] & (1 << other) for other in selected):
                continue
            selected.append(index)
        return [candidates[index] for index in selected], conflict_edges

    for component in components:
        if len(component) == 1:
            selected_indexes.add(component[0])
            continue

        local_index = {global_index: offset for offset, global_index in enumerate(component)}
        local_masks = [0] * len(component)
        weights = [candidates[index].score for index in component]
        for offset, global_index in enumerate(component):
            mask = 0
            for other in component:
                if masks[global_index] & (1 << other):
                    mask |= 1 << local_index[other]
            local_masks[offset] = mask

        if len(component) <= 18:
            memo: dict[int, tuple[int, int]] = {0: (0, 0)}

            def solve(mask: int) -> tuple[int, int]:
                cached = memo.get(mask)
                if cached is not None:
                    return cached
                # Pick the most constrained remaining node; this materially prunes
                # crossing-heavy components compared with taking the lowest bit.
                remaining = [i for i in range(len(component)) if mask & (1 << i)]
                vertex = max(
                    remaining,
                    key=lambda i: (local_masks[i] & mask).bit_count(),
                )
                without = mask & ~(1 << vertex)
                skip_weight, skip_choice = solve(without)
                take_mask = without & ~local_masks[vertex]
                take_weight, take_choice = solve(take_mask)
                take_weight += weights[vertex]
                take_choice |= 1 << vertex
                if take_weight > skip_weight:
                    answer = (take_weight, take_choice)
                elif take_weight < skip_weight:
                    answer = (skip_weight, skip_choice)
                else:
                    # Stable tie-break: retain more values, then lexical-low bits.
                    take_count = take_choice.bit_count()
                    skip_count = skip_choice.bit_count()
                    answer = (
                        (take_weight, take_choice)
                        if (take_count, -take_choice) >= (skip_count, -skip_choice)
                        else (skip_weight, skip_choice)
                    )
                memo[mask] = answer
                return answer

            _, choice = solve((1 << len(component)) - 1)
            for offset, global_index in enumerate(component):
                if choice & (1 << offset):
                    selected_indexes.add(global_index)
            continue

        # Bounded fallback: favor high benefit with low remaining conflict degree.
        remaining = set(component)
        while remaining:
            chosen = max(
                remaining,
                key=lambda index: (
                    candidates[index].score
                    / (1 + sum(bool(masks[index] & (1 << other)) for other in remaining)),
                    candidates[index].score,
                    -candidates[index].max_copy_depth,
                    -candidates[index].start,
                ),
            )
            selected_indexes.add(chosen)
            blocked = {
                other for other in remaining if masks[chosen] & (1 << other)
            }
            remaining.discard(chosen)
            remaining.difference_update(blocked)

    return [candidates[index] for index in sorted(selected_indexes)], conflict_edges



def _refine_speed_selection_with_prefix_splits(
    items: list[Any],
    candidates: list[_StackResidentCandidate],
    selected: list[_StackResidentCandidate],
) -> list[_StackResidentCandidate]:
    """Improve isolated two-node crossings when a zero-cost prefix split exists.

    The conservative speed allocator normally keeps the oldest crossing lifetime.
    For a two-node component only, a younger candidate with a strictly higher static
    benefit may replace it when the older value can remain resident through a useful
    prefix and spill later without adding instructions.  Larger conflict graphs keep
    the established conservative policy.
    """

    components, _masks = _conflict_components(candidates)
    chosen = {candidate.name: candidate for candidate in selected}
    by_index = {index: candidate for index, candidate in enumerate(candidates)}
    for component in components:
        if len(component) != 2:
            continue
        left, right = sorted(
            (by_index[index] for index in component),
            key=lambda value: (value.start, value.end),
        )
        if not (left.start < right.start <= left.end < right.end):
            continue
        if left.name not in chosen or right.name in chosen:
            continue
        if right.score <= left.score:
            continue
        plan = _stack_prefix_split_candidate_plan(items, left, [right])
        if plan is None:
            continue
        _trial, changed, _depth, saved, prefix_reads = (
            _schedule_one_stack_resident_prefix(items, left.name, plan.anchor)
        )
        if not changed or saved < 0 or prefix_reads != 2:
            continue
        chosen.pop(left.name, None)
        chosen[right.name] = right
    return list(chosen.values())


def _stack_nesting_metrics(
    candidates: list[_StackResidentCandidate],
) -> tuple[int, int]:
    """Return immediate dependency-edge count and peak resident stack pressure."""

    if not candidates:
        return 0, 0
    stack: list[_StackResidentCandidate] = []
    edges = 0
    peak = 0
    for candidate in sorted(candidates, key=lambda value: (value.start, -value.end)):
        while stack and candidate.start > stack[-1].end:
            stack.pop()
        if stack:
            if candidate.end <= stack[-1].end:
                edges += 1
            else:
                stack.clear()
        stack.append(candidate)
        peak = max(peak, len(stack))
    return edges, peak


def _schedule_stack_resident_synthetic_values(
    items: list[Any], synthetic_names: set[str], *, return_stats: bool = False,
    strategy: str = "density",
) -> tuple[list[Any], int | _StackScheduleStats]:
    """Select stack-resident values and spill incompatible lifetimes selectively.

    Version 0.9 completes the split-state model introduced in 0.8.  Every
    candidate is a node; crossing live ranges are conflict edges; nesting creates a
    dependency ordering.  The selected non-crossing set is lowered innermost-first.
    Rejected values may then receive a proven prefix, suffix, or middle resident
    segment, while remaining portions stay in fast locals.
    """

    original = list(items)
    if strategy == "off":
        stats = _StackScheduleStats()
        return original, stats if return_stats else stats.scheduled
    candidates = [
        candidate
        for name in sorted(synthetic_names)
        if (candidate := _stack_candidate_plan(original, synthetic_names, name))
        is not None
    ]
    if strategy == "speed":
        # Deep final rotations can trade one removed local store for several SWAPs.
        # Keep the default latency path conservative; density mode may still use
        # zero/negative-instruction-savings candidates for frame/code-footprint wins.
        candidates = [
            candidate for candidate in candidates if candidate.instruction_savings > 0
        ]
    if not candidates:
        stats = _StackScheduleStats()
        return original, stats if return_stats else stats.scheduled
    selected, conflicts = _select_stack_candidates(candidates, strategy=strategy)
    if strategy == "speed":
        selected = _refine_speed_selection_with_prefix_splits(
            original, candidates, selected
        )
    dependency_edges, peak_resident_values = _stack_nesting_metrics(selected)
    # Inner nodes are dependencies of their containing outer interval.  Lowering in
    # this topological order lets each outer pass see the exact stack pressure created
    # by already-resident descendants.
    selected.sort(key=lambda value: (value.end - value.start, -value.start))

    result = original
    scheduled = 0
    max_copy_depth = 0
    instruction_savings = 0
    successful_selected: list[_StackResidentCandidate] = []
    for candidate in selected:
        result, changed, deepest_copy, saved = _schedule_one_stack_resident_value(
            result, candidate.name
        )
        if not changed:
            continue
        scheduled += 1
        successful_selected.append(candidate)
        max_copy_depth = max(max_copy_depth, deepest_copy)
        instruction_savings += saved

    split_values = 0
    split_reads = 0
    split_instruction_cost = 0
    middle_splits = 0
    if strategy in {"density", "speed"} and successful_selected:
        resident_names = {candidate.name for candidate in successful_selected}
        split_plans: list[_StackSplitCandidate] = []
        for candidate in candidates:
            if candidate.name in resident_names:
                continue
            if strategy == "density":
                middle = _stack_middle_split_candidate_plan(
                    original, candidate, successful_selected
                )
                if middle is not None:
                    split_plans.append(middle)
                suffix = _stack_split_candidate_plan(
                    original, candidate, successful_selected
                )
                if suffix is not None:
                    split_plans.append(suffix)
            prefix = _stack_prefix_split_candidate_plan(
                original, candidate, successful_selected
            )
            if prefix is not None:
                # The speed path accepts only zero-cost prefix moves.  Density mode
                # may also take a fused-store prefix if it improves lifetime packing.
                if strategy == "speed":
                    _trial, changed, _depth, saved, _reads = (
                        _schedule_one_stack_resident_prefix(
                            original, candidate.name, prefix.anchor
                        )
                    )
                    if not changed or saved < 0 or prefix.reads != 2:
                        prefix = None
                if prefix is not None:
                    split_plans.append(prefix)
        for plan in _select_split_candidates(split_plans):
            if plan.kind == "middle":
                if plan.stop_anchor is None:
                    continue
                result, changed, deepest_copy, saved, resident_reads = (
                    _schedule_one_stack_resident_middle(
                        result, plan.name, plan.anchor, plan.stop_anchor
                    )
                )
            else:
                scheduler = (
                    _schedule_one_stack_resident_prefix
                    if plan.kind == "prefix"
                    else _schedule_one_stack_resident_suffix
                )
                result, changed, deepest_copy, saved, resident_reads = scheduler(
                    result, plan.name, plan.anchor
                )
            if not changed:
                continue
            split_values += 1
            if plan.kind == "middle":
                middle_splits += 1
            split_reads += resident_reads
            max_copy_depth = max(max_copy_depth, deepest_copy)
            if saved >= 0:
                instruction_savings += saved
            else:
                split_instruction_cost += -saved

    stats = _StackScheduleStats(
        candidates=len(candidates),
        scheduled=scheduled,
        spilled=len(candidates) - scheduled,
        conflicts=conflicts,
        max_copy_depth=max_copy_depth,
        instruction_savings=instruction_savings,
        dependency_edges=dependency_edges,
        peak_resident_values=peak_resident_values,
        split_values=split_values,
        split_reads=split_reads,
        split_instruction_cost=split_instruction_cost,
        middle_splits=middle_splits,
    )
    return result, stats if return_stats else stats.scheduled


def _fast_name_references(items: list[Any], name: str) -> int:
    count = 0
    for item in items:
        if not isinstance(item, Instr) or "FAST" not in item.name:
            continue
        values = item.arg if isinstance(item.arg, tuple) else (item.arg,)
        count += sum(value == name for value in values)
    return count


def _pure_fast_load_names(item: Any) -> tuple[str, ...] | None:
    if not isinstance(item, Instr):
        return None
    if item.name in {"LOAD_FAST", "LOAD_FAST_BORROW", "LOAD_FAST_CHECK"}:
        return (item.arg,) if isinstance(item.arg, str) else None
    if item.name in {"LOAD_FAST_LOAD_FAST", "LOAD_FAST_BORROW_LOAD_FAST_BORROW"}:
        if isinstance(item.arg, tuple) and all(isinstance(name, str) for name in item.arg):
            return item.arg
    return None


def _late_stack_forward(
    setup: list[Any], body: list[Any]
) -> tuple[list[Any], list[Any], int]:
    """Eliminate synthetic argument stores immediately reconstructed by loads.

    Call arguments already sit on the operand stack in source evaluation order.
    Normal binding pops them into synthetic fast locals in reverse order.  After
    constant/branch folding, a callee may begin by loading those same parameters
    once in declaration order.  In that case the STORE/LOAD round trip is exactly
    identity and can disappear.
    """

    if not setup or not body:
        return setup, body, 0

    # Collect the maximal trailing synthetic STORE_FAST run.
    store_start = len(setup)
    stored: list[str] = []
    while store_start > 0:
        item = setup[store_start - 1]
        if not isinstance(item, Instr) or item.name != "STORE_FAST" or not isinstance(item.arg, str):
            break
        store_start -= 1
        stored.append(item.arg)
    if not stored:
        return setup, body, 0

    # ``stored`` is observed walking backwards through setup, which restores the
    # original argument-stack order (a,b for STORE b; STORE a).
    expected = tuple(stored)
    loaded: list[str] = []
    consumed_indexes: list[int] = []
    for index, item in enumerate(body):
        if isinstance(item, Label):
            if not loaded:
                continue
            return setup, body, 0
        names = _pure_fast_load_names(item)
        if names is None:
            break
        loaded.extend(names)
        consumed_indexes.append(index)
        if len(loaded) >= len(expected):
            break

    if tuple(loaded) != expected:
        return setup, body, 0
    if any(_fast_name_references(body, name) != 1 for name in expected):
        return setup, body, 0

    consumed = set(consumed_indexes)
    new_body = [item for index, item in enumerate(body) if index not in consumed]
    return setup[:store_start], new_body, len(expected)


def _always_bound_fast_locals(func: Callable[..., Any]) -> frozenset[str]:
    code = func.__code__
    names = list(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
    index = code.co_argcount + code.co_kwonlyargcount
    if code.co_flags & inspect.CO_VARARGS:
        names.append(code.co_varnames[index])
        index += 1
    if code.co_flags & inspect.CO_VARKEYWORDS:
        names.append(code.co_varnames[index])
    return frozenset(names)


def _simple_fast_argument_names(items: list[Any]) -> tuple[str, ...] | None:
    names: list[str] = []
    for item in items:
        loaded = _pure_fast_load_names(item)
        if loaded is None or any(
            isinstance(item, Instr) and item.name == "LOAD_FAST_CHECK" for _ in (0,)
        ):
            return None
        names.extend(loaded)
    return tuple(names)


def _caller_local_aliases(
    caller: Callable[..., Any],
    site: _CallSite,
    explicit_argument_code: list[Any],
    written_names: set[str],
) -> dict[str, str]:
    """Map read-only callee parameters directly to always-bound caller params.

    The optimization is intentionally all-or-nothing for explicit positional
    arguments.  Removing only some argument loads would disturb operand-stack order.
    """

    if (
        site.kw_names_index is not None
        or site.implicit_values
        or site.implicit_keywords
        or site.binding.extra_positional_count
        or site.binding.vararg_name is not None
        or site.binding.varkw_name is not None
        or site.binding.keyword_targets
    ):
        return {}
    caller_names = _simple_fast_argument_names(explicit_argument_code)
    targets = site.binding.positional_targets
    if caller_names is None or len(caller_names) != len(targets):
        return {}
    # LOAD_FAST/LOAD_FAST_LOAD_FAST (rather than LOAD_FAST_CHECK) is CPython's
    # own proof that each caller local is initialized at this program point.
    if any(target in written_names for target in targets):
        return {}
    return dict(zip(targets, caller_names))


def _clone_callee(
    func: Callable[..., Any],
    prefix: str,
    forwarded_parameters: tuple[str, ...] = (),
    constant_parameters: dict[str, Any] | None = None,
    aliased_parameters: dict[str, str] | None = None,
    stack_strategy: str = "density",
) -> tuple[list[Any], dict[str, str], bool, int, int, int, int, int, int, int, int, _StackScheduleStats]:
    """Clone callee bytecode, rename locals, and run safe local optimizations."""

    source = Bytecode.from_code(func.__code__)
    constant_parameters = dict(constant_parameters or {})
    aliased_parameters = dict(aliased_parameters or {})
    written_names = _fast_written_names(source)
    constant_parameters = {
        name: value for name, value in constant_parameters.items() if name not in written_names
    }
    frozen_freevars: dict[str, Any] = {}
    freeze_globals = bool(getattr(func, "__inline_freeze_globals__", False))
    if func.__code__.co_freevars:
        closure = func.__closure__ or ()
        for name, cell in zip(func.__code__.co_freevars, closure):
            try:
                frozen_freevars[name] = cell.cell_contents
            except ValueError as exc:
                raise InlineUnsupportedError(
                    f"{func.__qualname__}: cannot freeze an empty closure cell {name!r}"
                ) from exc
    forwarded_rewrites = _forwardable_prefix_rewrites(
        source, forwarded_parameters, constant_parameters
    )
    if forwarded_rewrites is None:
        forwarded_rewrites = {}
        forwarded_parameters = ()
    label_map = {item: Label() for item in source if isinstance(item, Label)}
    begin_map, end_map = _clone_exception_markers(source, label_map)

    local_map = {
        name: aliased_parameters.get(name, f"{prefix}_{name}")
        for name in func.__code__.co_varnames
    }
    exit_label = Label()
    returns = [
        item
        for item in source
        if isinstance(item, Instr) and item.name in {"RETURN_VALUE", "RETURN_CONST"}
    ]
    last_instruction = next((item for item in reversed(source) if isinstance(item, Instr)), None)
    terminal_single_return = (
        len(returns) == 1
        and last_instruction is returns[0]
        and last_instruction.name in {"RETURN_VALUE", "RETURN_CONST"}
    )

    result: list[Any] = []
    for source_index, item in enumerate(source):
        if source_index in forwarded_rewrites:
            result.extend(forwarded_rewrites[source_index])
            continue
        if isinstance(item, Label):
            result.append(label_map[item])
            continue
        if isinstance(item, TryBegin):
            result.append(begin_map[item])
            continue
        if isinstance(item, TryEnd):
            result.append(end_map[item])
            continue
        if not isinstance(item, Instr):
            raise InlineUnsupportedError(
                f"{func.__qualname__}: unsupported bytecode marker {type(item).__name__}"
            )

        if item.name in {"RESUME", "COPY_FREE_VARS"}:
            continue
        if item.name == "RETURN_VALUE":
            if not terminal_single_return:
                result.append(Instr("JUMP_FORWARD", exit_label, location=item.location))
            continue
        if item.name == "RETURN_CONST":
            result.append(Instr("LOAD_CONST", item.arg, location=item.location))
            if not terminal_single_return:
                result.append(Instr("JUMP_FORWARD", exit_label, location=item.location))
            continue

        if item.name == "LOAD_DEREF" and isinstance(item.arg, FreeVar):
            result.append(Instr("LOAD_CONST", frozen_freevars[item.arg.name], location=item.location))
            continue

        if item.name == "LOAD_GLOBAL" and freeze_globals:
            push_null, name = item.arg
            result.append(
                Instr("LOAD_CONST", _resolve_frozen_global(func, name), location=item.location)
            )
            if push_null:
                # CPython 3.13 CALL expects callable, then NULL/self, then arguments.
                result.append(Instr("PUSH_NULL", location=item.location))
            continue

        constant_loads = _replace_constant_fast_load(item, constant_parameters, local_map)
        if constant_loads is not None:
            result.extend(constant_loads)
            continue

        arg = item.arg
        if isinstance(arg, Label):
            arg = label_map[arg]
        elif "FAST" in item.name:
            arg = _map_local_arg(arg, local_map)
        result.append(Instr(item.name, arg, location=item.location))

    if not terminal_single_return:
        result.append(exit_label)
    synthetic_names = {
        mapped for original, mapped in local_map.items()
        if mapped != aliased_parameters.get(original)
        and isinstance(mapped, str)
        and mapped.startswith("__inl_")
    }
    result, copies_propagated, constants_propagated = (
        _propagate_single_assignment_synthetic_locals(result, synthetic_names)
    )
    (
        result,
        unary_ops_folded,
        binary_ops_folded,
        comparisons_folded,
    ) = _fold_constant_expression_fixpoint(result)
    result, folded = _fold_constant_branches(result)
    result, pruned = _prune_unreachable_items(result)
    result, redundant_jumps = _remove_redundant_jumps(result)
    # Remove trivial synthetic frame traffic before stack allocation as well as
    # afterward.  This exposes a smaller expression DAG to the scheduler and avoids
    # retaining a value merely because a later transient result still occupies a
    # synthetic local.
    result, pre_roundtrips_elided = _elide_synthetic_stack_roundtrips(
        result, synthetic_names
    )
    result, stack_schedule = _schedule_stack_resident_synthetic_values(
        result, synthetic_names, return_stats=True, strategy=stack_strategy
    )
    assert isinstance(stack_schedule, _StackScheduleStats)
    result, post_roundtrips_elided = _elide_synthetic_stack_roundtrips(
        result, synthetic_names
    )
    roundtrips_elided = pre_roundtrips_elided + post_roundtrips_elided
    return (
        result,
        local_map,
        bool(forwarded_parameters),
        folded,
        pruned,
        redundant_jumps,
        comparisons_folded,
        binary_ops_folded,
        roundtrips_elided,
        copies_propagated,
        constants_propagated,
        unary_ops_folded,
        stack_schedule,
    )




def _callee_local_reuse_safe(func: Callable[..., Any]) -> bool:
    return analyze_fast_locals(func.__code__).reuse_safe


def _inline_local_prefix(
    caller: Callable[..., Any], callee: Callable[..., Any]
) -> tuple[str, dict[int, str]]:
    """Return a stable callee-local namespace when its lifetime is reusable."""

    prefixes = dict(getattr(caller, "__inline_reused_local_prefixes__", {}))
    original = getattr(callee, "__inline_original__", callee)
    key = id(original)
    if _callee_local_reuse_safe(callee):
        existing = prefixes.get(key)
        if existing is not None:
            return existing, prefixes
        prefix = f"__inl_reuse_{callee.__name__}_{_next_counter()}"
        prefixes[key] = prefix
        return prefix, prefixes
    return f"__inl_{callee.__name__}_{_next_counter()}", prefixes


def _validate_policy(policy: str) -> str:
    if policy not in {"speed", "always"}:
        raise ValueError("policy must be 'speed' or 'always'")
    return policy


def _validate_binding(binding: str) -> str:
    if binding not in {"guarded", "frozen"}:
        raise ValueError("binding must be 'guarded' or 'frozen'")
    return binding


def _validate_stack_strategy(strategy: str, policy: str) -> str:
    if strategy == "auto":
        return "speed" if policy == "speed" else "density"
    if strategy not in {"speed", "density", "off"}:
        raise ValueError(
            "stack_strategy must be 'auto', 'speed', 'density', or 'off'"
        )
    return strategy


def _validate_fusion_strategy(strategy: str, policy: str) -> str:
    """Resolve caller-result fusion policy.

    ``safe`` preserves the caller local binding.  ``aggressive`` may eliminate a
    single-use caller local entirely and therefore intentionally gives up exact
    ``f_locals``/trace-local observability for that handoff.  ``auto`` stays on the
    semantics-preserving path for both inline policies.
    """

    del policy
    if strategy == "auto":
        return "safe"
    if strategy not in {"safe", "aggressive", "off"}:
        raise ValueError(
            "fusion_strategy must be 'auto', 'safe', 'aggressive', or 'off'"
        )
    return strategy


def _instruction_count(items: list[Any]) -> int:
    return sum(isinstance(item, Instr) for item in items)


def _guard_hot_path_cost(site: _CallSite) -> int:
    """Return a conservative instruction-equivalent cost for guarded binding."""

    if not site.guard_loader or site.guarded_identity is None:
        return 1_000_000
    state = _callable_guard_state(site.guarded_identity, site.callee, site.binding)
    if state is None:
        return 1_000_000
    if (
        state.kind == "function"
        and state.closure_values is None
        and not state.kwdefaults
    ):
        # identity + code checks, optional positional-default tuple check, and POP.
        return 9 + (5 if state.check_defaults else 0)
    # Compound guards use a tiny Python helper. Charge the CALL/frame more heavily
    # than its raw bytecode count so speed policy does not inline merely on code size.
    return 16


def _is_profitable_setup(
    *,
    items: list[Any],
    site: _CallSite,
    explicit_argument_code: list[Any],
    replacement_setup: list[Any],
    optimization_credit: int = 0,
    guard_cost: int = 0,
) -> bool:
    """Conservative static estimate for removing call overhead.

    The callee body executes in both versions, so compare only the callable/CALL
    sequence against extra setup introduced by merging. One instruction-equivalent
    credit represents the eliminated Python frame setup performed inside CALL.
    """

    # CPython constructs variadic argument containers in optimized call machinery.
    # Rebuilding those containers with Python bytecodes has consistently cost more
    # than the CALL frame saved, so speed policy leaves these sites untouched.
    if site.binding.vararg_name is not None or site.binding.varkw_name is not None:
        return False

    original_overhead = _instruction_count(items[site.start : site.callable_end]) + 1
    if site.kw_names_index is not None:
        original_overhead += 1
    explicit_cost = _instruction_count(explicit_argument_code)
    added_setup = max(0, _instruction_count(replacement_setup) - explicit_cost)
    if guard_cost:
        added_setup += max(0, int(guard_cost))
        # Guarded binding is worthwhile under speed policy only when merging exposes
        # concrete simplifications that repay its runtime validation.
        optimization_credit = max(0, min(int(optimization_credit), 16))
    elif site.guarded_closure:
        # Historical frozen closure mode still has its identity-only guard.
        added_setup += 5
        optimization_credit = max(0, min(int(optimization_credit), 4))
    else:
        optimization_credit = 0
    return added_setup <= original_overhead + 1 + optimization_credit


def _all_direct_calls(bytecode: Bytecode, caller: Callable[..., Any]) -> tuple[_CallSite, ...]:
    excluded: set[int] = set()
    found: list[_CallSite] = []
    while True:
        site = _find_direct_call(bytecode, caller, excluded)
        if site is None:
            break
        found.append(site)
        excluded.add(site.call_index)
    return tuple(found)


def _instruction_exception_contexts(
    items: list[Any],
) -> dict[int, tuple[TryBegin, ...]]:
    active: list[TryBegin] = []
    contexts: dict[int, tuple[TryBegin, ...]] = {}
    for index, item in enumerate(items):
        if isinstance(item, TryBegin):
            active.append(item)
            continue
        if isinstance(item, TryEnd):
            for position in range(len(active) - 1, -1, -1):
                if active[position] is item.entry:
                    del active[position]
                    break
            continue
        if isinstance(item, Instr):
            contexts[index] = tuple(active)
    return contexts


def _simple_shared_site(site: _CallSite) -> bool:
    """Whether a site can enter a normalized frame-local shared region."""
    binding = site.binding
    return (
        site.kw_names_index is None
        and not site.implicit_values
        and not site.implicit_keywords
        and not binding.keyword_targets
        and binding.extra_positional_count == 0
        and binding.vararg_name is None
        and binding.varkw_name is None
    )


def _shared_region_policy(value: bool | str) -> str:
    if value is True:
        return "all"
    if value is False:
        return "off"
    if value not in {"auto", "off", "all"}:
        raise ValueError("shared_regions must be False, True, 'auto', 'off', or 'all'")
    return value


def _emit_continuation_dispatch(
    ids: tuple[int, ...],
    continuation_name: str,
    continuations: list[Label],
    *,
    leaf_jump: str = "JUMP_BACKWARD",
) -> list[Any]:
    """Emit O(log n) dispatch while preserving the call result below temporaries."""
    if len(ids) == 1:
        return [Instr(leaf_jump, continuations[ids[0]])]

    split = len(ids) // 2
    pivot = ids[split]
    right = Label()
    output: list[Any] = [
        Instr("LOAD_FAST", continuation_name),
        Instr("LOAD_CONST", pivot),
        Instr("COMPARE_OP", Compare.LT),
        Instr("POP_JUMP_IF_FALSE", right),
    ]
    output.extend(
        _emit_continuation_dispatch(
            ids[:split], continuation_name, continuations, leaf_jump=leaf_jump
        )
    )
    output.append(right)
    output.extend(
        _emit_continuation_dispatch(
            ids[split:], continuation_name, continuations, leaf_jump=leaf_jump
        )
    )
    return output


def _share_one_region(
    func: F,
    *,
    binding: str,
    shared_regions: bool | str,
    min_calls: int,
    min_body_instructions: int,
    stack_strategy: str = "speed",
) -> tuple[F, int, Callable[..., Any] | None]:
    """Replace one repeated eligible callee with one appended shared body."""
    policy = _shared_region_policy(shared_regions)
    if policy == "off":
        return func, 0, None

    bc = Bytecode.from_code(func.__code__)
    items = list(bc)
    contexts = _instruction_exception_contexts(items)
    sites = _all_direct_calls(bc, func)
    groups: dict[tuple[int, tuple[int, ...]], list[_CallSite]] = {}
    callees: dict[tuple[int, tuple[int, ...]], Callable[..., Any]] = {}
    group_contexts: dict[tuple[int, tuple[int, ...]], tuple[TryBegin, ...]] = {}
    for site in sites:
        if not _simple_shared_site(site):
            continue
        context = contexts.get(site.call_index, ())
        key = (id(site.callee), tuple(id(entry) for entry in context))
        groups.setdefault(key, []).append(site)
        callees[key] = site.callee
        group_contexts[key] = context

    chosen: list[_CallSite] | None = None
    callee: Callable[..., Any] | None = None
    chosen_context: tuple[TryBegin, ...] = ()
    for key, group in groups.items():
        candidate = callees[key]
        if len(group) < min_calls:
            continue
        if policy == "auto" and not getattr(candidate, "__inline_shared_region__", False):
            continue
        candidate_items = list(Bytecode.from_code(candidate.__code__))
        body_size = _instruction_count(candidate_items)
        if body_size < min_body_instructions:
            continue
        context = group_contexts[key]
        if context and any(isinstance(item, (TryBegin, TryEnd)) for item in candidate_items):
            # The third-party bytecode IR does not support nesting a callee's own
            # TryBegin inside the caller's active TryBegin. Keep that case duplicated.
            continue
        chosen = group
        callee = candidate
        chosen_context = context
        break

    if chosen is None or callee is None:
        return func, 0, None

    prefix = f"__inl_shared_{callee.__name__}_{_next_counter()}"
    (
        body,
        local_map,
        _,
        folded,
        pruned,
        redundant_jumps,
        comparisons_folded,
        binary_ops_folded,
        roundtrips_elided,
        copies_propagated,
        constants_propagated,
        unary_ops_folded,
        stack_schedule,
    ) = _clone_callee(
        callee, prefix, stack_strategy=stack_strategy
    )
    region = Label()
    continuation_name = f"{prefix}_continuation"
    continuations = [Label() for _ in chosen]

    # Replace from the end so original call-site indexes remain valid.
    for continuation_id, site in reversed(list(enumerate(chosen))):
        argument_end = site.call_index
        replacement = list(items[site.callable_end:argument_end])
        for target in reversed(site.binding.positional_targets):
            replacement.append(Instr("STORE_FAST", local_map[target]))
        for target, value in site.binding.defaults:
            replacement.append(Instr("LOAD_CONST", value))
            replacement.append(Instr("STORE_FAST", local_map[target]))
        replacement.extend(
            [
                Instr("LOAD_CONST", continuation_id),
                Instr("STORE_FAST", continuation_name),
                Instr("JUMP_BACKWARD" if chosen_context else "JUMP_FORWARD", region),
                continuations[continuation_id],
            ]
        )
        if binding == "guarded":
            guarded = _guarded_site_replacement(site, items, replacement)
            if guarded is None:
                # Shared regions are an optimization. If this call shape cannot be
                # guarded without user-visible extra lookups, let duplicated inline
                # processing (or the ordinary CALL) handle it instead.
                return func, 0, None
            replacement = guarded
        items[site.start : site.call_index + 1] = replacement

    if chosen_context:
        opening = chosen_context[-1]
        try:
            insert_at = next(i for i, item in enumerate(items) if item is opening) + 1
        except StopIteration:
            return func, 0, None
        skip_region = Label()
        region_code: list[Any] = [Instr("JUMP_FORWARD", skip_region), region, *body]
        region_code.extend(
            _emit_continuation_dispatch(
                tuple(range(len(chosen))),
                continuation_name,
                continuations,
                leaf_jump="JUMP_FORWARD",
            )
        )
        region_code.append(skip_region)
        items[insert_at:insert_at] = region_code
    else:
        items.append(region)
        items.extend(body)
        items.extend(
            _emit_continuation_dispatch(
                tuple(range(len(chosen))), continuation_name, continuations
            )
        )

    bc.clear()
    bc.extend(items)
    try:
        rebuilt = _rebuild_function(func, bc)
        verify_code(rebuilt.__code__)
    except (BytecodeVerificationError, ValueError, RuntimeError):
        # Shared regions are optional. If differing expression-stack depths or
        # another CFG invariant makes one common entry invalid, retain the
        # original caller so normal per-site inlining can proceed safely.
        return func, 0, None
    if folded:
        rebuilt.__inline_constant_branches_folded__ = (
            int(getattr(func, "__inline_constant_branches_folded__", 0)) + folded
        )
    if pruned:
        rebuilt.__inline_dead_instructions_pruned__ = (
            int(getattr(func, "__inline_dead_instructions_pruned__", 0)) + pruned
        )
    if redundant_jumps:
        rebuilt.__inline_redundant_jumps_removed__ = (
            int(getattr(func, "__inline_redundant_jumps_removed__", 0)) + redundant_jumps
        )
    if comparisons_folded:
        rebuilt.__inline_constant_comparisons_folded__ = (
            int(getattr(func, "__inline_constant_comparisons_folded__", 0))
            + comparisons_folded
        )
    if binary_ops_folded:
        rebuilt.__inline_constant_binary_ops_folded__ = (
            int(getattr(func, "__inline_constant_binary_ops_folded__", 0))
            + binary_ops_folded
        )
    if unary_ops_folded:
        rebuilt.__inline_constant_unary_ops_folded__ = (
            int(getattr(func, "__inline_constant_unary_ops_folded__", 0))
            + unary_ops_folded
        )
    if roundtrips_elided:
        rebuilt.__inline_synthetic_roundtrips_elided__ = (
            int(getattr(func, "__inline_synthetic_roundtrips_elided__", 0))
            + roundtrips_elided
        )
    if copies_propagated:
        rebuilt.__inline_synthetic_copies_propagated__ = (
            int(getattr(func, "__inline_synthetic_copies_propagated__", 0))
            + copies_propagated
        )
    if constants_propagated:
        rebuilt.__inline_synthetic_constants_propagated__ = (
            int(getattr(func, "__inline_synthetic_constants_propagated__", 0))
            + constants_propagated
        )
    if stack_schedule.scheduled:
        rebuilt.__inline_stack_resident_values__ = (
            int(getattr(func, "__inline_stack_resident_values__", 0))
            + stack_schedule.scheduled
        )
    if stack_schedule.candidates:
        rebuilt.__inline_stack_scheduler_candidates__ = (
            int(getattr(func, "__inline_stack_scheduler_candidates__", 0))
            + stack_schedule.candidates
        )
    if stack_schedule.spilled:
        rebuilt.__inline_stack_spilled_values__ = (
            int(getattr(func, "__inline_stack_spilled_values__", 0))
            + stack_schedule.spilled
        )
    if stack_schedule.conflicts:
        rebuilt.__inline_stack_crossing_conflicts__ = (
            int(getattr(func, "__inline_stack_crossing_conflicts__", 0))
            + stack_schedule.conflicts
        )
    if stack_schedule.max_copy_depth:
        rebuilt.__inline_stack_max_copy_depth__ = max(
            int(getattr(func, "__inline_stack_max_copy_depth__", 0)),
            stack_schedule.max_copy_depth,
        )
    if stack_schedule.instruction_savings:
        rebuilt.__inline_stack_instruction_savings__ = (
            int(getattr(func, "__inline_stack_instruction_savings__", 0))
            + stack_schedule.instruction_savings
        )
    if stack_schedule.dependency_edges:
        rebuilt.__inline_stack_dependency_edges__ = (
            int(getattr(func, "__inline_stack_dependency_edges__", 0))
            + stack_schedule.dependency_edges
        )
    if stack_schedule.peak_resident_values:
        rebuilt.__inline_stack_peak_resident_values__ = max(
            int(getattr(func, "__inline_stack_peak_resident_values__", 0)),
            stack_schedule.peak_resident_values,
        )
    if stack_schedule.split_values:
        rebuilt.__inline_stack_split_values__ = (
            int(getattr(func, "__inline_stack_split_values__", 0))
            + stack_schedule.split_values
        )
    if stack_schedule.split_reads:
        rebuilt.__inline_stack_split_reads__ = (
            int(getattr(func, "__inline_stack_split_reads__", 0))
            + stack_schedule.split_reads
        )
    if stack_schedule.split_instruction_cost:
        rebuilt.__inline_stack_split_instruction_cost__ = (
            int(getattr(func, "__inline_stack_split_instruction_cost__", 0))
            + stack_schedule.split_instruction_cost
        )
    if stack_schedule.middle_splits:
        rebuilt.__inline_stack_middle_splits__ = (
            int(getattr(func, "__inline_stack_middle_splits__", 0))
            + stack_schedule.middle_splits
        )
    if chosen_context:
        rebuilt.__inline_protected_shared_regions__ = (
            int(getattr(func, "__inline_protected_shared_regions__", 0)) + 1
        )
    return rebuilt, len(chosen), callee


@dataclass(frozen=True)
class _ResultFusionStats:
    fused_handoffs: int = 0
    constant_handoffs: int = 0
    aggressive_handoffs: int = 0
    branches_folded: int = 0
    comparisons_folded: int = 0
    binary_ops_folded: int = 0
    dead_pruned: int = 0
    redundant_jumps: int = 0


def _plain_fast_access(item: Any, name: str) -> str | None:
    if not isinstance(item, Instr):
        return None
    if item.name == "STORE_FAST" and item.arg == name:
        return "store"
    if item.name in {"LOAD_FAST", "LOAD_FAST_BORROW"} and item.arg == name:
        return "load"
    if item.name in {"LOAD_FAST_CHECK", "DELETE_FAST"} and item.arg == name:
        return "unsafe"
    if "FAST" in item.name:
        values = item.arg if isinstance(item.arg, tuple) else (item.arg,)
        if name in values:
            return "unsafe"
    return None


def _fuse_inline_result_handoffs(
    func: F, *, strategy: str
) -> tuple[F, _ResultFusionStats]:
    """Fuse caller-local handoffs produced by inlined calls.

    The safe form retains the caller binding.  For a dynamic immediate handoff,
    ``STORE_FAST x; LOAD_FAST x`` becomes ``COPY 1; STORE_FAST x`` so the value
    continues directly on the operand stack while ``x`` remains observable through
    ``locals()``.  When an inlined producer leaves a literal constant, later direct
    loads of the uniquely-assigned handoff local are replaced with the same
    ``LOAD_CONST`` and the whole caller receives the normal constant/branch cleanup
    passes.  This enables propagation across two separately inlined callees.

    Aggressive mode may remove a single-use immediate STORE/LOAD pair completely.
    It is opt-in because the eliminated caller local no longer appears as bound in
    ``f_locals``/trace snapshots after that point.
    """

    if strategy == "off":
        return func, _ResultFusionStats()
    produced = set(getattr(func, "__inline_produced_result_locals__", ()))
    if not produced:
        return func, _ResultFusionStats()

    bc = Bytecode.from_code(func.__code__)
    items = list(bc)
    fused = constant = aggressive = 0

    for name in sorted(produced):
        accesses: list[tuple[int, str]] = []
        unsafe = False
        for index, item in enumerate(items):
            kind = _plain_fast_access(item, name)
            if kind is None:
                continue
            if kind == "unsafe":
                unsafe = True
                break
            accesses.append((index, kind))
        if unsafe:
            continue
        stores = [index for index, kind in accesses if kind == "store"]
        loads = [index for index, kind in accesses if kind == "load"]
        if len(stores) != 1 or not loads:
            continue
        store = stores[0]
        if any(index < store for index in loads):
            continue

        # Literal-return handoff: keep STORE_FAST for caller-local observability but
        # substitute all proven reads.  The same constant object is loaded, so identity
        # semantics are unchanged.
        if store > 0 and isinstance(items[store - 1], Instr) and items[store - 1].name == "LOAD_CONST":
            value = items[store - 1].arg
            replacements = 0
            for index in loads:
                old = items[index]
                if not isinstance(old, Instr) or old.name not in {"LOAD_FAST", "LOAD_FAST_BORROW"}:
                    replacements = 0
                    break
                items[index] = Instr("LOAD_CONST", value, location=old.location)
                replacements += 1
            if replacements:
                constant += replacements
                fused += 1
                if strategy == "aggressive":
                    # If every dynamic read disappeared, the producer's return constant
                    # and the caller-local store are dead apart from local observability.
                    del items[store - 1 : store + 1]
                    aggressive += 1
            continue

        # Dynamic direct handoff.  Recompute the immediate indexes from current items;
        # previous constant candidates may have shortened the stream.
        accesses = []
        unsafe = False
        for index, item in enumerate(items):
            kind = _plain_fast_access(item, name)
            if kind is None:
                continue
            if kind == "unsafe":
                unsafe = True
                break
            accesses.append((index, kind))
        if unsafe:
            continue
        stores = [index for index, kind in accesses if kind == "store"]
        loads = [index for index, kind in accesses if kind == "load"]
        if len(stores) != 1 or len(loads) != 1:
            continue
        store, load = stores[0], loads[0]
        if load != store + 1:
            continue
        store_instr = items[store]
        load_instr = items[load]
        if not isinstance(store_instr, Instr) or not isinstance(load_instr, Instr):
            continue
        if strategy == "aggressive":
            del items[store : load + 1]
            aggressive += 1
        else:
            # Keep the local assignment while forwarding the original value directly.
            items[store : load + 1] = [
                Instr("COPY", 1, location=store_instr.location),
                Instr("STORE_FAST", name, location=store_instr.location),
            ]
        fused += 1

    if not fused and not constant:
        return func, _ResultFusionStats()

    binary_folded = comparisons_folded = branches_folded = pruned = jumps = 0
    if constant:
        items, binary_folded = _fold_constant_binary_ops(items)
        items, comparisons_folded = _fold_constant_comparisons(items)
        items, branches_folded = _fold_constant_branches(items)
        items, pruned = _prune_unreachable_items(items)
        items, jumps = _remove_redundant_jumps(items)

    bc.clear()
    bc.extend(items)
    try:
        rebuilt = _rebuild_function(func, bc)
        verify_code(rebuilt.__code__)
    except (BytecodeVerificationError, ValueError, RuntimeError):
        return func, _ResultFusionStats()

    # Fold counters feed the existing global report as well as the fusion-specific
    # counters so users can account for the secondary optimization unlocked by fusion.
    if branches_folded:
        rebuilt.__inline_constant_branches_folded__ = (
            int(getattr(func, "__inline_constant_branches_folded__", 0))
            + branches_folded
        )
    if comparisons_folded:
        rebuilt.__inline_constant_comparisons_folded__ = (
            int(getattr(func, "__inline_constant_comparisons_folded__", 0))
            + comparisons_folded
        )
    if binary_folded:
        rebuilt.__inline_constant_binary_ops_folded__ = (
            int(getattr(func, "__inline_constant_binary_ops_folded__", 0))
            + binary_folded
        )
    if pruned:
        rebuilt.__inline_dead_instructions_pruned__ = (
            int(getattr(func, "__inline_dead_instructions_pruned__", 0)) + pruned
        )
    if jumps:
        rebuilt.__inline_redundant_jumps_removed__ = (
            int(getattr(func, "__inline_redundant_jumps_removed__", 0)) + jumps
        )
    return rebuilt, _ResultFusionStats(
        fused,
        constant,
        aggressive,
        branches_folded,
        comparisons_folded,
        binary_folded,
        pruned,
        jumps,
    )



@dataclass(frozen=True)
class _RegionDataflowStats:
    rounds: int = 0
    constant_propagations: int = 0
    copy_propagations: int = 0
    branches_folded: int = 0
    comparisons_folded: int = 0
    binary_ops_folded: int = 0
    dead_pruned: int = 0
    redundant_jumps: int = 0


def _region_barrier(item: Any) -> bool:
    """Whether a whole-region fact may not flow across ``item``.

    The pass intentionally stays local to straight-line bytecode regions.  Labels,
    exception-table pseudo instructions, and control-flow instructions terminate a
    fact even when a later fixed-point cleanup ultimately removes the boundary.
    A subsequent round may then rediscover facts in the simplified region.
    """

    if isinstance(item, Instr) and item.name in {"LOAD_GLOBAL", "LOAD_NAME"}:
        raw = item.arg
        name = raw[1] if isinstance(raw, tuple) and len(raw) == 2 else raw
        if name in {"goto", "label"}:
            # The composed pipeline resolves goto after inline optimization.  Treat
            # its pseudo-operations as control-flow barriers now so dataflow never
            # propagates a loop-carried value across a future backward jump.
            return True
    if isinstance(item, Instr) and item.name in {"CALL", "CALL_KW", "CALL_FUNCTION_EX"}:
        # A remaining (non-inlined) Python call may inspect or mutate its caller frame.
        # Do not carry local-value facts across that observability boundary.
        return True
    return (
        isinstance(item, (Label, TryBegin, TryEnd))
        or (isinstance(item, Instr) and _is_control_flow_instruction(item))
    )


def _simple_assignment_source(items: list[Any], store_index: int) -> tuple[str, Any] | None:
    """Return a pure source for a caller-local assignment at ``store_index``.

    Only a single stack-producing ``LOAD_CONST``/plain ``LOAD_FAST`` immediately
    before the store is admitted.  The assignment itself is never removed by the
    safe whole-region optimizer, preserving caller-local bindings and normal
    ``locals()`` visibility.
    """

    if store_index <= 0:
        return None
    store = items[store_index]
    dest: str | None = None
    if isinstance(store, Instr) and store.name == "STORE_FAST" and isinstance(store.arg, str):
        dest = store.arg
    elif (
        isinstance(store, Instr)
        and store.name == "STORE_FAST_LOAD_FAST"
        and isinstance(store.arg, tuple)
        and len(store.arg) == 2
        and isinstance(store.arg[0], str)
    ):
        dest = store.arg[0]
    if dest is None:
        return None
    producer = items[store_index - 1]
    if not isinstance(producer, Instr):
        return None
    if producer.name == "LOAD_CONST":
        return ("const", producer.arg)
    if producer.name in {"LOAD_FAST", "LOAD_FAST_BORROW"} and isinstance(producer.arg, str):
        return ("fast", producer.arg)
    if (
        producer.name == "STORE_FAST_LOAD_FAST"
        and isinstance(producer.arg, tuple)
        and len(producer.arg) == 2
        and isinstance(producer.arg[1], str)
    ):
        # The fused store consumes one value and immediately produces the second
        # operand's local value.  For a following STORE_FAST this is exactly a
        # plain copy source.
        return ("fast", producer.arg[1])
    return None


def _propagate_one_region_assignment(
    items: list[Any],
    *,
    tracked_names: set[str],
) -> tuple[list[Any], str | None, str | None]:
    """Propagate one safe caller-local assignment through its straight-line region.

    Returns ``(items, destination, source_kind)`` when a rewrite happened.  Caller
    stores are retained.  Fast-local copies are invalidated by either a destination
    rewrite or a source rewrite, so a copied value is never accidentally replaced by
    a later version of its source local.
    """

    for store_index, store in enumerate(items):
        if not isinstance(store, Instr):
            continue
        accesses = _fast_accesses(store)
        written = [name for name, kind in accesses if kind == "write"]
        if len(written) != 1:
            continue
        dest = written[0]
        source = _simple_assignment_source(items, store_index)
        if source is None:
            continue
        source_kind, source_value = source
        # A result local created by an inlined call is a region root.  Facts may
        # subsequently flow into ordinary caller locals assigned from that root.
        if dest not in tracked_names and not (
            source_kind == "fast" and source_value in tracked_names
        ) and source_kind != "const":
            # Exact LOAD_CONST assignments are self-contained facts and may safely
            # become new roots.  This is what lets an inlined constant flow through
            # an ordinary caller copy (``alias = produced``) into a later callee.
            continue
        if source_kind == "fast" and source_value == dest:
            continue

        rewrites: dict[int, list[Instr]] = {}
        saw_read = False
        index = store_index + 1
        while index < len(items):
            item = items[index]
            if _region_barrier(item):
                break
            item_accesses = _fast_accesses(item)
            # A new destination assignment ends this SSA-like version.
            if any(
                name == dest and kind in {"write", "delete", "read_delete"}
                for name, kind in item_accesses
            ):
                break
            # Re-reading a copied source remains equivalent only until that source
            # local changes.  Constants do not have this dependency.
            if source_kind == "fast" and any(
                name == source_value and kind in {"write", "delete", "read_delete"}
                for name, kind in item_accesses
            ):
                break
            if any(name == dest and kind == "read" for name, kind in item_accesses):
                if not isinstance(item, Instr):
                    break
                replacement = _replacement_loads_for_value(
                    item, dest, source_kind, source_value
                )
                if replacement is None:
                    break
                rewrites[index] = replacement
                saw_read = True
            index += 1

        if not saw_read:
            # Even without a rewrite, a tracked copy creates another region root for
            # later fixed-point rounds only when it is a plain copy/constant.
            if dest in tracked_names:
                continue
            continue

        rebuilt: list[Any] = []
        for position, item in enumerate(items):
            replacement = rewrites.get(position)
            if replacement is not None:
                rebuilt.extend(replacement)
            else:
                rebuilt.append(item)
        return rebuilt, dest, source_kind
    return items, None, None



def _expand_region_fact_roots(
    items: list[Any], tracked_names: set[str]
) -> tuple[int, int]:
    """Close ``tracked_names`` over simple caller copies/constants.

    CPython 3.13 often represents ``a = produced; b = a`` as a chain of
    ``STORE_FAST_LOAD_FAST`` instructions.  The load feeding the next assignment is
    embedded in the *same* instruction as the previous store, so no later load exists
    for a rewrite pass to discover.  This closure records the value-flow edge even
    when it requires no immediate bytecode replacement.
    """

    constant_roots = copy_roots = 0
    changed = True
    while changed:
        changed = False
        for index, item in enumerate(items):
            if not isinstance(item, Instr):
                continue
            writes = [name for name, kind in _fast_accesses(item) if kind == "write"]
            if len(writes) != 1:
                continue
            dest = writes[0]
            if dest in tracked_names:
                continue
            source = _simple_assignment_source(items, index)
            if source is None:
                continue
            kind, value = source
            if kind == "const":
                tracked_names.add(dest)
                constant_roots += 1
                changed = True
            elif kind == "fast" and value in tracked_names:
                tracked_names.add(dest)
                copy_roots += 1
                changed = True
    return constant_roots, copy_roots


def _whole_region_cross_inline_dataflow(
    func: F, *, enabled: bool = True, max_rounds: int = 16, require_gain: bool = True
) -> tuple[F, _RegionDataflowStats]:
    """Propagate facts through several consecutive inlined callees as one region.

    The pass is deliberately semantics-preserving with respect to ordinary caller
    local bindings: stores are kept.  Its roots are locals known to receive results
    from inlined calls; facts may then flow through direct caller copies into later
    inlined consumers.  Every rewrite is bounded by control-flow and exception
    boundaries.  Constant folding/pruning runs between rounds, so simplifying one
    inlined callee can expose a new fact for the next callee in the chain.
    """

    if not enabled:
        return func, _RegionDataflowStats()
    tracked = set(getattr(func, "__inline_produced_result_locals__", ()))
    if not tracked:
        return func, _RegionDataflowStats()

    bc = Bytecode.from_code(func.__code__)
    items = list(bc)
    rounds = consts = copies = branches = comparisons = binaries = pruned = jumps = 0
    for _ in range(max_rounds):
        round_changed = False
        new_consts, new_copies = _expand_region_fact_roots(items, tracked)
        if new_consts or new_copies:
            consts += new_consts
            copies += new_copies
            round_changed = True
        # A successful propagation can reveal a new constant/copy assignment.  Run
        # one rewrite at a time so instruction indexes remain trivial and deterministic.
        while True:
            rewritten, dest, kind = _propagate_one_region_assignment(
                items, tracked_names=tracked
            )
            if dest is None:
                break
            items = rewritten
            tracked.add(dest)
            if kind == "const":
                consts += 1
            else:
                copies += 1
            round_changed = True

        before = list(items)
        items, binary_count = _fold_constant_binary_ops(items)
        items, compare_count = _fold_constant_comparisons(items)
        items, branch_count = _fold_constant_branches(items)
        items, prune_count = _prune_unreachable_items(items)
        items, jump_count = _remove_redundant_jumps(items)
        binaries += binary_count
        comparisons += compare_count
        branches += branch_count
        pruned += prune_count
        jumps += jump_count
        if items != before:
            round_changed = True
        if not round_changed:
            break
        rounds += 1
    else:
        # Bounded fixed point: correctness never depends on reaching another round.
        pass

    if not any((consts, copies, branches, comparisons, binaries, pruned, jumps)):
        return func, _RegionDataflowStats()

    bc.clear()
    bc.extend(items)
    try:
        rebuilt = _rebuild_function(func, bc)
        verify_code(rebuilt.__code__)
    except (BytecodeVerificationError, ValueError, RuntimeError):
        return func, _RegionDataflowStats()

    if require_gain and not (
        len(rebuilt.__code__.co_code) < len(func.__code__.co_code)
        or branches
        or comparisons
        or binaries
        or pruned
        or jumps
    ):
        # Copy-only substitutions can merely exchange one fast-local load for
        # another.  CPython's specialization makes that performance-neutral or
        # slightly negative depending on local layout, so speed policy keeps the
        # original code unless the region pass creates an objective structural win.
        return func, _RegionDataflowStats()

    # Feed the pre-existing global counters as well as region-specific diagnostics.
    if branches:
        rebuilt.__inline_constant_branches_folded__ = (
            int(getattr(func, "__inline_constant_branches_folded__", 0)) + branches
        )
    if comparisons:
        rebuilt.__inline_constant_comparisons_folded__ = (
            int(getattr(func, "__inline_constant_comparisons_folded__", 0)) + comparisons
        )
    if binaries:
        rebuilt.__inline_constant_binary_ops_folded__ = (
            int(getattr(func, "__inline_constant_binary_ops_folded__", 0)) + binaries
        )
    if pruned:
        rebuilt.__inline_dead_instructions_pruned__ = (
            int(getattr(func, "__inline_dead_instructions_pruned__", 0)) + pruned
        )
    if jumps:
        rebuilt.__inline_redundant_jumps_removed__ = (
            int(getattr(func, "__inline_redundant_jumps_removed__", 0)) + jumps
        )
    return rebuilt, _RegionDataflowStats(
        rounds,
        consts,
        copies,
        branches,
        comparisons,
        binaries,
        pruned,
        jumps,
    )



@dataclass(frozen=True)
class _CfgValueFact:
    kind: str
    payload: Any
    origin: str | None = None


@dataclass(frozen=True)
class _CfgRegionDataflowStats:
    rounds: int = 0
    merge_facts: int = 0
    constant_propagations: int = 0
    copy_propagations: int = 0
    branches_folded: int = 0
    comparisons_folded: int = 0
    binary_ops_folded: int = 0
    dead_pruned: int = 0
    redundant_jumps: int = 0
    loop_headers: int = 0
    loop_invariant_facts: int = 0
    loop_variant_kills: int = 0
    affine_recurrences: int = 0
    recurrence_folds: int = 0


@dataclass(frozen=True)
class _BytecodeBlock:
    ident: int
    start: int
    end: int
    successors: tuple[int, ...]
    predecessors: tuple[int, ...]
    has_hard_barrier: bool = False


def _cfg_fact_equal(left: _CfgValueFact, right: _CfgValueFact) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind == "const":
        # LOAD_CONST values are compiler constants.  Compare exact builtin type first
        # so 1/True and similar equality aliases are never merged accidentally.
        if type(left.payload) is not type(right.payload):
            return False
        try:
            return bool(left.payload == right.payload)
        except Exception:
            return left.payload is right.payload
    return left.payload == right.payload


def _cfg_join_states(states: list[dict[str, _CfgValueFact]]) -> dict[str, _CfgValueFact]:
    if not states:
        return {}
    common = dict(states[0])
    for state in states[1:]:
        for name in list(common):
            other = state.get(name)
            if other is None or not _cfg_fact_equal(common[name], other):
                common.pop(name, None)
    return common


def _cfg_unconditional_jump(item: Instr) -> bool:
    return item.name in {
        "JUMP_FORWARD",
        "JUMP_BACKWARD",
        "JUMP_BACKWARD_NO_INTERRUPT",
        "JUMP_NO_INTERRUPT",
    }


def _cfg_terminal(item: Instr) -> bool:
    return item.name in {
        "RETURN_VALUE",
        "RETURN_CONST",
        "RAISE_VARARGS",
        "RERAISE",
    }


def _build_bytecode_blocks(items: list[Any]) -> tuple[list[_BytecodeBlock], dict[int, int]]:
    """Build a small index-based CFG over ``bytecode`` IR.

    This intentionally models only normal control-flow edges.  Exception markers,
    loops/backedges, and remaining calls are hard dataflow barriers; the common code
    verifier remains authoritative for bytecode/stack correctness after rewriting.
    """

    if not items:
        return [], {}
    leaders: set[int] = {0}
    label_positions: dict[Label, int] = {}
    for index, item in enumerate(items):
        if isinstance(item, Label):
            leaders.add(index)
            label_positions[item] = index
        if isinstance(item, Instr) and (_is_control_flow_instruction(item) or _cfg_terminal(item)):
            if index + 1 < len(items):
                leaders.add(index + 1)
    starts = sorted(leaders)
    ranges: list[tuple[int, int]] = [
        (start, starts[pos + 1] if pos + 1 < len(starts) else len(items))
        for pos, start in enumerate(starts)
    ]
    index_to_block: dict[int, int] = {}
    for ident, (start, end) in enumerate(ranges):
        for index in range(start, end):
            index_to_block[index] = ident
    label_to_block = {
        label: index_to_block[position]
        for label, position in label_positions.items()
        if position in index_to_block
    }

    raw_successors: list[list[int]] = [[] for _ in ranges]
    barriers: list[bool] = [False for _ in ranges]
    for ident, (start, end) in enumerate(ranges):
        last_instr: Instr | None = None
        last_index: int | None = None
        for index in range(end - 1, start - 1, -1):
            item = items[index]
            if isinstance(item, (TryBegin, TryEnd)):
                barriers[ident] = True
            if isinstance(item, Instr):
                if item.name in {"CALL", "CALL_KW", "CALL_FUNCTION_EX"}:
                    # Calls are handled as in-block state kills as well, but mark the
                    # block so facts are not assumed to survive odd call/control mixes.
                    pass
                if last_instr is None:
                    last_instr = item
                    last_index = index
        if last_instr is None:
            if ident + 1 < len(ranges):
                raw_successors[ident].append(ident + 1)
            continue
        target_block: int | None = None
        if isinstance(last_instr.arg, Label):
            target_block = label_to_block.get(last_instr.arg)
        if target_block is not None and (
            "JUMP" in last_instr.name or last_instr.name == "FOR_ITER"
        ):
            if target_block is not None:
                # Backedges contribute an empty state rather than loop-carried facts.
                raw_successors[ident].append(target_block)
        if not _cfg_terminal(last_instr) and not _cfg_unconditional_jump(last_instr):
            if ident + 1 < len(ranges):
                raw_successors[ident].append(ident + 1)

    predecessors: list[list[int]] = [[] for _ in ranges]
    for source, successors in enumerate(raw_successors):
        for target in successors:
            if source not in predecessors[target]:
                predecessors[target].append(source)

    blocks = [
        _BytecodeBlock(
            ident,
            start,
            end,
            tuple(dict.fromkeys(raw_successors[ident])),
            tuple(predecessors[ident]),
            barriers[ident],
        )
        for ident, (start, end) in enumerate(ranges)
    ]
    return blocks, index_to_block



@dataclass(frozen=True)
class _AffineRecurrence:
    name: str
    start: int
    step: int
    loop_header: int


@dataclass(frozen=True)
class _NaturalLoop:
    header: int
    latch: int
    blocks: frozenset[int]


def _cfg_dominators(
    blocks: list[_BytecodeBlock], entry: int = 0
) -> list[frozenset[int]]:
    """Return normal-edge dominator sets for the bytecode CFG.

    The solver is intentionally small and conservative.  Unreachable blocks dominate
    only themselves.  Strength reduction uses this to reject lexical backward-edge
    shapes whose apparent loop header does not actually dominate every rewritten use.
    """

    if not blocks:
        return []
    reachable: set[int] = set()
    pending = [entry] if 0 <= entry < len(blocks) else []
    while pending:
        block_id = pending.pop()
        if block_id in reachable:
            continue
        reachable.add(block_id)
        pending.extend(
            succ for succ in blocks[block_id].successors if succ not in reachable
        )

    universe = frozenset(reachable)
    dom: list[set[int]] = []
    for block in blocks:
        if block.ident == entry and block.ident in reachable:
            dom.append({entry})
        elif block.ident in reachable:
            dom.append(set(universe))
        else:
            dom.append({block.ident})

    changed = True
    while changed:
        changed = False
        for block in blocks:
            ident = block.ident
            if ident == entry or ident not in reachable:
                continue
            preds = [pred for pred in block.predecessors if pred in reachable]
            if not preds:
                new = {ident}
            else:
                common = set(dom[preds[0]])
                for pred in preds[1:]:
                    common.intersection_update(dom[pred])
                new = common | {ident}
            if new != dom[ident]:
                dom[ident] = new
                changed = True
    return [frozenset(values) for values in dom]


def _cfg_min_strength_savings_to_update(
    blocks: list[_BytecodeBlock],
    *,
    header: int,
    members: set[int],
    update_block: int,
    block_savings: dict[int, int],
) -> int | None:
    """Minimum eliminated instructions on any loop path that reaches the update.

    All weights are non-negative, so a bounded Bellman-Ford style relaxation finds
    the cheapest simple path even when the natural loop contains nested cycles.
    Paths that leave the loop are ignored because they never pay the derived update.
    A backedge into ``header`` cannot improve a non-negative minimum and is skipped to
    keep the quantity scoped to one logical iteration.
    """

    if header not in members or update_block not in members:
        return None
    inf = 1 << 60
    cost = {block_id: inf for block_id in members}
    cost[header] = int(block_savings.get(header, 0))
    for _ in range(max(1, len(members) - 1)):
        changed = False
        for source in members:
            source_cost = cost[source]
            if source_cost == inf:
                continue
            for target in blocks[source].successors:
                if target not in members or target == header:
                    continue
                candidate = source_cost + int(block_savings.get(target, 0))
                if candidate < cost[target]:
                    cost[target] = candidate
                    changed = True
        if not changed:
            break
    result = cost.get(update_block, inf)
    return None if result == inf else int(result)


def _cfg_min_strength_savings_from_update_to_boundary(
    blocks: list[_BytecodeBlock],
    *,
    header: int,
    members: set[int],
    update_block: int,
    block_savings: dict[int, int],
) -> int | None:
    """Minimum savings from the update block to the current iteration boundary.

    An iteration boundary is a backedge to ``header``, an edge leaving the natural
    loop, or a terminal block.  This lets speed policy credit affine work that is
    guaranteed *after* the induction update, including work distributed across both
    arms of a branch, while still rejecting an early-exit path with no reduced use.
    """

    if update_block not in members:
        return None
    inf = 1 << 60
    cost = {block_id: inf for block_id in members}
    for block_id in members:
        block = blocks[block_id]
        boundary = not block.successors or any(
            successor == header or successor not in members
            for successor in block.successors
        )
        if boundary:
            cost[block_id] = int(block_savings.get(block_id, 0))

    for _ in range(max(1, len(members) - 1)):
        changed = False
        for block_id in members:
            block = blocks[block_id]
            internal = [
                successor
                for successor in block.successors
                if successor in members and successor != header
            ]
            if not internal:
                continue
            successor_cost = min(cost[successor] for successor in internal)
            if successor_cost == inf:
                continue
            candidate = int(block_savings.get(block_id, 0)) + successor_cost
            if candidate < cost[block_id]:
                cost[block_id] = candidate
                changed = True
        if not changed:
            break
    result = cost.get(update_block, inf)
    return None if result == inf else int(result)


def _cfg_natural_loops(blocks: list[_BytecodeBlock]) -> dict[tuple[int, int], _NaturalLoop]:
    """Return natural loops keyed by ``(latch, header)`` block ids.

    The bytecode emitted by CPython for ordinary ``while``/``for`` constructs is
    reducible: a backward normal edge targets a dominating header.  We only need the
    natural-loop block set here so value versions defined inside that set can be
    invalidated when they travel around the backedge.
    """

    loops: dict[tuple[int, int], _NaturalLoop] = {}
    for latch in blocks:
        for header_id in latch.successors:
            header = blocks[header_id]
            if latch.start < header.start:
                continue
            members: set[int] = {header_id, latch.ident}
            pending = [latch.ident]
            while pending:
                current = pending.pop()
                for predecessor in blocks[current].predecessors:
                    if predecessor in members:
                        continue
                    # Do not walk above the lexical header.  This is conservative for
                    # irreducible control flow and keeps only the reducible natural
                    # loop that CPython emits for structured source loops.
                    if blocks[predecessor].start < header.start:
                        continue
                    members.add(predecessor)
                    if predecessor != header_id:
                        pending.append(predecessor)
            loops[(latch.ident, header_id)] = _NaturalLoop(
                header_id, latch.ident, frozenset(members)
            )
    return loops


def _cfg_filter_backedge_state(
    state: dict[str, _CfgValueFact],
    loop: _NaturalLoop,
    index_to_block: dict[int, int],
) -> tuple[dict[str, _CfgValueFact], int]:
    """Filter facts that cannot denote the same value on the next iteration.

    Constants are iteration-independent.  A dynamic token defined outside the loop
    is also stable if the body did not overwrite its local fact.  A token produced by
    an instruction *inside* the loop, however, denotes a new runtime value on every
    trip even though its static instruction index is unchanged; carrying that token
    to the header would be an invalid SSA merge.
    """

    filtered: dict[str, _CfgValueFact] = {}
    killed = 0
    for name, fact in state.items():
        if fact.kind == "token":
            payload = fact.payload
            definition_index = (
                payload[0]
                if isinstance(payload, tuple) and payload and isinstance(payload[0], int)
                else None
            )
            definition_block = (
                index_to_block.get(definition_index)
                if definition_index is not None
                else None
            )
            if definition_block in loop.blocks:
                killed += 1
                continue
        filtered[name] = fact
    return filtered, killed



def _cfg_affine_update_step(items: list[Any], index: int, dest: str) -> int | None:
    """Return a constant integer self-update step for ``dest`` at ``index``.

    Only exact-int ``x = x +/- c`` and ``x +=/-= c`` shapes are accepted.  This
    keeps the recurrence proof inside Python's unbounded exact-integer semantics and
    avoids user-defined arithmetic dispatch entirely.
    """

    if index < 3:
        return None
    load, constant, binary = items[index - 3 : index]
    load_matches = (
        isinstance(load, Instr)
        and (
            (load.name == "LOAD_FAST" and load.arg == dest)
            or (
                load.name == "STORE_FAST_LOAD_FAST"
                and isinstance(load.arg, tuple)
                and len(load.arg) == 2
                and load.arg[1] == dest
            )
        )
    )
    if not (
        load_matches
        and isinstance(constant, Instr)
        and constant.name == "LOAD_CONST"
        and type(constant.arg) is int
        and isinstance(binary, Instr)
        and binary.name == "BINARY_OP"
    ):
        return None
    if binary.arg in {BinaryOp.ADD, BinaryOp.INPLACE_ADD}:
        return int(constant.arg)
    if binary.arg in {BinaryOp.SUBTRACT, BinaryOp.INPLACE_SUBTRACT}:
        return -int(constant.arg)
    return None


def _cfg_loop_has_recurrence_barrier(
    items: list[Any], blocks: list[_BytecodeBlock], members: set[int]
) -> bool:
    """Return whether a loop can observe/mutate fast locals outside our model."""

    for block_id in members:
        block = blocks[block_id]
        for index in range(block.start, block.end):
            item = items[index]
            if isinstance(item, (TryBegin, TryEnd)):
                return True
            if not isinstance(item, Instr):
                continue
            if item.name in {"CALL", "CALL_KW", "CALL_FUNCTION_EX"}:
                return True
            if item.name in {"LOAD_GLOBAL", "LOAD_NAME"}:
                raw = item.arg
                name = raw[1] if isinstance(raw, tuple) and len(raw) == 2 else raw
                if name in {"goto", "label"}:
                    return True
    return False


def _cfg_detect_affine_recurrences(
    items: list[Any],
    blocks: list[_BytecodeBlock],
    natural_loops: dict[tuple[int, int], _NaturalLoop],
    exits: list[dict[str, _CfgValueFact]],
    initialized: list[bool],
) -> dict[int, dict[str, _CfgValueFact]]:
    """Detect exact-int affine induction variables for each natural-loop header.

    The accepted form has one static fast-local write in the natural loop and that
    write is ``x = x +/- const`` (including inplace syntax).  The preheader must prove
    an exact integer start value.  Remaining calls, exception regions, and unresolved
    goto pseudo-edges reject the loop because they can make fast-local state
    observable or mutable outside this analysis.

    Conditional execution of the update is allowed: skipping ``x += step`` only
    repeats a value and therefore preserves the congruence/monotonic properties used
    by the recurrence folder below.
    """

    grouped: dict[int, set[int]] = {}
    for loop in natural_loops.values():
        grouped.setdefault(loop.header, set()).update(loop.blocks)

    output: dict[int, dict[str, _CfgValueFact]] = {}
    for header, members in grouped.items():
        if _cfg_loop_has_recurrence_barrier(items, blocks, members):
            continue
        latch_preds = {
            latch
            for (latch, target) in natural_loops
            if target == header
        }
        forward_preds = [
            pred for pred in blocks[header].predecessors
            if pred not in latch_preds and initialized[pred]
        ]
        if not forward_preds:
            continue
        preheader = _cfg_join_states([exits[pred] for pred in forward_preds])
        if not preheader:
            continue

        writes: dict[str, list[int]] = {}
        invalid: set[str] = set()
        for block_id in members:
            block = blocks[block_id]
            for index in range(block.start, block.end):
                item = items[index]
                if not isinstance(item, Instr):
                    continue
                for name, kind in _fast_accesses(item):
                    if kind == "write":
                        writes.setdefault(name, []).append(index)
                    elif kind in {"delete", "read_delete"}:
                        invalid.add(name)

        header_facts: dict[str, _CfgValueFact] = {}
        for name, fact in preheader.items():
            if name in invalid or fact.kind != "const" or type(fact.payload) is not int:
                continue
            positions = writes.get(name, ())
            if len(positions) != 1:
                continue
            step = _cfg_affine_update_step(items, positions[0], name)
            if step is None:
                continue
            recurrence = _AffineRecurrence(name, int(fact.payload), step, header)
            header_facts[name] = _CfgValueFact("recurrence", recurrence, name)
        if header_facts:
            output[header] = header_facts
    return output


def _cfg_recurrence_compare(
    recurrence: _AffineRecurrence, compare: Compare, constant: int
) -> bool | None:
    """Prove a comparison result that holds for every recurrence value."""

    op = int(compare) & 0x0F
    start = recurrence.start
    step = recurrence.step
    if step == 0:
        if op == int(Compare.LT):
            return start < constant
        if op == int(Compare.LE):
            return start <= constant
        if op == int(Compare.EQ):
            return start == constant
        if op == int(Compare.NE):
            return start != constant
        if op == int(Compare.GT):
            return start > constant
        if op == int(Compare.GE):
            return start >= constant
        return None

    if op in {int(Compare.EQ), int(Compare.NE)}:
        unreachable = (
            (step > 0 and constant < start)
            or (step < 0 and constant > start)
            or ((constant - start) % abs(step) != 0)
        )
        if unreachable:
            return op == int(Compare.NE)
        return None

    if step > 0:
        if op == int(Compare.LT) and start >= constant:
            return False
        if op == int(Compare.LE) and start > constant:
            return False
        if op == int(Compare.GT) and start > constant:
            return True
        if op == int(Compare.GE) and start >= constant:
            return True
    else:
        if op == int(Compare.LT) and start < constant:
            return True
        if op == int(Compare.LE) and start <= constant:
            return True
        if op == int(Compare.GT) and start <= constant:
            return False
        if op == int(Compare.GE) and start < constant:
            return False
    return None


def _cfg_reverse_compare(compare: Compare) -> Compare | None:
    op = int(compare) & 0x0F
    reverse = {
        int(Compare.LT): Compare.GT,
        int(Compare.LE): Compare.GE,
        int(Compare.EQ): Compare.EQ,
        int(Compare.NE): Compare.NE,
        int(Compare.GT): Compare.LT,
        int(Compare.GE): Compare.LE,
    }
    return reverse.get(op)


def _cfg_recurrence_sequence_rewrite(
    items: list[Any], index: int, state: dict[str, _CfgValueFact]
) -> tuple[int, list[Instr]] | None:
    """Fold recurrence-derived expressions to constants.

    Supported proofs deliberately target properties invariant across all iterations:
    modulo residue, low-bit masks for power-of-two periods, monotonic bounds, and
    equality values that the affine progression can never reach.
    """

    def fact_for_load(item: Any) -> _AffineRecurrence | None:
        if not (isinstance(item, Instr) and item.name == "LOAD_FAST" and isinstance(item.arg, str)):
            return None
        fact = state.get(item.arg)
        if fact is None or fact.kind != "recurrence" or not isinstance(fact.payload, _AffineRecurrence):
            return None
        return fact.payload

    if index + 2 < len(items):
        first, second, third = items[index], items[index + 1], items[index + 2]
        recurrence = fact_for_load(first)
        if recurrence is not None and isinstance(second, Instr) and second.name == "LOAD_CONST" and type(second.arg) is int:
            constant = int(second.arg)
            if isinstance(third, Instr) and third.name == "BINARY_OP":
                result: int | None = None
                if third.arg in {BinaryOp.REMAINDER, BinaryOp.INPLACE_REMAINDER} and constant != 0:
                    if recurrence.step % constant == 0:
                        result = recurrence.start % constant
                elif third.arg in {BinaryOp.AND, BinaryOp.INPLACE_AND} and constant >= 0:
                    period = constant + 1
                    if period > 0 and period & (period - 1) == 0 and recurrence.step % period == 0:
                        result = recurrence.start & constant
                if result is not None:
                    return 3, [Instr("LOAD_CONST", result, location=first.location)]
            if isinstance(third, Instr) and third.name == "COMPARE_OP" and isinstance(third.arg, Compare):
                result = _cfg_recurrence_compare(recurrence, third.arg, constant)
                if result is not None:
                    return 3, [Instr("LOAD_CONST", bool(result), location=first.location)]

        if isinstance(first, Instr) and first.name == "LOAD_CONST" and type(first.arg) is int:
            recurrence = fact_for_load(second)
            if recurrence is not None and isinstance(third, Instr) and third.name == "COMPARE_OP" and isinstance(third.arg, Compare):
                reversed_compare = _cfg_reverse_compare(third.arg)
                if reversed_compare is not None:
                    result = _cfg_recurrence_compare(recurrence, reversed_compare, int(first.arg))
                    if result is not None:
                        return 3, [Instr("LOAD_CONST", bool(result), location=first.location)]
    return None




@dataclass(frozen=True)
class _StrengthReductionUse:
    index: int
    width: int
    block_id: int
    recurrence: _AffineRecurrence
    scale: int
    offset: int
    paired_left: str | None
    location: Any


@dataclass(frozen=True)
class _StrengthReductionStats:
    derived_values: int = 0
    rewritten_uses: int = 0
    update_sites: int = 0
    lazy_values: int = 0
    lazy_rewritten_uses: int = 0
    lazy_materializations: int = 0


def _cfg_strength_expression_at(
    items: list[Any],
    index: int,
    block_end: int,
    state: dict[str, _CfgValueFact],
    block_id: int,
) -> _StrengthReductionUse | None:
    """Match an exact-int affine expression based on a known recurrence.

    The first strength-reduction slice deliberately recognizes only the forms that
    CPython emits compactly and that have a clear profitability model::

        i * SCALE
        SCALE * i
        i * SCALE + OFFSET
        SCALE * i + OFFSET
        i * SCALE - OFFSET
        SCALE * i - OFFSET

    ``i`` must carry an ``_AffineRecurrence`` fact, and SCALE/OFFSET must be exact
    Python ``int`` constants.  A CPython 3.13 ``LOAD_FAST_LOAD_FAST(other, i)``
    superinstruction is accepted for the ``i * SCALE`` orientation as well,
    preserving the unrelated left operand.
    """

    if index >= block_end:
        return None
    first = items[index]
    recurrence: _AffineRecurrence | None = None
    paired_left: str | None = None
    location = getattr(first, "location", None)
    scale: int | None = None
    multiply_index: int | None = None

    # i * SCALE, including a paired load whose second value is i.
    if isinstance(first, Instr) and first.name == "LOAD_FAST" and isinstance(first.arg, str):
        fact = state.get(first.arg)
        if fact is not None and fact.kind == "recurrence" and isinstance(fact.payload, _AffineRecurrence):
            recurrence = fact.payload
    elif (
        isinstance(first, Instr)
        and first.name == "LOAD_FAST_LOAD_FAST"
        and isinstance(first.arg, tuple)
        and len(first.arg) == 2
        and all(isinstance(name, str) for name in first.arg)
    ):
        fact = state.get(first.arg[1])
        if fact is not None and fact.kind == "recurrence" and isinstance(fact.payload, _AffineRecurrence):
            recurrence = fact.payload
            paired_left = first.arg[0]

    if recurrence is not None:
        if index + 2 >= block_end:
            return None
        scale_load = items[index + 1]
        multiply = items[index + 2]
        if not (
            isinstance(scale_load, Instr)
            and scale_load.name == "LOAD_CONST"
            and type(scale_load.arg) is int
            and isinstance(multiply, Instr)
            and multiply.name == "BINARY_OP"
            and multiply.arg in {BinaryOp.MULTIPLY, BinaryOp.INPLACE_MULTIPLY}
        ):
            return None
        scale = int(scale_load.arg)
        multiply_index = index + 2
    else:
        # SCALE * i.  This orientation cannot be embedded in LOAD_FAST_LOAD_FAST,
        # but otherwise has the same exact-int semantics because both operands are
        # proven exact ints rather than user-defined arithmetic objects.
        if index + 2 >= block_end or not (
            isinstance(first, Instr)
            and first.name == "LOAD_CONST"
            and type(first.arg) is int
            and isinstance(items[index + 1], Instr)
            and items[index + 1].name == "LOAD_FAST"
            and isinstance(items[index + 1].arg, str)
            and isinstance(items[index + 2], Instr)
            and items[index + 2].name == "BINARY_OP"
            and items[index + 2].arg in {BinaryOp.MULTIPLY, BinaryOp.INPLACE_MULTIPLY}
        ):
            return None
        fact = state.get(items[index + 1].arg)
        if fact is None or fact.kind != "recurrence" or not isinstance(fact.payload, _AffineRecurrence):
            return None
        recurrence = fact.payload
        scale = int(first.arg)
        multiply_index = index + 2

    if recurrence.step == 0 or scale == 0 or multiply_index is None:
        return None

    width = 3
    offset = 0
    if index + 4 < block_end:
        offset_load = items[index + 3]
        combine = items[index + 4]
        if (
            isinstance(offset_load, Instr)
            and offset_load.name == "LOAD_CONST"
            and type(offset_load.arg) is int
            and isinstance(combine, Instr)
            and combine.name == "BINARY_OP"
        ):
            if combine.arg in {BinaryOp.ADD, BinaryOp.INPLACE_ADD}:
                offset = int(offset_load.arg)
                width = 5
            elif combine.arg in {BinaryOp.SUBTRACT, BinaryOp.INPLACE_SUBTRACT}:
                offset = -int(offset_load.arg)
                width = 5

    return _StrengthReductionUse(
        index=index,
        width=width,
        block_id=block_id,
        recurrence=recurrence,
        scale=scale,
        offset=offset,
        paired_left=paired_left,
        location=location,
    )


def _cfg_header_has_fallthrough_preheader(
    items: list[Any],
    blocks: list[_BytecodeBlock],
    header: int,
    members: set[int],
) -> bool:
    """Return whether inserting immediately before the header label runs once.

    The accepted shape has one external predecessor immediately before the loop
    header and enters the header by fallthrough.  Backedges target the existing
    ``Label`` object, so an initialization inserted *before* that label is skipped on
    later iterations.  This is the common CPython ``while`` layout and avoids a
    first-iteration flag or extra loop-body branch.
    """

    block = blocks[header]
    if block.start >= len(items) or not isinstance(items[block.start], Label):
        return False
    external = [pred for pred in block.predecessors if pred not in members]
    if len(external) != 1:
        return False
    pred_id = external[0]
    if pred_id + 1 != header:
        return False
    pred = blocks[pred_id]
    last: Instr | None = None
    for index in range(pred.end - 1, pred.start - 1, -1):
        if isinstance(items[index], Instr):
            last = items[index]
            break
    if last is None or _cfg_terminal(last) or _cfg_unconditional_jump(last):
        return False
    # If the branch explicitly targets the header, entry is not the simple
    # fallthrough form this transformation relies on.
    header_label = items[block.start]
    if isinstance(last.arg, Label) and last.arg is header_label:
        return False
    return header in pred.successors


def _cfg_strength_reduce_affine_expressions(
    func: F,
    *,
    enabled: bool = True,
    require_gain: bool = True,
) -> tuple[F, _StrengthReductionStats]:
    """Replace repeated affine induction expressions with a derived recurrence.

    For a proven ``i = start; ...; i += step`` loop and repeated exact-int
    ``i * scale + offset`` expressions, introduce a synthetic local ``d`` with::

        d_0 = start * scale + offset
        d_{n+1} = d_n + step * scale

    When every update-paying loop path has enough affine work, matched expressions
    become fast-local loads from a globally maintained secondary induction value.
    Its initial value is inserted on the first fallthrough into the body and its
    update is emitted immediately after the unique induction write.

    If that global recurrence is unprofitable because some paths do not consume the
    affine value, repeated same-version uses inside an affine basic block may instead
    use lazy path materialization.  The first exact-int expression remains in place
    and snapshots its result with ``COPY``/``STORE_FAST``; later uses in the same
    block load that snapshot.  Re-entering the block recomputes from the current
    induction value, so non-consuming paths pay no derived-maintenance cost and a
    write inside the block is never crossed by one lazy value version.
    """

    if not enabled:
        return func, _StrengthReductionStats()

    bc = Bytecode.from_code(func.__code__)
    items = list(bc)
    tracked = set(getattr(func, "__inline_produced_result_locals__", ()))
    (
        blocks,
        entries,
        _exits,
        _rounds,
        _merge_facts,
        _loop_headers,
        _loop_invariant_facts,
        _loop_variant_kills,
        affine_recurrences,
    ) = _cfg_region_states(items, tracked)
    if not blocks or affine_recurrences == 0:
        return func, _StrengthReductionStats()

    natural_loops = _cfg_natural_loops(blocks)
    dominators = _cfg_dominators(blocks)
    header_members: dict[int, set[int]] = {}
    for loop in natural_loops.values():
        header_members.setdefault(loop.header, set()).update(loop.blocks)

    uses: list[_StrengthReductionUse] = []
    for block in blocks:
        state = dict(entries[block.ident])
        for index in range(block.start, block.end):
            candidate = _cfg_strength_expression_at(
                items, index, block.end, state, block.ident
            )
            if candidate is not None:
                uses.append(candidate)
            _cfg_kill_or_assign(items, index, state, tracked_names=tracked)
    if not uses:
        return func, _StrengthReductionStats()

    groups: dict[tuple[int, str, int, int], list[_StrengthReductionUse]] = {}
    for use in uses:
        key = (
            use.recurrence.loop_header,
            use.recurrence.name,
            use.scale,
            use.offset,
        )
        groups.setdefault(key, []).append(use)

    replacements: dict[int, list[Any]] = {}
    consumed: set[int] = set()
    before: dict[int, list[Instr]] = {}
    after: dict[int, list[Instr]] = {}
    derived_values = rewritten_uses = update_sites = 0
    lazy_values = lazy_rewritten_uses = lazy_materializations = 0
    fused_update_loads: set[int] = set()

    for (header, induction_name, scale, offset), group in sorted(
        groups.items(), key=lambda pair: min(use.index for use in pair[1])
    ):
        recurrence = group[0].recurrence
        members = header_members.get(header)
        if not members or not _cfg_header_has_fallthrough_preheader(
            items, blocks, header, members
        ):
            continue

        writes: list[int] = []
        invalid = False
        for block_id in members:
            block = blocks[block_id]
            for index in range(block.start, block.end):
                item = items[index]
                for name, kind in _fast_accesses(item):
                    if name != induction_name:
                        continue
                    if kind == "write":
                        writes.append(index)
                    elif kind in {"delete", "read_delete"}:
                        invalid = True
        if invalid or len(writes) != 1:
            continue
        update_index = writes[0]
        if _cfg_affine_update_step(items, update_index, induction_name) != recurrence.step:
            continue
        update_block = next(
            (block.ident for block in blocks if block.start <= update_index < block.end),
            None,
        )
        if update_block is None:
            continue

        if header >= len(dominators) or header not in dominators[update_block]:
            continue

        # A derived recurrence is synchronized immediately after the unique induction
        # write, so any dominated use in the same natural loop may consume it --
        # before or after the update and on either side of structured branches.
        eligible = [
            use
            for use in group
            if use.block_id in members
            and header in dominators[use.block_id]
            and not any(pos in consumed for pos in range(use.index, use.index + use.width))
        ]
        if len(eligible) != len(group) or len(eligible) < 2:
            continue

        per_iteration_savings = sum(use.width - 1 for use in eligible)
        update_item = items[update_index]
        fused_update = (
            update_index not in fused_update_loads
            and isinstance(update_item, Instr)
            and update_item.name == "STORE_FAST"
            and update_item.arg == induction_name
            and update_index not in replacements
        )
        update_cost = 3 if fused_update else 4

        use_global_recurrence = True
        if require_gain:
            # Static code shrinkage alone can overvalue mutually-exclusive branch
            # uses.  Charge the derived update against the *cheapest* path that
            # actually reaches it.  This permits branch-distributed reduction when
            # every update-paying path eliminates enough affine work, while rejecting
            # a hot update guarded only by rare affine uses.
            block_savings: dict[int, int] = {}
            for use in eligible:
                block_savings[use.block_id] = (
                    block_savings.get(use.block_id, 0) + use.width - 1
                )
            prefix_savings = _cfg_min_strength_savings_to_update(
                blocks,
                header=header,
                members=members,
                update_block=update_block,
                block_savings=block_savings,
            )
            suffix_savings = _cfg_min_strength_savings_from_update_to_boundary(
                blocks,
                header=header,
                members=members,
                update_block=update_block,
                block_savings=block_savings,
            )
            if prefix_savings is None or suffix_savings is None:
                use_global_recurrence = False
            else:
                minimum_update_path_savings = (
                    prefix_savings
                    + suffix_savings
                    - block_savings.get(update_block, 0)
                )
                use_global_recurrence = minimum_update_path_savings > update_cost
        elif per_iteration_savings < update_cost:
            use_global_recurrence = False

        if not use_global_recurrence:
            # Partial redundancy elimination for branch-local affine work.
            #
            # A globally maintained secondary induction value is unattractive when
            # some loop paths pay its update without consuming it.  In that case,
            # materialize the exact affine value lazily at the first use on a basic
            # block path and reuse it for later same-version uses in that block.  The
            # first expression is intentionally left in place; COPY/STORE snapshots
            # its already-computed exact-int result without moving arithmetic across
            # control flow or exception boundaries.  Re-entering the block on a later
            # iteration therefore re-synchronizes from the current induction value,
            # while paths that never enter the affine block pay no maintenance cost.
            #
            # The unique induction write can split one basic block into a pre-update
            # and post-update value version.  Never share a lazy materialization
            # across that boundary.
            segments: dict[tuple[int, int], list[_StrengthReductionUse]] = {}
            for use in eligible:
                version_side = 0
                if use.block_id == update_block:
                    version_side = -1 if use.index < update_index else 1
                segments.setdefault((use.block_id, version_side), []).append(use)

            selected_segments: list[list[_StrengthReductionUse]] = []
            for segment in segments.values():
                segment.sort(key=lambda use: use.index)
                if len(segment) < 2:
                    continue
                # The first occurrence still performs the original affine
                # expression.  Capturing it costs COPY + STORE_FAST.  Every later
                # occurrence becomes one fast-local load, saving width-1
                # instructions.  Speed policy requires a strict local win before
                # the final byte-size gate; density policy may accept break-even IR
                # when later CPython 3.13 superinstruction compaction can shrink it.
                saved = sum(use.width - 1 for use in segment[1:])
                if saved > 2 or (not require_gain and saved >= 2):
                    selected_segments.append(segment)

            if not selected_segments:
                continue

            for segment in selected_segments:
                first = segment[0]
                derived_name = (
                    f"__inl_sr_lazy_{_next_counter()}_{induction_name}"
                )
                capture_index = first.index + first.width - 1
                after.setdefault(capture_index, []).extend(
                    [
                        Instr("COPY", 1, location=first.location),
                        Instr("STORE_FAST", derived_name, location=first.location),
                    ]
                )

                for use in segment[1:]:
                    if use.paired_left is None:
                        replacement = [
                            Instr("LOAD_FAST", derived_name, location=use.location)
                        ]
                    else:
                        replacement = [
                            Instr(
                                "LOAD_FAST_LOAD_FAST",
                                (use.paired_left, derived_name),
                                location=use.location,
                            )
                        ]
                    replacements[use.index] = replacement
                    for pos in range(use.index + 1, use.index + use.width):
                        replacements[pos] = []
                        consumed.add(pos)
                    consumed.add(use.index)

                derived_values += 1
                rewritten_uses += len(segment) - 1
                lazy_values += 1
                lazy_rewritten_uses += len(segment) - 1
                lazy_materializations += 1
            continue

        delta = recurrence.step * scale
        if delta == 0:
            continue
        derived_name = f"__inl_sr_{_next_counter()}_{induction_name}"
        initial = recurrence.start * scale + offset
        header_index = blocks[header].start
        init_location = eligible[0].location
        before.setdefault(header_index, []).extend(
            [
                Instr("LOAD_CONST", initial, location=init_location),
                Instr("STORE_FAST", derived_name, location=init_location),
            ]
        )
        update_location = getattr(items[update_index], "location", init_location)
        if fused_update:
            # CPython 3.13 can combine the original induction store with the load of
            # the derived recurrence.  The ADD/STORE sequence then consumes the
            # loaded derived value and leaves the evaluation stack exactly as the
            # original STORE_FAST did.
            replacements[update_index] = [
                Instr(
                    "STORE_FAST_LOAD_FAST",
                    (induction_name, derived_name),
                    location=update_location,
                )
            ]
            fused_update_loads.add(update_index)
            update_tail = [
                Instr("LOAD_CONST", delta, location=update_location),
                Instr("BINARY_OP", BinaryOp.ADD, location=update_location),
                Instr("STORE_FAST", derived_name, location=update_location),
            ]
        else:
            update_tail = [
                Instr("LOAD_FAST", derived_name, location=update_location),
                Instr("LOAD_CONST", delta, location=update_location),
                Instr("BINARY_OP", BinaryOp.ADD, location=update_location),
                Instr("STORE_FAST", derived_name, location=update_location),
            ]
        after.setdefault(update_index, []).extend(update_tail)

        for use in eligible:
            if use.paired_left is None:
                replacement = [
                    Instr("LOAD_FAST", derived_name, location=use.location)
                ]
            else:
                replacement = [
                    Instr(
                        "LOAD_FAST_LOAD_FAST",
                        (use.paired_left, derived_name),
                        location=use.location,
                    )
                ]
            replacements[use.index] = replacement
            for pos in range(use.index + 1, use.index + use.width):
                replacements[pos] = []
                consumed.add(pos)
            consumed.add(use.index)
        derived_values += 1
        rewritten_uses += len(eligible)
        update_sites += 1

    if not replacements:
        return func, _StrengthReductionStats()

    rebuilt_items: list[Any] = []
    for index, item in enumerate(items):
        rebuilt_items.extend(before.get(index, ()))
        rebuilt_items.extend(replacements.get(index, [item]))
        rebuilt_items.extend(after.get(index, ()))

    # When several derived recurrences share one induction update, the tail of one
    # update naturally ends in STORE_FAST(sr_a) followed by LOAD_FAST(sr_b).  Keep
    # CPython 3.13's compact local-pair form rather than paying two instructions.
    compacted: list[Any] = []
    cursor = 0
    while cursor < len(rebuilt_items):
        item = rebuilt_items[cursor]
        if (
            cursor + 1 < len(rebuilt_items)
            and isinstance(item, Instr)
            and item.name == "STORE_FAST"
            and isinstance(item.arg, str)
            and item.arg.startswith("__inl_sr_")
            and isinstance(rebuilt_items[cursor + 1], Instr)
            and rebuilt_items[cursor + 1].name == "LOAD_FAST"
            and isinstance(rebuilt_items[cursor + 1].arg, str)
            and rebuilt_items[cursor + 1].arg.startswith("__inl_sr_")
        ):
            following = rebuilt_items[cursor + 1]
            compacted.append(
                Instr(
                    "STORE_FAST_LOAD_FAST",
                    (item.arg, following.arg),
                    location=item.location,
                )
            )
            cursor += 2
            continue
        compacted.append(item)
        cursor += 1
    rebuilt_items = compacted

    bc.clear()
    bc.extend(rebuilt_items)
    try:
        rebuilt = _rebuild_function(func, bc)
        verify_code(rebuilt.__code__)
    except (BytecodeVerificationError, ValueError, RuntimeError):
        return func, _StrengthReductionStats()

    if require_gain and len(rebuilt.__code__.co_code) >= len(func.__code__.co_code):
        return func, _StrengthReductionStats()

    return rebuilt, _StrengthReductionStats(
        derived_values=derived_values,
        rewritten_uses=rewritten_uses,
        update_sites=update_sites,
        lazy_values=lazy_values,
        lazy_rewritten_uses=lazy_rewritten_uses,
        lazy_materializations=lazy_materializations,
    )


def _cfg_assignment_fact(
    items: list[Any],
    index: int,
    state: dict[str, _CfgValueFact],
    *,
    tracked_names: set[str],
) -> tuple[str | None, _CfgValueFact | None]:
    item = items[index]
    if not isinstance(item, Instr):
        return None, None
    writes = [name for name, kind in _fast_accesses(item) if kind == "write"]
    if len(writes) != 1:
        return None, None
    dest = writes[0]
    source = _simple_assignment_source(items, index)
    if source is not None:
        kind, value = source
        if kind == "const":
            return dest, _CfgValueFact("const", value, None)
        if kind == "fast":
            fact = state.get(value)
            if fact is not None:
                return dest, fact
    recurrence_fact = state.get(dest)
    if (
        recurrence_fact is not None
        and recurrence_fact.kind == "recurrence"
        and isinstance(recurrence_fact.payload, _AffineRecurrence)
        and _cfg_affine_update_step(items, index, dest) == recurrence_fact.payload.step
    ):
        return dest, recurrence_fact
    if dest in tracked_names:
        # A non-simple inlined result is still a stable SSA-like value version.  Its
        # token is tied to the physical defining instruction, so both branch paths can
        # prove copies of the same pre-branch result equal at a merge.
        return dest, _CfgValueFact("token", (index, dest), dest)
    return dest, None


def _cfg_kill_or_assign(
    items: list[Any],
    index: int,
    state: dict[str, _CfgValueFact],
    *,
    tracked_names: set[str],
) -> None:
    item = items[index]
    if not isinstance(item, Instr):
        if isinstance(item, (TryBegin, TryEnd)):
            state.clear()
        return
    if item.name in {"LOAD_GLOBAL", "LOAD_NAME"}:
        raw = item.arg
        global_name = raw[1] if isinstance(raw, tuple) and len(raw) == 2 else raw
        if global_name in {"goto", "label"}:
            # optimize_extensions resolves goto after inline lowering.  These pseudo
            # operations therefore represent not-yet-materialized CFG edges and must
            # kill every fact before the real backward/forward jump is installed.
            state.clear()
            return
    if item.name in {"CALL", "CALL_KW", "CALL_FUNCTION_EX"}:
        # A remaining call may mutate optimized caller locals through frame.f_locals.
        state.clear()
        return
    accesses = _fast_accesses(item)
    writes = [name for name, kind in accesses if kind == "write"]
    deletes = [name for name, kind in accesses if kind in {"delete", "read_delete"}]
    for name in deletes:
        state.pop(name, None)
    if writes:
        dest, fact = _cfg_assignment_fact(
            items, index, state, tracked_names=tracked_names
        )
        for name in writes:
            if name != dest:
                state.pop(name, None)
        if dest is not None:
            if fact is None:
                state.pop(dest, None)
            else:
                state[dest] = fact


def _cfg_materializer(
    name: str, state: dict[str, _CfgValueFact]
) -> tuple[str, Any] | None:
    fact = state.get(name)
    if fact is None:
        return None
    if fact.kind == "const":
        return ("const", fact.payload)
    if fact.kind == "recurrence" and isinstance(fact.payload, _AffineRecurrence):
        if fact.payload.step == 0:
            return ("const", fact.payload.start)
    # Reuse another definitely-bound local carrying the exact same abstract version.
    preferred: list[str] = []
    if fact.origin is not None:
        preferred.append(fact.origin)
    preferred.extend(sorted(state))
    for candidate in preferred:
        if candidate == name:
            continue
        other = state.get(candidate)
        if other is not None and _cfg_fact_equal(fact, other):
            return ("fast", candidate)
    return None


def _cfg_rewrite_load(
    item: Instr, state: dict[str, _CfgValueFact]
) -> tuple[list[Instr] | None, int, int]:
    """Return replacement plus (constant, copy) propagation counts."""

    if item.name == "LOAD_FAST" and isinstance(item.arg, str):
        materializer = _cfg_materializer(item.arg, state)
        if materializer is None:
            return None, 0, 0
        kind, value = materializer
        if kind == "const":
            return [Instr("LOAD_CONST", value, location=item.location)], 1, 0
        return [Instr("LOAD_FAST", value, location=item.location)], 0, 1
    if item.name == "LOAD_FAST_LOAD_FAST" and isinstance(item.arg, tuple):
        mats = [_cfg_materializer(name, state) for name in item.arg]
        if all(materializer is None for materializer in mats):
            return None, 0, 0
        if all(materializer is None or materializer[0] == "fast" for materializer in mats):
            names = tuple(
                materializer[1] if materializer is not None else name
                for name, materializer in zip(item.arg, mats)
            )
            if names == item.arg:
                return None, 0, 0
            copies = sum(materializer is not None for materializer in mats)
            return [Instr("LOAD_FAST_LOAD_FAST", names, location=item.location)], 0, copies
        output: list[Instr] = []
        constants = copies = 0
        for name, materializer in zip(item.arg, mats):
            if materializer is None:
                output.append(Instr("LOAD_FAST", name, location=item.location))
            elif materializer[0] == "const":
                output.append(Instr("LOAD_CONST", materializer[1], location=item.location))
                constants += 1
            else:
                output.append(Instr("LOAD_FAST", materializer[1], location=item.location))
                copies += 1
        return output, constants, copies
    return None, 0, 0


def _cfg_transfer_block(
    items: list[Any],
    block: _BytecodeBlock,
    entry: dict[str, _CfgValueFact],
    *,
    tracked_names: set[str],
) -> dict[str, _CfgValueFact]:
    state = dict(entry)
    for index in range(block.start, block.end):
        item = items[index]
        if isinstance(item, (TryBegin, TryEnd)):
            state.clear()
            continue
        _cfg_kill_or_assign(items, index, state, tracked_names=tracked_names)
    if block.has_hard_barrier:
        # Backedges/exception-region boundaries never export loop/protected facts.
        return {}
    return state


def _cfg_region_states(
    items: list[Any], tracked_names: set[str], max_rounds: int = 64
) -> tuple[
    list[_BytecodeBlock],
    list[dict[str, _CfgValueFact]],
    list[dict[str, _CfgValueFact]],
    int,
    int,
    int,
    int,
    int,
    int,
]:
    """Solve must-value facts over branches and reducible loop backedges.

    Version 0.13 preserved constants and outside-loop value versions around a
    backedge.  The 0.14 solver adds a second, symbolic pass for exact-integer affine
    induction variables.  The symbolic recurrence is injected only after a plain
    fixed point proves an exact preheader start and a structurally unique ``x += c``
    or ``x -= c`` update.  It therefore never treats an iteration-varying induction
    value as a concrete constant.
    """

    blocks, index_to_block = _build_bytecode_blocks(items)
    if not blocks:
        return [], [], [], 0, 0, 0, 0, 0, 0
    natural_loops = _cfg_natural_loops(blocks)
    loop_headers = {loop.header for loop in natural_loops.values()}

    def solve(
        recurrence_headers: dict[int, dict[str, _CfgValueFact]] | None = None,
    ) -> tuple[
        list[dict[str, _CfgValueFact]],
        list[dict[str, _CfgValueFact]],
        list[bool],
        int,
    ]:
        entries: list[dict[str, _CfgValueFact]] = [{} for _ in blocks]
        exits: list[dict[str, _CfgValueFact]] = [{} for _ in blocks]
        initialized: list[bool] = [False for _ in blocks]
        rounds = 0

        for _ in range(max_rounds):
            changed = False
            for block in blocks:
                if block.ident == 0:
                    incoming: list[dict[str, _CfgValueFact]] = [{}]
                else:
                    incoming = []
                    for predecessor in block.predecessors:
                        loop = natural_loops.get((predecessor, block.ident))
                        if loop is not None:
                            if not initialized[predecessor]:
                                continue
                            filtered, _ = _cfg_filter_backedge_state(
                                exits[predecessor], loop, index_to_block
                            )
                            incoming.append(filtered)
                        else:
                            incoming.append(
                                exits[predecessor] if initialized[predecessor] else {}
                            )
                new_entry = _cfg_join_states(incoming) if incoming else {}
                if recurrence_headers is not None:
                    # A recurrence fact represents a set/property valid for every
                    # reachable iteration, not the current concrete value.  It can
                    # therefore widen the preheader constant and latch recurrence at
                    # the natural-loop header without violating must semantics.
                    for name, fact in recurrence_headers.get(block.ident, {}).items():
                        new_entry[name] = fact
                new_exit = _cfg_transfer_block(
                    items, block, new_entry, tracked_names=tracked_names
                )
                if (
                    new_entry != entries[block.ident]
                    or new_exit != exits[block.ident]
                    or not initialized[block.ident]
                ):
                    entries[block.ident] = new_entry
                    exits[block.ident] = new_exit
                    initialized[block.ident] = True
                    changed = True
            rounds += 1
            if not changed:
                break
        return entries, exits, initialized, rounds

    # First establish ordinary preheader facts.  Recurrence discovery never guesses a
    # start value from source or syntax; it consumes the actual must-state produced by
    # the same bytecode transfer engine used by the optimizer.
    entries, exits, initialized, plain_rounds = solve(None)
    recurrence_headers = _cfg_detect_affine_recurrences(
        items, blocks, natural_loops, exits, initialized
    )
    if recurrence_headers:
        entries, exits, initialized, recurrence_rounds = solve(recurrence_headers)
        rounds = plain_rounds + recurrence_rounds
    else:
        rounds = plain_rounds

    merge_facts = 0
    for block in blocks:
        if len(block.predecessors) > 1:
            merge_facts += len(entries[block.ident])

    loop_invariant_facts = sum(len(entries[header]) for header in loop_headers)
    loop_variant_kills = 0
    for (latch, header), loop in natural_loops.items():
        if not initialized[latch]:
            continue
        _filtered, killed = _cfg_filter_backedge_state(
            exits[latch], loop, index_to_block
        )
        loop_variant_kills += killed

    affine_recurrences = sum(len(facts) for facts in recurrence_headers.values())
    return (
        blocks,
        entries,
        exits,
        rounds,
        merge_facts,
        len(loop_headers),
        loop_invariant_facts,
        loop_variant_kills,
        affine_recurrences,
    )

def _cfg_wide_cross_inline_dataflow_once(
    func: F, *, enabled: bool = True, require_gain: bool = True
) -> tuple[F, _CfgRegionDataflowStats]:
    """Propagate equal values through forward CFG branch merges.

    This extends the straight-line 0.11 region pass with a conservative phi-like
    analysis.  Only facts equal on every incoming normal edge survive a merge.
    Remaining calls, exception markers, and loop/backward edges kill facts.  Stores
    are preserved, so ordinary caller-local bindings remain visible.
    """

    if not enabled:
        return func, _CfgRegionDataflowStats()
    tracked = set(getattr(func, "__inline_produced_result_locals__", ()))

    bc = Bytecode.from_code(func.__code__)
    items = list(bc)
    (
        blocks,
        entries,
        _exits,
        rounds,
        merge_facts,
        loop_headers,
        loop_invariant_facts,
        loop_variant_kills,
        affine_recurrences,
    ) = _cfg_region_states(items, tracked)
    if not blocks:
        return func, _CfgRegionDataflowStats()

    replacements: dict[int, list[Instr]] = {}
    constants = copies = recurrence_folds = 0
    consumed_by_recurrence: set[int] = set()
    for block in blocks:
        state = dict(entries[block.ident])
        for index in range(block.start, block.end):
            item = items[index]
            if isinstance(item, Instr) and index not in consumed_by_recurrence:
                recurrence_rewrite = _cfg_recurrence_sequence_rewrite(items, index, state)
                if recurrence_rewrite is not None:
                    width, replacement_items = recurrence_rewrite
                    replacements[index] = replacement_items
                    for consumed_index in range(index + 1, index + width):
                        replacements[consumed_index] = []
                        consumed_by_recurrence.add(consumed_index)
                    recurrence_folds += 1
                else:
                    replacement, const_count, copy_count = _cfg_rewrite_load(item, state)
                    if replacement is not None:
                        replacements[index] = replacement
                        constants += const_count
                        copies += copy_count
            _cfg_kill_or_assign(items, index, state, tracked_names=tracked)

    if not replacements:
        return func, _CfgRegionDataflowStats(
            rounds=rounds,
            merge_facts=merge_facts,
            loop_headers=loop_headers,
            loop_invariant_facts=loop_invariant_facts,
            loop_variant_kills=loop_variant_kills,
            affine_recurrences=affine_recurrences,
            recurrence_folds=recurrence_folds,
        )
    rebuilt_items: list[Any] = []
    for index, item in enumerate(items):
        rebuilt_items.extend(replacements.get(index, [item]))

    before_cleanup = list(rebuilt_items)
    rebuilt_items, binary_count = _fold_constant_binary_ops(rebuilt_items)
    rebuilt_items, compare_count = _fold_constant_comparisons(rebuilt_items)
    rebuilt_items, branch_count = _fold_constant_branches(rebuilt_items)
    rebuilt_items, prune_count = _prune_unreachable_items(rebuilt_items)
    rebuilt_items, jump_count = _remove_redundant_jumps(rebuilt_items)

    bc.clear()
    bc.extend(rebuilt_items)
    try:
        rebuilt = _rebuild_function(func, bc)
        verify_code(rebuilt.__code__)
    except (BytecodeVerificationError, ValueError, RuntimeError):
        return func, _CfgRegionDataflowStats()

    if require_gain and not (
        len(rebuilt.__code__.co_code) < len(func.__code__.co_code)
        or binary_count
        or compare_count
        or branch_count
        or prune_count
        or jump_count
    ):
        return func, _CfgRegionDataflowStats(
            rounds=rounds,
            merge_facts=merge_facts,
            loop_headers=loop_headers,
            loop_invariant_facts=loop_invariant_facts,
            loop_variant_kills=loop_variant_kills,
            affine_recurrences=affine_recurrences,
            recurrence_folds=recurrence_folds,
        )

    if branch_count:
        rebuilt.__inline_constant_branches_folded__ = (
            int(getattr(func, "__inline_constant_branches_folded__", 0)) + branch_count
        )
    if compare_count:
        rebuilt.__inline_constant_comparisons_folded__ = (
            int(getattr(func, "__inline_constant_comparisons_folded__", 0)) + compare_count
        )
    if binary_count:
        rebuilt.__inline_constant_binary_ops_folded__ = (
            int(getattr(func, "__inline_constant_binary_ops_folded__", 0)) + binary_count
        )
    if prune_count:
        rebuilt.__inline_dead_instructions_pruned__ = (
            int(getattr(func, "__inline_dead_instructions_pruned__", 0)) + prune_count
        )
    if jump_count:
        rebuilt.__inline_redundant_jumps_removed__ = (
            int(getattr(func, "__inline_redundant_jumps_removed__", 0)) + jump_count
        )
    return rebuilt, _CfgRegionDataflowStats(
        rounds,
        merge_facts,
        constants,
        copies,
        branch_count,
        compare_count,
        binary_count,
        prune_count,
        jump_count,
        loop_headers,
        loop_invariant_facts,
        loop_variant_kills,
        affine_recurrences,
        recurrence_folds,
    )



def _cfg_wide_cross_inline_dataflow(
    func: F, *, enabled: bool = True, require_gain: bool = True, max_rounds: int = 8
) -> tuple[F, _CfgRegionDataflowStats]:
    """Run CFG merge propagation to a bounded structural fixed point.

    A first round may fold a branch and remove an incoming path.  Rebuilding the CFG
    then exposes stronger phi-like equalities at a downstream merge.  Each accepted
    speed-policy round must still create an objective structural gain, so the loop is
    naturally monotone in code/CFG complexity and remains explicitly bounded.
    """

    if not enabled:
        return func, _CfgRegionDataflowStats()
    current = func
    total = _CfgRegionDataflowStats()
    accepted = 0
    for _ in range(max_rounds):
        rebuilt, stats = _cfg_wide_cross_inline_dataflow_once(
            current, enabled=True, require_gain=require_gain
        )
        if rebuilt is current:
            # Preserve analysis diagnostics from the first no-rewrite solve only when
            # no prior round was accepted; otherwise the accepted rounds are the
            # useful optimization report.
            if accepted == 0:
                total = stats
            break
        accepted += 1
        current = rebuilt
        total = _CfgRegionDataflowStats(
            total.rounds + stats.rounds,
            total.merge_facts + stats.merge_facts,
            total.constant_propagations + stats.constant_propagations,
            total.copy_propagations + stats.copy_propagations,
            total.branches_folded + stats.branches_folded,
            total.comparisons_folded + stats.comparisons_folded,
            total.binary_ops_folded + stats.binary_ops_folded,
            total.dead_pruned + stats.dead_pruned,
            total.redundant_jumps + stats.redundant_jumps,
            max(total.loop_headers, stats.loop_headers),
            max(total.loop_invariant_facts, stats.loop_invariant_facts),
            total.loop_variant_kills + stats.loop_variant_kills,
            max(total.affine_recurrences, stats.affine_recurrences),
            total.recurrence_folds + stats.recurrence_folds,
        )
    return current, total


def _coalesce_inline_local_slots(func: F) -> tuple[F, int, int]:
    """Color non-overlapping synthetic fast-local *segments* onto shared slots.

    Version 0.9 extends the old one-interval-per-name allocator.  A synthetic local
    may now have several definition/use segments after live-range splitting (for
    example ``write -> reads -> reload-to-stack -> write -> later reads``).  Each
    definite write starts a new segment and kills the previous local-backed value.
    Those segments are colored independently, allowing the physical fast-local slot
    to be reused during stack-resident holes.

    The allocator remains deliberately straight-line only: exception markers,
    backward jumps, checked loads, deletes, and maybe-null stores disable the pass.
    User locals are never renamed.
    """

    bc = Bytecode.from_code(func.__code__)
    items = list(bc)
    if any(isinstance(item, (TryBegin, TryEnd)) for item in items):
        return func, 0, 0
    if any(
        isinstance(item, Instr) and item.name.startswith("JUMP_BACKWARD")
        for item in items
    ):
        return func, 0, 0

    refs: dict[str, list[tuple[int, str]]] = {}
    blocked_names: set[str] = set()
    for index, item in enumerate(items):
        for name, kind in _fast_accesses(item):
            if not isinstance(name, str) or not name.startswith("__inl_"):
                continue
            refs.setdefault(name, []).append((index, kind))
            if kind not in {"read", "write"}:
                blocked_names.add(name)
            if isinstance(item, Instr) and item.name in {
                "LOAD_FAST_CHECK",
                "LOAD_FAST_AND_CLEAR",
                "DELETE_FAST",
                "STORE_FAST_MAYBE_NULL",
            }:
                blocked_names.add(name)

    # (start, end, logical name, segment ordinal, access indexes)
    segments: list[tuple[int, int, str, int, tuple[int, ...]]] = []
    eligible_names: set[str] = set()
    segmented_extra = 0
    for name, accesses in refs.items():
        if name in blocked_names:
            continue
        ordered = sorted(accesses)
        if not ordered or ordered[0][1] != "write":
            continue

        name_segments: list[tuple[int, int, str, int, tuple[int, ...]]] = []
        current_start: int | None = None
        current_accesses: list[int] = []
        ordinal = 0
        valid = True
        for index, kind in ordered:
            if kind == "write":
                if current_start is not None:
                    name_segments.append(
                        (
                            current_start,
                            current_accesses[-1],
                            name,
                            ordinal,
                            tuple(current_accesses),
                        )
                    )
                    ordinal += 1
                current_start = index
                current_accesses = [index]
            elif kind == "read":
                if current_start is None:
                    valid = False
                    break
                current_accesses.append(index)
            else:
                valid = False
                break
        if not valid or current_start is None:
            continue
        name_segments.append(
            (
                current_start,
                current_accesses[-1],
                name,
                ordinal,
                tuple(current_accesses),
            )
        )
        segments.extend(name_segments)
        eligible_names.add(name)
        segmented_extra += max(0, len(name_segments) - 1)

    if len(segments) < 2:
        return func, 0, segmented_extra

    segments.sort(key=lambda value: (value[0], value[1], value[2], value[3]))
    # Interval-partitioning is optimal for straight-line lexical intervals.  Reuse a
    # physical slot only after the previous segment's final access has passed.
    color_end: list[int] = []
    segment_color: dict[tuple[str, int], int] = {}
    for start, end, name, ordinal, _accesses in segments:
        chosen: int | None = None
        for color, last_end in enumerate(color_end):
            if last_end < start:
                chosen = color
                break
        if chosen is None:
            chosen = len(color_end)
            color_end.append(end)
        else:
            color_end[chosen] = end
        segment_color[(name, ordinal)] = chosen

    # A segmented allocator is useful only if it decreases the number of physical
    # synthetic slots.  Otherwise keep the original names for clearer debugging.
    savings = len(eligible_names) - len(color_end)
    if savings <= 0:
        return func, 0, segmented_extra

    token = _next_counter()
    slot_names = {
        color: f"__inl_slot_{token}_{color}" for color in range(len(color_end))
    }
    access_mapping: dict[tuple[int, str], str] = {}
    for _start, _end, name, ordinal, access_indexes in segments:
        slot = slot_names[segment_color[(name, ordinal)]]
        for index in access_indexes:
            access_mapping[(index, name)] = slot

    rewritten: list[Any] = []
    for index, item in enumerate(items):
        if isinstance(item, Instr) and "FAST" in item.name:
            if isinstance(item.arg, tuple):
                mapped = tuple(
                    access_mapping.get((index, value), value)
                    if isinstance(value, str)
                    else value
                    for value in item.arg
                )
            elif isinstance(item.arg, str):
                mapped = access_mapping.get((index, item.arg), item.arg)
            else:
                mapped = item.arg
            if mapped != item.arg:
                item = Instr(item.name, mapped, location=item.location)
        rewritten.append(item)

    bc.clear()
    bc.extend(rewritten)
    try:
        rebuilt = _rebuild_function(func, bc)
        verify_code(rebuilt.__code__)
    except (BytecodeVerificationError, ValueError, RuntimeError):
        return func, 0, segmented_extra
    return rebuilt, savings, segmented_extra


def _rebuild_function(func: F, bytecode: Bytecode) -> F:
    new_code = bytecode.to_code()
    rebuilt = types.FunctionType(
        new_code,
        func.__globals__,
        func.__name__,
        func.__defaults__,
        func.__closure__,
    )
    rebuilt.__kwdefaults__ = (
        None if func.__kwdefaults__ is None else dict(func.__kwdefaults__)
    )
    rebuilt.__annotations__ = dict(getattr(func, "__annotations__", {}))
    rebuilt.__dict__.update(func.__dict__)
    rebuilt.__module__ = func.__module__
    rebuilt.__qualname__ = func.__qualname__
    rebuilt.__doc__ = func.__doc__
    if hasattr(func, "__type_params__"):
        rebuilt.__type_params__ = func.__type_params__
    return rebuilt  # type: ignore[return-value]


def _callee_has_exception_markers(func: Callable[..., Any]) -> bool:
    return any(
        isinstance(item, (TryBegin, TryEnd)) for item in Bytecode.from_code(func.__code__)
    )


def _count_protected_nested_try_calls(func: Callable[..., Any]) -> int:
    bc = Bytecode.from_code(func.__code__)
    items = list(bc)
    contexts = _instruction_exception_contexts(items)
    return sum(
        bool(contexts.get(site.call_index)) and _callee_has_exception_markers(site.callee)
        for site in _all_direct_calls(bc, func)
    )


def _inline_once(
    func: F,
    *,
    policy: str,
    stack_strategy: str,
    binding: str,
) -> tuple[F, bool, Callable[..., Any] | None, int]:
    bc = Bytecode.from_code(func.__code__)
    excluded: set[int] = set()
    skipped = 0

    while True:
        site = _find_direct_call(bc, func, excluded)
        if site is None:
            return func, False, None, skipped

        callee = site.callee
        if (
            callee.__globals__ is not func.__globals__
            and not getattr(callee, "__inline_freeze_globals__", False)
        ):
            raise InlineUnsupportedError(
                f"{callee.__qualname__} and {func.__qualname__} use different globals; "
                "register the callee with freeze_globals=True to capture them"
            )

        prefix, reusable_prefixes = _inline_local_prefix(func, callee)
        items = list(bc)
        if (
            _instruction_exception_contexts(items).get(site.call_index)
            and _callee_has_exception_markers(callee)
        ):
            # ``bytecode`` forbids nested TryBegin pseudo instructions.  Retain the
            # original CALL rather than failing decoration or generating malformed IR.
            excluded.add(site.call_index)
            continue

        # Preserve all explicit argument-producing instructions in original evaluation
        # order. Remove only the callable prefix and CALL_KW's keyword-name tuple.
        argument_end = site.kw_names_index if site.kw_names_index is not None else site.call_index
        produced_result_local: str | None = None
        if site.call_index + 1 < len(items):
            following = items[site.call_index + 1]
            if (
                isinstance(following, Instr)
                and following.name == "STORE_FAST"
                and isinstance(following.arg, str)
            ):
                produced_result_local = following.arg
            elif (
                isinstance(following, Instr)
                and following.name == "STORE_FAST_LOAD_FAST"
                and isinstance(following.arg, tuple)
                and len(following.arg) == 2
                and isinstance(following.arg[0], str)
            ):
                # CPython 3.13 can fuse the result store with the caller's next
                # local load.  The first packed operand is still the call-result
                # destination and must participate in cross-inline dataflow.
                produced_result_local = following.arg[0]

        # Exact implicit receivers and omitted defaults are compile-time objects. Replace
        # their read-only parameter loads with LOAD_CONST and remove their synthetic
        # argument stores. Mutated parameters automatically fall back to ordinary binding.
        implicit_targets = site.binding.positional_targets[: len(site.implicit_values)]
        constant_candidates = dict(zip(implicit_targets, site.implicit_values))
        implicit_keyword_targets = site.binding.keyword_targets[: len(site.implicit_keywords)]
        for target, (_, value) in zip(implicit_keyword_targets, site.implicit_keywords):
            if target is not None:
                constant_candidates[target] = value
        constant_candidates.update(site.binding.defaults)
        source = Bytecode.from_code(callee.__code__)
        written_names = _fast_written_names(source)
        constant_parameters = {
            name: value for name, value in constant_candidates.items() if name not in written_names
        }
        explicit_argument_code = list(items[site.callable_end:argument_end])
        aliased_parameters = _caller_local_aliases(
            func, site, explicit_argument_code, written_names
        )
        always_bound_parameters = _always_bound_fast_locals(func)
        caller_parameter_alias_count = sum(
            name in always_bound_parameters for name in aliased_parameters.values()
        )
        caller_local_alias_count = len(aliased_parameters) - caller_parameter_alias_count

        runtime_implicit_values = [
            value
            for target, value in zip(implicit_targets, site.implicit_values)
            if target not in constant_parameters
        ]
        runtime_implicit_keywords = [
            value
            for target, (_, value) in zip(implicit_keyword_targets, site.implicit_keywords)
            if target is None or target not in constant_parameters
        ]
        argument_code: list[Any] = [Instr("LOAD_CONST", value) for value in runtime_implicit_values]
        if not aliased_parameters:
            argument_code.extend(explicit_argument_code)
        argument_code.extend(Instr("LOAD_CONST", value) for value in runtime_implicit_keywords)

        total_parameters = callee.__code__.co_argcount + callee.__code__.co_kwonlyargcount
        parameter_order = tuple(
            name
            for name in callee.__code__.co_varnames[:total_parameters]
            if name not in constant_parameters and name not in aliased_parameters
        )
        runtime_positional_targets = tuple(
            name
            for name in site.binding.positional_targets
            if name not in constant_parameters and name not in aliased_parameters
        )
        runtime_keyword_targets = tuple(
            name for name in site.binding.keyword_targets if name is not None and name not in constant_parameters
        )
        runtime_defaults = tuple(
            (target, value)
            for target, value in site.binding.defaults
            if target not in constant_parameters
        )
        combined_targets = runtime_positional_targets + runtime_keyword_targets + tuple(
            target for target, _ in runtime_defaults
        )
        forward_parameters: tuple[str, ...] = ()
        if (
            site.binding.extra_positional_count == 0
            and site.binding.vararg_name is None
            and site.binding.varkw_name is None
            and combined_targets == parameter_order
        ):
            forward_parameters = combined_targets

        (
            body,
            local_map,
            forwarded,
            folded,
            pruned,
            redundant_jumps,
            comparisons_folded,
            binary_ops_folded,
            roundtrips_elided,
            copies_propagated,
            constants_propagated,
            unary_ops_folded,
            stack_schedule,
        ) = _clone_callee(
            callee,
            prefix,
            forward_parameters,
            constant_parameters,
            aliased_parameters,
            stack_strategy=stack_strategy,
        )
        if forwarded:
            # Defaults are existing objects, not evaluated expressions, so appending them
            # after explicit argument evaluation preserves call semantics.
            argument_code.extend(Instr("LOAD_CONST", value) for _, value in runtime_defaults)
        replacement: list[Any] = list(argument_code)
        if not forwarded:
            # Keyword values occupy the top of the argument stack. Pop them in reverse
            # source order, collecting unknown names for **kwargs without re-evaluation.
            varkw_pairs: list[tuple[str, str]] = []
            for name, target in reversed(tuple(zip(site.binding.keyword_names, site.binding.keyword_targets))):
                if target is None:
                    temp = f"{prefix}_kw_{len(varkw_pairs)}"
                    replacement.append(Instr("STORE_FAST", temp))
                    varkw_pairs.append((name, temp))
                else:
                    if target not in constant_parameters:
                        replacement.append(Instr("STORE_FAST", local_map[target]))

            if site.binding.extra_positional_count:
                replacement.append(Instr("BUILD_TUPLE", site.binding.extra_positional_count))
                replacement.append(Instr("STORE_FAST", local_map[site.binding.vararg_name]))
            elif site.binding.vararg_name is not None:
                replacement.append(Instr("LOAD_CONST", ()))
                replacement.append(Instr("STORE_FAST", local_map[site.binding.vararg_name]))

            for target in reversed(runtime_positional_targets):
                replacement.append(Instr("STORE_FAST", local_map[target]))

            if site.binding.varkw_name is not None:
                if varkw_pairs:
                    # Values were popped in reverse order; restore source order before
                    # BUILD_CONST_KEY_MAP so insertion order matches a normal call.
                    ordered = list(reversed(varkw_pairs))
                    for _, temp in ordered:
                        replacement.append(Instr("LOAD_FAST", temp))
                    replacement.append(Instr("LOAD_CONST", tuple(name for name, _ in ordered)))
                    replacement.append(Instr("BUILD_CONST_KEY_MAP", len(ordered)))
                else:
                    replacement.append(Instr("BUILD_MAP", 0))
                replacement.append(Instr("STORE_FAST", local_map[site.binding.varkw_name]))

        # Omitted defaults are the original default objects, not re-evaluated expressions.
        if not forwarded:
            for target, value in runtime_defaults:
                replacement.append(Instr("LOAD_CONST", value))
                replacement.append(Instr("STORE_FAST", local_map[target]))

        replacement, body, late_forwarded = _late_stack_forward(replacement, body)

        body_optimization_credit = (
            int(folded)
            + int(pruned)
            + int(redundant_jumps)
            + int(comparisons_folded)
            + int(binary_ops_folded)
            + int(unary_ops_folded)
        )
        guarded_body_credit = (
            min(4, body_optimization_credit) if site.guarded_closure else 0
        )
        guard_cost = _guard_hot_path_cost(site) if binding == "guarded" else 0

        if policy == "speed" and not _is_profitable_setup(
            items=items,
            site=site,
            explicit_argument_code=explicit_argument_code,
            replacement_setup=replacement,
            optimization_credit=(
                body_optimization_credit if binding == "guarded" else guarded_body_credit
            ),
            guard_cost=guard_cost,
        ):
            excluded.add(site.call_index)
            skipped += 1
            continue

        replacement.extend(body)

        if binding == "guarded":
            guarded = _guarded_site_replacement(site, items, replacement)
            if guarded is None:
                # A target shape we cannot validate without invoking user code is
                # left as an ordinary CALL in the semantics-preserving mode.
                excluded.add(site.call_index)
                skipped += 1
                continue
            replacement = guarded
        elif site.guarded_closure:
            # Frozen mode retains the historical closure-identity guard so a nonlocal
            # rebind still falls back to the exact callable loaded for this invocation.
            source_load = items[site.start]
            assert isinstance(source_load, Instr) and source_load.name == "LOAD_DEREF"
            fallback = Label()
            done = Label()
            location = source_load.location
            original_tail = list(items[site.callable_end : site.call_index + 1])
            guarded: list[Any] = [
                Instr("LOAD_DEREF", source_load.arg, location=location),
                Instr("COPY", 1, location=location),
                Instr("LOAD_CONST", site.guarded_identity, location=location),
                Instr("IS_OP", 0, location=location),
                Instr("POP_JUMP_IF_FALSE", fallback, location=location),
                Instr("POP_TOP", location=location),
                *replacement,
                Instr("JUMP_FORWARD", done, location=location),
                fallback,
                Instr("PUSH_NULL", location=location),
                *original_tail,
                done,
            ]
            replacement = guarded

        new_items = items[: site.start] + replacement + items[site.call_index + 1 :]
        bc.clear()
        bc.extend(new_items)
        rebuilt = _rebuild_function(func, bc)
        produced_locals = set(
            getattr(func, "__inline_produced_result_locals__", ())
        )
        if produced_result_local is not None:
            produced_locals.add(produced_result_local)
        if produced_locals:
            rebuilt.__inline_produced_result_locals__ = tuple(sorted(produced_locals))
        if reusable_prefixes:
            rebuilt.__inline_reused_local_prefixes__ = reusable_prefixes
        if site.guarded_closure:
            rebuilt.__inline_guarded_closure_calls__ = (
                int(getattr(func, "__inline_guarded_closure_calls__", 0)) + 1
            )
            if policy == "speed":
                rebuilt.__inline_guarded_closure_speed_accepted__ = (
                    int(getattr(func, "__inline_guarded_closure_speed_accepted__", 0)) + 1
                )
                rebuilt.__inline_guarded_closure_body_credit__ = (
                    int(getattr(func, "__inline_guarded_closure_body_credit__", 0))
                    + guarded_body_credit
                )
        if folded:
            rebuilt.__inline_constant_branches_folded__ = (
                int(getattr(func, "__inline_constant_branches_folded__", 0)) + folded
            )
        if pruned:
            rebuilt.__inline_dead_instructions_pruned__ = (
                int(getattr(func, "__inline_dead_instructions_pruned__", 0)) + pruned
            )
        if late_forwarded:
            rebuilt.__inline_late_stack_forwards__ = (
                int(getattr(func, "__inline_late_stack_forwards__", 0)) + late_forwarded
            )
        if redundant_jumps:
            rebuilt.__inline_redundant_jumps_removed__ = (
                int(getattr(func, "__inline_redundant_jumps_removed__", 0)) + redundant_jumps
            )
        if caller_parameter_alias_count:
            rebuilt.__inline_caller_parameter_aliases__ = (
                int(getattr(func, "__inline_caller_parameter_aliases__", 0))
                + caller_parameter_alias_count
            )
        if caller_local_alias_count:
            rebuilt.__inline_caller_local_aliases__ = (
                int(getattr(func, "__inline_caller_local_aliases__", 0))
                + caller_local_alias_count
            )
        if comparisons_folded:
            rebuilt.__inline_constant_comparisons_folded__ = (
                int(getattr(func, "__inline_constant_comparisons_folded__", 0))
                + comparisons_folded
            )
        if binary_ops_folded:
            rebuilt.__inline_constant_binary_ops_folded__ = (
                int(getattr(func, "__inline_constant_binary_ops_folded__", 0))
                + binary_ops_folded
            )
        if unary_ops_folded:
            rebuilt.__inline_constant_unary_ops_folded__ = (
                int(getattr(func, "__inline_constant_unary_ops_folded__", 0))
                + unary_ops_folded
            )
        if roundtrips_elided:
            rebuilt.__inline_synthetic_roundtrips_elided__ = (
                int(getattr(func, "__inline_synthetic_roundtrips_elided__", 0))
                + roundtrips_elided
            )
        if copies_propagated:
            rebuilt.__inline_synthetic_copies_propagated__ = (
                int(getattr(func, "__inline_synthetic_copies_propagated__", 0))
                + copies_propagated
            )
        if constants_propagated:
            rebuilt.__inline_synthetic_constants_propagated__ = (
                int(getattr(func, "__inline_synthetic_constants_propagated__", 0))
                + constants_propagated
            )
        if stack_schedule.scheduled:
            rebuilt.__inline_stack_resident_values__ = (
                int(getattr(func, "__inline_stack_resident_values__", 0))
                + stack_schedule.scheduled
            )
        if stack_schedule.candidates:
            rebuilt.__inline_stack_scheduler_candidates__ = (
                int(getattr(func, "__inline_stack_scheduler_candidates__", 0))
                + stack_schedule.candidates
            )
        if stack_schedule.spilled:
            rebuilt.__inline_stack_spilled_values__ = (
                int(getattr(func, "__inline_stack_spilled_values__", 0))
                + stack_schedule.spilled
            )
        if stack_schedule.conflicts:
            rebuilt.__inline_stack_crossing_conflicts__ = (
                int(getattr(func, "__inline_stack_crossing_conflicts__", 0))
                + stack_schedule.conflicts
            )
        if stack_schedule.max_copy_depth:
            rebuilt.__inline_stack_max_copy_depth__ = max(
                int(getattr(func, "__inline_stack_max_copy_depth__", 0)),
                stack_schedule.max_copy_depth,
            )
        if stack_schedule.instruction_savings:
            rebuilt.__inline_stack_instruction_savings__ = (
                int(getattr(func, "__inline_stack_instruction_savings__", 0))
                + stack_schedule.instruction_savings
            )
        if stack_schedule.dependency_edges:
            rebuilt.__inline_stack_dependency_edges__ = (
                int(getattr(func, "__inline_stack_dependency_edges__", 0))
                + stack_schedule.dependency_edges
            )
        if stack_schedule.peak_resident_values:
            rebuilt.__inline_stack_peak_resident_values__ = max(
                int(getattr(func, "__inline_stack_peak_resident_values__", 0)),
                stack_schedule.peak_resident_values,
            )
        if stack_schedule.split_values:
            rebuilt.__inline_stack_split_values__ = (
                int(getattr(func, "__inline_stack_split_values__", 0))
                + stack_schedule.split_values
            )
        if stack_schedule.split_reads:
            rebuilt.__inline_stack_split_reads__ = (
                int(getattr(func, "__inline_stack_split_reads__", 0))
                + stack_schedule.split_reads
            )
        if stack_schedule.split_instruction_cost:
            rebuilt.__inline_stack_split_instruction_cost__ = (
                int(getattr(func, "__inline_stack_split_instruction_cost__", 0))
                + stack_schedule.split_instruction_cost
            )
        if stack_schedule.middle_splits:
            rebuilt.__inline_stack_middle_splits__ = (
                int(getattr(func, "__inline_stack_middle_splits__", 0))
                + stack_schedule.middle_splits
            )
        return rebuilt, True, callee, skipped

def _direct_registered_callees(func: Callable[..., Any]) -> tuple[Callable[..., Any], ...]:
    """Collect direct registered callees without mutating the function."""

    bc = Bytecode.from_code(func.__code__)
    current = func
    found: list[Callable[..., Any]] = []
    # Work on successively masked call sites so repeated calls are discovered without
    # performing an inline expansion.
    items = list(bc)
    for call_index, item in enumerate(items):
        if not isinstance(item, Instr) or item.name not in {"CALL", "CALL_KW"}:
            continue
        start = _call_region_start(items, call_index)
        if start is None:
            continue
        resolved = _parse_callable_prefix(current, items, start, call_index)
        if resolved is not None:
            found.append(resolved[0])
    return tuple(found)


def _assert_acyclic(func: Callable[..., Any]) -> None:
    """Reject direct or mutual cycles using function identity, not global names.

    Name-based keys falsely classify an unrelated root function as recursive when it
    happens to share a name with a registered callee. Identity is the semantic node.
    """

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(node: Callable[..., Any]) -> None:
        identity = id(node)
        if identity in visiting:
            raise InlineRecursionError(
                f"recursive inline cycle detected at {node.__module__}.{node.__qualname__}"
            )
        if identity in visited:
            return
        visiting.add(identity)
        for callee in _direct_registered_callees(node):
            visit(callee)
        visiting.remove(identity)
        visited.add(identity)

    visiting.add(id(func))
    for callee in _direct_registered_callees(func):
        visit(callee)
    visiting.remove(id(func))


def _compile(
    func: F,
    *,
    policy: str = "speed",
    binding: str = "frozen",
    max_expansions: int = _DEFAULT_MAX_EXPANSIONS,
    max_growth_factor: int = _DEFAULT_MAX_GROWTH_FACTOR,
    max_code_bytes: int = _DEFAULT_MAX_CODE_BYTES,
    shared_regions: bool | str = "auto",
    shared_min_calls: int = 3,
    shared_min_body_instructions: int = 12,
    stack_strategy: str = "auto",
    fusion_strategy: str = "auto",
    region_dataflow: bool = True,
) -> F:
    _require_runtime()
    _assert_acyclic(func)
    policy = _validate_policy(policy)
    binding = _validate_binding(binding)
    stack_strategy = _validate_stack_strategy(stack_strategy, policy)
    fusion_strategy = _validate_fusion_strategy(fusion_strategy, policy)
    func.__inline_root_varnames__ = tuple(func.__code__.co_varnames)

    if max_expansions < 0 or max_growth_factor < 1 or max_code_bytes < 1:
        raise ValueError("invalid inline expansion limits")

    original_size = len(func.__code__.co_code)
    growth_limit = min(max_code_bytes, max(original_size, 1) * max_growth_factor)
    if shared_min_calls < 2:
        raise ValueError("shared_min_calls must be at least 2")
    if shared_min_body_instructions < 1:
        raise ValueError("shared_min_body_instructions must be positive")
    _shared_region_policy(shared_regions)

    current: F = func
    count = 0
    shared_calls = 0
    shared_region_count = 0
    while True:
        current, shared, _shared_callee = _share_one_region(
            current,
            binding=binding,
            shared_regions=shared_regions,
            min_calls=shared_min_calls,
            min_body_instructions=shared_min_body_instructions,
            stack_strategy=stack_strategy,
        )
        if not shared:
            break
        shared_calls += shared
        shared_region_count += 1
        current_size = len(current.__code__.co_code)
        if current_size > growth_limit:
            raise InlineExpansionError(
                f"shared inline regions grew code to {current_size} bytes; limit is {growth_limit}"
            )

    while True:
        current, changed, _, _ = _inline_once(
            current, policy=policy, stack_strategy=stack_strategy, binding=binding
        )
        if not changed:
            break
        count += 1
        if count > max_expansions:
            raise InlineExpansionError(
                f"inline expansion count exceeded {max_expansions}"
            )
        current_size = len(current.__code__.co_code)
        if current_size > growth_limit:
            raise InlineExpansionError(
                f"inlined code grew to {current_size} bytes; limit is {growth_limit}"
            )

    skipped_unsupported = _count_protected_nested_try_calls(current)
    remaining_calls = len(_direct_registered_callees(current))
    skipped_total = (
        max(0, remaining_calls - skipped_unsupported) if policy == "speed" else 0
    )
    reused_prefixes = tuple(
        getattr(current, "__inline_reused_local_prefixes__", {}).values()
    )
    reused_local_groups = sum(
        any(name.startswith(prefix + "_") for name in current.__code__.co_varnames)
        for prefix in reused_prefixes
    )
    current, fusion_stats = _fuse_inline_result_handoffs(
        current, strategy=fusion_strategy
    )
    current, region_stats = _whole_region_cross_inline_dataflow(
        current, enabled=bool(region_dataflow), require_gain=(policy == "speed")
    )
    current, cfg_region_stats = _cfg_wide_cross_inline_dataflow(
        current, enabled=bool(region_dataflow), require_gain=(policy == "speed")
    )
    current, strength_stats = _cfg_strength_reduce_affine_expressions(
        current, enabled=bool(region_dataflow), require_gain=(policy == "speed")
    )
    current, coalesced_local_slots, segmented_local_lifetimes = (
        _coalesce_inline_local_slots(current)
    )
    constant_branches_folded = int(
        getattr(current, "__inline_constant_branches_folded__", 0)
    )
    dead_instructions_pruned = int(
        getattr(current, "__inline_dead_instructions_pruned__", 0)
    )
    late_stack_forwards = int(
        getattr(current, "__inline_late_stack_forwards__", 0)
    )
    redundant_jumps_removed = int(
        getattr(current, "__inline_redundant_jumps_removed__", 0)
    )
    caller_parameter_aliases = int(
        getattr(current, "__inline_caller_parameter_aliases__", 0)
    )
    caller_local_aliases = int(
        getattr(current, "__inline_caller_local_aliases__", 0)
    )
    constant_comparisons_folded = int(
        getattr(current, "__inline_constant_comparisons_folded__", 0)
    )
    constant_binary_ops_folded = int(
        getattr(current, "__inline_constant_binary_ops_folded__", 0)
    )
    constant_unary_ops_folded = int(
        getattr(current, "__inline_constant_unary_ops_folded__", 0)
    )
    guarded_closure_calls = int(
        getattr(current, "__inline_guarded_closure_calls__", 0)
    )
    guarded_closure_speed_accepted = int(
        getattr(current, "__inline_guarded_closure_speed_accepted__", 0)
    )
    guarded_closure_body_credit = int(
        getattr(current, "__inline_guarded_closure_body_credit__", 0)
    )
    protected_shared_regions = int(
        getattr(current, "__inline_protected_shared_regions__", 0)
    )
    synthetic_roundtrips_elided = int(
        getattr(current, "__inline_synthetic_roundtrips_elided__", 0)
    )
    synthetic_copies_propagated = int(
        getattr(current, "__inline_synthetic_copies_propagated__", 0)
    )
    synthetic_constants_propagated = int(
        getattr(current, "__inline_synthetic_constants_propagated__", 0)
    )
    stack_resident_values = int(
        getattr(current, "__inline_stack_resident_values__", 0)
    )
    stack_scheduler_candidates = int(
        getattr(current, "__inline_stack_scheduler_candidates__", 0)
    )
    stack_spilled_values = int(
        getattr(current, "__inline_stack_spilled_values__", 0)
    )
    stack_crossing_conflicts = int(
        getattr(current, "__inline_stack_crossing_conflicts__", 0)
    )
    stack_max_copy_depth = int(
        getattr(current, "__inline_stack_max_copy_depth__", 0)
    )
    stack_instruction_savings = int(
        getattr(current, "__inline_stack_instruction_savings__", 0)
    )
    stack_dependency_edges = int(
        getattr(current, "__inline_stack_dependency_edges__", 0)
    )
    stack_peak_resident_values = int(
        getattr(current, "__inline_stack_peak_resident_values__", 0)
    )
    stack_split_values = int(
        getattr(current, "__inline_stack_split_values__", 0)
    )
    stack_split_reads = int(
        getattr(current, "__inline_stack_split_reads__", 0)
    )
    stack_split_instruction_cost = int(
        getattr(current, "__inline_stack_split_instruction_cost__", 0)
    )
    stack_middle_splits = int(
        getattr(current, "__inline_stack_middle_splits__", 0)
    )
    fused_result_handoffs = fusion_stats.fused_handoffs
    constant_result_handoffs = fusion_stats.constant_handoffs
    aggressive_result_handoffs = fusion_stats.aggressive_handoffs
    region_dataflow_rounds = region_stats.rounds
    region_constant_propagations = region_stats.constant_propagations
    region_copy_propagations = region_stats.copy_propagations
    region_branches_folded = region_stats.branches_folded
    region_dead_instructions_pruned = region_stats.dead_pruned
    region_redundant_jumps_removed = region_stats.redundant_jumps
    cfg_dataflow_rounds = cfg_region_stats.rounds
    cfg_merge_facts = cfg_region_stats.merge_facts
    cfg_constant_propagations = cfg_region_stats.constant_propagations
    cfg_copy_propagations = cfg_region_stats.copy_propagations
    cfg_branches_folded = cfg_region_stats.branches_folded
    cfg_dead_instructions_pruned = cfg_region_stats.dead_pruned
    cfg_redundant_jumps_removed = cfg_region_stats.redundant_jumps
    cfg_loop_headers = cfg_region_stats.loop_headers
    cfg_loop_invariant_facts = cfg_region_stats.loop_invariant_facts
    cfg_loop_variant_kills = cfg_region_stats.loop_variant_kills
    cfg_affine_recurrences = cfg_region_stats.affine_recurrences
    cfg_recurrence_folds = cfg_region_stats.recurrence_folds
    cfg_strength_reduced_values = strength_stats.derived_values
    cfg_strength_reduced_uses = strength_stats.rewritten_uses
    cfg_strength_reduction_updates = strength_stats.update_sites
    cfg_strength_lazy_values = strength_stats.lazy_values
    cfg_strength_lazy_uses = strength_stats.lazy_rewritten_uses
    cfg_strength_lazy_materializations = strength_stats.lazy_materializations
    current.__inline_stats__ = InlineStats(
        count,
        original_size,
        len(current.__code__.co_code),
        skipped_total,
        skipped_unsupported,
        shared_calls,
        shared_region_count,
        reused_local_groups,
        constant_branches_folded,
        dead_instructions_pruned,
        late_stack_forwards,
        redundant_jumps_removed,
        caller_parameter_aliases,
        caller_local_aliases,
        constant_comparisons_folded,
        constant_binary_ops_folded,
        protected_shared_regions,
        synthetic_roundtrips_elided,
        synthetic_copies_propagated,
        synthetic_constants_propagated,
        coalesced_local_slots,
        stack_resident_values,
        stack_scheduler_candidates,
        stack_spilled_values,
        stack_crossing_conflicts,
        stack_max_copy_depth,
        stack_instruction_savings,
        stack_dependency_edges,
        stack_peak_resident_values,
        stack_split_values,
        stack_split_reads,
        stack_split_instruction_cost,
        stack_middle_splits,
        segmented_local_lifetimes,
        fused_result_handoffs,
        constant_result_handoffs,
        aggressive_result_handoffs,
        region_dataflow_rounds,
        region_constant_propagations,
        region_copy_propagations,
        region_branches_folded,
        region_dead_instructions_pruned,
        region_redundant_jumps_removed,
        cfg_dataflow_rounds,
        cfg_merge_facts,
        cfg_constant_propagations,
        cfg_copy_propagations,
        cfg_branches_folded,
        cfg_dead_instructions_pruned,
        cfg_redundant_jumps_removed,
        cfg_loop_headers,
        cfg_loop_invariant_facts,
        cfg_loop_variant_kills,
        cfg_affine_recurrences,
        cfg_recurrence_folds,
        cfg_strength_reduced_values,
        cfg_strength_reduced_uses,
        cfg_strength_reduction_updates,
        cfg_strength_lazy_values,
        cfg_strength_lazy_uses,
        cfg_strength_lazy_materializations,
        constant_unary_ops_folded,
        guarded_closure_calls,
        guarded_closure_speed_accepted,
        guarded_closure_body_credit,
    )
    current.__inline_original__ = func
    report = make_report(
        "inline",
        func.__code__,
        current.__code__,
        details=(("calls_inlined", count),
                 ("calls_skipped_unprofitable", skipped_total),
                 ("calls_skipped_unsupported", skipped_unsupported),
                 ("calls_shared", shared_calls),
                 ("shared_regions", shared_region_count),
                 ("reused_local_groups", reused_local_groups),
                 ("constant_branches_folded", constant_branches_folded),
                 ("dead_instructions_pruned", dead_instructions_pruned),
                 ("late_stack_forwards", late_stack_forwards),
                 ("redundant_jumps_removed", redundant_jumps_removed),
                 ("caller_parameter_aliases", caller_parameter_aliases),
                 ("caller_local_aliases", caller_local_aliases),
                 ("constant_comparisons_folded", constant_comparisons_folded),
                 ("constant_binary_ops_folded", constant_binary_ops_folded),
                 ("constant_unary_ops_folded", constant_unary_ops_folded),
                 ("guarded_closure_calls", guarded_closure_calls),
                 ("guarded_closure_speed_accepted", guarded_closure_speed_accepted),
                 ("guarded_closure_body_credit", guarded_closure_body_credit),
                 ("protected_shared_regions", protected_shared_regions),
                 ("synthetic_roundtrips_elided", synthetic_roundtrips_elided),
                 ("synthetic_copies_propagated", synthetic_copies_propagated),
                 ("synthetic_constants_propagated", synthetic_constants_propagated),
                 ("coalesced_local_slots", coalesced_local_slots),
                 ("stack_resident_values", stack_resident_values),
                 ("stack_scheduler_candidates", stack_scheduler_candidates),
                 ("stack_spilled_values", stack_spilled_values),
                 ("stack_crossing_conflicts", stack_crossing_conflicts),
                 ("stack_max_copy_depth", stack_max_copy_depth),
                 ("stack_instruction_savings", stack_instruction_savings),
                 ("stack_dependency_edges", stack_dependency_edges),
                 ("stack_peak_resident_values", stack_peak_resident_values),
                 ("stack_split_values", stack_split_values),
                 ("stack_split_reads", stack_split_reads),
                 ("stack_split_instruction_cost", stack_split_instruction_cost),
                 ("stack_middle_splits", stack_middle_splits),
                 ("segmented_local_lifetimes", segmented_local_lifetimes),
                 ("fused_result_handoffs", fused_result_handoffs),
                 ("constant_result_handoffs", constant_result_handoffs),
                 ("aggressive_result_handoffs", aggressive_result_handoffs),
                 ("region_dataflow_rounds", region_dataflow_rounds),
                 ("region_constant_propagations", region_constant_propagations),
                 ("region_copy_propagations", region_copy_propagations),
                 ("region_branches_folded", region_branches_folded),
                 ("region_dead_instructions_pruned", region_dead_instructions_pruned),
                 ("region_redundant_jumps_removed", region_redundant_jumps_removed),
                 ("cfg_dataflow_rounds", cfg_dataflow_rounds),
                 ("cfg_merge_facts", cfg_merge_facts),
                 ("cfg_constant_propagations", cfg_constant_propagations),
                 ("cfg_copy_propagations", cfg_copy_propagations),
                 ("cfg_branches_folded", cfg_branches_folded),
                 ("cfg_dead_instructions_pruned", cfg_dead_instructions_pruned),
                 ("cfg_redundant_jumps_removed", cfg_redundant_jumps_removed),
                 ("cfg_loop_headers", cfg_loop_headers),
                 ("cfg_loop_invariant_facts", cfg_loop_invariant_facts),
                 ("cfg_loop_variant_kills", cfg_loop_variant_kills),
                 ("cfg_affine_recurrences", cfg_affine_recurrences),
                 ("cfg_recurrence_folds", cfg_recurrence_folds),
                 ("cfg_strength_reduced_values", cfg_strength_reduced_values),
                 ("cfg_strength_reduced_uses", cfg_strength_reduced_uses),
                 ("cfg_strength_reduction_updates", cfg_strength_reduction_updates),
                 ("cfg_strength_lazy_values", cfg_strength_lazy_values),
                 ("cfg_strength_lazy_uses", cfg_strength_lazy_uses),
                 ("cfg_strength_lazy_materializations", cfg_strength_lazy_materializations),
                 ("stack_strategy", stack_strategy),
                 ("fusion_strategy", fusion_strategy),
                 ("region_dataflow", bool(region_dataflow)),
                 ("binding", binding),
                 ("policy", policy)),
    )
    current.__inline_binding__ = binding
    attach_report(current, report)
    return current


@overload
def inline_function(func: F) -> F: ...


@overload
def inline_function(
    *,
    register_only: bool = False,
    freeze_closures: bool = False,
    policy: str = "speed",
    binding: str = "frozen",
    stack_strategy: str = "auto",
    fusion_strategy: str = "auto",
    region_dataflow: bool = True,
    freeze_globals: bool = False,
    shared_region: bool = False,
    shared_regions: bool | str = "auto",
    shared_min_calls: int = 3,
    shared_min_body_instructions: int = 12,
    max_expansions: int = _DEFAULT_MAX_EXPANSIONS,
    max_growth_factor: int = _DEFAULT_MAX_GROWTH_FACTOR,
    max_code_bytes: int = _DEFAULT_MAX_CODE_BYTES,
) -> Callable[[F], F]: ...


def inline_function(
    func: F | None = None,
    *,
    register_only: bool = False,
    freeze_closures: bool = False,
    policy: str = "speed",
    binding: str = "frozen",
    stack_strategy: str = "auto",
    fusion_strategy: str = "auto",
    region_dataflow: bool = True,
    freeze_globals: bool = False,
    shared_region: bool = False,
    shared_regions: bool | str = "auto",
    shared_min_calls: int = 3,
    shared_min_body_instructions: int = 12,
    max_expansions: int = _DEFAULT_MAX_EXPANSIONS,
    max_growth_factor: int = _DEFAULT_MAX_GROWTH_FACTOR,
    max_code_bytes: int = _DEFAULT_MAX_CODE_BYTES,
):
    """Register a function as inlineable and optionally inline calls in its body.

    ``binding="frozen"`` keeps the high-performance historical compiler model:
    direct callable identity/implementation/defaults are captured when the caller is
    transformed. ``binding="guarded"`` validates those semantics at each expanded
    call site and deoptimizes to the already-loaded ordinary callable after rebinding
    or function-state mutation.
    """

    def decorate(target: F) -> F:
        _require_runtime()
        _validate_binding(binding)
        if isinstance(target, staticmethod):
            return staticmethod(decorate(target.__func__))  # type: ignore[return-value]
        if isinstance(target, classmethod):
            return classmethod(decorate(target.__func__))  # type: ignore[return-value]
        _validate_callee(
            target,
            freeze_closures=freeze_closures,
            freeze_globals=freeze_globals,
        )
        # Snapshot extension-owned metadata before touching the function so a failed
        # decoration is atomic from the caller's point of view.
        metadata_prefixes = ("__inline_", "__python_extensions_report")
        previous_metadata = {
            name: value
            for name, value in target.__dict__.items()
            if name.startswith(metadata_prefixes)
        }
        target.__inline_freeze_closures__ = freeze_closures
        target.__inline_freeze_globals__ = freeze_globals
        target.__inline_shared_region__ = bool(shared_region)

        key = _registry_key_for_function(target)
        # Registration is a transaction: recursive analysis must see the temporary
        # target, while concurrent decorators must not observe a half-committed entry.
        # RLock keeps recursive registry reads in _compile cheap and deadlock-free.
        with _registry_lock:
            previous_current, previous_identities = _remove_registry_entry(key)
            # Register before compilation so direct recursion is diagnosed explicitly.
            _install_registry_entry(key, target, (target,))
            try:
                if register_only:
                    target.__inline_stats__ = InlineStats(
                        0,
                        len(target.__code__.co_code),
                        len(target.__code__.co_code),
                    )
                    target.__inline_original__ = target
                    attach_report(
                        target,
                        make_report(
                            "inline-register",
                            target.__code__,
                            target.__code__,
                            details=(("registered_only", True),),
                        ),
                    )
                    return target

                transformed = _compile(
                    target,
                    policy=policy,
                    binding=binding,
                    stack_strategy=stack_strategy,
                    fusion_strategy=fusion_strategy,
                    region_dataflow=region_dataflow,
                    max_expansions=max_expansions,
                    max_growth_factor=max_growth_factor,
                    max_code_bytes=max_code_bytes,
                    shared_regions=shared_regions,
                    shared_min_calls=shared_min_calls,
                    shared_min_body_instructions=shared_min_body_instructions,
                )
                transformed = functools.update_wrapper(transformed, target)
                _remove_registry_entry(key)
                _install_registry_entry(key, transformed, (target, transformed))
                return transformed
            except BaseException:
                _remove_registry_entry(key)
                if previous_current is not None:
                    restored_identities = previous_identities or (previous_current,)
                    _install_registry_entry(
                        key, previous_current, restored_identities
                    )
                for name in tuple(target.__dict__):
                    if name.startswith(metadata_prefixes) and name not in previous_metadata:
                        target.__dict__.pop(name, None)
                target.__dict__.update(previous_metadata)
                raise

    return decorate if func is None else decorate(func)


def inline_calls(
    func: F | None = None,
    *,
    policy: str = "speed",
    binding: str = "frozen",
    stack_strategy: str = "auto",
    fusion_strategy: str = "auto",
    region_dataflow: bool = True,
    shared_regions: bool | str = "auto",
    shared_min_calls: int = 3,
    shared_min_body_instructions: int = 12,
    max_expansions: int = _DEFAULT_MAX_EXPANSIONS,
    max_growth_factor: int = _DEFAULT_MAX_GROWTH_FACTOR,
    max_code_bytes: int = _DEFAULT_MAX_CODE_BYTES,
):
    """Inline registered direct calls without registering the caller.

    The default ``binding="frozen"`` maximizes optimizer freedom and preserves the
    established compilation semantics. Use ``binding="guarded"`` when globals,
    methods, partials, callable objects, ``__code__``, or defaults may change after
    decoration and Python's dynamic call behavior must be retained.
    """

    def decorate(target: F) -> F:
        _require_runtime()
        if isinstance(target, staticmethod):
            return staticmethod(decorate(target.__func__))  # type: ignore[return-value]
        if isinstance(target, classmethod):
            return classmethod(decorate(target.__func__))  # type: ignore[return-value]
        if not isinstance(target, types.FunctionType):
            raise TypeError("@inline_calls requires a Python function")
        return _compile(
            target,
            policy=policy,
            binding=binding,
            stack_strategy=stack_strategy,
            fusion_strategy=fusion_strategy,
            region_dataflow=region_dataflow,
            max_expansions=max_expansions,
            max_growth_factor=max_growth_factor,
            max_code_bytes=max_code_bytes,
            shared_regions=shared_regions,
            shared_min_calls=shared_min_calls,
            shared_min_body_instructions=shared_min_body_instructions,
        )

    return decorate if func is None else decorate(func)


def registered_inline_functions() -> tuple[str, ...]:
    with _registry_lock:
        entries: set[str] = set()
        stale: list[_RegistryKey] = []
        for key, ref in _registry.items():
            func = ref()
            if func is None:
                stale.append(key)
            else:
                entries.add(f"{func.__module__}.{func.__qualname__}")
        for key in stale:
            _remove_registry_entry(key)
        return tuple(sorted(entries))


def clear_inline_registry() -> None:
    """Remove all registrations. Existing transformed functions remain valid."""

    with _registry_lock:
        for key in tuple(_registry):
            _remove_registry_entry(key)
        _registered_identity_counts.clear()
        _registry_identities.clear()


def unregister_inline_function(func: Callable[..., Any]) -> bool:
    """Remove *func* from the registry if it is the current registration.

    ``staticmethod`` and ``classmethod`` descriptors are accepted for symmetry
    with the registration decorators.
    """

    if isinstance(func, (staticmethod, classmethod, types.MethodType)):
        func = func.__func__
    if not isinstance(func, types.FunctionType):
        return False
    key = _registry_key_for_function(func)
    with _registry_lock:
        ref = _registry.get(key)
        current = ref() if ref is not None else None
        if current is func or getattr(current, "__inline_original__", None) is func:
            _remove_registry_entry(key)
            return True
        if ref is not None and current is None:
            _remove_registry_entry(key)
        return False


__all__ = [
    "__version__",
    "InlineCallSiteError",
    "InlineError",
    "InlineRecursionError",
    "InlineExpansionError",
    "InlineStats",
    "InlineUnsupportedError",
    "clear_inline_registry",
    "inline_calls",
    "inline_function",
    "registered_inline_functions",
    "unregister_inline_function",
]
