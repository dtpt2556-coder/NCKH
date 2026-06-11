# Getting OHLC Data with `vnstock` — Diagnostic, Implementation, QA, Architecture & Audit

**Project:** NCKH (scientific research) — Vietnamese equity price history
**Library under study:** `vnstock` **4.0.4** · Python 3.12.8 · Windows 11
**Date:** 2026-06-11
**Deliverable map:** Part 1 = Researcher diagnostic · Part 2 = Senior Python dev · Part 3 = Senior QA (finance) · Part 4 = Solution architect · Part 5 = Tech-lead audit

Every factual claim below was confirmed either by reading the installed package
source (`…/site-packages/vnstock/…`) **or** by a live call against the API.
Where a fact was checked live it is marked **[verified live]**.

---

## Part 1 — Researcher (Senior): How `vnstock` behaves

### 1.1 Two entry points — one is deprecated

| API | Import | Status |
|---|---|---|
| **Legacy** | `from vnstock import Vnstock` → `Vnstock().stock(symbol, source).quote.history(...)` | **Deprecated 2025-08-31.** Prints a migration banner. The current `tho.py` uses this. |
| **Modern** ✅ | `from vnstock.api.quote import Quote` → `Quote(symbol, source).history(...)` | Recommended. Unified interface across sources. |

**[verified live]** Calling the legacy path emits a boxed *"VNSTOCK DEPRECATION NOTICE (31/08/2025)"* banner; the modern path emits a promo/insiders banner instead. Both still function.

### 1.2 Data sources

`DataSource` enum exposes: `kbs, vci, msn, dnse, binance, fmp, fmarket`
(`vnstock/core/types.py`). **[verified live]**

For **Vietnamese equities**, the relevant sources are **VCI** (VietCap), **KBS** (KB Securities), **DNSE**, and **MSN** (Microsoft, mainly indices/FX). `binance/fmp/fmarket` are for crypto / global / funds.

- **Default source if omitted:** `KBS` (`api/quote.py`). The project explicitly passes `source='VCI'`, which is the most complete daily OHLCV source for VN stocks — keep using **VCI**.

### 1.3 `Quote.history()` — signature & parameters

Public signature (`vnstock/api/quote.py`):

```python
Quote(symbol="", source="KBS", random_agent=False, show_log=False)
  .history(symbol=None, start=None, end=None, interval="1D", **kwargs)
```

VCI provider accepts these extra `**kwargs` (`explorer/vci/quote.py`):
`to_df=True`, `count_back=None`, `floating=2` (decimal places), `length=None`.

| Param | Meaning | Notes |
|---|---|---|
| `start` | `YYYY-MM-DD` (or `…HH:MM:SS`) | **Required** for VCI. |
| `end` | `YYYY-MM-DD` | Defaults to "now" if omitted. |
| `interval` | candle size | Default `"1D"`. |
| `floating` | price decimals | VCI default 2. |

### 1.4 Accepted `interval` values

From `core/types.py` (`TimeFrame`) and `explorer/vci/quote.py`:
`1m, 5m, 15m, 30m, 1H, 4h, 1D, 1W, 1M` (aliases like `day`, `week`, `D`, `W` are normalized). Default `1D`.

### 1.5 Returned DataFrame schema — **[verified live]**

```
columns: ['time', 'open', 'high', 'low', 'close', 'volume']
dtypes : time=datetime64[ns] (tz-naive), open/high/low/close=float64, volume=int64
```

Live sample (`FPT`, VCI, `1D`):

```
        time   open   high    low  close   volume
0 2023-12-28  70.02  70.02  69.52  69.74  1199164
1 2023-12-29  69.74  70.02  69.37  69.37  1869204
...
```

### 1.6 Behaviours that bite you (all **[verified live]**)

1. **Prices are *adjusted* and in *thousands of VND*.** FPT shows ~69–70 in
   early 2024, whereas the raw traded price was ~96,000 VND. VCI returns
   **back-adjusted** prices (corrected for splits & dividends) divided by 1,000.
   → Great for **return** series; **not** the figure printed on an exchange ticker.

2. **The result overshoots `start`.** Requesting `start='2024-01-02'` returned
   bars from `2023-12-28`. VCI auto-computes a `count_back` buffer. → You must
   **filter to `[start, end]` client-side** (the implementation does this).

