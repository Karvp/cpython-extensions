#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src/python_extensions/_version.py"

# Preview tags exercise the complete release path without changing the package
# version or making the build eligible for PyPI. Keep this intentionally small
# and predictable rather than accepting arbitrary suffixes.
_PREVIEW_SUFFIX = re.compile(
    r"^(?:alpha|beta|rc|preview|test)(?:[.-]?\d+)?$",
    re.IGNORECASE,
)


def package_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit("cannot determine package version")
    return match.group(1)


def tag_channel(tag: str, version: str) -> str:
    """Return ``stable`` or ``preview`` for an allowed release tag.

    ``vX.Y.Z`` is the only stable tag.  Preview tags such as
    ``vX.Y.Z-beta`` and ``vX.Y.Z-rc1`` are GitHub-only release rehearsals;
    they still build the exact X.Y.Z package and are never PyPI-eligible.
    """

    stable_tag = f"v{version}"
    if tag == stable_tag:
        return "stable"

    preview_prefix = f"{stable_tag}-"
    if tag.startswith(preview_prefix):
        suffix = tag[len(preview_prefix) :]
        if _PREVIEW_SUFFIX.fullmatch(suffix):
            return "preview"
        raise SystemExit(
            "unsupported preview tag suffix: "
            f"{suffix!r}; use alpha, beta, rc, preview, or test "
            "with an optional numeric suffix"
        )

    raise SystemExit(f"tag/version mismatch: tag={tag!r}, package=v{version}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="git tag, e.g. v1.2.3 or v1.2.3-beta")
    parser.add_argument(
        "--print-channel",
        action="store_true",
        help="print stable/preview instead of the package version",
    )
    args = parser.parse_args()

    version = package_version()
    channel = "stable"
    if args.tag:
        channel = tag_channel(args.tag, version)

    print(channel if args.print_channel else version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
