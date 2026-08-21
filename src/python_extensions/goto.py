"""Offset-preserving ``goto .label`` bytecode extension for CPython 3.13.

The decorator recognizes expression statements of the form ``goto .name`` and
``label .name``.  It rewrites their existing code-unit ranges in place, so code
length, inline-cache spacing, exception tables, and all unrelated jump offsets
remain unchanged.

This module intentionally targets CPython 3.13 only.  Strict mode validates
protected-region and operand-stack compatibility and is the supported default.
``mode="unsafe"`` deliberately relaxes the protected-region check for controlled
low-level experiments; unstructured jumps should still be used sparingly.
"""
from __future__ import annotations

import dis
import functools
import platform
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from ._core import (
    BytecodeVerificationError,
    attach_report,
    build_cfg,
    decode_instructions,
    make_report,
    make_report_from_verified_cfg,
    verify_cfg,
)

F = TypeVar("F", bound=Callable[..., Any])
_WORD = 2

from ._version import __version__

__all__ = [
    "__version__",
    "GotoError",
    "GotoSyntaxError",
    "MissingLabelError",
    "DuplicateLabelError",
    "GotoControlFlowError",
    "UnsupportedGotoRuntimeError",
    "enable_goto",
]


class GotoError(Exception):
    """Base class for goto transformation failures."""


class GotoSyntaxError(GotoError, SyntaxError):
    """Raised when a goto/label pseudo statement has an invalid shape."""


class MissingLabelError(GotoError):
    """Raised when a goto targets a label that does not exist."""


class DuplicateLabelError(GotoError):
    """Raised when a label name is declared more than once."""


class GotoControlFlowError(GotoError):
    """Raised when strict goto would cross a protected control-flow region."""


class UnsupportedGotoRuntimeError(GotoError, RuntimeError):
    """Raised when the interpreter is not the supported CPython version."""


@dataclass(frozen=True)
class _PseudoStatement:
    action: str
    name: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class _SyntheticJump:
    action: str
    name: str
    source_offset: int
    target_offset: int
    source_exception_signature: tuple[tuple[int, int, bool], ...]


def _require_runtime() -> None:
    if platform.python_implementation() != "CPython":
        raise UnsupportedGotoRuntimeError("goto requires CPython")
    if sys.version_info[:2] != (3, 13):
        raise UnsupportedGotoRuntimeError(
            f"goto requires CPython 3.13.x, got {sys.version.split()[0]}"
        )
    required = {"NOP", "EXTENDED_ARG", "JUMP_FORWARD", "JUMP_BACKWARD"}
    missing = sorted(required - dis.opmap.keys())
    if missing:
        raise UnsupportedGotoRuntimeError(
            "required opcodes are unavailable: " + ", ".join(missing)
        )


def _scan(code: types.CodeType) -> list[_PseudoStatement]:
    instructions = list(decode_instructions(code))
    result: list[_PseudoStatement] = []
    i = 0
    while i < len(instructions):
        first = instructions[i]
        if first.opname not in {"LOAD_GLOBAL", "LOAD_NAME"} or first.argval not in {
            "goto",
            "label",
        }:
            i += 1
            continue

        if i + 2 >= len(instructions):
            raise GotoSyntaxError(
                f"incomplete {first.argval} pseudo statement at offset {first.offset}"
            )
        attr = instructions[i + 1]
        pop = instructions[i + 2]
        if attr.opname != "LOAD_ATTR" or pop.opname != "POP_TOP":
            raise GotoSyntaxError(
                f"use exactly `{first.argval} .name` as a standalone statement"
            )
        if not isinstance(attr.argval, str):
            raise GotoSyntaxError("goto/label name must be an attribute identifier")

        result.append(
            _PseudoStatement(
                action=cast(str, first.argval),
                name=attr.argval,
                start_offset=first.offset,
                end_offset=pop.offset,
            )
        )
        i += 3
    return result


