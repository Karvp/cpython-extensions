#!/usr/bin/env python3
"""Install dependency groups from pyproject.toml without installing the project.

Release and packaging workflows need the dependencies declared by the project,
but installing ``.[extra]`` first dirties a source checkout by creating build/
and ``*.egg-info`` directories.  This helper keeps pyproject.toml as the single
source of dependency constraints while leaving the repository untouched.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def load_pyproject(path: Path = PYPROJECT) -> dict:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def collect_requirements(
    data: dict,
    extras: list[str],
    *,
    include_runtime: bool = False,
    include_build_system: bool = False,
) -> list[str]:
    requirements: list[str] = []

    if include_build_system:
        requirements.extend(data.get("build-system", {}).get("requires", ()))
    if include_runtime:
        requirements.extend(data.get("project", {}).get("dependencies", ()))

    optional = data.get("project", {}).get("optional-dependencies", {})
    unknown = [name for name in extras if name not in optional]
    if unknown:
        available = ", ".join(sorted(optional)) or "<none>"
        raise ValueError(
            f"unknown optional dependency group(s): {', '.join(unknown)}; "
            f"available: {available}"
        )
    for name in extras:
        requirements.extend(optional[name])

    # Preserve declaration order while avoiding duplicate resolver inputs.
    return list(dict.fromkeys(requirements))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("extras", nargs="*", help="optional-dependency groups to install")
    parser.add_argument("--include-runtime", action="store_true")
    parser.add_argument("--include-build-system", action="store_true")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved requirement arguments without invoking pip",
    )
    args = parser.parse_args()

    try:
        requirements = collect_requirements(
            load_pyproject(),
            args.extras,
            include_runtime=args.include_runtime,
            include_build_system=args.include_build_system,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if not requirements:
        parser.error("no dependency groups selected")

    if args.dry_run:
        for requirement in requirements:
            print(requirement)
        return 0

    command = [sys.executable, "-m", "pip", "install"]
    if args.upgrade:
        command.append("--upgrade")
    command.extend(requirements)
    print("installing declared dependencies:")
    for requirement in requirements:
        print(f"  {requirement}")
    subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
