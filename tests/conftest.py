from __future__ import annotations

import pytest
from helpers import BASELINE_CONFIG, ROOT

from flightdrop.config import Config, load_config


@pytest.fixture
def cfg() -> Config:
    """Neutral baseline config (anywhere in the world), independent of the user's own config.yaml."""
    return load_config(BASELINE_CONFIG)


@pytest.fixture
def shipped_cfg() -> Config:
    """The real config.yaml the bot runs with; only for tests about the user's actual settings."""
    return load_config(ROOT / "config.yaml")