def _write_unit(buffer: bytearray, byte_offset: int, opcode: int, argument: int) -> None:
    if byte_offset % _WORD:
        raise GotoError(f"unaligned bytecode offset: {byte_offset}")
    if not 0 <= argument <= 0xFF:
        raise GotoError(f"instruction argument does not fit one code unit: {argument}")
    buffer[byte_offset] = opcode
    buffer[byte_offset + 1] = argument


def _fill_nops(buffer: bytearray, start: int, end: int) -> None:
    nop = dis.opmap["NOP"]
    for offset in range(start, end + _WORD, _WORD):
        _write_unit(buffer, offset, nop, 0)


def _label_target(statement: _PseudoStatement, code_size: int) -> int:
    """Return the first real instruction after a ``label .name`` marker."""

    target = statement.end_offset + _WORD
    if target >= code_size:
        raise GotoControlFlowError(
            f"label {statement.name!r} has no executable continuation"
        )
    return target


def _early_jump_layout(
    start_offset: int,
    target_offset: int,
    *,
    cache_units: int,
    available_units: int,
) -> tuple[int, int, list[int]]:
    """Choose the earliest valid EXTENDED_ARG* + jump layout in a marker region.

    Moving a relative jump changes its argument, which can in turn change the
    number of required EXTENDED_ARG units.  Search the tiny marker region for the
    smallest prefix that can encode its own resulting relative argument.
    """

    max_prefix = available_units - 1 - cache_units
    if max_prefix < 0:
        raise GotoError("pseudo statement is too small for jump inline caches")
    for prefix_units in range(max_prefix + 1):
        opcode_offset = start_offset + prefix_units * _WORD
        opcode, encoded = _jump_encoding(opcode_offset, target_offset, cache_units)
        required_prefix = len(encoded) - 1
        if required_prefix <= prefix_units:
            if required_prefix < prefix_units:
                encoded = [0] * (prefix_units - required_prefix) + encoded
            return opcode_offset, opcode, encoded
    raise GotoError(
        f"relative jump needs more than {available_units - cache_units} encoding units"
    )


def _jump_encoding(
    opcode_offset: int, target_offset: int, cache_units: int
) -> tuple[int, list[int]]:
    target_unit = target_offset // _WORD
    forward_base = opcode_offset // _WORD + 1
    if target_unit >= forward_base:
        opcode = dis.opmap["JUMP_FORWARD"]
        argument = target_unit - forward_base
    else:
        opcode = dis.opmap["JUMP_BACKWARD"]
        # CPython 3.13 defines the backward relative base after the opcode
        # and its inline cache entries.
        backward_base = opcode_offset // _WORD + 1 + cache_units
        argument = backward_base - target_unit

    bytes_be: list[int] = []
    while argument:
        bytes_be.append(argument & 0xFF)
        argument >>= 8
    bytes_be.reverse()
    if not bytes_be:
        bytes_be = [0]
    return opcode, bytes_be


def _active_exception_regions(code: types.CodeType, offset: int) -> tuple[tuple[int, int, bool], ...]:
    """Return the semantic exception-handler stack active at *offset*.

    Physical exception-table ranges are an encoding detail.  CPython may split
    one logical protected region around ``yield``/``await`` suspension points
    while retaining the same handler target, unwind depth, and ``lasti`` policy.
    Strict goto therefore compares those semantic handler properties rather than
    range coordinates.  The post-patch bytecode verifier separately proves that
    operand-stack depths remain compatible at the actual jump target.
    """
    entries = getattr(dis.Bytecode(code), "exception_entries", ())
    return tuple(
        (entry.target, entry.depth, entry.lasti)
        for entry in entries
        if entry.start <= offset < entry.end
    )


