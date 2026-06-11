"""
Offline unit tests for ohlc_fetcher (no network required).

Covers the pure logic that the QA-finance role cares about: schema
normalization, strict date bounding, and the OHLC integrity validator.
The live vnstock call is exercised separately (see report Part 3 test plan).

Run:  python -m pytest tests/ -q
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ohlc_fetcher import (  # noqa: E402
    OHLCV_COLUMNS, empty_ohlc, _normalize, _bounds, _root_cause,
    validate_ohlc, year_end_close_pivot,
)


def _good_frame():
    return pd.DataFrame({
        "time": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        "open": [69.95, 69.01, 69.52],
        "high": [70.10, 69.66, 71.03],
        "low": [69.09, 68.87, 69.45],
        "close": [69.23, 69.52, 70.17],
        "volume": [1717109, 1439581, 2978466],
    })


# --- schema / normalization -------------------------------------------------

def test_empty_ohlc_schema_and_dtypes():
    e = empty_ohlc()
    assert list(e.columns) == list(OHLCV_COLUMNS)
    assert str(e["time"].dtype) == "datetime64[ns]"
    assert str(e["volume"].dtype) == "int64"


def test_normalize_reorders_and_coerces():
    raw = pd.DataFrame({
        "volume": ["100", "200"],            # string volume
        "close": [70.0, 71.0],
        "open": [69.0, 70.5],
        "high": [70.5, 71.2],
        "low": [68.8, 70.1],
        "time": ["2024-01-02", "2024-01-03"],  # string time
    })
    out = _normalize(raw)
    assert list(out.columns) == list(OHLCV_COLUMNS)
    assert str(out["time"].dtype) == "datetime64[ns]"
    assert str(out["volume"].dtype) == "int64"
    assert out["volume"].tolist() == [100, 200]


def test_normalize_missing_column_raises():
    raw = _good_frame().drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing columns"):
        _normalize(raw)


# --- RetryError unwrapping (the GEL/AAN/HPA/AAD case) ----------------------

def test_root_cause_unwraps_retryerror_to_valueerror():
    from tenacity import RetryError, Future
    fut = Future(attempt_number=1)
    fut.set_exception(ValueError("Khong tim thay du lieu"))
    root = _root_cause(RetryError(fut))
    assert isinstance(root, ValueError)
    assert "Khong tim thay" in str(root)


def test_root_cause_passthrough_for_plain_exception():
    e = ConnectionError("boom")
    assert _root_cause(e) is e


# --- date bounds ------------------------------------------------------------

def test_bounds_date_only_end_is_whole_day():
    lo, hi = _bounds("2024-01-02", "2024-01-10")
    assert lo == pd.Timestamp("2024-01-02 00:00:00")
    assert hi == pd.Timestamp("2024-01-10 23:59:59")


# --- validation -------------------------------------------------------------

def test_validate_good_frame_ok():
    rep = validate_ohlc(_good_frame(), symbol="FPT")
    assert rep.ok
    assert rep.rows == 3
    assert rep.errors == []


def test_validate_empty_is_ok_with_warning():
    rep = validate_ohlc(empty_ohlc(), symbol="DEAD")
    assert rep.ok
    assert any("empty" in w for w in rep.warnings)


def test_validate_high_lt_low_fails():
    df = _good_frame()
    df.loc[1, "high"] = 1.0  # high < low
    rep = validate_ohlc(df)
    assert not rep.ok
    assert any("high < low" in e for e in rep.errors)


def test_validate_high_below_close_fails():
    df = _good_frame()
    df.loc[0, "high"] = df.loc[0, "close"] - 1
    rep = validate_ohlc(df)
    assert not rep.ok
    assert any("high < open/close" in e for e in rep.errors)


def test_validate_nonpositive_price_fails():
    df = _good_frame()
    df.loc[2, "low"] = 0.0
    rep = validate_ohlc(df)
    assert not rep.ok
    assert any("non-positive" in e for e in rep.errors)


def test_validate_negative_volume_fails():
    df = _good_frame()
    df.loc[0, "volume"] = -5
    rep = validate_ohlc(df)
    assert not rep.ok
    assert any("negative volume" in e for e in rep.errors)


def test_validate_duplicate_timestamp_fails():
    df = _good_frame()
    df.loc[1, "time"] = df.loc[0, "time"]
    rep = validate_ohlc(df)
    assert not rep.ok
    assert any("duplicate" in e for e in rep.errors)


def test_validate_unsorted_time_fails():
    df = _good_frame().iloc[::-1].reset_index(drop=True)
    rep = validate_ohlc(df)
    assert not rep.ok
    assert any("not sorted" in e for e in rep.errors)


def test_validate_big_jump_warns_not_errors():
    df = _good_frame()
    df.loc[2, ["open", "high", "low", "close"]] = [200, 205, 199, 204]  # +190%
    rep = validate_ohlc(df, jump_threshold=0.40)
    assert rep.ok  # advisory only
    assert any("close-to-close" in w for w in rep.warnings)


# --- transform --------------------------------------------------------------

def test_year_end_close_pivot():
    panel = pd.DataFrame({
        "symbol": ["FPT", "FPT", "FPT", "VNM"],
        "time": pd.to_datetime(["2023-12-28", "2023-12-29", "2024-12-31", "2024-12-31"]),
        "open": [1, 1, 1, 1], "high": [1, 1, 1, 1], "low": [1, 1, 1, 1],
        "close": [69.74, 69.37, 95.0, 60.0],
        "volume": [1, 1, 1, 1],
    })
    pivot = year_end_close_pivot(panel)
    assert pivot.loc["FPT", 2023] == 69.37   # last session of 2023
    assert pivot.loc["FPT", 2024] == 95.0
    assert pd.isna(pivot.loc["VNM", 2023])   # VNM has no 2023 row
