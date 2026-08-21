from __future__ import annotations

from dataclasses import dataclass, field
from types import CodeType
from typing import Literal

EdgeKind = Literal["fallthrough", "jump"]


@dataclass(frozen=True, slots=True)
class InstructionInfo:
    offset: int
    opcode: int
    opname: str
    arg: int | None
    argval: object
    size: int
    starts_line: int | None
    is_jump_target: bool


@dataclass(frozen=True, slots=True)
class ExceptionRegion:
    start: int
    end: int
    target: int
    depth: int
    lasti: bool


@dataclass(frozen=True, slots=True)
class Edge:
    source: int
    target: int
    kind: EdgeKind


@dataclass(slots=True)
class BasicBlock:
    start: int
    instructions: tuple[InstructionInfo, ...]
    successors: tuple[Edge, ...] = ()
    predecessors: set[int] = field(default_factory=set)
    stack_in: int | None = None
    stack_out: int | None = None

    @property
    def end(self) -> int:
        if not self.instructions:
            return self.start
        last = self.instructions[-1]
        return last.offset + last.size


@dataclass(slots=True)
class ControlFlowGraph:
    code: CodeType
    instructions: tuple[InstructionInfo, ...]
    blocks: dict[int, BasicBlock]
    entry: int
    exception_regions: tuple[ExceptionRegion, ...]

    @property
    def code_bytes(self) -> int:
        return len(self.code.co_code)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    valid: bool
    max_stack_depth: int
    reachable_blocks: int
    total_blocks: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransformationReport:
    feature: str
    original_code_bytes: int
    final_code_bytes: int
    blocks: int
    reachable_blocks: int
    max_stack_depth: int
    verifier_warnings: tuple[str, ...] = ()
    details: tuple[tuple[str, object], ...] = ()

    @property
    def code_growth(self) -> int:
        return self.final_code_bytes - self.original_code_bytes

    @property
    def code_growth_ratio(self) -> float:
        if self.original_code_bytes == 0:
            return 1.0
        return self.final_code_bytes / self.original_code_bytes

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "feature": self.feature,
            "original_code_bytes": self.original_code_bytes,
            "final_code_bytes": self.final_code_bytes,
            "code_growth": self.code_growth,
            "code_growth_ratio": self.code_growth_ratio,
            "blocks": self.blocks,
            "reachable_blocks": self.reachable_blocks,
            "max_stack_depth": self.max_stack_depth,
            "verifier_warnings": self.verifier_warnings,
        }
        result.update(self.details)
        return result