def _marker_exception_signature(
    code: types.CodeType, statement: _PseudoStatement
) -> tuple[tuple[int, int, bool], ...]:
    """Return one stable protected-region signature for a pseudo statement.

    Early-jump lowering may place EXTENDED_ARG units and the real jump anywhere
    inside the original ``goto .name`` footprint.  Strict mode therefore proves
    that the whole marker span belongs to one semantic exception-handler stack,
    rather than validating only its first instruction and assuming the rest of the
    pseudo expression cannot cross an exception-table boundary.
    """

    signatures = {
        _active_exception_regions(code, offset)
        for offset in range(statement.start_offset, statement.end_offset + _WORD, _WORD)
    }
    if len(signatures) != 1:
        raise GotoControlFlowError(
            f"{statement.action} {statement.name!r} straddles a protected "
            "exception/cleanup boundary; strict early-jump lowering requires "
            "one stable protected region across the complete marker"
        )
    return next(iter(signatures))


def _validate_control_flow(
    code: types.CodeType,
    gotos: list[_PseudoStatement],
    labels: dict[str, _PseudoStatement],
    *,
    mode: str,
) -> None:
    if mode == "unsafe":
        return
    if mode != "strict":
        raise ValueError("goto mode must be 'strict' or 'unsafe'")

    exception_targets = {
        entry.target for entry in getattr(dis.Bytecode(code), "exception_entries", ())
    }
    for statement in gotos:
        # Strict permission is defined at the lexical label marker, matching
        # ordinary fallthrough into that statement.  The patcher may later jump
        # directly to the marker's continuation to skip its dead expression
        # footprint; compiler-generated cleanup immediately after the label must
        # not make an otherwise-valid lexical jump look cross-region.
        target = labels[statement.name].start_offset
        source_signature = _marker_exception_signature(code, statement)
        target_signature = _active_exception_regions(code, target)
        if source_signature != target_signature:
            raise GotoControlFlowError(
                f"goto {statement.name!r} crosses a protected exception/cleanup region; "
                "use mode='unsafe' only if bypassing structured cleanup is intentional"
            )
        if target in exception_targets:
            raise GotoControlFlowError(
                f"goto {statement.name!r} targets an exception-handler entry; "
                "strict mode forbids synthetic handler entry"
            )


def _patch_code(
    code: types.CodeType, *, mode: str = "strict"
) -> tuple[types.CodeType, tuple[_SyntheticJump, ...]]:
    pseudo = _scan(code)
    labels: dict[str, _PseudoStatement] = {}
    gotos: list[_PseudoStatement] = []

    for statement in pseudo:
        if statement.action == "label":
            if statement.name in labels:
                raise DuplicateLabelError(f"duplicate label: {statement.name!r}")
            labels[statement.name] = statement
        else:
            gotos.append(statement)

    for statement in gotos:
        if statement.name not in labels:
            raise MissingLabelError(f"undefined goto label: {statement.name!r}")

    _validate_control_flow(code, gotos, labels, mode=mode)
    raw = bytearray(code.co_code)
    synthetic_jumps: list[_SyntheticJump] = []

    # Labels are semantic no-ops.  Replace each marker with one forward jump over
    # its dead LOAD_GLOBAL/LOAD_ATTR/POP_TOP footprint; this keeps every original
    # byte offset stable while avoiding a run of NOPs on ordinary fallthrough.
    for label_statement in labels.values():
        _fill_nops(raw, label_statement.start_offset, label_statement.end_offset)
        target = _label_target(label_statement, len(raw))
        argument = target // _WORD - (label_statement.start_offset // _WORD + 1)
        if not 0 <= argument <= 0xFF:
            raise GotoError(
                f"label {label_statement.name!r} marker is unexpectedly too large"
            )
        _write_unit(
            raw, label_statement.start_offset, dis.opmap["JUMP_FORWARD"], argument
        )
        synthetic_jumps.append(
            _SyntheticJump(
                "label",
                label_statement.name,
                label_statement.start_offset,
                target,
                _active_exception_regions(code, label_statement.start_offset),
            )
        )

    # A goto always transfers control, so place EXTENDED_ARG* + JUMP as early as
    # the reserved pseudo-expression permits.  Remaining code units stay NOPs and
    # are unreachable on the taken path.
    for goto_statement in gotos:
        target = _label_target(labels[goto_statement.name], len(raw))
        _fill_nops(raw, goto_statement.start_offset, goto_statement.end_offset)
        backward = target < goto_statement.start_offset
        cache_units = dis._inline_cache_entries.get("JUMP_BACKWARD", 0) if backward else 0
        available_units = (
            goto_statement.end_offset - goto_statement.start_offset
        ) // _WORD + 1
        opcode_offset, opcode, argument_bytes = _early_jump_layout(
            goto_statement.start_offset,
            target,
            cache_units=cache_units,
            available_units=available_units,
        )
        first_offset = opcode_offset - (len(argument_bytes) - 1) * _WORD
        for index, byte in enumerate(argument_bytes[:-1]):
            _write_unit(
                raw,
                first_offset + index * _WORD,
                dis.opmap["EXTENDED_ARG"],
                byte,
            )
        _write_unit(raw, opcode_offset, opcode, argument_bytes[-1])
        for index in range(cache_units):
            _write_unit(raw, opcode_offset + (index + 1) * _WORD, 0, 0)
        synthetic_jumps.append(
            _SyntheticJump(
                "goto",
                goto_statement.name,
                opcode_offset,
                target,
                _marker_exception_signature(code, goto_statement),
            )
        )

    return code.replace(co_code=bytes(raw)), tuple(synthetic_jumps)


