from __future__ import annotations

import dis
import types

from .model import BasicBlock, ControlFlowGraph, Edge, ExceptionRegion, InstructionInfo


_TERMINATORS = {
    "RETURN_VALUE",
    "RETURN_CONST",
    "RAISE_VARARGS",
    "RERAISE",
}


def _instruction_size(
    instructions: list[dis.Instruction], index: int, code_size: int
) -> int:
    current = instructions[index].offset
    if index + 1 < len(instructions):
        return instructions[index + 1].offset - current
    return code_size - current


def decode_instructions(code: types.CodeType) -> tuple[InstructionInfo, ...]:
    raw = list(dis.get_instructions(code, adaptive=False, show_caches=False))
    size = len(code.co_code)
    return tuple(
        InstructionInfo(
            offset=item.offset,
            opcode=item.opcode,
            opname=item.opname,
            arg=item.arg,
            argval=item.argval,
            size=_instruction_size(raw, index, size),
            starts_line=item.starts_line,
            is_jump_target=item.is_jump_target,
        )
        for index, item in enumerate(raw)
    )


def exception_regions(code: types.CodeType) -> tuple[ExceptionRegion, ...]:
    entries = getattr(dis.Bytecode(code), "exception_entries", ())
    return tuple(
        ExceptionRegion(
            start=entry.start,
            end=entry.end,
            target=entry.target,
            depth=entry.depth,
            lasti=entry.lasti,
        )
        for entry in entries
    )


def _is_unconditional_jump(item: InstructionInfo) -> bool:
    return item.opcode in dis.hasjabs or item.opcode in dis.hasjrel and (
        item.opname.startswith("JUMP_FORWARD")
        or item.opname.startswith("JUMP_BACKWARD")
        or item.opname in {"JUMP", "JUMP_NO_INTERRUPT"}
    )


def _is_jump(item: InstructionInfo) -> bool:
    return item.opcode in dis.hasjabs or item.opcode in dis.hasjrel


def _target(item: InstructionInfo) -> int | None:
    if not _is_jump(item):
        return None
    return item.argval if isinstance(item.argval, int) else None


def _next_offset(instructions: tuple[InstructionInfo, ...], index: int) -> int | None:
    if index + 1 >= len(instructions):
        return None
    return instructions[index + 1].offset


def build_cfg(code: types.CodeType) -> ControlFlowGraph:
    instructions = decode_instructions(code)
    if not instructions:
        return ControlFlowGraph(code, (), {}, 0, exception_regions(code))

    by_offset = {item.offset: index for index, item in enumerate(instructions)}
    leaders: set[int] = {instructions[0].offset}

    for index, item in enumerate(instructions):
        target = _target(item)
        if target is not None:
            leaders.add(target)
        if _is_jump(item) or item.opname in _TERMINATORS:
            nxt = _next_offset(instructions, index)
            if nxt is not None:
                leaders.add(nxt)

    regions = exception_regions(code)
    for region in regions:
        for offset in (region.start, region.end, region.target):
            if offset in by_offset:
                leaders.add(offset)

    ordered = sorted(offset for offset in leaders if offset in by_offset)
    blocks: dict[int, BasicBlock] = {}
    for pos, start in enumerate(ordered):
        start_index = by_offset[start]
        end_index = by_offset[ordered[pos + 1]] if pos + 1 < len(ordered) else len(instructions)
        blocks[start] = BasicBlock(start, instructions[start_index:end_index])

    starts = set(blocks)
    instruction_to_block: dict[int, int] = {}
    current_start = ordered[0]
    for item in instructions:
        if item.offset in starts:
            current_start = item.offset
        instruction_to_block[item.offset] = current_start

    for start, block in blocks.items():
        if not block.instructions:
            continue
        last = block.instructions[-1]
        last_index = by_offset[last.offset]
        successors: list[Edge] = []
        target = _target(last)
        if target is not None and target in blocks:
            successors.append(Edge(start, target, "jump"))

        if last.opname not in _TERMINATORS and not _is_unconditional_jump(last):
            nxt = _next_offset(instructions, last_index)
            if nxt is not None:
                target_block = instruction_to_block[nxt]
                if all(edge.target != target_block for edge in successors):
                    successors.append(Edge(start, target_block, "fallthrough"))

        block.successors = tuple(successors)

    for block in blocks.values():
        for edge in block.successors:
            blocks[edge.target].predecessors.add(block.start)

    return ControlFlowGraph(
        code=code,
        instructions=instructions,
        blocks=blocks,
        entry=instructions[0].offset,
        exception_regions=regions,
    )
