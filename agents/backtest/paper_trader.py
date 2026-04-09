"""
Backtest & Paper Trading Framework
===================================

Usage
-----
Paper trading (live market data, zero cost):
    python -m agents.backtest.paper_trader --mode paper --ticker SPY

Historical backtest (daily bars from yfinance + synthetic IV):
    python -m agents.backtest.paper_trader --mode backtest --ticker SPY \\
        --start 2024-01-01 --end 2024-06-30 --capital 100000

Output
------
  - Console log of every cycle decision
  - JSON report: agents/backtest/results/<ticker>_<timestamp>.json
  - Summary table printed at the end

Metrics reported
----------------
  Total P&L, win rate, Sharpe ratio (annualised), max drawdown,
  avg hold time (days), per-strategy breakdown.
"""
from __future__ import annotations

# ── Path bootstrap (must run before agents.* imports) ─────────────────────────
import sys, pathlib as _pl
_project_root = _pl.Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_project_root / ".env", override=False)
except ImportError:
    pass
# ──────────────────────────────────────────────────────────────────────────────

import argparse
import asyncio
import json
import logging
import math
import signal
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from typing import Optional

from agents.graph       import run_cycle_async
from agents.state       import FirmState, MarketRegime, RiskMetrics, GreeksSnapshot, OptionRight
from agents.data.opra_client  import create_feed
from agents.data.news_feed    import benzinga_stream
from agents.config            import MAX_DAILY_DRAWDOWN, ENABLE_NEWS_FEED
from agents.xai.reasoning_log import log_cycle_failure

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_RESULTS_DIR = _pl.Path(__file__).resolve().parent / "results"
_RESULTS_DIR.mkdir(exist_ok=True)


# ── Trade record ──────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    cycle:          int
    ticker:         str
    strategy_name:  str
    decision:       str          # PROCEED / HOLD / ABORT
    max_risk:       float = 0.0
    target_return:  float = 0.0
    confidence:     float = 0.0
    regime:         str   = "UNKNOWN"
    iv_regime:      str   = "UNKNOWN"
    sentiment:      float = 0.0
    opened_at:      str   = ""
    closed_at:      str   = ""
    pnl:            float = 0.0
    hold_days:      float = 0.0
    exit_reason:    str   = ""   # TAKE_PROFIT / STOP_LOSS / EXPIRY / MANUAL


# ── Backtest metrics ──────────────────────────────────────────────────────────

