"""Global test configuration: isolate tests from a developer's local site_overrides/."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Tests always run against the fictional demo plant, never against local plant data.
os.environ["SITE_OVERRIDES_DIR"] = str(REPO_ROOT / "tests" / "_no_overrides")
os.environ["SITE_DEFAULTS_DIR"] = str(REPO_ROOT / "examples" / "demo_plant")
os.environ.setdefault("SITE_TIMEZONE", "Europe/Berlin")


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