def _prove_synthetic_jumps(
    code: types.CodeType,
    cfg,
    jumps: tuple[_SyntheticJump, ...],
    *,
    mode: str,
) -> int:
    """Prove every emitted synthetic jump against the final verified CFG."""
    by_offset = {item.offset: item for item in cfg.instructions}
    instruction_to_block: dict[int, Any] = {}
    for block in cfg.blocks.values():
        for item in block.instructions:
            instruction_to_block[item.offset] = block

    for jump in jumps:
        item = by_offset.get(jump.source_offset)
        if item is None:
            raise GotoControlFlowError(
                f"synthetic {jump.action} {jump.name!r} has no final instruction "
                f"at offset {jump.source_offset}"
            )
        if item.opname not in {"JUMP_FORWARD", "JUMP_BACKWARD"}:
            raise GotoControlFlowError(
                f"synthetic {jump.action} {jump.name!r} did not remain a native jump "
                f"at offset {jump.source_offset}: found {item.opname}"
            )
        if item.argval != jump.target_offset:
            raise GotoControlFlowError(
                f"synthetic {jump.action} {jump.name!r} targets {item.argval!r}, "
                f"expected {jump.target_offset}"
            )
        block = instruction_to_block.get(jump.source_offset)
        if block is None or not block.instructions or block.instructions[-1].offset != jump.source_offset:
            raise GotoControlFlowError(
                f"synthetic {jump.action} {jump.name!r} is not a CFG terminator"
            )
        if not any(
            edge.kind == "jump" and edge.target == jump.target_offset
            for edge in block.successors
        ):
            raise GotoControlFlowError(
                f"synthetic {jump.action} {jump.name!r} has no verified CFG edge "
                f"to {jump.target_offset}"
            )
        if mode == "strict":
            final_signature = _active_exception_regions(code, jump.source_offset)
            if final_signature != jump.source_exception_signature:
                raise GotoControlFlowError(
                    f"synthetic {jump.action} {jump.name!r} changed protected-region "
                    "semantics during lowering"
                )
    return len(jumps)


