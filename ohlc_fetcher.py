"""
ohlc_fetcher.py
================
Production-grade OHLC (Open / High / Low / Close / Volume) historical-data
fetcher built on the ``vnstock`` library (v4.x), for the NCKH research project.

This single module is the implementation referenced by the accompanying report
``docs/VNSTOCK_OHLC_REPORT.md`` (researcher diagnostic, QA-finance checks,
solution architecture and tech-lead audit).

Behaviours handled (all verified against vnstock 4.0.4 — see the report):
  * Uses the MODERN API (``vnstock.api.quote.Quote``). The legacy
    ``Vnstock().stock()`` path is deprecated as of 2025-08-31.
  * Forces UTF-8 stdout *before* importing vnstock so the library's Vietnamese
    banners do not raise UnicodeEncodeError on Windows consoles that use a
    legacy code page (cp1252 / cp1258).
  * VCI returns *adjusted* prices (back-adjusted for splits & dividends), in
    units of 1,000 VND. Good for return series; see report for the caveat.
  * The provider may return bars *before* the requested ``start``; we filter
    the frame strictly to ``[start, end]``.
  * Invalid symbols and empty ranges raise ``ValueError`` inside vnstock; we
    turn those into a well-typed *empty* DataFrame and log them (no crash).
  * Respects rate limits (guest ~20 req/min, community ~60 req/min) via a
    configurable minimum inter-request interval + backoff on RateLimitExceeded.
  * Per-symbol on-disk cache makes a multi-hundred-symbol pull resumable.

Quick start
-----------
    from ohlc_fetcher import fetch_ohlc, download, validate_ohlc

    df = fetch_ohlc("FPT", "2024-01-01", "2024-06-30")        # one symbol
    panel = download(["FPT", "VNM"], "2024-01-01", "2024-06-30")  # many (long format)

CLI
---
    python ohlc_fetcher.py --symbols FPT,VNM --start 2024-01-01 --end 2024-06-30 --out ohlc.csv
    python ohlc_fetcher.py --symbols-file tickers.txt --start 2018-01-01 --end 2025-12-31 \
        --cache-dir .cache --out panel.csv
"""

from __future__ import annotations

# --- Windows console fix: must run before vnstock prints anything. ----------
import io
import sys


def _configure_stdout_utf8() -> None:
    """Make stdout/stderr tolerate vnstock's Vietnamese/emoji banner output.

    On Windows the default console encoding is often cp1252/cp1258, which
    cannot encode the library's banner characters and raises
    UnicodeEncodeError on the very first print. We re-wrap the streams in
    UTF-8 with ``errors="replace"`` so a banner can never crash a data job.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        # Python 3.7+ exposes reconfigure(); fall back to a wrapper otherwise.
        reconfigure = getattr(stream, "reconfigure", None)
        try:
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="replace")
            elif hasattr(stream, "buffer"):
                setattr(sys, name, io.TextIOWrapper(
                    stream.buffer, encoding="utf-8", errors="replace"))
        except (ValueError, AttributeError):
            # Stream already detached/redirected (e.g. pytest capture) — ignore.
            pass


_configure_stdout_utf8()

import argparse
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical OHLCV column order returned by vnstock and by this module.
OHLCV_COLUMNS: tuple[str, ...] = ("time", "open", "high", "low", "close", "volume")
_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

#: Default data source. VCI (VietCap) gives the most complete adjusted daily
#: OHLCV for Vietnamese equities in vnstock 4.x.
DEFAULT_SOURCE = "VCI"
DEFAULT_INTERVAL = "1D"

#: Sources that serve Vietnamese-equity OHLC in vnstock 4.0.4.
#: (Full enum also includes BINANCE / FMP / FMARKET for non-VN assets.)
EQUITY_SOURCES = frozenset({"VCI", "KBS", "DNSE", "MSN"})

#: Conservative default spacing between calls. Guest tier ~20 req/min => 3.0s.
#: Set to 1.0 for a registered Community account (~60 req/min).
DEFAULT_MIN_REQUEST_INTERVAL = 3.0

logger = logging.getLogger("ohlc_fetcher")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def empty_ohlc() -> pd.DataFrame:
    """Return an empty DataFrame with the canonical OHLCV schema & dtypes."""
    return pd.DataFrame({
        "time": pd.Series([], dtype="datetime64[ns]"),
        "open": pd.Series([], dtype="float64"),
        "high": pd.Series([], dtype="float64"),
        "low": pd.Series([], dtype="float64"),
        "close": pd.Series([], dtype="float64"),
        "volume": pd.Series([], dtype="int64"),
    })


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a raw vnstock frame into the canonical schema, order and dtypes.

    Defensive against future column-naming drift across sources.
    """
    if df is None or df.empty:
        return empty_ohlc()

    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"vnstock returned an unexpected schema; missing columns {missing}. "
            f"Got: {list(df.columns)}"
        )

    out = df.loc[:, list(OHLCV_COLUMNS)].copy()
    out["time"] = pd.to_datetime(out["time"])
    for col in _PRICE_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    # volume is integer shares; coerce safely (NaN -> 0 only if no NaN present).
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0).astype("int64")
    return out