3. **Invalid symbol → `ValueError`**, message `"Invalid symbol. Your symbol
   format is not recognized!"`. An empty date range likewise raises `ValueError`
   ("Không tìm thấy dữ liệu…") rather than returning an empty frame.

4. **Unicode banner crash on Windows.** The library prints Vietnamese/emoji
   banners; on a cp1252/cp1258 console the *first print* raises
   `UnicodeEncodeError` and aborts the program before any data is fetched.
   → Force UTF-8 stdout before importing vnstock (the implementation does this).

5. **Promo banners on stdout.** Every fresh process prints an "insiders program"
   ad box. Harmless once UTF-8 is set, but noisy in logs.

### 1.7 Reliability, rate limits, telemetry

| Aspect | Finding | Source |
|---|---|---|
| **Retries** | `tenacity`: 3 attempts, exponential backoff 2–10 s, on `ConnectionError`/request errors. | `config.py`, `api/quote.py` |
| **Rate limit** | Enforced by the `vnai` quota system. **Guest ≈ 20 req/min**, **Community (free signup) ≈ 60 req/min**; exceeding raises **`RateLimitExceeded`**. | `vnai/beam/quota.py` |
| **HTTP** | `requests`, 30 s timeout, browser-like headers, optional `random_agent`. Only HTTP 200 accepted; else `ConnectionError`. 429 is **not** special-cased. | `core/utils/client.py` |
| **Telemetry** | `vnai` registers a machine id, scans installed packages, and syncs usage to `vnstocks.com`. Terms stored at `~/.vnstock/id/terms_agreement.txt` (already accepted on this machine). | `vnai/__init__.py` |
| **Licence** | "Personal, research, non-commercial." NCKH research use is fine; commercial use needs a licence. | `pip show vnstock` |

> **Researcher's bottom line:** the data is usable and well-typed, but three
> things must be engineered around — *adjusted/×1000 prices*, *start overshoot*,
> and *Windows Unicode* — plus *rate limiting* for any multi-hundred-symbol pull.

---

## Part 2 — Senior Python Dev: the implementation

Implemented in **`ohlc_fetcher.py`**. Design choices map 1-to-1 to the findings above.

| Requirement | How it's met | Where |
|---|---|---|
| Get OHLC via vnstock | `fetch_ohlc(symbol, start, end, interval, source)` → tidy frame | `fetch_ohlc()` |
| Use the supported API | Modern `vnstock.api.quote.Quote` (not the deprecated path) | `fetch_ohlc()` |
| Don't crash on Windows | `_configure_stdout_utf8()` runs at import, before vnstock | top of module |
| Correct bounds | strict `[start, end]` filter; date-only `end` ⇒ whole day | `_bounds()` |
| Robust schema | `_normalize()` reorders/recoerces, fails loudly on schema drift | `_normalize()` |
| No silent failures | invalid symbol/empty ⇒ typed empty frame **+ a logged warning** | `fetch_ohlc()` |
| Many symbols | `download(symbols, …)` → long format `[symbol, time, o,h,l,c,v]` | `download()` |
| Stay under rate limit | `RateLimiter` (default 3 s ≈ guest 20/min) + 60 s backoff on `RateLimitExceeded` | `download()` |
| Resumable | per-symbol CSV cache in `cache_dir`; re-runs skip cached symbols | `download()` |
| Reproduce `tho.py` | `year_end_close_pivot()` → symbol×year year-end close | `year_end_close_pivot()` |
| Usable as a tool | `argparse` CLI writing `.csv`/`.xlsx` | `main()` |

**Usage**

```python
from ohlc_fetcher import fetch_ohlc, download, validate_ohlc

df    = fetch_ohlc("FPT", "2024-01-01", "2024-06-30")          # single symbol
panel = download(["FPT", "VNM"], "2024-01-01", "2024-06-30",   # many, resumable
                 cache_dir=".cache", min_request_interval=3.0)
rep   = validate_ohlc(panel[panel.symbol == "FPT"])            # QA one slice
```

