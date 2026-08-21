from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from importlib import import_module

import python_extensions as pe

switch_module = import_module("python_extensions.switch")
inline_module = import_module("python_extensions.inline")
goto_module = import_module("python_extensions.goto")


def test_extension_versions_are_coherent():
    assert pe.__version__ == "1.0.3"
    assert switch_module.__version__ == pe.__version__
    assert inline_module.__version__ == pe.__version__
    assert goto_module.__version__ == pe.__version__

    import pyswitch
    import inline_function
    import pygoto

    assert pyswitch.__version__ == pe.__version__
    assert inline_function.__version__ == pe.__version__
    assert pygoto.__version__ == pe.__version__


def _register_isolated(index: int):
    namespace = {
        "__name__": f"release_thread_{index}",
        "inline_function": pe.inline_function,
        "inline_calls": pe.inline_calls,
    }
    exec(
        "@inline_function(register_only=True)\n"
        "def helper(x):\n"
        "    return x + 1\n\n"
        "@inline_calls(policy='always')\n"
        "def caller(x):\n"
        "    return helper(x)\n",
        namespace,
    )
    return namespace["helper"], namespace["caller"]


def test_inline_registry_supports_concurrent_registration_transactions():
    pe.clear_inline_registry()
    with ThreadPoolExecutor(max_workers=16) as pool:
        pairs = list(pool.map(_register_isolated, range(64)))

    try:
        assert len(pe.registered_inline_functions()) == 64
        for index, (_helper, caller) in enumerate(pairs):
            assert caller(index) == index + 1
            assert caller.__inline_stats__.calls_inlined == 1
    finally:
        pe.clear_inline_registry()
