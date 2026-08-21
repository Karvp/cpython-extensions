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
    partial: Mapping[str, Any] | bool = False,
    inline: bool | Mapping[str, Any] = False,
    goto: bool | Mapping[str, Any] = False,
    specialize: bool | Mapping[str, Any] = False,
    hotpath: bool | Mapping[str, Any] = False,
) -> Callable[[F], F]: ...


def optimize_extensions(
    func: F | None = None,
    /,
    *,
    switch: bool | Mapping[str, Any] = False,
    partial: Mapping[str, Any] | bool = False,
    inline: bool | Mapping[str, Any] = False,
    goto: bool | Mapping[str, Any] = False,
    specialize: bool | Mapping[str, Any] = False,
    hotpath: bool | Mapping[str, Any] = False,
):
    """Apply python_extensions transforms in their canonical safe order.

    The order is intentionally fixed as
    ``switch -> partial -> inline -> goto -> specialize/hotpath``:

    * switch may recompile source and therefore runs before bytecode-only passes;
    * partial removes explicitly frozen parameters and exposes constant/dead-code
      opportunities before interprocedural optimization;
    * inline merges registered callee bytecode into that statically simplified body;
    * goto resolves pseudo labels only after static code growth is complete;
    * specialize/hotpath wrap the final verified function with guarded variants.

    ``specialize`` and ``hotpath`` are alternatives and cannot both be enabled in
    one pipeline. Boolean/mapping options follow the individual decorators;
    ``partial`` accepts a mapping of parameter names to frozen values.
    """

    switch_options = _options(switch, "switch")
    if partial is True:
        raise TypeError("partial must be False or a mapping of parameter names to frozen values")
    if partial is False:
        partial_options = None
    elif isinstance(partial, Mapping):
        partial_options = dict(partial)
        if not partial_options:
            raise ValueError("partial mapping must bind at least one parameter")
    else:
        raise TypeError("partial must be False or a mapping of parameter names to frozen values")
    inline_options = _options(inline, "inline")
    goto_options = _options(goto, "goto")
    specialize_options = _options(specialize, "specialize")
    hotpath_options = _options(hotpath, "hotpath")
    if specialize_options is not None and hotpath_options is not None:
        raise ValueError("specialize and hotpath are alternative final dispatch layers")

    def decorate(target: F) -> F:
        result: F = target
        pipeline: list[str] = []

        if switch_options is not None:
            from .switch import enable_switch

            result = enable_switch(**switch_options)(result)
            pipeline.append("switch")

        if partial_options is not None:
            from ._specialize import partial as partial_function

            result = partial_function(result, **partial_options)
            pipeline.append("partial")

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

        if specialize_options is not None:
            from ._specialize import specialize as specialize_function

            result = specialize_function(**specialize_options)(result)
            pipeline.append("specialize")

        if hotpath_options is not None:
            from ._specialize import hotpath as hotpath_function

            result = hotpath_function(**hotpath_options)(result)
            pipeline.append("hotpath")

        final_function = _function_target(result)
        verify_code(final_function.__code__)
        final_function.__python_extensions_pipeline__ = tuple(pipeline)
        return result

    # Bare @optimize_extensions is deliberately validation-only. It does not
    # guess which source-level extensions the user intended to enable.
    return decorate if func is None else decorate(func)
