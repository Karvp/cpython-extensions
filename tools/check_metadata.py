#!/usr/bin/env python3
"""Validate publish-facing project metadata that local build tools do not verify."""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def main() -> int:
    try:
        from trove_classifiers import classifiers, deprecated_classifiers
    except ImportError as exc:
        raise SystemExit(
            "trove-classifiers is required for metadata validation; install the build extra"
        ) from exc

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    configured = project.get("classifiers", [])
    if not isinstance(configured, list) or not all(isinstance(x, str) for x in configured):
        raise SystemExit("project.classifiers must be a list of strings")

    errors: list[str] = []
    for classifier in configured:
        if classifier not in classifiers:
            errors.append(f"invalid PyPI classifier: {classifier!r}")
        elif classifier in deprecated_classifiers:
            replacements = ", ".join(repr(x) for x in deprecated_classifiers[classifier])
            errors.append(
                f"deprecated PyPI classifier: {classifier!r}; use {replacements or 'a current replacement'}"
            )

    expected = "Programming Language :: Python :: Implementation :: CPython"
    if expected not in configured:
        errors.append(f"required implementation classifier missing: {expected!r}")

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"metadata classifier validation: PASS ({len(configured)} classifiers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
