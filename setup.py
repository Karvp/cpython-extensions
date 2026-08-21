"""Setuptools bridge for the optional CPython 3.13 live-switch accelerator.

The extension intentionally suppresses compiler debug-path metadata on POSIX.
The project release gate rebuilds the wheel from the exact sdist in a different
checkout directory and requires byte-identical artifacts; CPython's default
Unix compiler flags include ``-g``, which otherwise records the build path in
DWARF even when ``SOURCE_DATE_EPOCH`` is fixed.
"""
from __future__ import annotations

import os

from setuptools import Extension, setup


extra_compile_args = [] if os.name == "nt" else ["-g0"]

setup(
    ext_modules=[
        Extension(
            "python_extensions._livegate",
            sources=["src/python_extensions/_livegate.c"],
            optional=True,
            extra_compile_args=extra_compile_args,
        )
    ]
)