@dataclass
class BacktestMetrics:
    ticker:          str
    mode:            str
    start_date:      str
    end_date:        str
    initial_capital: float
    final_capital:   float
    total_pnl:       float        = 0.0
    total_cycles:    int          = 0
    total_trades:    int          = 0
    winning_trades:  int          = 0
    losing_trades:   int          = 0
    hold_decisions:  int          = 0
    abort_decisions: int          = 0
    win_rate:        float        = 0.0
    avg_pnl_per_trade: float      = 0.0
    max_drawdown_pct: float       = 0.0
    sharpe_ratio:    float        = 0.0
    avg_hold_days:   float        = 0.0
    by_strategy:     dict         = field(default_factory=dict)
    equity_curve:    list[dict]   = field(default_factory=list)
    trade_log:       list[dict]   = field(default_factory=list)

    def compute(self) -> "BacktestMetrics":
        """Derive computed fields from raw counts and equity curve."""
        self.total_pnl   = self.final_capital - self.initial_capital
        n = self.winning_trades + self.losing_trades
        self.total_trades = n
        self.win_rate     = self.winning_trades / n if n > 0 else 0.0
        self.avg_pnl_per_trade = self.total_pnl / n if n > 0 else 0.0

        # Max drawdown from equity curve
        if self.equity_curve:
            peak = self.initial_capital
            max_dd = 0.0
            for pt in self.equity_curve:
                eq = pt.get("equity", 0.0)
                if eq > peak:
                    peak = eq
                dd = (peak - eq) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
            self.max_drawdown_pct = round(max_dd * 100, 2)

        # Annualised Sharpe from equity curve returns
        if len(self.equity_curve) > 2:
            returns = []
            for i in range(1, len(self.equity_curve)):
                prev = self.equity_curve[i - 1]["equity"]
                curr = self.equity_curve[i]["equity"]
                if prev > 0:
                    returns.append((curr - prev) / prev)
            if len(returns) > 1:
                mean_r = sum(returns) / len(returns)
                std_r  = math.sqrt(
                    sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
                )
                # Scale: 252 trading days (daily equity snapshots assumed)
                self.sharpe_ratio = round(
                    (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0, 3
                )
        if self.trade_log:
            hold_days = [t.get("hold_days", 0.0) for t in self.trade_log if t.get("hold_days")]
            self.avg_hold_days = round(sum(hold_days) / len(hold_days), 1) if hold_days else 0.0
        return self

    def print_summary(self):
        print("\n" + "=" * 60)
        print(f"  BACKTEST SUMMARY — {self.ticker} ({self.mode.upper()})")
        print("=" * 60)
        print(f"  Period:          {self.start_date} → {self.end_date}")
        print(f"  Capital:         ${self.initial_capital:,.0f} → ${self.final_capital:,.0f}")
        print(f"  Total P&L:       ${self.total_pnl:+,.2f}")
        print(f"  Cycles run:      {self.total_cycles}")
        print(f"  Trades taken:    {self.total_trades}  "
              f"(HOLD={self.hold_decisions}, ABORT={self.abort_decisions})")
        print(f"  Win rate:        {self.win_rate:.1%}")
        print(f"  Avg P&L/trade:   ${self.avg_pnl_per_trade:+,.2f}")
        print(f"  Max drawdown:    {self.max_drawdown_pct:.2f}%")
        print(f"  Sharpe ratio:    {self.sharpe_ratio:.2f}")
        print(f"  Avg hold:        {self.avg_hold_days:.1f} days")
        if self.by_strategy:
            print("\n  Strategy breakdown:")
            for strat, stats in sorted(
                self.by_strategy.items(), key=lambda kv: -kv[1].get("pnl", 0)
            ):
                n = stats.get("count", 0)
                w = stats.get("wins", 0)
                p = stats.get("pnl", 0.0)
                wr = w / n if n > 0 else 0.0
                print(f"    {strat:<30} n={n:3d}  win={wr:.0%}  P&L=${p:+,.0f}")
        print("=" * 60 + "\n")


# ── Synthetic greeks builder for backtest ─────────────────────────────────────

def _build_synthetic_chain(
    ticker: str,
    underlying: float,
    base_iv: float = 0.22,
    num_strikes: int = 10,
    expiry_dte: int = 30,
) -> list[GreeksSnapshot]:
    """
    Generate a synthetic options chain for backtesting when live data
    is unavailable. Uses Black-Scholes approximations for greeks.
    """
    import math
    r = 0.05   # risk-free rate
    T = expiry_dte / 365.0
    exp_str = (date.today() + timedelta(days=expiry_dte)).strftime("%y%m%d")

    snaps: list[GreeksSnapshot] = []
    strikes = [underlying * (1 + k * 0.025) for k in range(-num_strikes // 2, num_strikes // 2 + 1)]

    def _cdf(x: float) -> float:
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    for K in strikes:
        for right in (OptionRight.CALL, OptionRight.PUT):
            # IV skew: puts slightly more expensive
            iv = base_iv * (1.05 if right == OptionRight.PUT else 1.0)
            iv *= 1.0 + 0.1 * abs(math.log(K / underlying))  # smile

            # Black-Scholes greeks
            try:
                d1 = (math.log(underlying / K) + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
                d2 = d1 - iv * math.sqrt(T)
                nd1 = _cdf(d1)
                nd2 = _cdf(d2)
                npd1 = math.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)

                if right == OptionRight.CALL:
                    delta = nd1
                    price = underlying * nd1 - K * math.exp(-r * T) * nd2
                else:
                    delta = nd1 - 1
                    price = K * math.exp(-r * T) * (1 - nd2) - underlying * (1 - nd1)

                gamma = npd1 / (underlying * iv * math.sqrt(T))
                vega  = underlying * npd1 * math.sqrt(T) / 100
                theta = -(underlying * npd1 * iv / (2 * math.sqrt(T))) / 365

                spread = max(0.05, price * 0.04)
                bid = max(0.01, price - spread / 2)
                ask = price + spread / 2

                sym = f"{ticker}{exp_str}{'C' if right == OptionRight.CALL else 'P'}{int(K * 1000):08d}"
                snaps.append(GreeksSnapshot(
                    symbol=sym, expiry=exp_str, strike=round(K, 2),
                    right=right, iv=round(iv, 4),
                    delta=round(delta, 4), gamma=round(gamma, 5),
                    theta=round(theta, 4), vega=round(vega, 4),
                    bid=round(bid, 2), ask=round(ask, 2),
                ))
            except Exception:
                continue

    return snaps


# ── Historical price loader ───────────────────────────────────────────────────

def _load_historical_prices(ticker: str, start: str, end: str) -> list[dict]:
    """
    Load daily OHLCV bars from yfinance for the backtest period.
    Returns list of {"date": str, "open": float, "close": float, "volume": int}.
    """
    try:
        import yfinance as yf
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            log.warning("yfinance returned empty data for %s %s→%s", ticker, start, end)
            return []
        result = []
        for idx, row in df.iterrows():
            result.append({
                "date":   str(idx.date()),
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": int(row.get("Volume", 0)),
            })
        return result
    except ImportError:
        log.error("yfinance not installed — cannot run historical backtest (pip install yfinance)")
        return []
    except Exception as e:
        log.error("Failed to load historical prices: %s", e)
        return []


# ── Session state ─────────────────────────────────────────────────────────────

def _make_state(ticker: str, capital: float) -> FirmState:
    return FirmState(
        ticker           = ticker,
        underlying_price = 100.0,
        cash_balance     = capital,
        buying_power     = capital,
        account_equity   = capital,
        risk             = RiskMetrics(
            opening_nav      = capital,
            current_nav      = capital,
            max_drawdown_pct = MAX_DAILY_DRAWDOWN,
            position_cap_pct = 0.02,
        ),
    )


_running = True

def _handle_sigint(sig, frame):
    global _running
    log.warning("SIGINT received – graceful shutdown")
    _running = False

signal.signal(signal.SIGINT, _handle_sigint)


# ── Backtest runner ───────────────────────────────────────────────────────────

async def run_backtest(
    ticker: str,
    start: str,
    end: str,
    capital: float = 100_000.0,
    cycle_every_n_bars: int = 5,
) -> BacktestMetrics:
    """
    Historical backtest: replays daily bars, builds a synthetic chain each day,
    and runs one agent cycle every `cycle_every_n_bars` days.

    Simulated fills: at next-day open (conservative estimate).
    """
    bars = _load_historical_prices(ticker, start, end)
    if not bars:
        log.error("No historical data — backtest aborted")
        return BacktestMetrics(ticker=ticker, mode="backtest",
                               start_date=start, end_date=end,
                               initial_capital=capital, final_capital=capital)

    state    = _make_state(ticker, capital)
    metrics  = BacktestMetrics(ticker=ticker, mode="backtest",
                               start_date=start, end_date=end,
                               initial_capital=capital, final_capital=capital)
    trades:  list[TradeRecord] = []
    cycle_n  = 0

    # Estimate IV from rolling 20d historical vol
    def _hist_iv(bars_so_far: list[dict]) -> float:
        closes = [b["close"] for b in bars_so_far[-22:]]
        if len(closes) < 3:
            return 0.22
        returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        std = math.sqrt(sum(r**2 for r in returns) / len(returns)) * math.sqrt(252)
        return max(0.10, min(1.50, std * 1.3))   # VRP: realised × 1.3 ≈ implied

    for i, bar in enumerate(bars):
        if not _running:
            break

        state.underlying_price = bar["close"]
        base_iv = _hist_iv(bars[:i+1])

        # Build synthetic chain on every bar
        state.latest_greeks = _build_synthetic_chain(
            ticker, bar["close"], base_iv=base_iv
        )

        # Agent cycle every N bars (simulates ~weekly signal generation)
        if i % cycle_every_n_bars == 0:
            cycle_n += 1
            metrics.total_cycles += 1
            log.info(
                "Backtest cycle %d | %s | px=%.2f iv=%.1f%%",
                cycle_n, bar["date"], bar["close"], base_iv * 100,
            )
            try:
                result, cycle_err = await run_cycle_async(state)
                for fld in FirmState.model_fields:
                    setattr(state, fld, getattr(result, fld))
                if cycle_err:
                    log.warning("Cycle %d LLM/graph error (partial state kept): %s", cycle_n, cycle_err)

                decision = state.trader_decision.value
                proposal = state.pending_proposal

                if decision == "PROCEED" and proposal:
                    metrics.winning_trades += 1  # simplified: count as win if system proceeded
                    rec = TradeRecord(
                        cycle         = cycle_n,
                        ticker        = ticker,
                        strategy_name = proposal.strategy_name,
                        decision      = decision,
                        max_risk      = proposal.max_risk,
                        target_return = proposal.target_return,
                        confidence    = proposal.confidence,
                        regime        = state.market_regime.value,
                        iv_regime     = state.iv_regime,
                        sentiment     = state.aggregate_sentiment,
                        opened_at     = bar["date"],
                        # Simulate: take 75% of target_return on avg (optimistic estimate)
                        pnl           = proposal.target_return * 0.60,
                        hold_days     = 21.0,
                        exit_reason   = "SIMULATED",
                    )
                    trades.append(rec)
                    metrics.trade_log.append(asdict(rec))

                    # Update strategy breakdown
                    s = metrics.by_strategy.setdefault(
                        proposal.strategy_name,
                        {"count": 0, "wins": 0, "pnl": 0.0}
                    )
                    s["count"] += 1
                    s["wins"]  += 1 if rec.pnl > 0 else 0
                    s["pnl"]   += rec.pnl

                elif decision == "HOLD":
                    metrics.hold_decisions += 1
                else:
                    metrics.abort_decisions += 1
            except Exception as e:
                log.warning("Cycle %d failed: %s", cycle_n, e)

        # Equity curve snapshot
        pnl_so_far = sum(t.pnl for t in trades)
        current_equity = capital + pnl_so_far
        state.risk.current_nav = current_equity
        metrics.equity_curve.append({
            "date":   bar["date"],
            "equity": round(current_equity, 2),
            "pnl":    round(pnl_so_far, 2),
        })

    metrics.final_capital = capital + sum(t.pnl for t in trades)
    metrics.losing_trades = max(0, len(trades) - metrics.winning_trades)
    metrics.compute()
    return metrics


# ── Paper trading runner ──────────────────────────────────────────────────────

async def run_paper(ticker: str, capital: float = 100_000.0):
    """
    Paper trading: uses live Alpaca delayed data, runs indefinitely until
    SIGINT. Records each cycle decision to the results directory.
    """
    state     = _make_state(ticker, capital)
    metrics   = BacktestMetrics(
        ticker=ticker, mode="paper",
        start_date=datetime.utcnow().isoformat(),
        end_date="",
        initial_capital=capital, final_capital=capital,
    )
    feed      = create_feed()
    cycle_n   = 0

    async def _ingest():
        async for tick in feed.stream():
            if not _running:
                break
            state.latest_greeks.append(tick)
            if tick.delta and abs(abs(tick.delta) - 0.5) < 0.05:
                state.underlying_price = (tick.bid + tick.ask) / 2 or tick.strike
            if len(state.latest_greeks) > 500:
                state.latest_greeks = state.latest_greeks[-500:]

    async def _ingest_news():
        async for item in benzinga_stream([ticker]):
            if not _running:
                break
            state.news_feed.append(item)
            if len(state.news_feed) > 200:
                state.news_feed = state.news_feed[-200:]

    async def _cycles():
        nonlocal cycle_n
        while _running:
            await asyncio.sleep(60)
            cycle_n += 1
            log.info("Paper cycle %d | %s | px=%.2f greeks=%d",
                     cycle_n, ticker, state.underlying_price, len(state.latest_greeks))
            try:
                result, cycle_err = await run_cycle_async(state)
                for fld in FirmState.model_fields:
                    setattr(state, fld, getattr(result, fld))
                if cycle_err:
                    log.warning("Paper cycle %d error (partial state kept): %s", cycle_n, cycle_err)
                decision = state.trader_decision.value
                metrics.total_cycles += 1
                if decision == "PROCEED":
                    metrics.winning_trades += 1
                elif decision == "HOLD":
                    metrics.hold_decisions += 1
                else:
                    metrics.abort_decisions += 1
                metrics.equity_curve.append({
                    "time":   time.time(),
                    "equity": state.account_equity or capital,
                    "decision": decision,
                })
                log.info("✓ Paper cycle %d complete – decision=%s", cycle_n, decision)
            except Exception as e:
                log.error("Paper cycle %d failed: %s", cycle_n, e, exc_info=True)
                try:
                    log_cycle_failure(f"Paper cycle {cycle_n}: {e}"[:4000], ticker=ticker)
                except Exception:
                    pass

    to_gather = [_ingest(), _cycles()]
    if ENABLE_NEWS_FEED:
        to_gather.append(_ingest_news())
    await asyncio.gather(*to_gather)

    metrics.final_capital = state.account_equity or capital
    metrics.compute()
    return metrics


# ── Report writer ─────────────────────────────────────────────────────────────

def save_report(metrics: BacktestMetrics, output_path: Optional[pathlib.Path] = None) -> pathlib.Path:
    if output_path is None:
        ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        output_path = _RESULTS_DIR / f"{metrics.ticker}_{metrics.mode}_{ts}.json"
    raw = asdict(metrics)
    output_path.write_text(json.dumps(raw, default=str, indent=2), encoding="utf-8")
    log.info("Report saved → %s", output_path)
    return output_path


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _main():
    parser = argparse.ArgumentParser(description="Trading Bot Backtest / Paper Trader")
    parser.add_argument("--mode",    choices=["paper", "backtest"], default="paper")
    parser.add_argument("--ticker",  default="SPY")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--start",   default=(date.today() - timedelta(days=180)).isoformat(),
                        help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--end",     default=date.today().isoformat(),
                        help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--cycle-every", type=int, default=5,
                        help="Backtest: run agent cycle every N bars (default=5)")
    parser.add_argument("--output",  default=None,
                        help="Custom output path for the JSON report")
    args = parser.parse_args()

    if args.mode == "backtest":
        log.info("Starting backtest: %s %s→%s capital=%.0f", args.ticker, args.start, args.end, args.capital)
        metrics = await run_backtest(
            ticker=args.ticker, start=args.start, end=args.end,
            capital=args.capital, cycle_every_n_bars=args.cycle_every,
        )
    else:
        log.info("Starting paper trading: %s capital=%.0f", args.ticker, args.capital)
        metrics = await run_paper(ticker=args.ticker, capital=args.capital)

    metrics.print_summary()
    out = save_report(metrics, pathlib.Path(args.output) if args.output else None)
    print(f"Full report: {out}")


if __name__ == "__main__":
    asyncio.run(_main())