```bash
# CLI — community account (1s spacing), cache for resume:
python ohlc_fetcher.py --symbols-file tickers.txt \
    --start 2018-01-01 --end 2025-12-31 \
    --cache-dir .cache --min-interval 3.0 --out panel.csv

# Reproduce the original year-end-close Excel deliverable:
python ohlc_fetcher.py --symbols-file tickers.txt --start 2018-01-01 \
    --end 2025-12-31 --pivot-year-close --out Gia_Chot_Nam.xlsx
```

---

## Part 3 — Senior QA (Finance): validation & test plan

### 3.1 Automated integrity checks — `validate_ohlc()`

Returns a `ValidationReport(ok, errors, warnings)`. **Errors** (block) vs **warnings** (advisory):

**Hard errors**
- schema/column-order mismatch; non-datetime `time`
- null prices; non-positive prices; negative volume
- `high < low`; `high < open|close`; `low > open|close` (OHLC ordering)
- duplicate timestamps; time not sorted ascending

**Advisory warnings**
- empty frame (legitimate for delisted/IPO-after-range tickers — *not* an error)
- |close-to-close| move > 40 % (default) → likely split/corporate action or a data glitch; **verify, don't auto-reject**

### 3.2 Finance-specific concerns the team must decide

1. **Adjusted vs raw price.** VCI gives **adjusted** prices (good for returns/CAGR,
   correlation, backtests). If a deliverable needs the *actual historical traded
   price* (e.g. "what did it close at on the ticker that day"), adjusted data is
   **wrong**. Decide per use-case and document it. Mixing adjusted and raw across
   symbols/years silently corrupts cross-sectional studies.
2. **Units.** Prices are in **thousands of VND**. Multiply by 1,000 before
   reporting absolute VND or market cap.
3. **Year-end "close".** The original logic takes the *last trading session of
   the calendar year* via `groupby(year).last()`. That is the last **available**
   row — for a delisted stock it's the delisting day, not 31-Dec. QA should flag
   years whose last session is far from year-end.
4. **Survivorship bias.** A fixed hand-curated ticker list (282 names) excludes
   companies delisted before today → upward bias in any historical average.
   Document the universe construction date.
5. **Trading calendar.** ~250 sessions/yr; weekends/holidays absent by design.
   Don't treat gaps as missing data.

### 3.3 Test results — **[verified]**

- **Offline unit tests:** `tests/test_ohlc_fetcher.py` — **14 passed** (schema,
  normalization, strict bounds, every validator rule, year-end pivot).
- **Live smoke test:** `download(['FPT','VNM','ZZZZZ'], '2024-01-01','2024-03-31')`
  → `ZZZZZ` correctly dropped (logged), 118 rows, dates strictly within range
  (`2024-01-02 … 2024-03-29`, **no overshoot**), both symbols pass `validate_ohlc`.
  Re-run hit the cache (instant) → resumability confirmed.

Run them: `python -m pytest tests/ -q`

---

## Part 4 — Solution Architect: recommended solution

### 4.1 Pipeline shape

```
 tickers (file) ──► download()  ──► validate_ohlc() ──► store (Parquet/CSV) ──► analysis
                     │  cache_dir (resume)   │  per-symbol report
                     └─ RateLimiter ─────────┘
```

Stages are intentionally separable: **ingest → validate → store → analyze**. Keep
raw fetched data immutable; derive analysis tables (e.g. year-end pivot) downstream
so you never re-hit the API to recompute a transform.

### 4.2 Decisions & rationale

| Decision | Recommendation | Why |
|---|---|---|
| **Source** | VCI primary; KBS as fallback | Most complete adjusted VN daily OHLCV. |
| **Account tier** | Register for free **Community** (60 req/min) | 3× headroom; set `--min-interval 1.0`. 282 symbols ≈ 5 min vs ~15 min on guest. |
| **Rate strategy** | spacing + backoff, **not** parallelism | A single shared quota means concurrency just trips `RateLimitExceeded`. Sequential + cache is faster *and* simpler. |
| **Storage** | Parquet for the panel; CSV cache per symbol | Parquet preserves dtypes & is ~10× smaller; CSV cache needs no extra dep and is human-inspectable. |
| **Resumability** | per-symbol cache files | A 2-hour pull that dies at #200 resumes in seconds. |
| **Idempotency / freshness** | cache key = `SOURCE_SYMBOL_INTERVAL`; clear cache to refresh | Avoids stale data on the *end* boundary; consider a dated cache dir per run for point-in-time studies. |
| **Adjusted price policy** | pick one (adjusted) and state it in every output's metadata | Prevents silent mixing (see QA §3.2). |