def _bounds(start: str, end: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return inclusive [start, end] timestamps; a date-only `end` covers the
    whole day so intraday bars on that day are kept."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if end_ts == end_ts.normalize():  # `end` had no time component
        end_ts = end_ts + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    return start_ts, end_ts


def _is_rate_limit(exc: BaseException) -> bool:
    """vnai raises RateLimitExceeded; match by name to avoid a hard import."""
    return type(exc).__name__ == "RateLimitExceeded"


def _root_cause(exc: BaseException) -> BaseException:
    """Unwrap a ``tenacity.RetryError`` chain to the original exception.

    vnstock decorates ``history()`` with ``@retry``; when the underlying call
    keeps raising (e.g. ValueError "no data"), tenacity re-raises a
    ``RetryError`` wrapping the last attempt. We drill through to the real
    cause so the caller can classify "no data" (ValueError) vs a transient
    network error correctly.
    """
    for _ in range(5):  # bounded — guards against any cyclic wrapping
        if type(exc).__name__ != "RetryError":
            break
        last = getattr(exc, "last_attempt", None)
        inner = None
        if last is not None:
            try:
                inner = last.exception()
            except Exception:  # noqa: BLE001 — future not in a failed state
                inner = None
        if inner is None:
            break
        exc = inner
    return exc


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Enforce a minimum wall-clock interval between successive calls."""

    def __init__(self, min_interval_s: float = DEFAULT_MIN_REQUEST_INTERVAL):
        self.min_interval = max(0.0, float(min_interval_s))
        self._last: Optional[float] = None

    def wait(self) -> None:
        now = time.monotonic()
        if self._last is not None and self.min_interval > 0:
            elapsed = now - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


# ---------------------------------------------------------------------------
# Single-symbol fetch
# ---------------------------------------------------------------------------

def fetch_ohlc(
    symbol: str,
    start: str,
    end: str,
    *,
    interval: str = DEFAULT_INTERVAL,
    source: str = DEFAULT_SOURCE,
) -> pd.DataFrame:
    """Fetch OHLCV for a single symbol, normalized and strictly date-bounded.

    Parameters
    ----------
    symbol : str
        Ticker, e.g. ``"FPT"``. Case-insensitive (upper-cased internally).
    start, end : str
        ``YYYY-MM-DD`` (or ``YYYY-MM-DD HH:MM:SS`` for intraday). Inclusive.
    interval : str
        One of ``1m 5m 15m 30m 1H 1D 1W 1M`` (vnstock TimeFrame). Default ``1D``.
    source : str
        ``VCI`` (default), ``KBS``, ``DNSE`` or ``MSN``.

    Returns
    -------
    pandas.DataFrame
        Columns ``[time, open, high, low, close, volume]`` sorted by time,
        de-duplicated, and clipped to ``[start, end]``. Returns an *empty*
        (correctly typed) frame for an unknown symbol or a range with no data,
        rather than raising.

    Notes
    -----
    Raises ``ValueError`` only for caller mistakes (bad ``source``/``interval``).
    Network/rate-limit exceptions propagate so the batch layer can handle them.
    """
    symbol = symbol.strip().upper()
    src = source.strip().upper()
    if src not in EQUITY_SOURCES:
        raise ValueError(f"source must be one of {sorted(EQUITY_SOURCES)}, got {source!r}")

    # Imported lazily so _configure_stdout_utf8() has already run.
    from vnstock.api.quote import Quote

    try:
        quote = Quote(symbol=symbol, source=src)
        raw = quote.history(start=start, end=end, interval=interval)
    except Exception as exc:  # noqa: BLE001 — classified immediately below
        # vnstock raises ValueError for "Invalid symbol" / empty ranges, but its
        # @retry wrapper may re-raise it as tenacity.RetryError. Unwrap, then:
        #   * ValueError  -> permanent "no data": return empty + warn.
        #   * anything else (ConnectionError, RateLimitExceeded, ...) -> propagate
        #     so the batch layer can retry/back off/record it as a real failure.
        root = _root_cause(exc)
        if isinstance(root, ValueError):
            logger.warning("No data for %s [%s..%s] (%s): %s",
                           symbol, start, end, src, str(root).splitlines()[0])
            return empty_ohlc()
        raise

    df = _normalize(raw)
    if df.empty:
        logger.warning("Empty frame for %s [%s..%s]", symbol, start, end)
        return df

    lo, hi = _bounds(start, end)
    df = df[(df["time"] >= lo) & (df["time"] <= hi)]
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# QA / finance validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationReport:
    symbol: Optional[str]
    rows: int
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        tag = "OK" if self.ok else "FAIL"
        head = f"[{tag}] {self.symbol or '<frame>'}: {self.rows} rows"
        lines = [head]
        lines += [f"  ERROR: {e}" for e in self.errors]
        lines += [f"  WARN : {w}" for w in self.warnings]
        return "\n".join(lines)


def validate_ohlc(
    df: pd.DataFrame,
    *,
    symbol: Optional[str] = None,
    jump_threshold: float = 0.40,
) -> ValidationReport:
    """Finance-grade integrity checks on an OHLCV frame.

    Errors (hard) make ``ok=False``; warnings are advisory (e.g. likely
    corporate-action jumps). An empty frame is reported but not an error —
    "no data" is a legitimate result for delisted/young tickers.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Accept a tidy panel slice too: drop a leading `symbol` column if present
    # and infer the symbol from it when the caller didn't pass one.
    if "symbol" in df.columns:
        if symbol is None and not df.empty:
            uniq = df["symbol"].unique()
            symbol = uniq[0] if len(uniq) == 1 else f"<{len(uniq)} symbols>"
        df = df.drop(columns="symbol")

    # Schema & dtypes ------------------------------------------------------
    if list(df.columns) != list(OHLCV_COLUMNS):
        errors.append(f"unexpected columns {list(df.columns)}")
        return ValidationReport(symbol, len(df), False, errors, warnings)

    if df.empty:
        warnings.append("empty frame (no data in range)")
        return ValidationReport(symbol, 0, True, errors, warnings)

    if not pd.api.types.is_datetime64_any_dtype(df["time"]):
        errors.append("`time` is not datetime")

    # Nulls ----------------------------------------------------------------
    for col in _PRICE_COLUMNS:
        n = int(df[col].isna().sum())
        if n:
            errors.append(f"{n} null value(s) in `{col}`")

    # Positivity -----------------------------------------------------------
    nonpos = (df[list(_PRICE_COLUMNS)] <= 0).any(axis=1).sum()
    if nonpos:
        errors.append(f"{int(nonpos)} row(s) with non-positive price(s)")
    if (df["volume"] < 0).any():
        errors.append("negative volume present")

    # OHLC ordering invariants --------------------------------------------
    bad_hl = int((df["high"] < df["low"]).sum())
    if bad_hl:
        errors.append(f"{bad_hl} row(s) where high < low")
    bad_hi = int(((df["high"] < df["open"]) | (df["high"] < df["close"])).sum())
    if bad_hi:
        errors.append(f"{bad_hi} row(s) where high < open/close")
    bad_lo = int(((df["low"] > df["open"]) | (df["low"] > df["close"])).sum())
    if bad_lo:
        errors.append(f"{bad_lo} row(s) where low > open/close")

    # Time ordering / duplicates ------------------------------------------
    if df["time"].duplicated().any():
        errors.append(f"{int(df['time'].duplicated().sum())} duplicate timestamp(s)")
    if not df["time"].is_monotonic_increasing:
        errors.append("time column is not sorted ascending")

    # Advisory: large close-to-close jumps (possible split / data glitch) --
    if len(df) > 1:
        ret = df["close"].pct_change().abs()
        jumps = int((ret > jump_threshold).sum())
        if jumps:
            warnings.append(
                f"{jumps} day(s) with |close-to-close move| > {jump_threshold:.0%} "
                "(verify corporate actions / adjustment)"
            )

    return ValidationReport(symbol, len(df), len(errors) == 0, errors, warnings)


# ---------------------------------------------------------------------------
# Batch download with cache + rate limiting
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: Path, symbol: str, interval: str, source: str) -> Path:
    return cache_dir / f"{source.upper()}_{symbol.upper()}_{interval}.csv"


