#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src/python_extensions/_version.py"


def package_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit("cannot determine package version")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="expected git tag, e.g. v1.2.3")
    args = parser.parse_args()
    version = package_version()
    if args.tag and args.tag != f"v{version}":
        raise SystemExit(f"tag/version mismatch: tag={args.tag!r}, package=v{version}")
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