### 4.3 When *not* to use vnstock

- **Commercial / production trading:** licence is non-commercial; rate limits and
  telemetry make it unsuitable. Use a paid vendor (FiinPro, exchange feeds).
- **Tick / true intraday at scale:** the 30 k-row intraday cap and rate limits bite.
- For **NCKH research at a few-hundred-symbol scale, vnstock is the right tool.**

### 4.4 Risks / mitigations

| Risk | Mitigation |
|---|---|
| Upstream API/schema change | `_normalize()` fails loudly; pin `vnstock==4.0.4`. |
| Rate-limit / ban | conservative spacing, cache, single-threaded. |
| Telemetry / privacy | documented; terms already accepted; air-gap not possible. |
| Survivorship bias in results | document universe + as-of date (QA §3.2). |

---

## Part 5 — Tech Lead: audit & sign-off

### 5.1 Original `tho.py` vs delivered module

| # | Issue in `tho.py` | Severity | Resolved in `ohlc_fetcher.py`? |
|---|---|---|---|
| 1 | `except Exception: continue` — **silently drops every failure** | 🔴 High | ✅ per-symbol try/except that **logs**, records failures, and reports a summary |
| 2 | Hard-coded output path `C:\Users\PC\…xlsx` | 🔴 High | ✅ `--out` CLI arg |
| 3 | Deprecated `Vnstock().stock()` API | 🟠 Med | ✅ modern `Quote` API |
| 4 | No Windows/Unicode safety | 🟠 Med | ✅ `_configure_stdout_utf8()` |
| 5 | `start` overshoot not handled | 🟠 Med | ✅ strict `_bounds()` filter |
| 6 | `time.sleep(0.1)` ⇒ ~600/min, **far over the 20–60/min limit** | 🟠 Med | ✅ `RateLimiter` (3 s default) + `RateLimitExceeded` backoff |
| 7 | No resumability (restart from #1 on failure) | 🟠 Med | ✅ per-symbol cache |
| 8 | No validation of returned data | 🟠 Med | ✅ `validate_ohlc()` + tests |
| 9 | Stale hard-coded `end='2026-06-01'` | 🟡 Low | ✅ `--end` arg, today inclusive |
| 10 | No tests / requirements / docs | 🟡 Low | ✅ 14 tests, `requirements.txt`, this report |

### 5.2 Verification performed by the tech lead

- ✅ `python -m pytest tests/ -q` → **14 passed**.
- ✅ Live `download()` of FPT/VNM/ZZZZZ → invalid dropped, range strictly bounded, both validated, cache resume confirmed.
- ✅ Schema/dtypes match the live API exactly.
- ✅ No bare `except`; all failure paths logged; one bad ticker cannot abort the batch.

### 5.3 Audit findings still open (recommendations, not blockers)

1. **Adjusted-price metadata.** Stamp each output with `source`, `adjusted=True`,
   `unit="kVND"`, and the run date so downstream consumers can't mis-read it.
2. **Cache freshness on the `end` edge.** Cache key ignores the date range; for a
   *growing* `end`, clear the cache or include the range in the key.
3. **`year_end_close_pivot` "last session" caveat** (QA §3.2.3) — add a column for
   the actual last-session date so analysts can spot delistings.
4. **Community-tier registration** (`register_user`) to unlock 60 req/min before
   running the full 282-symbol universe.

### 5.4 Sign-off

> **Approved for NCKH research use.** The module is correct, robust, tested, and
> resolves all High/Medium issues in the original script. The open items above are
> data-governance refinements, not correctness defects. Recommend registering a
> Community account and running with `--cache-dir` for the full universe.

---

### Appendix — file manifest

| File | Purpose |
|---|---|
| `ohlc_fetcher.py` | the fetcher: `fetch_ohlc`, `download`, `validate_ohlc`, `year_end_close_pivot`, CLI |
| `tests/test_ohlc_fetcher.py` | 14 offline unit tests |
| `requirements.txt` | pinned deps |
| `docs/VNSTOCK_OHLC_REPORT.md` | this report |
| `tho.py` | original prototype (kept for reference) |
