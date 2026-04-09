// OPRA Pillar Multicast Feed Handler
// Joins the 96-line UDP multicast network; parses Pillar binary messages;
// pushes RawTicks onto the SPSC ring buffer.
//
// For production, replace the stub parser with the licensed Pillar spec decoder
// (OPRA Technical Specifications, Section 3).

use crate::circuit_breaker::CircuitBreaker;
use crate::ring_buffer::{OptionRight, RawTick, RingProducer};
use anyhow::Result;
use core_affinity::CoreId;
use socket2::{Domain, Protocol, Socket, Type};
use std::net::{Ipv4Addr, SocketAddrV4};
use std::time::{SystemTime, UNIX_EPOCH};
use tracing::{debug, info, warn};

#[derive(Debug)]
pub struct FeedConfig {
    pub multicast_group: String,
    pub port:            u16,
    pub interface:       String,
}

pub struct OPRAFeedHandler {
    config:   FeedConfig,
    producer: RingProducer,
    cb:       CircuitBreaker,
}

impl OPRAFeedHandler {
    pub fn new(config: FeedConfig, producer: RingProducer, cb: CircuitBreaker) -> Result<Self> {
        Ok(Self { config, producer, cb })
    }

    /// Blocking receive loop – must be called from a dedicated OS thread.
    /// Pins itself to CPU core 1 (leave core 0 for OS interrupts).
    pub fn run(self) -> Result<()> {
        // Core pinning – prevents context-switch jitter
        if let Some(cores) = core_affinity::get_core_ids() {
            if cores.len() > 1 {
                core_affinity::set_for_current(cores[1]);
                info!("Feed handler pinned to CPU core {:?}", cores[1]);
            }
        }

        let socket = self.build_multicast_socket()?;
        let mut buf = vec![0u8; 65_536];

        info!(
            group = %self.config.multicast_group,
            port  = self.config.port,
            "OPRA feed handler running"
        );

        loop {
            if self.cb.is_tripped() {
                warn!("Circuit breaker active – feed handler stopping");
                break;
            }

            match socket.recv_from(&mut buf) {
                Ok((len, _addr)) => {
                    let now_ns = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_nanos() as u64;

                    // Parse each Pillar message in the UDP datagram
                    for tick in parse_pillar_datagram(&buf[..len], now_ns) {
                        self.producer.push(tick);
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::yield_now();
                }
                Err(e) => {
                    warn!("Socket recv error: {e}");
                }
            }
        }
        Ok(())
    }

    fn build_multicast_socket(&self) -> Result<std::net::UdpSocket> {
        let socket = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
        socket.set_reuse_address(true)?;
        socket.set_nonblocking(true)?;

        let addr: Ipv4Addr = self.config.multicast_group.parse()?;
        let bind_addr = SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, self.config.port);
        socket.bind(&bind_addr.into())?;

        let iface: Ipv4Addr = self.config.interface.parse().unwrap_or(Ipv4Addr::UNSPECIFIED);
        socket.join_multicast_v4(&addr, &iface)?;

        debug!("Multicast socket bound on {}", self.config.port);
        Ok(socket.into())
    }
}

// ── Pillar binary parser stub ─────────────────────────────────────────────────
// Production: implement full OPRA Pillar spec (message types 0x21, 0x22, 0x35…)
fn parse_pillar_datagram(data: &[u8], timestamp_ns: u64) -> Vec<RawTick> {
    // Minimum viable: try to parse as JSON for Databento / normalised feed fallback
    if let Ok(s) = std::str::from_utf8(data) {
        if let Ok(tick) = serde_json::from_str::<RawTick>(s) {
            return vec![tick];
        }
    }

    // Return synthetic tick for dev/simulation mode
    #[cfg(feature = "simulation")]
    {
        let synthetic = make_synthetic_tick(timestamp_ns);
        return vec![synthetic];
    }

    vec![]
}

#[allow(dead_code)]
fn make_synthetic_tick(timestamp_ns: u64) -> RawTick {
    use rand::Rng;
    let mut rng = rand::thread_rng();
    let underlying: f64 = 500.0 + rng.gen_range(-5.0..5.0);
    RawTick {
        symbol:       "SPY".into(),
        expiry:       "20260620".into(),
        strike:       500.0,
        right:        OptionRight::Call,
        bid:          10.0 + rng.gen_range(-0.5..0.5),
        ask:          10.1 + rng.gen_range(-0.5..0.5),
        last:         10.05,
        volume:       rng.gen_range(100..10_000),
        open_int:     50_000,
        underlying,
        risk_free:    0.053,
        timestamp_ns,
    }
}
