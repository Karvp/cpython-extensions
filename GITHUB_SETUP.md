# GitHub Repository Setup

This document records the recommended GitHub configuration for the canonical repository:

- **Repository:** <https://github.com/Karvp/cpython-extensions>
- **Default branch:** `main`
- **Package:** `cpython-extensions`
- **Import package:** `python_extensions`
- **Current release:** `1.2.0`

## Clone or connect the repository

For a new local checkout:

```powershell
git clone https://github.com/Karvp/cpython-extensions.git
cd cpython-extensions
```

If an existing local checkout does not yet have the remote configured:

```powershell
git remote add origin https://github.com/Karvp/cpython-extensions.git
git push -u origin main
```

SSH users can use:

```powershell
git remote add origin git@github.com:Karvp/cpython-extensions.git
```

## Recommended repository settings

1. Keep `main` as the default branch.
2. Enable the dependency graph, Dependabot alerts, and Dependabot updates. The dependency-review workflow requires the dependency graph.
3. Enable CodeQL/code scanning and private vulnerability reporting.
4. Protect `main` with pull requests and the normal CI/package-validation checks.
5. Disallow force-pushes and branch deletion on `main`.
6. Enable secret scanning and push protection when available.
7. Prefer signed release tags when the maintainer signing workflow is configured.

## PyPI Trusted Publishing

The release workflow builds and certifies artifacts for validated `v*` tags. Only the exact stable tag for the package version can enter the PyPI publishing job; preview tags such as `v1.2.0-beta` create GitHub prereleases and are never eligible for PyPI.

Configure publishing as follows:

1. Configure `Karvp/cpython-extensions` as a Trusted Publisher for the `cpython-extensions` project on PyPI.
2. Create a protected GitHub environment named `pypi`.
3. Add the desired required reviewers or other deployment-protection rules to that environment.
4. If the tag pusher is the only required reviewer, either add another eligible reviewer or disable **Prevent self-review**; otherwise the deployment cannot be approved.

No PyPI API token is required in GitHub secrets. Publishing uses GitHub OIDC with `id-token: write` only in the dedicated PyPI job.

Do **not** gate that job on an environment-scoped variable in a job-level `if:` expression. GitHub evaluates job eligibility before the job enters the environment, so those variables are unavailable at that point. The workflow instead gates publication on the validated stable release channel and uses the protected `pypi` environment as the approval boundary.

## Release tag

The source version is `1.2.0`. After CI is green and the release commit is final:

```powershell
git tag -a v1.2.0 -m "cpython-extensions 1.2.0"
git push origin v1.2.0
```

If `v1.2.0` already exists remotely, do not move or overwrite a published stable tag. Increment the package version and changelog instead.

For a release-pipeline rehearsal, use an allowed preview tag such as:

```powershell
git tag -a v1.2.0-beta -m "cpython-extensions 1.2.0 release rehearsal"
git push origin v1.2.0-beta
```

## Package metadata links

The package metadata points to the canonical project resources:

- Homepage: <https://github.com/Karvp/cpython-extensions>
- Repository: <https://github.com/Karvp/cpython-extensions>
- Issues: <https://github.com/Karvp/cpython-extensions/issues>
- Documentation: <https://github.com/Karvp/cpython-extensions/blob/main/docs/COMPREHENSIVE_GUIDE.md>
- PyPI: <https://pypi.org/project/cpython-extensions/>

See [`.github/REPOSITORY_METADATA.md`](.github/REPOSITORY_METADATA.md) for the recommended description, topics, labels, and repository-policy settings.
