"""
Stock fundamentals, peer/competitor lookup, and supply-chain dependency map.

Data sources:
  - yfinance (free, no API key) → financials, sector, industry, description
  - Curated static maps → competitors, depends_on, depended_by
  - Sector-peer fallback using SP500_TOP100 for unknowns

Note: caching is handled at the API layer (SQLite) to keep reads fast while
refreshing only when values change.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def dividend_yield_as_decimal(raw: Any) -> float | None:
    """
    Yahoo / yfinance ``dividendYield`` is inconsistent: often a **decimal** (e.g. 0.0348
    for 3.48%) but sometimes a **percent number** (e.g. 1.14 for 1.14%). The UI multiplies
    by 100 again, so percent-style values must be converted to decimals here.
    """
    if raw is None or raw == "N/A":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v < 0:  # NaN or negative
        return None
    if v > 1.0:
        v = v / 100.0
    return round(v, 6)


def dividend_yield_from_yfinance_info(info: dict[str, Any]) -> float | None:
    """
    Best-effort dividend yield as a **decimal** (e.g. 0.04 = 4%).

    Yahoo often populates ``trailingAnnualDividendYield`` correctly while ``dividendYield``
    may be a percent-style number in the 0–1 range (e.g. 0.4 meaning 0.4%, which would
    wrongly show as 40% if passed straight to the UI).
    """
    if not info:
        return None

    def _get(k: str) -> Any:
        v = info.get(k)
        if v is None or v == "N/A":
            return None
        return v

    tr = _get("trailingAnnualDividendYield")
    if tr is not None:
        v = dividend_yield_as_decimal(tr)
        if v is not None:
            return v

    try:
        rate = _get("dividendRate")
        px = _get("currentPrice") or _get("regularMarketPrice")
        if rate is not None and px is not None:
            r, p = float(rate), float(px)
            if p > 0 and r >= 0:
                return round(r / p, 6)
    except (TypeError, ValueError):
        pass

    return dividend_yield_as_decimal(_get("dividendYield"))


# ── Curated competitor map ────────────────────────────────────────────────────
# Key = ticker, value = list of main direct competitor tickers.

_COMPETITORS: dict[str, list[str]] = {
    # Mega-cap tech
    "AAPL":  ["MSFT", "GOOGL", "META", "AMZN", "SAMSF"],
    "MSFT":  ["AAPL", "GOOGL", "AMZN", "CRM", "ORCL"],
    "GOOGL": ["MSFT", "META", "AMZN", "SNAP", "PINS"],
    "GOOG":  ["MSFT", "META", "AMZN", "SNAP", "PINS"],
    "META":  ["GOOGL", "SNAP", "PINS", "MTCH", "TWTR"],
    "AMZN":  ["MSFT", "GOOGL", "AAPL", "WMT", "SHOP"],
    "TSLA":  ["GM", "F", "RIVN", "NIO", "LCID"],
    "NVDA":  ["AMD", "INTC", "QCOM", "AVGO", "MRVL"],
    "AMD":   ["NVDA", "INTC", "QCOM", "MRVL"],
    "INTC":  ["NVDA", "AMD", "QCOM", "AVGO"],
    "AVGO":  ["NVDA", "QCOM", "MRVL", "TXN", "ADI"],
    "QCOM":  ["AVGO", "AMD", "INTC", "MRVL", "TXN"],
    # Cloud / SaaS
    "CRM":   ["MSFT", "SAP", "ORCL", "NOW", "WDAY"],
    "ORCL":  ["MSFT", "SAP", "CRM", "IBM", "NOW"],
    "NOW":   ["CRM", "ORCL", "WDAY", "MSFT"],
    "WDAY":  ["SAP", "ORCL", "CRM", "ADP", "PAYX"],
    "ADBE":  ["MSFT", "AAPL", "FIGMA", "CRM"],
    "INTU":  ["ADP", "PAYX", "H&R BLOCK", "WDAY"],
    # Payments
    "V":     ["MA", "PYPL", "AXP", "FIS", "FISV"],
    "MA":    ["V", "PYPL", "AXP", "FIS", "FISV"],
    "PYPL":  ["V", "MA", "SQ", "AXP", "STRIPE"],
    "AXP":   ["V", "MA", "PYPL", "COF"],
    # Banks
    "JPM":   ["BAC", "WFC", "C", "GS", "MS"],
    "BAC":   ["JPM", "WFC", "C", "GS", "MS"],
    "WFC":   ["JPM", "BAC", "C", "GS", "USB"],
    "GS":    ["MS", "JPM", "BAC", "BLK"],
    "MS":    ["GS", "JPM", "BAC", "BLK", "SCHW"],
    # Consumer
    "NFLX":  ["DIS", "AMZN", "PARA", "CMCSA", "WBD"],
    "DIS":   ["NFLX", "CMCSA", "WBD", "PARA"],
    "SBUX":  ["MCD", "CMG", "TGT", "DNKN"],
    "NKE":   ["ADDYY", "UAA", "SKX", "LULU"],
    "MCD":   ["SBUX", "CMG", "YUM", "QSR"],
    "CMG":   ["MCD", "SBUX", "DPZ", "YUM"],
    # Healthcare
    "UNH":   ["ELV", "CI", "HUM", "CVS"],
    "JNJ":   ["PFE", "MRK", "ABBV", "LLY"],
    "LLY":   ["NVO", "PFE", "MRK", "ABBV"],
    "ABBV":  ["JNJ", "BMY", "AMGN", "REGN"],
    "PFE":   ["MRK", "JNJ", "ABBV", "LLY"],
    # Energy
    "XOM":   ["CVX", "COP", "BP", "SHEL"],
    "CVX":   ["XOM", "COP", "BP", "SHEL"],
    # Retail
    "WMT":   ["AMZN", "TGT", "COST", "KR"],
    "COST":  ["WMT", "TGT", "BJ", "AMZN"],
    "TGT":   ["WMT", "AMZN", "COST", "DG"],
    # Semiconductors / EDA
    "TXN":   ["ADI", "MCHP", "NXPI", "AVGO"],
    "ADI":   ["TXN", "MCHP", "NXPI", "AVGO"],
    "MCHP":  ["TXN", "ADI", "NXPI", "AVGO"],
    # Chipmakers / Fabless
    "AMAT":  ["LRCX", "KLAC", "ASML"],
    "LRCX":  ["AMAT", "KLAC", "TEL"],
    "KLAC":  ["AMAT", "LRCX", "ONTO"],
    # Consulting / IT services
    "ACN":   ["IBM", "CTSH", "WIT", "INFY"],
    "IBM":   ["ACN", "CTSH", "ORCL", "MSFT"],
    # Cyber
    "CRWD":  ["PANW", "FTNT", "S", "MSFT"],
    "PANW":  ["CRWD", "FTNT", "ZS", "MSFT"],
    "FTNT":  ["CRWD", "PANW", "CHKP", "CSCO"],
    # Aerospace & Defense
    "BA":    ["AIR", "LMT", "RTX", "EADSY"],
    "LMT":   ["RTX", "NOC", "GD", "BA"],
    "RTX":   ["LMT", "NOC", "GD", "BA"],
}


# ── Supply-chain / ecosystem dependency map ───────────────────────────────────
# depends_on  = upstream suppliers / critical inputs
# depended_by = key downstream customers / ecosystem participants

_DEPENDS_ON: dict[str, list[str]] = {
    "AAPL":  ["AVGO", "QCOM", "TXN", "ADI", "MRVL", "MU"],      # chips inside iPhone/Mac
    "MSFT":  ["NVDA", "AMD", "INTC", "AVGO"],                     # cloud + gaming silicon
    "GOOGL": ["NVDA", "AMD", "INTC", "AMZN", "MSFT"],             # infra + custom chips
    "AMZN":  ["NVDA", "INTC", "UPS", "FDX", "JBHT"],              # cloud HW, logistics
    "META":  ["NVDA", "AMD", "AVGO", "MU", "INTC"],               # AI infra
    "TSLA":  ["NVDA", "PANASONIC", "LG", "CATL", "ALB"],          # chips, batteries, lithium
    "NVDA":  ["TSMC", "SAMSUNG", "SKH", "ASML", "MU"],            # pure-play fabless
    "AMD":   ["TSMC", "SAMSUNG", "ASML", "MU"],
    "INTC":  ["ASML", "AMAT", "LRCX", "KLAC"],                    # IDM: fabs itself
    "AVGO":  ["TSMC", "AMAT", "ASML"],
    "QCOM":  ["TSMC", "SAMSUNG", "AMAT"],
    "AMAT":  ["APPLIED MATS"],                                     # itself is upstream
    "V":     ["AAPL", "MSFT", "JPM", "BAC"],                       # network built on banks
    "MA":    ["AAPL", "MSFT", "JPM", "BAC"],
    "PYPL":  ["V", "MA", "AAPL"],                                  # rides card rails
    "JPM":   ["FED", "IQVIA", "FIS"],
    "NFLX":  ["AMZN", "MSFT", "GOOGL"],                           # AWS/Azure content delivery
    "DIS":   ["COMCAST", "AT&T", "WBD"],
    "XOM":   ["SLB", "HAL", "BKR"],                                # oilfield services
    "CVX":   ["SLB", "HAL", "BKR"],
    "BA":    ["GE", "RTX", "HWM", "SPR"],                          # engine, avionics suppliers
    "RTX":   ["GE", "HON", "SPR"],
    "LMT":   ["RTX", "GE", "BA"],
    "UNH":   ["CVS", "LH", "DGX"],
    "COST":  ["WMT", "UPS", "FDX"],
    "WMT":   ["UPS", "FDX", "PG", "KO", "PEP"],
    "MCD":   ["TYSON", "LAMB", "SYY"],
    "SBUX":  ["NESTLE", "SYY", "CALM"],
    "CRM":   ["AMZN", "GOOGL", "MSFT"],                           # runs on cloud
    "NOW":   ["AMZN", "MSFT", "GOOGL"],
    "WDAY":  ["AMZN", "MSFT"],
    "ADBE":  ["AMZN", "MSFT", "GOOGL"],
    "CRWD":  ["AMZN", "MSFT", "GOOGL"],
    "PANW":  ["AMZN", "MSFT", "GOOGL"],
    "LLY":   ["CATALENT", "LONZA", "WBA"],
    "PFE":   ["CATALENT", "LONZA", "WBA"],
    "ABBV":  ["CATALENT", "LONZA"],
    "TXN":   ["AMAT", "ASML", "LRCX"],
    "ADI":   ["AMAT", "ASML", "TSMC"],
}

_DEPENDED_BY: dict[str, list[str]] = {
    "NVDA":  ["MSFT", "META", "GOOGL", "AMZN", "TSLA", "NFLX", "CRM"],  # AI chips everywhere
    "AMD":   ["MSFT", "META", "GOOGL", "AMZN", "DELL"],
    "INTC":  ["DELL", "HPQ", "HPE", "AAPL", "MSFT"],
    "AVGO":  ["AAPL", "MSFT", "GOOGL", "META"],
    "QCOM":  ["AAPL", "SAMSUNG", "MSFT", "META"],
    "TXN":   ["AAPL", "TSLA", "MSFT", "HON", "BA"],
    "ADI":   ["AAPL", "MSFT", "GE", "BA", "TSLA"],
    "AMAT":  ["NVDA", "AMD", "INTC", "TSMC", "SAMSUNG"],
    "LRCX":  ["NVDA", "AMD", "INTC", "TSMC", "SAMSUNG"],
    "KLAC":  ["NVDA", "AMD", "INTC", "TSMC"],
    "MU":    ["AAPL", "MSFT", "NVDA", "AMD", "AMZN"],
    "V":     ["WMT", "AMZN", "SBUX", "MCD", "COST"],
    "MA":    ["WMT", "AMZN", "SBUX", "MCD", "COST"],
    "SLB":   ["XOM", "CVX", "COP", "OXY"],
    "HAL":   ["XOM", "CVX", "COP", "OXY"],
    "BKR":   ["XOM", "CVX", "COP"],
    "GE":    ["BA", "RTX", "LMT"],
    "UPS":   ["AMZN", "WMT", "COST", "EBAY"],
    "FDX":   ["AMZN", "WMT", "COST", "EBAY"],
    "AMZN":  ["AAPL", "MSFT", "META", "NFLX", "CRM"],  # AWS underpins these
    "MSFT":  ["AAPL", "CRM", "ADBE", "SAP", "CRWD"],   # Azure, Windows, Office
    "GOOGL": ["META", "SNAP", "PINS", "AMZN"],
    "JPM":   ["V", "MA", "PYPL", "AXP"],
    "BAC":   ["V", "MA", "PYPL", "COF"],
    "FIS":   ["JPM", "BAC", "WFC", "USB"],
    "PG":    ["WMT", "COST", "TGT", "KR"],
    "KO":    ["WMT", "MCD", "SBUX", "COST"],
    "PEP":   ["WMT", "MCD", "COST", "TGT"],
    "LLY":   ["UNH", "CVS", "CI", "HUM"],
    "PFE":   ["UNH", "CVS", "CI", "HUM"],
    "MRK":   ["UNH", "CVS", "CI", "HUM"],
    "CRM":   ["MCD", "WMT", "SBUX", "COST"],
    "NOW":   ["JPM", "BAC", "UNH", "GS"],
    "WDAY":  ["WMT", "AMZN", "JPM", "BAC"],
    "ADBE":  ["AAPL", "MSFT", "META", "DIS"],
    "CRWD":  ["JPM", "BAC", "MSFT", "GOOGL"],
    "PANW":  ["JPM", "BAC", "MSFT", "GOOGL"],
}


def _yfinance_fetch(ticker: str) -> dict[str, Any]:
    """Pull key info fields from yfinance. Returns empty dict on error."""
    try:
        import yfinance as yf
    except ImportError:
        log.debug("yfinance not installed; install with: pip install yfinance")
        return {}

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        log.debug("yfinance fetch %s: %s", ticker, e)
        return {}

    def _f(k: str, default=None):
        v = info.get(k)
        if v is None or v == "N/A":
            return default
        return v

    def _round(v, n=2):
        try:
            return round(float(v), n)
        except (TypeError, ValueError):
            return None

    # Revenue / earnings helpers
    rev = _f("totalRevenue")
    mc  = _f("marketCap")

    return {
        "ticker":          ticker.upper(),
        "name":            _f("longName") or _f("shortName") or ticker,
        "description":     (_f("longBusinessSummary") or "")[:500],
        "sector":          _f("sector", ""),
        "industry":        _f("industry", ""),
        "exchange":        _f("exchange", ""),
        "country":         _f("country", ""),
        # Valuation
        "market_cap":      mc,
        "enterprise_value": _f("enterpriseValue"),
        "pe_ratio":        _round(_f("trailingPE")),
        "forward_pe":      _round(_f("forwardPE")),
        "peg_ratio":       _round(_f("pegRatio")),
        "price_to_book":   _round(_f("priceToBook")),
        "ev_to_ebitda":    _round(_f("enterpriseToEbitda")),
        # Earnings
        "eps_trailing":    _round(_f("trailingEps")),
        "eps_forward":     _round(_f("forwardEps")),
        "revenue":         rev,
        "revenue_growth":  _round(_f("revenueGrowth"), 4),
        "gross_margin":    _round(_f("grossMargins"), 4),
        "operating_margin": _round(_f("operatingMargins"), 4),
        "profit_margin":   _round(_f("profitMargins"), 4),
        "return_on_equity": _round(_f("returnOnEquity"), 4),
        "return_on_assets": _round(_f("returnOnAssets"), 4),
        # Price data
        "current_price":   _round(_f("currentPrice") or _f("regularMarketPrice")),
        "week52_high":     _round(_f("fiftyTwoWeekHigh")),
        "week52_low":      _round(_f("fiftyTwoWeekLow")),
        "fifty_day_avg":   _round(_f("fiftyDayAverage")),
        "two_hundred_day_avg": _round(_f("twoHundredDayAverage")),
        # Risk / yield
        "beta":            _round(_f("beta")),
        "dividend_yield":  _round(dividend_yield_from_yfinance_info(info), 4),
        "payout_ratio":    _round(_f("payoutRatio"), 4),
        # Size
        "shares_outstanding": _f("sharesOutstanding"),
        "float_shares":    _f("floatShares"),
        "short_ratio":     _round(_f("shortRatio")),
        "analyst_target":  _round(_f("targetMeanPrice")),
        "recommendation":  _f("recommendationKey", ""),
    }


def _sector_peers(ticker: str, sector: str, industry: str) -> list[str]:
    """Find tickers in the same industry/sector within SP500_TOP100."""
    from agents.data.sp500 import SP500_TOP100
    t = ticker.upper()
    if not sector:
        return []
    results: list[str] = []
    # We don't want to call yfinance for every candidate — use the sector map
    # derived from our curated competitor list first.
    comp = _COMPETITORS.get(t, [])
    for c in comp:
        if c in SP500_TOP100 and c != t:
            results.append(c)
    # Pad to 6 with same-sector members (from the static sector tags we know)
    _SECTOR_BUCKETS: dict[str, list[str]] = {
        "Technology": [
            "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "QCOM", "INTC", "TXN",
            "ADI", "MCHP", "AMAT", "LRCX", "KLAC", "MU", "CSCO", "IBM",
            "ACN", "ORCL", "CRM", "ADBE", "NOW", "INTU", "PANW", "CRWD",
        ],
        "Financials": [
            "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "PYPL",
            "AXP", "BLK", "SCHW", "COF", "ICE", "CME",
        ],
        "Healthcare": [
            "UNH", "JNJ", "LLY", "ABBV", "MRK", "ABT", "PFE", "AMGN",
            "GILD", "BMY", "REGN", "VRTX", "MDT", "BSX", "SYK",
        ],
        "Consumer Cyclical": [
            "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "CMG", "TJX", "BKNG",
        ],
        "Consumer Defensive": [
            "WMT", "PG", "COST", "KO", "PEP", "PM", "MO",
        ],
        "Communication Services": [
            "GOOGL", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS",
        ],
        "Energy": [
            "XOM", "CVX", "COP", "EOG", "SLB", "OXY",
        ],
        "Industrials": [
            "HON", "BA", "UPS", "RTX", "LMT", "GE", "NOC", "GD", "CAT",
        ],
        "Basic Materials": [
            "LIN", "APD", "NEM", "FCX", "DD",
        ],
        "Real Estate": [
            "AMT", "PLD", "EQIX", "CCI",
        ],
        "Utilities": [
            "NEE", "DUK", "SO", "AEP",
        ],
    }
    bucket = _SECTOR_BUCKETS.get(sector, [])
    for c in bucket:
        if c not in results and c != t and len(results) < 8:
            results.append(c)
    return results[:8]


def fetch_stock_info(ticker: str) -> dict[str, Any]:
    """
    Full stock info: fundamentals + similar/competitor/dependency data.
    This function does not cache; callers can cache the resulting payload.
    """
    t = ticker.upper().strip()

    fund = _yfinance_fetch(t)
    sector   = fund.get("sector", "")
    industry = fund.get("industry", "")

    similar   = _sector_peers(t, sector, industry)
    competitors = _COMPETITORS.get(t, similar[:5])
    depends_on  = _DEPENDS_ON.get(t, [])
    depended_by = _DEPENDED_BY.get(t, [])

    result: dict[str, Any] = {
        **fund,
        "ticker": t,
        "similar_tickers": similar,
        "competitors":     competitors,
        "depends_on":      depends_on,
        "depended_by":     depended_by,
        "data_source":     "yfinance" if fund else "none",
    }

    return result
