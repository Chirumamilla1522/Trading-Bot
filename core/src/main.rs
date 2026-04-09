// Trading Core – Data Plane Entry Point
// Responsibilities:
//   1. Ingest OPRA multicast feed
//   2. Calculate Greeks on every tick via lock-free SPSC ring buffer
//   3. Write normalised ticks to shared-memory block (Python agents read)
//   4. Enforce the hardware-level Kill Switch / circuit breaker

mod circuit_breaker;
mod feed_handler;
mod greeks;
mod ring_buffer;
mod shm_bridge;

use anyhow::Result;
use circuit_breaker::CircuitBreaker;
use feed_handler::OPRAFeedHandler;
use ring_buffer::TickRingBuffer;
use shm_bridge::ShmBridge;
use tracing::{info, warn};
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("trading_core=debug".parse()?))
        .init();

    info!("Trading Core v0.1 – Data Plane starting");

    // ── Shared-memory bridge (Python control plane reads from here) ──────────
    let shm = ShmBridge::create("trading_ticks", 64 * 1024 * 1024)?; // 64 MB

    // ── Lock-free ring buffer between feed handler and Greeks engine ─────────
    let ring = TickRingBuffer::new(1 << 20); // 1M slots

    // ── Circuit breaker (max 5 % daily drawdown → kill switch) ──────────────
    let cb = CircuitBreaker::new(0.05);

    // ── OPRA feed handler ────────────────────────────────────────────────────
    let feed_cfg = feed_handler::FeedConfig {
        multicast_group: std::env::var("OPRA_MULTICAST_GROUP")
            .unwrap_or_else(|_| "233.43.202.1".to_string()),
        port: std::env::var("OPRA_PORT")
            .unwrap_or_else(|_| "11700".to_string())
            .parse()?,
        interface: std::env::var("OPRA_IFACE").unwrap_or_else(|_| "eth0".to_string()),
    };

    info!(?feed_cfg.multicast_group, feed_cfg.port, "Joining OPRA multicast");

    let handler = OPRAFeedHandler::new(feed_cfg, ring.producer(), cb.clone())?;

    // Consumer task: drain ring buffer, compute Greeks, write to SHM
    let consumer_ring = ring.consumer();
    let shm_writer    = shm.writer();
    let cb_consumer   = cb.clone();

    tokio::spawn(async move {
        let mut greeks_engine = greeks::GreeksEngine::default();
        loop {
            if let Some(tick) = consumer_ring.try_pop() {
                if cb_consumer.is_tripped() {
                    warn!("Circuit breaker TRIPPED – dropping tick");
                    continue;
                }
                let enriched = greeks_engine.enrich(tick);
                shm_writer.write(&enriched);
            } else {
                tokio::task::yield_now().await;
            }
        }
    });

    // Feed handler runs in its own OS thread with pinned core
    tokio::task::spawn_blocking(move || handler.run()).await??;

    Ok(())
}
