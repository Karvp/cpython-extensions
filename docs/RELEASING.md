# Releasing

The repository is designed so release artifacts are produced from a clean tagged commit and can be reproduced from the same source and epoch.

## 1. Prepare the release

1. Update `src/python_extensions/_version.py`.
2. Add the release section to `CHANGELOG.md`.
3. Update certification/audit documents when the runtime changes.
4. Run all required tests and relevant full-scale stress harnesses.
5. Run repository hygiene checks:

```bash
python tools/check_repo.py
python -m pytest
python -X dev -W error -m pytest
python -m compileall -q src tests
```

## 2. Build reproducibly

`tools/build_release.py` requires a fixed Unix epoch. A release should normally use the tagged commit timestamp:

```bash
EPOCH=$(git show -s --format=%ct HEAD)
python tools/build_release.py --out-dir dist --epoch "$EPOCH"
```

On PowerShell:

```powershell
$epoch = git show -s --format=%ct HEAD
python tools/build_release.py --out-dir dist --epoch $epoch
```

The command writes a canonical sdist, wheel, and `SHA256SUMS.txt`.

Build twice from the same clean commit/epoch and compare hashes before claiming reproducibility.

## 3. Validate artifacts

Install and smoke-test the exact wheel and sdist, not merely the checkout. The GitHub release workflow performs this automatically.

At minimum:

```bash
python -m pip install -U build twine
python -m twine check dist/*
```

Also extract the sdist and run the full unit suite from the extracted source. Rebuilding a wheel from that sdist should reproduce the shipped wheel when the same epoch is used.

## 4. Tag

The tag must match the package version exactly:

```bash
git tag -s v1.0.3 -m "cpython-extensions 1.0.3"
git push origin v1.0.3
```

Use an unsigned annotated tag if signing is not configured, but signed tags are preferred.

The workflow checks that `vX.Y.Z` equals `python_extensions.__version__` before creating release artifacts.

## 5. GitHub release

A pushed `v*` tag builds and validates artifacts, then creates a GitHub Release with the wheel, sdist, and SHA-256 manifest.

## 6. PyPI Trusted Publishing

PyPI publishing is deliberately disabled by default. To enable it:

1. Create/configure the `cpython-extensions` project and Trusted Publisher on PyPI.
2. Configure the GitHub environment named `pypi` and its protection rules.
3. Add repository variable `PYPI_PUBLISH_ENABLED=true`.

The publish job uses OIDC (`id-token: write`) and does not require a stored PyPI API token.

Keep build and publish jobs separate; the publish job should only download already-built artifacts and send them to PyPI.
