from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK_VERSION = ROOT / "tools" / "check_version.py"


def _load_check_version():
    spec = importlib.util.spec_from_file_location("check_version_test_module", CHECK_VERSION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_tag_channels():
    module = _load_check_version()
    version = module.package_version()

    assert module.tag_channel(f"v{version}", version) == "stable"
    for suffix in ("alpha", "alpha1", "beta", "beta2", "rc1", "preview.2", "test-3"):
        assert module.tag_channel(f"v{version}-{suffix}", version) == "preview"


@pytest.mark.parametrize(
    "tag",
    [
        "v1.0.2",
        "1.0.3",
        "v1.0.3-nightly",
        "v1.0.3-beta-extra",
        "v1.0.3-",
    ],
)
def test_release_tag_rejects_unapproved_shapes(tag: str):
    module = _load_check_version()
    with pytest.raises(SystemExit):
        module.tag_channel(tag, module.package_version())