def enable_goto(func: F | None = None, /, *, mode: str = "strict"):
    """Enable ``goto .name`` and ``label .name`` inside a function.

    ``mode="strict"`` (default) rejects jumps across CPython exception-table
    protection boundaries and direct synthetic entry into exception handlers.
    ``mode="unsafe"`` retains the raw low-level behavior for controlled
    experiments. Bytecode length is preserved exactly in both modes.
    """
    if mode not in {"strict", "unsafe"}:
        raise ValueError("goto mode must be 'strict' or 'unsafe'")

    def decorate(target: F) -> F:
        _require_runtime()
        if isinstance(target, staticmethod):
            return staticmethod(decorate(target.__func__))  # type: ignore[return-value]
        if isinstance(target, classmethod):
            return classmethod(decorate(target.__func__))  # type: ignore[return-value]
        if not isinstance(target, types.FunctionType):
            raise TypeError("@enable_goto requires a Python function")

        original_code = target.__code__
        pseudo = _scan(original_code)
        if not pseudo:
            # A marker-free function needs no code-object reconstruction.  Keep
            # the original callable identity/closure/descriptor behavior and
            # attach only the normal transformation report.
            report = make_report(
                "goto",
                original_code,
                original_code,
                details=(("mode", mode), ("gotos", 0), ("labels", 0),
                         ("early_jump_lowering", False), ("extended_arg_units", 0),
                         ("marker_units_elided", 0), ("synthetic_jumps_verified", 0),
                         ("cfg_verification_passes", 1), ("no_op", True)),
            )
            return cast(F, attach_report(target, report))

        patched_code, synthetic_jumps = _patch_code(original_code, mode=mode)
        new = types.FunctionType(
            patched_code,
            target.__globals__,
            target.__name__,
            target.__defaults__,
            target.__closure__,
        )
        new.__kwdefaults__ = (
            None if target.__kwdefaults__ is None else dict(target.__kwdefaults__)
        )
        new.__annotations__ = dict(target.__annotations__)
        new.__dict__.update(target.__dict__)
        if hasattr(target, "__type_params__"):
            new.__type_params__ = target.__type_params__  # type: ignore[attr-defined]
        wrapped = cast(F, functools.update_wrapper(new, target))
        goto_statements = [item for item in pseudo if item.action == "goto"]
        label_statements = [item for item in pseudo if item.action == "label"]
        label_by_name = {item.name: item for item in label_statements}
        cfg = build_cfg(patched_code)
        verification = verify_cfg(cfg)
        if not verification.valid:
            detail = verification.errors[-1] if verification.errors else "unknown verification failure"
            raise GotoControlFlowError(
                f"goto would create invalid CPython stack/control flow: {detail}"
            )
        synthetic_jumps_verified = _prove_synthetic_jumps(
            patched_code, cfg, synthetic_jumps, mode=mode
        )
        extended_arg_units = 0
        marker_units_elided = 0
        for statement in goto_statements:
            target_offset = _label_target(
                label_by_name[statement.name], len(original_code.co_code)
            )
            backward = target_offset < statement.start_offset
            cache_units = (
                dis._inline_cache_entries.get("JUMP_BACKWARD", 0) if backward else 0
            )
            available_units = (statement.end_offset - statement.start_offset) // _WORD + 1
            _opcode_offset, _opcode, argument_bytes = _early_jump_layout(
                statement.start_offset,
                target_offset,
                cache_units=cache_units,
                available_units=available_units,
            )
            extended_arg_units += max(0, len(argument_bytes) - 1)
            marker_units_elided += max(
                0, available_units - (len(argument_bytes) + cache_units)
            )
        marker_units_elided += sum(
            max(0, (item.end_offset - item.start_offset) // _WORD)
            for item in label_statements
        )
        try:
            report = make_report_from_verified_cfg(
                "goto",
                original_code,
                cfg,
                verification,
                details=(
                    ("mode", mode),
                    ("gotos", len(goto_statements)),
                    ("labels", len(label_statements)),
                    ("early_jump_lowering", True),
                    ("extended_arg_units", extended_arg_units),
                    ("marker_units_elided", marker_units_elided),
                    ("synthetic_jumps_verified", synthetic_jumps_verified),
                    ("cfg_verification_passes", 1),
                ),
            )
        except BytecodeVerificationError as exc:
            detail = str(exc).splitlines()[-1].removeprefix("- ")
            raise GotoControlFlowError(
                f"goto would create invalid CPython stack/control flow: {detail}"
            ) from exc
        return cast(F, attach_report(wrapped, report))

    return decorate if func is None else decorate(func)
