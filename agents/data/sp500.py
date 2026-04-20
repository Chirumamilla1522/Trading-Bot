"""
S&P 500 Ticker Roster + Async Options Scanner

``SP500_TICKERS`` is ordered with **index / sector / macro ETFs first** (see
``INDEX_SECTOR_ETF_TICKERS``), then S&P 500–style single names. The scanner cycles
through this list in concurrent batches, building per-ticker summary metrics
(IV, P/C ratio, OI, volume). Results are cached; individual tickers refresh
every ~5 minutes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger(__name__)


# ─── Index / sector / macro ETFs (always first in ``SP500_TICKERS``) ─────────
# Broad + sector SPDRs + common tradable proxies; equities follow in sector blocks below.
INDEX_SECTOR_ETF_TICKERS: list[str] = [
    "SPY", "QQQ", "IWM", "DIA",
    "VOO", "VTI", "IJH", "IJR", "MDY",
    "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC",
    "SMH", "SOXX",
    "GLD", "SLV", "TLT", "EEM",
    "ARKK", "VXX", "UVXY", "SQQQ", "TQQQ", "SPXL", "SPXU",
]
_ETF_TICKERS_UPPER: frozenset[str] = frozenset(t.upper() for t in INDEX_SECTOR_ETF_TICKERS)

# UI / API: benchmark strip + scanner section headers (order matches ``INDEX_SECTOR_ETF_TICKERS``)
BENCHMARK_SCANNER_SECTIONS: list[dict[str, str | list[str]]] = [
    {
        "id": "core",
        "label": "Core indices",
        "tickers": ["SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IJH", "IJR", "MDY"],
    },
    {
        "id": "sectors",
        "label": "Sectors & semis",
        "tickers": ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC", "SMH", "SOXX"],
    },
    {
        "id": "macro",
        "label": "Macro & vol",
        "tickers": ["GLD", "SLV", "TLT", "EEM", "ARKK", "VXX", "UVXY", "SQQQ", "TQQQ", "SPXL", "SPXU"],
    },
]
_flat_sec = [t for sec in BENCHMARK_SCANNER_SECTIONS for t in sec["tickers"]]  # type: ignore[misc]
if _flat_sec != INDEX_SECTOR_ETF_TICKERS:
    raise RuntimeError("BENCHMARK_SCANNER_SECTIONS must partition INDEX_SECTOR_ETF_TICKERS in order")

# ─── S&P 500 + high-liquidity names (equities; ETFs removed — re-prefixed above) ─
_SP500_EQUITIES: list[str] = [
    # ── Tier 1: Mega-cap tech ───────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "GOOG", "AMD", "ORCL", "CRM", "ADBE", "NFLX", "QCOM", "TXN",

    # ── Technology ───────────────────────────────────────────────────────────
    "AMAT", "LRCX", "KLAC", "MU", "NOW", "SNPS", "CDNS", "CSCO",
    "IBM", "ACN", "INTU", "FTNT", "PANW", "CRWD", "WDAY", "ANET",
    "AKAM", "HPQ", "HPE", "NTAP", "STX", "WDC", "ZS", "KEYS",
    "GLW", "CTSH", "MPWR", "ENPH", "ZBRA", "CDW", "IT", "FFIV",
    "GEN", "VRSN", "FSLR", "INTC", "EPAM", "NLOK", "TER", "SWKS",
    "QRVO", "MCHP", "ON", "ADI", "NXPI", "MRVL", "MTSI", "SLAB",

    # ── Financials ────────────────────────────────────────────────────────────
    "JPM", "BAC", "WFC", "MS", "GS", "BLK", "SCHW", "V", "MA",
    "CB", "USB", "PNC", "AXP", "COF", "TFC", "FIS", "PYPL",
    "AIG", "MET", "PRU", "ALL", "TRV", "AFL", "MMC", "AON",
    "WRB", "HIG", "LNC", "UNM", "PFG", "RF", "HBAN", "CFG",
    "KEY", "MTB", "BK", "STT", "NTRS", "IVZ", "TROW", "BEN",
    "AMG", "AMP", "MKTX", "ICE", "CME", "CBOE", "NDAQ", "FITB",
    "CINF", "WTW", "ERIE", "GL", "AIZ", "EG", "RE", "RNR", "ACGL",

    # ── Healthcare ────────────────────────────────────────────────────────────
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "ABT", "DHR", "TMO",
    "PFE", "AMGN", "GILD", "CVS", "CI", "ELV", "HUM", "BMY",
    "REGN", "VRTX", "MDT", "BSX", "ZBH", "EW", "ALGN", "HOLX",
    "WAT", "IQV", "CRL", "A", "ILMN", "BIIB", "MRNA", "DXCM",
    "PODD", "ISRG", "STE", "BDX", "BAX", "MTD", "IDXX", "MASI",
    "HSIC", "SYK", "ZTS", "GEHC", "MCK", "ABC", "CAH", "DGX",
    "LH", "RMD", "TECH", "PKI", "INCY", "VTRS", "CTLT", "HZNP",

    # ── Energy ────────────────────────────────────────────────────────────────
    "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "VLO", "PSX",
    "OXY", "DVN", "HES", "BKR", "HAL", "APA", "EQT", "MRO",
    "FANG", "LNG", "OKE", "WMB", "KMI", "TRGP", "CTRA", "SM",

    # ── Consumer Discretionary ────────────────────────────────────────────────
    "HD", "MCD", "NKE", "LOW", "SBUX", "CMG", "TJX",
    "ROST", "ORLY", "AZO", "DG", "DLTR", "BBY", "EBAY", "ETSY",
    "F", "GM", "APTV", "BWA", "LEA", "KMX", "AN", "LVS",
    "WYNN", "MGM", "CZR", "HLT", "MAR", "CCL", "RCL", "NCLH",
    "AAL", "DAL", "UAL", "LUV", "ALK", "EXPE", "BKNG", "ABNB",
    "POOL", "PHM", "DHI", "LEN", "NVR", "TOL", "MTH", "GPC",

    # ── Consumer Staples ──────────────────────────────────────────────────────
    "WMT", "PG", "COST", "KO", "PEP", "PM", "MO", "MDLZ",
    "CL", "KHC", "GIS", "K", "CPB", "HSY", "MKC", "SJM",
    "CAG", "HRL", "STZ", "TAP", "CHD", "CLX", "ENR", "KR",
    "SYY", "PFGC",

    # ── Industrials ───────────────────────────────────────────────────────────
    "HON", "BA", "UPS", "RTX", "LMT", "GE", "NOC", "GD",
    "EMR", "ETN", "IR", "PCAR", "DE", "CAT", "CMI", "GNRC",
    "RSG", "WM", "CTAS", "PAYX", "ADP", "VRSK", "CPRT", "FAST",
    "GWW", "SNA", "PH", "ROK", "AME", "FTV", "LDOS", "SAIC",
    "BAH", "HII", "TDG", "SPR", "HWM", "TXT", "JBHT", "CHRW",
    "EXPD", "XPO", "ODFL", "SAIA", "UNP", "CSX", "NSC", "WAB",
    "CARR", "OTIS", "AXON", "ROP", "TT", "XYL", "IDEX", "IEX",
    "MAS", "PNR", "WCN",

    # ── Utilities ─────────────────────────────────────────────────────────────
    "NEE", "DUK", "SO", "D", "EXC", "AEP", "XEL", "ED",
    "PEG", "AWK", "ES", "WEC", "ETR", "CNP", "ATO", "NI",
    "LNT", "EVRG", "PPL", "FE", "CMS", "DTE", "PCG", "SRE",
    "CEG", "VST",

    # ── Real Estate ───────────────────────────────────────────────────────────
    "AMT", "PLD", "EQIX", "CCI", "SPG", "WELL", "O", "DLR",
    "PSA", "EQR", "AVB", "VTR", "SUI", "ELS", "MAA", "CPT",
    "UDR", "KIM", "REG", "FRT", "BXP", "VICI", "GLPI",

    # ── Materials ─────────────────────────────────────────────────────────────
    "LIN", "APD", "DD", "DOW", "NEM", "FCX", "ALB", "IFF",
    "PPG", "SHW", "ECL", "EMN", "CE", "HUN", "FMC", "CF",
    "MOS", "NUE", "STLD", "RS", "ATI", "AA", "X", "CLF",
    "MLM", "VMC", "PKG", "IP", "WRK", "SEE", "BALL", "AVY",

    # ── Communication Services ────────────────────────────────────────────────
    "CMCSA", "DIS", "T", "VZ", "TMUS", "WBD", "PARA",
    "FOX", "FOXA", "OMC", "IPG", "MTCH", "PINS", "SNAP",
    "SPOT", "TTWO", "EA", "RBLX", "U",
]

# Full scanner roster: all index/sector/macro ETFs first, then equities (deduped).
SP500_TICKERS = list(
    dict.fromkeys(
        INDEX_SECTOR_ETF_TICKERS
        + [t for t in _SP500_EQUITIES if t.upper() not in _ETF_TICKERS_UPPER]
    )
)

# Default scanner universe: first 50 / 100 symbols (indices + sectors + stocks in order)
SP500_TOP50: list[str] = list(SP500_TICKERS[:50])
SP500_TOP100: list[str] = list(SP500_TICKERS[:100])


# ─── Per-ticker scan result ───────────────────────────────────────────────────

@dataclass
class TickerScan:
    ticker:           str
    underlying_price: float = 0.0
    avg_iv_30d:       float = 0.0   # avg IV of options with ~30 DTE
    avg_iv_all:       float = 0.0   # avg IV across all loaded contracts
    pc_ratio:         float = 0.0   # put OI / call OI
    total_oi:         int   = 0
    call_oi:          int   = 0
    put_oi:           int   = 0
    num_contracts:    int   = 0
    last_updated:     float = field(default_factory=time.time)
    error:            str   = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["age_s"] = round(time.time() - self.last_updated, 1)
        return d


def sort_scan_rows(rows: list[dict], sort: str) -> None:
    """In-place sort for scanner table. `price` / `chg` expect merged quote fields (`last`, `change_pct`)."""
    key_map = {
        "iv": lambda d: float(d.get("avg_iv_30d") or 0),
        "pc": lambda d: float(d.get("pc_ratio") or 0),
        "oi": lambda d: int(d.get("total_oi") or 0),
        "ticker": lambda d: str(d.get("ticker") or ""),
        "price": lambda d: float(
            d.get("last") if d.get("last") is not None else (d.get("underlying_price") or 0)
        ),
        "chg": lambda d: float(d.get("change_pct") if d.get("change_pct") is not None else -1e9),
    }
    key_fn = key_map.get(sort, key_map["iv"])
    rows.sort(key=key_fn, reverse=(sort != "ticker"))


# ─── Scanner ──────────────────────────────────────────────────────────────────

class SP500Scanner:
    """
    Async scanner that cycles through all S&P 500 tickers, fetching options
    chain summaries from Alpaca.  Results are cached and served to the API.

    Tier-1 tickers (most liquid) are scanned immediately on startup and
    re-scanned first in every subsequent cycle.
    """

    CONCURRENCY     = 8    # max simultaneous Alpaca calls
    RESCAN_INTERVAL = 300  # seconds between full scan cycles (5 min)
    CHAIN_LIMIT     = 100  # option contracts per ticker for scanner
    DRILLDOWN_LIMIT = 500  # contracts for full drilldown view

    def __init__(self):
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from agents.config import ALPACA_API_KEY, ALPACA_SECRET_KEY

        self._client = OptionHistoricalDataClient(
            api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY,
        )
        self._scan_cache:  dict[str, TickerScan] = {}
        self._chain_cache: dict[str, list]        = {}  # raw GreeksSnapshot list
        self._sem = asyncio.Semaphore(self.CONCURRENCY)
        self._cycle = 0

    # ── Public accessors ─────────────────────────────────────────────────────

    def get_scan_rows(self) -> list[dict]:
        """All cached scan rows (no sorting)."""
        return [v.to_dict() for v in self._scan_cache.values() if not v.error]

    def get_all_scans(self, sort: str = "iv") -> list[dict]:
        """Return all cached scan results as dicts, sorted by `sort` key."""
        results = self.get_scan_rows()
        sort_scan_rows(results, sort)
        return results

    def get_scan(self, ticker: str) -> Optional[dict]:
        s = self._scan_cache.get(ticker.upper())
        return s.to_dict() if s else None

    def get_chain(self, ticker: str) -> list:
        """Return cached GreeksSnapshot dicts for `ticker`, or empty list."""
        return self._chain_cache.get(ticker.upper(), [])

    def all_tickers(self) -> list[str]:
        """
        Scanner universe (default: top 50 — lowest Alpaca / options load).

        Env:
          - ``SCANNER_UNIVERSE`` = ``top50`` | ``top100`` | ``full`` (default ``top50``)
          - Legacy: ``SP500_FULL_SCAN=true`` → full list (overrides SCANNER_UNIVERSE)
        """
        import os
        if os.getenv("SP500_FULL_SCAN", "false").lower() in ("1", "true", "yes"):
            return list(SP500_TICKERS)
        mode = os.getenv("SCANNER_UNIVERSE", "top50").strip().lower()
        if mode in ("full", "all", "sp500"):
            return list(SP500_TICKERS)
        if mode in ("top100", "100"):
            return list(SP500_TOP100)
        return list(SP500_TOP50)

    # ── Background scan loop ─────────────────────────────────────────────────

    async def run_forever(self):
        while True:
            self._cycle += 1
            tickers = self.all_tickers()
            log.info("SP500Scanner cycle %d starting (%d tickers)", self._cycle, len(tickers))
            tasks = [self._scan_one(t) for t in tickers]
            await asyncio.gather(*tasks, return_exceptions=True)
            ok = sum(1 for v in self._scan_cache.values() if not v.error)
            log.info("SP500Scanner cycle %d complete. %d/%d tickers OK.",
                     self._cycle, ok, len(tickers))
            await asyncio.sleep(self.RESCAN_INTERVAL)

    async def fetch_drilldown(self, ticker: str) -> list:
        """Force-fetch a full options chain (up to DRILLDOWN_LIMIT) for `ticker`."""
        try:
            snaps = await asyncio.to_thread(
                self._fetch_chain, ticker.upper(), self.DRILLDOWN_LIMIT
            )
            self._chain_cache[ticker.upper()] = [s.model_dump() for s in snaps]
        except Exception as e:
            log.warning("Drilldown failed for %s: %s", ticker, e)
        return self._chain_cache.get(ticker.upper(), [])

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _scan_one(self, ticker: str):
        async with self._sem:
            try:
                scan, greeks = await asyncio.to_thread(self._compute_scan, ticker)
                self._scan_cache[ticker]  = scan
                self._chain_cache[ticker] = [g.model_dump() for g in greeks]
            except Exception as e:
                log.debug("Scan error %s: %s", ticker, e)
                self._scan_cache[ticker] = TickerScan(ticker=ticker, error=str(e)[:80])

    def _fetch_chain(self, ticker: str, limit: int) -> list:
        from alpaca.data.requests import OptionChainRequest
        from agents.data.opra_client import _alpaca_chain_to_greeks
        chain = self._client.get_option_chain(
            OptionChainRequest(underlying_symbol=ticker, limit=limit)
        )
        return [_alpaca_chain_to_greeks(sym, snap) for sym, snap in (chain or {}).items()]

    def _compute_scan(self, ticker: str) -> tuple[TickerScan, list]:
        from datetime import date, datetime
        today = date.today()
        greeks = self._fetch_chain(ticker, self.CHAIN_LIMIT)

        if not greeks:
            return TickerScan(ticker=ticker, error="empty chain"), []

        ivs_30d, ivs_all = [], []
        call_oi = put_oi = 0
        underlying_est = 0.0
        atm_delta_diff = 1.0

        for g in greeks:
            iv = g.iv or 0.0
            if iv > 0:
                ivs_all.append(iv)

            # Estimate underlying from option closest to delta=0.5
            d = abs(g.delta) if g.delta else 0
            diff = abs(d - 0.5)
            if diff < atm_delta_diff and g.strike > 0:
                atm_delta_diff = diff
                underlying_est = g.strike

            # 20-40 DTE bucket for "30d IV"
            try:
                yr = "20" + g.expiry[:2] if len(g.expiry) == 6 else g.expiry[:4]
                mo = g.expiry[2:4] if len(g.expiry) == 6 else g.expiry[4:6]
                dy = g.expiry[4:6] if len(g.expiry) == 6 else g.expiry[6:8]
                exp = date(int(yr), int(mo), int(dy))
                dte = (exp - today).days
                if 20 <= dte <= 45 and iv > 0:
                    ivs_30d.append(iv)
            except Exception:
                pass

            # Use OCC symbol to determine call/put OI (we don't have OI from Alpaca
            # snapshot directly, so approximate from number of contracts)
            if g.right.value == "CALL":
                call_oi += 1
            else:
                put_oi += 1

        avg_iv_30d = (sum(ivs_30d) / len(ivs_30d)) if ivs_30d else (
            sum(ivs_all) / len(ivs_all) if ivs_all else 0.0
        )
        avg_iv_all = (sum(ivs_all) / len(ivs_all)) if ivs_all else 0.0

        scan = TickerScan(
            ticker=ticker,
            underlying_price=round(underlying_est, 2),
            avg_iv_30d=round(avg_iv_30d, 4),
            avg_iv_all=round(avg_iv_all, 4),
            pc_ratio=round(put_oi / call_oi, 2) if call_oi > 0 else 0.0,
            total_oi=call_oi + put_oi,
            call_oi=call_oi,
            put_oi=put_oi,
            num_contracts=len(greeks),
        )
        return scan, greeks
