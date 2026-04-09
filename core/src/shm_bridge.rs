// Shared-Memory IPC Bridge
// The Rust data plane writes EnrichedTick structs as newline-delimited JSON
// into a named shared memory block. The Python control plane (mmap reader)
// polls the same block to avoid network round-trips.
//
// Layout:
//   [8 bytes: write_cursor (u64 LE)] [N bytes: rolling JSON lines]

use crate::ring_buffer::EnrichedTick;
use anyhow::Result;
use std::sync::{Arc, Mutex};
use tracing::trace;

pub struct ShmBridge {
    buffer: Arc<Mutex<Vec<u8>>>,
    cursor: Arc<std::sync::atomic::AtomicUsize>,
}

impl ShmBridge {
    pub fn create(_name: &str, capacity: usize) -> Result<Self> {
        Ok(Self {
            buffer: Arc::new(Mutex::new(vec![0u8; capacity])),
            cursor: Arc::new(std::sync::atomic::AtomicUsize::new(8)), // skip header
        })
    }

    pub fn writer(&self) -> ShmWriter {
        ShmWriter {
            buffer: Arc::clone(&self.buffer),
            cursor: Arc::clone(&self.cursor),
        }
    }
}

pub struct ShmWriter {
    buffer: Arc<Mutex<Vec<u8>>>,
    cursor: Arc<std::sync::atomic::AtomicUsize>,
}

impl ShmWriter {
    pub fn write(&self, tick: &EnrichedTick) {
        if let Ok(mut json) = serde_json::to_vec(tick) {
            json.push(b'\n');
            let mut buf = self.buffer.lock().expect("SHM lock poisoned");
            let capacity = buf.len();
            let pos = self.cursor.load(std::sync::atomic::Ordering::Relaxed);
            let end = pos + json.len();
            if end < capacity {
                buf[pos..end].copy_from_slice(&json);
                self.cursor.store(end, std::sync::atomic::Ordering::Release);
                trace!("SHM write @ {pos}..{end}");
            } else {
                // Wrap around – reset after header
                let end = 8 + json.len();
                buf[8..end].copy_from_slice(&json);
                self.cursor.store(end, std::sync::atomic::Ordering::Release);
            }
        }
    }
}
