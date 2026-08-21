#!/usr/bin/env python3
"""Build reproducible wheel/sdist artifacts for python_extensions.

The setuptools wheel backend honors SOURCE_DATE_EPOCH.  Setuptools' sdist
selection is also used, then its tarball is repacked with canonical ownership,
ordering, and timestamps so repeated builds from identical source bytes match.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import shutil
import tarfile
import tempfile


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(destination, filter="data")
    roots = sorted(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError("sdist must contain exactly one top-level directory")
    return roots[0]


def _tarinfo(path: Path, arcname: str, epoch: int) -> tarfile.TarInfo:
    stat = path.lstat()
    info = tarfile.TarInfo(arcname)
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = epoch
    info.mode = stat.st_mode & 0o777
    info.pax_headers = {}
    if path.is_symlink():
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
        info.size = 0
    elif path.is_dir():
        info.type = tarfile.DIRTYPE
        info.size = 0
    elif path.is_file():
        info.type = tarfile.REGTYPE
        info.size = stat.st_size
    else:
        raise RuntimeError(f"unsupported sdist entry type: {path}")
    return info


def canonicalize_sdist(source: Path, destination: Path, epoch: int) -> None:
    with tempfile.TemporaryDirectory(prefix="cpython_extensions_sdist_") as temp_name:
        temp = Path(temp_name)
        root = _safe_extract(source, temp)
        entries = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                compresslevel=9,
                mtime=epoch,
            ) as zipped:
                with tarfile.open(
                    fileobj=zipped,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as target:
                    for path in entries:
                        arcname = path.relative_to(temp).as_posix()
                        info = _tarinfo(path, arcname, epoch)
                        if path.is_file() and not path.is_symlink():
                            with path.open("rb") as stream:
                                target.addfile(info, stream)
                        else:
                            target.addfile(info)


def build(out_dir: Path, epoch: int) -> tuple[Path, Path]:
    # Import only after fixing the environment used by the backend.
    os.environ["SOURCE_DATE_EPOCH"] = str(epoch)
    from setuptools import build_meta

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cpython_extensions_build_") as temp_name:
        temporary = Path(temp_name)
        raw_sdist_name = build_meta.build_sdist(str(temporary))
        wheel_name = build_meta.build_wheel(str(temporary))
        raw_sdist = temporary / raw_sdist_name
        wheel_source = temporary / wheel_name
        final_sdist = out_dir / raw_sdist_name
        final_wheel = out_dir / wheel_name
        canonicalize_sdist(raw_sdist, final_sdist, epoch)
        shutil.copyfile(wheel_source, final_wheel)
    return final_sdist, final_wheel


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--epoch",
        type=int,
        default=(int(os.environ["SOURCE_DATE_EPOCH"]) if "SOURCE_DATE_EPOCH" in os.environ else None),
        help="fixed Unix timestamp; defaults to SOURCE_DATE_EPOCH",
    )
    args = parser.parse_args()
    if args.epoch is None:
        parser.error("--epoch or SOURCE_DATE_EPOCH is required for reproducible output")
    if args.epoch < 315532800:  # 1980-01-01, wheel ZIP timestamp floor
        parser.error("epoch must be >= 1980-01-01 for wheel reproducibility")

    sdist, wheel = build(args.out_dir, args.epoch)
    checksums = args.out_dir / "SHA256SUMS.txt"
    lines = [
        f"{_sha256(wheel)}  {wheel.name}",
        f"{_sha256(sdist)}  {sdist.name}",
    ]
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