def _read_cache(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["time"])
        return _normalize(df) if not df.empty else empty_ohlc()
    except Exception as exc:  # corrupt cache — re-fetch
        logger.warning("Ignoring unreadable cache %s: %s", path.name, exc)
        return None


def download(
    symbols: Iterable[str],
    start: str,
    end: str,
    *,
    interval: str = DEFAULT_INTERVAL,
    source: str = DEFAULT_SOURCE,
    cache_dir: Optional[str | Path] = None,
    min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
    max_rate_retries: int = 3,
    validate: bool = True,
) -> pd.DataFrame:
    """Download OHLCV for many symbols and return a tidy long-format frame.

    Returns columns ``[symbol, time, open, high, low, close, volume]``.

    Robustness:
      * Per-symbol on-disk cache (CSV) in ``cache_dir`` makes the job resumable —
        re-running skips symbols already fetched.
      * ``min_request_interval`` spaces calls to stay under the rate limit.
      * On ``RateLimitExceeded`` it backs off (60s) and retries up to
        ``max_rate_retries`` times; on any other unexpected error it logs and
        skips the symbol so one bad ticker can't abort the whole run.
    """
    symbols = [s.strip().upper() for s in symbols if s and s.strip()]
    symbols = list(dict.fromkeys(symbols))  # de-dup, preserve order
    limiter = RateLimiter(min_request_interval)
    cache = Path(cache_dir) if cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    total = len(symbols)
    logger.info("Downloading %d symbol(s) %s [%s..%s] from %s",
                total, interval, start, end, source)

    for i, symbol in enumerate(symbols, 1):
        df: Optional[pd.DataFrame] = None

        # 1) cache hit -----------------------------------------------------
        if cache:
            df = _read_cache(_cache_path(cache, symbol, interval, source))
            if df is not None:
                logger.debug("cache hit %s", symbol)

        # 2) fetch with rate-limit backoff --------------------------------
        if df is None:
            for attempt in range(max_rate_retries + 1):
                limiter.wait()
                try:
                    df = fetch_ohlc(symbol, start, end, interval=interval, source=source)
                    break
                except Exception as exc:  # noqa: BLE001 — boundary, logged below
                    if _is_rate_limit(_root_cause(exc)) and attempt < max_rate_retries:
                        logger.warning("Rate limit on %s; backing off 60s "
                                       "(attempt %d/%d)", symbol, attempt + 1, max_rate_retries)
                        time.sleep(60)
                        continue
                    logger.error("Failed %s: %s: %s", symbol, type(exc).__name__, exc)
                    failures.append(symbol)
                    df = empty_ohlc()
                    break
            if cache and df is not None:
                _cache_path(cache, symbol, interval, source).write_text(
                    df.to_csv(index=False), encoding="utf-8")

        # 3) validate ------------------------------------------------------
        if validate and df is not None and not df.empty:
            report = validate_ohlc(df, symbol=symbol)
            if not report.ok:
                logger.warning("Validation issues for %s:\n%s", symbol, report)

        # 4) accumulate (long format) -------------------------------------
        if df is not None and not df.empty:
            tagged = df.copy()
            tagged.insert(0, "symbol", symbol)
            frames.append(tagged)

        if i % 20 == 0 or i == total:
            logger.info("progress %d/%d (%d ok, %d failed)",
                        i, total, len(frames), len(failures))

    if failures:
        logger.warning("%d symbol(s) failed: %s", len(failures), ", ".join(failures))

    if not frames:
        out = empty_ohlc()
        out.insert(0, "symbol", pd.Series([], dtype="object"))
        return out
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Convenience transform — reproduces the original tho.py output
# ---------------------------------------------------------------------------

