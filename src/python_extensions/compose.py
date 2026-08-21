from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar, overload

from ._core import verify_code

F = TypeVar("F", bound=Callable[..., Any])


def _function_target(value):
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    return value


def _options(value: bool | Mapping[str, Any], feature: str) -> dict[str, Any] | None:
    if value is False:
        return None
    if value is True:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{feature} must be bool or a mapping of keyword options")


@overload
def optimize_extensions(func: F, /) -> F: ...


@overload
def optimize_extensions(
    *,
    switch: bool | Mapping[str, Any] = False,
    inline: bool | Mapping[str, Any] = False,
    goto: bool | Mapping[str, Any] = False,
) -> Callable[[F], F]: ...


def optimize_extensions(
    func: F | None = None,
    /,
    *,
    switch: bool | Mapping[str, Any] = False,
    inline: bool | Mapping[str, Any] = False,
    goto: bool | Mapping[str, Any] = False,
):
    """Apply python_extensions transforms in their canonical safe order.

    The order is intentionally fixed as ``switch -> inline -> goto``:

    * switch may recompile source and therefore must run before bytecode-only passes;
    * inline merges registered callee bytecode into that lowered function;
    * goto is offset-preserving and resolves pseudo labels after all code growth.

    Each feature accepts ``True`` for defaults or a mapping of decorator options.
    """

    switch_options = _options(switch, "switch")
    inline_options = _options(inline, "inline")
    goto_options = _options(goto, "goto")

    def decorate(target: F) -> F:
        result: F = target
        pipeline: list[str] = []

        if switch_options is not None:
            from .switch import enable_switch

            result = enable_switch(**switch_options)(result)
            pipeline.append("switch")

        if inline_options is not None:
            try:
                from .inline import inline_calls
            except ModuleNotFoundError as exc:
                if exc.name == "bytecode":
                    raise ModuleNotFoundError(
                        "optimize_extensions(inline=...) requires the 'bytecode' package; "
                        "install cpython-extensions with its declared dependencies"
                    ) from exc
                raise
            result = inline_calls(**inline_options)(result)
            pipeline.append("inline")

        if goto_options is not None:
            from .goto import enable_goto

            result = enable_goto(**goto_options)(result)
            pipeline.append("goto")

        final_function = _function_target(result)
        verify_code(final_function.__code__)
        final_function.__python_extensions_pipeline__ = tuple(pipeline)
        return result

    # Bare @optimize_extensions is deliberately validation-only. It does not
    # guess which source-level extensions the user intended to enable.
    return decorate if func is None else decorate(func)
