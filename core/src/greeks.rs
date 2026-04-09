// Real-time Greeks engine
// Computes Δ, Γ, Θ, V, ρ and implied volatility on every OPRA tick.
// Uses the Black-Scholes closed form; swap for Black-76 on futures options.
//
// All math uses the standard normal CDF approximated by Horner's method
// (7th-order polynomial, max error < 1.5e-7) to avoid libm overhead.

use crate::ring_buffer::{EnrichedTick, OptionRight, RawTick};
use std::f64::consts::{E, PI, SQRT_2};

// ── Normal CDF via Hart's rational approximation ─────────────────────────────
fn norm_cdf(x: f64) -> f64 {
    let t = 1.0 / (1.0 + 0.2316419 * x.abs());
    let poly = t * (0.319381530
        + t * (-0.356563782
        + t * (1.781477937
        + t * (-1.821255978
        + t * 1.330274429))));
    let pdf = (-x * x / 2.0).exp() / (2.0 * PI).sqrt();
    if x >= 0.0 { 1.0 - pdf * poly } else { pdf * poly }
}

fn norm_pdf(x: f64) -> f64 {
    (-x * x / 2.0).exp() / (2.0 * PI).sqrt()
}

// ── Implied Volatility via Brent-Dekker bisection ────────────────────────────
fn implied_vol(
    market_price: f64,
    s: f64, k: f64, r: f64, t: f64,
    right: &OptionRight,
) -> f64 {
    if t <= 0.0 || market_price <= 0.0 { return 0.0; }
    let (mut lo, mut hi) = (1e-6_f64, 5.0_f64);
    for _ in 0..100 {
        let mid = (lo + hi) / 2.0;
        let price = bs_price(s, k, r, t, mid, right);
        if (price - market_price).abs() < 1e-8 { return mid; }
        if price > market_price { hi = mid; } else { lo = mid; }
    }
    (lo + hi) / 2.0
}

// ── Black-Scholes price ───────────────────────────────────────────────────────
fn bs_price(s: f64, k: f64, r: f64, t: f64, sigma: f64, right: &OptionRight) -> f64 {
    let d1 = (s / k).ln() + (r + sigma * sigma / 2.0) * t;
    let d1 = d1 / (sigma * t.sqrt());
    let d2 = d1 - sigma * t.sqrt();
    match right {
        OptionRight::Call =>  s * norm_cdf(d1)  - k * (-r * t).exp() * norm_cdf(d2),
        OptionRight::Put  => -s * norm_cdf(-d1) + k * (-r * t).exp() * norm_cdf(-d2),
    }
}

// ── Greeks ────────────────────────────────────────────────────────────────────
fn bs_greeks(s: f64, k: f64, r: f64, t: f64, sigma: f64, right: &OptionRight)
    -> (f64, f64, f64, f64, f64)
{
    if t <= 0.0 || sigma <= 0.0 {
        return (0.0, 0.0, 0.0, 0.0, 0.0);
    }
    let sqrt_t = t.sqrt();
    let d1 = ((s / k).ln() + (r + sigma * sigma / 2.0) * t) / (sigma * sqrt_t);
    let d2 = d1 - sigma * sqrt_t;
    let disc = (-r * t).exp();

    let delta = match right {
        OptionRight::Call => norm_cdf(d1),
        OptionRight::Put  => norm_cdf(d1) - 1.0,
    };
    let gamma = norm_pdf(d1) / (s * sigma * sqrt_t);
    let theta = match right {
        OptionRight::Call =>
            (-s * norm_pdf(d1) * sigma / (2.0 * sqrt_t)
             - r * k * disc * norm_cdf(d2)) / 365.0,
        OptionRight::Put  =>
            (-s * norm_pdf(d1) * sigma / (2.0 * sqrt_t)
             + r * k * disc * norm_cdf(-d2)) / 365.0,
    };
    let vega = s * norm_pdf(d1) * sqrt_t / 100.0; // per 1% IV move
    let rho = match right {
        OptionRight::Call =>  k * t * disc * norm_cdf(d2)  / 100.0,
        OptionRight::Put  => -k * t * disc * norm_cdf(-d2) / 100.0,
    };
    (delta, gamma, theta, vega, rho)
}

// ── Engine ────────────────────────────────────────────────────────────────────
#[derive(Default)]
pub struct GreeksEngine;

impl GreeksEngine {
    pub fn enrich(&mut self, tick: RawTick) -> EnrichedTick {
        // Time to expiry in years
        let expiry_date = chrono_tte(&tick.expiry);
        let t = expiry_date.max(1.0 / 365.0); // floor at 1 day

        let mid = (tick.bid + tick.ask) / 2.0;
        let iv  = implied_vol(mid, tick.underlying, tick.strike,
                              tick.risk_free, t, &tick.right);

        let (delta, gamma, theta, vega, rho) =
            bs_greeks(tick.underlying, tick.strike, tick.risk_free, t, iv, &tick.right);

        EnrichedTick { raw: tick, iv, delta, gamma, theta, vega, rho }
    }
}

/// Rough days-to-expiry → years conversion (production: use proper calendar).
fn chrono_tte(expiry: &str) -> f64 {
    // expiry format: "YYYYMMDD"
    if expiry.len() < 8 { return 0.0; }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let y: i64 = expiry[0..4].parse().unwrap_or(2026);
    let m: i64 = expiry[4..6].parse().unwrap_or(1);
    let d: i64 = expiry[6..8].parse().unwrap_or(1);
    // Approximate: days_left / 365
    let exp_epoch = (y - 1970) * 365 * 86400 + m * 30 * 86400 + d * 86400;
    let days_left = ((exp_epoch - now as i64) as f64) / 86400.0;
    days_left.max(0.0) / 365.0
}
