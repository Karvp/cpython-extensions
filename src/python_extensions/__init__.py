"""CPython 3.13 extensions for switch dispatch, function inlining, and goto.

The inline subsystem depends on the third-party ``bytecode`` package.  It is
lazily imported so ``import python_extensions`` and the switch/goto APIs remain
available even when a source checkout has not had its dependencies installed.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

from .switch import (
    DuplicateCaseError,
    DuplicateDefaultError,
    SwitchError,
    SwitchRangeError,
    SwitchSyntaxError,
    UnsupportedRuntimeError,
    case,
    enable_switch,
    fallthrough,
    switch,
)
from .goto import (
    DuplicateLabelError,
    GotoControlFlowError,
    GotoError,
    GotoSyntaxError,
    MissingLabelError,
    UnsupportedGotoRuntimeError,
    enable_goto,
)
from .compose import optimize_extensions
from ._core import TransformationReport, explain as explain_extensions, verify_code

from ._version import __version__

_INLINE_EXPORTS = frozenset({
    "InlineCallSiteError",
    "InlineError",
    "InlineExpansionError",
    "InlineRecursionError",
    "InlineStats",
    "InlineUnsupportedError",
    "clear_inline_registry",
    "inline_calls",
    "inline_function",
    "registered_inline_functions",
    "unregister_inline_function",
})


def __getattr__(name: str) -> Any:
    if name not in _INLINE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        module = import_module(".inline", __name__)
    except ModuleNotFoundError as exc:
        if exc.name == "bytecode":
            raise ModuleNotFoundError(
                "python_extensions.inline requires the 'bytecode' package. "
                "Install the project and its dependencies with "
                "`py -3.13 -m pip install -e .`, or install the dependency with "
                "`py -3.13 -m pip install 'bytecode>=0.17,<0.18'`."
            ) from exc
        raise
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _INLINE_EXPORTS)


__all__ = [
    "__version__",
    "switch", "case", "fallthrough", "enable_switch",
    "SwitchError", "SwitchSyntaxError", "DuplicateCaseError",
    "DuplicateDefaultError", "UnsupportedRuntimeError", "SwitchRangeError",
    "inline_function", "inline_calls", "clear_inline_registry",
    "registered_inline_functions", "unregister_inline_function",
    "InlineError", "InlineUnsupportedError", "InlineCallSiteError",
    "InlineRecursionError", "InlineExpansionError", "InlineStats",
    "enable_goto", "GotoError", "GotoSyntaxError", "GotoControlFlowError",
    "MissingLabelError", "DuplicateLabelError", "UnsupportedGotoRuntimeError",
    "optimize_extensions", "explain_extensions", "verify_code", "TransformationReport",
]