def year_end_close_pivot(panel: pd.DataFrame,
                         year_min: Optional[int] = None,
                         year_max: Optional[int] = None) -> pd.DataFrame:
    """From a long OHLCV panel, build a (symbol x year) table of the last
    closing price of each year — i.e. the original ``tho.py`` deliverable,
    but driven by validated data.
    """
    if panel.empty:
        return pd.DataFrame()
    df = panel.copy()
    df["year"] = df["time"].dt.year
    last = (df.sort_values("time")
              .groupby(["symbol", "year"], as_index=False)
              .last())
    if year_min is not None:
        last = last[last["year"] >= year_min]
    if year_max is not None:
        last = last[last["year"] <= year_max]
    return last.pivot(index="symbol", columns="year", values="close")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_symbols_file(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    # Accept newline- or comma-separated, ignore blanks / # comments.
    raw = text.replace(",", "\n").splitlines()
    return [s.strip().upper() for s in raw if s.strip() and not s.strip().startswith("#")]


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Fetch OHLC data via vnstock (v4.x).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--symbols", help="comma-separated tickers, e.g. FPT,VNM")
    src.add_argument("--symbols-file", help="file with tickers (newline/comma separated)")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--interval", default=DEFAULT_INTERVAL,
                   help="1m 5m 15m 30m 1H 1D 1W 1M (default 1D)")
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help=f"one of {sorted(EQUITY_SOURCES)} (default VCI)")
    p.add_argument("--out", required=True, help="output path (.csv or .xlsx)")
    p.add_argument("--cache-dir", default=None, help="per-symbol cache dir (resumable)")
    p.add_argument("--min-interval", type=float, default=DEFAULT_MIN_REQUEST_INTERVAL,
                   help="seconds between requests (3.0 guest, 1.0 community)")
    p.add_argument("--pivot-year-close", action="store_true",
                   help="write a symbol x year year-end-close table instead of the raw panel")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    symbols = (_read_symbols_file(args.symbols_file)
               if args.symbols_file else
               [s.strip().upper() for s in args.symbols.split(",") if s.strip()])
    if not symbols:
        logger.error("no symbols provided")
        return 2

    panel = download(
        symbols, args.start, args.end,
        interval=args.interval, source=args.source,
        cache_dir=args.cache_dir, min_request_interval=args.min_interval,
    )

    out = Path(args.out)
    result = (year_end_close_pivot(panel) if args.pivot_year_close else panel)
    if out.suffix.lower() in (".xlsx", ".xls"):
        result.to_excel(out, index=args.pivot_year_close)
    else:
        result.to_csv(out, index=args.pivot_year_close)

    rows = len(result)
    syms = result.index.nunique() if args.pivot_year_close else result["symbol"].nunique() if rows else 0
    logger.info("Wrote %s (%d rows, %d symbols)", out, rows, syms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
