from __future__ import annotations

import types
from collections.abc import Iterable

from .cfg import build_cfg
from .model import ControlFlowGraph, TransformationReport, VerificationResult
from .verify import BytecodeVerificationError, verify_cfg


def make_report_from_verified_cfg(
    feature: str,
    original: types.CodeType,
    cfg: ControlFlowGraph,
    verification: VerificationResult,
    *,
    details: Iterable[tuple[str, object]] = (),
) -> TransformationReport:
    """Build a transformation report from an already-verified CFG.

    Bytecode-mutating extensions often need the CFG for feature-specific proofs
    after generic stack/control-flow verification.  Reusing that exact CFG keeps
    the report tied to the proof that was actually performed and avoids decoding
    and verifying a large transformed function a second time.
    """
    if not verification.valid:
        detail = "\n".join(f"- {error}" for error in verification.errors)
        raise BytecodeVerificationError(f"{feature} generated invalid bytecode:\n{detail}")
    final = cfg.code
    return TransformationReport(
        feature=feature,
        original_code_bytes=len(original.co_code),
        final_code_bytes=len(final.co_code),
        blocks=verification.total_blocks,
        reachable_blocks=verification.reachable_blocks,
        max_stack_depth=verification.max_stack_depth,
        verifier_warnings=verification.warnings,
        details=tuple(details),
    )


def make_report(
    feature: str,
    original: types.CodeType,
    final: types.CodeType,
    *,
    details: Iterable[tuple[str, object]] = (),
) -> TransformationReport:
    cfg = build_cfg(final)
    verification = verify_cfg(cfg)
    return make_report_from_verified_cfg(
        feature, original, cfg, verification, details=details
    )


def attach_report(function, report: TransformationReport):
    history = tuple(getattr(function, "__python_extensions_reports__", ()))
    function.__python_extensions_reports__ = (*history, report)
    function.__python_extensions_report__ = report
    return function


def _format_report(report: TransformationReport, *, indent: str = "") -> list[str]:
    data = report.as_dict()
    lines = [
        f"{indent}{data.pop('feature')}",
        f"{indent}  code: {data.pop('original_code_bytes')} -> {data.pop('final_code_bytes')} bytes "
        f"({data.pop('code_growth_ratio'):.3f}x)",
        f"{indent}  cfg: {data.pop('reachable_blocks')}/{data.pop('blocks')} reachable blocks, "
        f"max normal stack={data.pop('max_stack_depth')}",
    ]
    data.pop("code_growth", None)
    warnings = data.pop("verifier_warnings", ())
    for key, value in data.items():
        lines.append(f"{indent}  {key}: {value}")
    for warning in warnings:
        lines.append(f"{indent}  warning: {warning}")
    return lines


def explain(function) -> str:
    reports = tuple(getattr(function, "__python_extensions_reports__", ()))
    if not reports:
        report = getattr(function, "__python_extensions_report__", None)
        reports = () if report is None else (report,)
    if not reports:
        return f"{getattr(function, '__qualname__', function)!s}: no python_extensions report"
    lines = [f"{getattr(function, '__qualname__', function)!s}: python_extensions"]
    for report in reports:
        lines.extend(_format_report(report, indent="  "))
    pipeline = getattr(function, "__python_extensions_pipeline__", None)
    if pipeline is not None:
        lines.append("  pipeline: " + " -> ".join(pipeline))
    return "\n".join(lines)
