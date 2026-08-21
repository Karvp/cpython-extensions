from __future__ import annotations

import dis
import types
from collections import deque

from .cfg import build_cfg
from .model import ControlFlowGraph, VerificationResult


class BytecodeVerificationError(RuntimeError):
    """Raised when transformed bytecode violates verified CPython invariants."""


def _effect(opcode: int, arg: int | None, *, jump: bool | None) -> int:
    try:
        if arg is None:
            return dis.stack_effect(opcode, jump=jump)
        return dis.stack_effect(opcode, arg, jump=jump)
    except ValueError:
        # A handful of opcodes reject a jump keyword because their stack effect
        # is not branch-sensitive. Retrying without it is exact for those ops.
        if arg is None:
            return dis.stack_effect(opcode)
        return dis.stack_effect(opcode, arg)


def _run_block(block, depth: int, edge_kind: str | None = None) -> tuple[int, int, list[str]]:
    errors: list[str] = []
    current = depth
    maximum = depth
    for index, item in enumerate(block.instructions):
        is_last = index == len(block.instructions) - 1
        jump_flag: bool | None = None
        if is_last and (item.opcode in dis.hasjabs or item.opcode in dis.hasjrel):
            if edge_kind == "jump":
                jump_flag = True
            elif edge_kind == "fallthrough":
                jump_flag = False
        try:
            current += _effect(item.opcode, item.arg, jump=jump_flag)
        except (ValueError, TypeError) as exc:
            errors.append(f"cannot compute stack effect at {item.offset} {item.opname}: {exc}")
            continue
        if current < 0:
            errors.append(
                f"stack underflow at offset {item.offset} ({item.opname}); depth became {current}"
            )
            current = 0
        maximum = max(maximum, current)
    return current, maximum, errors


def verify_cfg(cfg: ControlFlowGraph) -> VerificationResult:
    errors: list[str] = []
    warnings: list[str] = []
    code = cfg.code

    if len(code.co_code) % 2:
        errors.append("co_code length is not aligned to CPython 3.13 two-byte code units")

    valid_offsets = {item.offset for item in cfg.instructions}
    code_end = len(code.co_code)
    for item in cfg.instructions:
        if item.offset % 2:
            errors.append(f"unaligned instruction offset {item.offset}")
        if item.opcode in dis.hasjabs or item.opcode in dis.hasjrel:
            if not isinstance(item.argval, int) or item.argval not in valid_offsets:
                errors.append(
                    f"jump at offset {item.offset} ({item.opname}) targets invalid offset {item.argval!r}"
                )

    for region in cfg.exception_regions:
        if not (0 <= region.start < region.end <= code_end):
            errors.append(f"invalid exception region [{region.start}, {region.end})")
        if region.start not in valid_offsets:
            errors.append(f"exception region starts at non-instruction offset {region.start}")
        if region.end != code_end and region.end not in valid_offsets:
            errors.append(f"exception region ends at non-instruction offset {region.end}")
        if region.target not in valid_offsets:
            errors.append(f"exception handler targets non-instruction offset {region.target}")
        if region.depth > code.co_stacksize:
            errors.append(
                f"exception unwind depth {region.depth} exceeds co_stacksize {code.co_stacksize}"
            )

    if not cfg.blocks:
        return VerificationResult(not errors, 0, 0, 0, tuple(errors), tuple(warnings))

    # Verify normal control-flow stack depths. Exception edges are intentionally
    # validated structurally above but are not mixed into normal propagation;
    # their entry stacks are encoded separately by CPython's exception table.
    entry_depths: dict[int, int] = {cfg.entry: 0}
    queue = deque([cfg.entry])
    max_depth = 0

    while queue:
        start = queue.popleft()
        block = cfg.blocks[start]
        depth = entry_depths[start]
        block.stack_in = depth

        if not block.successors:
            out, local_max, block_errors = _run_block(block, depth)
            block.stack_out = out
            errors.extend(block_errors)
            max_depth = max(max_depth, local_max)
            continue

        edge_outs: list[int] = []
        for edge in block.successors:
            out, local_max, block_errors = _run_block(block, depth, edge.kind)
            errors.extend(block_errors)
            max_depth = max(max_depth, local_max)
            edge_outs.append(out)
            known = entry_depths.get(edge.target)
            if known is None:
                entry_depths[edge.target] = out
                queue.append(edge.target)
            elif known != out:
                errors.append(
                    f"stack-depth mismatch entering block {edge.target}: {known} versus {out} "
                    f"from predecessor {start}"
                )
        block.stack_out = edge_outs[0] if edge_outs and len(set(edge_outs)) == 1 else None

    if max_depth > code.co_stacksize:
        errors.append(
            f"computed normal-flow stack depth {max_depth} exceeds co_stacksize {code.co_stacksize}"
        )

    unreachable = len(cfg.blocks) - len(entry_depths)
    if unreachable:
        warnings.append(
            f"{unreachable} block(s) are unreachable by normal control flow; some may be exception handlers"
        )

    return VerificationResult(
        valid=not errors,
        max_stack_depth=max_depth,
        reachable_blocks=len(entry_depths),
        total_blocks=len(cfg.blocks),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def verify_code(code: types.CodeType, *, raise_on_error: bool = True) -> VerificationResult:
    result = verify_cfg(build_cfg(code))
    if raise_on_error and not result.valid:
        detail = "\n".join(f"- {error}" for error in result.errors)
        raise BytecodeVerificationError(f"bytecode verification failed:\n{detail}")
    return result
