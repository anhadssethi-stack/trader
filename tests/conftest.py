import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nse_research.settings import load_settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return load_settings(overrides={"data": {"db_path": str(tmp_path / "t.duckdb"),
                                             "cache_dir": str(tmp_path / "cache")}})


@pytest.fixture
def cfg(settings):
    return settings["scanner"]


def make_ohlc(close, start="2024-01-01", vol=1e5):
    close = np.asarray(close, float)
    dates = pd.bdate_range(start, periods=len(close))
    return pd.DataFrame({"date": dates, "open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": np.full(len(close), vol, float),
                         "turnover": close * vol})
