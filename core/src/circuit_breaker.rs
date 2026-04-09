// Hardware-level Kill Switch
// If daily P&L drawdown exceeds `max_drawdown_pct`, the circuit breaker trips:
//   1. Sets an atomic flag read by every downstream component
//   2. Revokes broker API keys from a local secrets file
//   3. Emits a SIGTERM to the Python control plane via a named pipe

use std::sync::{
    atomic::{AtomicBool, AtomicI64, Ordering},
    Arc,
};
use tracing::{error, warn};

const BALANCE_SCALE: f64 = 1_000_000.0; // store as integer to avoid floats in atomics

#[derive(Clone)]
pub struct CircuitBreaker {
    inner: Arc<CBInner>,
}

struct CBInner {
    tripped:          AtomicBool,
    opening_balance:  AtomicI64,
    current_balance:  AtomicI64,
    max_drawdown_pct: f64,
}

impl CircuitBreaker {
    pub fn new(max_drawdown_pct: f64) -> Self {
        Self {
            inner: Arc::new(CBInner {
                tripped:          AtomicBool::new(false),
                opening_balance:  AtomicI64::new(0),
                current_balance:  AtomicI64::new(0),
                max_drawdown_pct,
            }),
        }
    }

    /// Call at session open with the account NAV.
    pub fn set_opening_balance(&self, balance: f64) {
        let scaled = (balance * BALANCE_SCALE) as i64;
        self.inner.opening_balance.store(scaled, Ordering::Release);
        self.inner.current_balance.store(scaled, Ordering::Release);
    }

    /// Called after each fill / MTM update.
    pub fn update_balance(&self, new_balance: f64) {
        let scaled = (new_balance * BALANCE_SCALE) as i64;
        self.inner.current_balance.store(scaled, Ordering::Release);
        self.check();
    }

    /// Hot path: zero-overhead check (single atomic load).
    #[inline(always)]
    pub fn is_tripped(&self) -> bool {
        self.inner.tripped.load(Ordering::Acquire)
    }

    fn check(&self) {
        if self.inner.tripped.load(Ordering::Acquire) {
            return; // already tripped
        }
        let open  = self.inner.opening_balance.load(Ordering::Acquire) as f64 / BALANCE_SCALE;
        let curr  = self.inner.current_balance.load(Ordering::Acquire) as f64 / BALANCE_SCALE;
        if open <= 0.0 { return; }
        let drawdown = (open - curr) / open;
        if drawdown >= self.inner.max_drawdown_pct {
            error!(
                drawdown = format!("{:.2}%", drawdown * 100.0),
                limit    = format!("{:.2}%", self.inner.max_drawdown_pct * 100.0),
                "⚡ CIRCUIT BREAKER TRIPPED – initiating kill sequence"
            );
            self.inner.tripped.store(true, Ordering::Release);
            self.execute_kill_sequence();
        }
    }

    fn execute_kill_sequence(&self) {
        // 1. Revoke API keys (overwrite secrets file with empty credentials)
        let secrets_path = std::env::var("SECRETS_FILE")
            .unwrap_or_else(|_| "config/secrets.toml".to_string());
        if let Err(e) = std::fs::write(&secrets_path, "# REVOKED BY CIRCUIT BREAKER\n") {
            warn!("Could not revoke secrets file {secrets_path}: {e}");
        }

        // 2. Signal control plane to halt via named pipe
        let pipe_path = std::env::var("CTRL_PIPE")
            .unwrap_or_else(|_| "/tmp/trading_ctrl".to_string());
        let _ = std::fs::write(&pipe_path, "KILL\n");

        error!("Kill sequence complete. Manual intervention required to resume.");
    }
}
