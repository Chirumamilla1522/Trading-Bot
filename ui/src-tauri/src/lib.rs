// Tauri v2 library entry point
// All commands and setup live here; main.rs just calls run().

use tauri::{AppHandle, Emitter};
use serde::{Deserialize, Serialize};
use std::time::Duration;

#[derive(Debug, Clone, Serialize, Deserialize)]
struct AgentEvent {
    agent:     String,
    action:    String,
    reasoning: String,
    timestamp: String,
}

/// Return today's XAI reasoning log entries.
#[tauri::command]
fn get_reasoning_log() -> Vec<AgentEvent> {
    let log_dir  = std::env::var("XAI_LOG_DIR").unwrap_or_else(|_| "logs/xai".to_string());
    let date_str = today_yyyymmdd();
    let path     = format!("{log_dir}/reasoning_{date_str}.jsonl");
    std::fs::read_to_string(&path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect()
}

/// Write KILL to the control pipe – mirrors the Rust data-plane circuit breaker.
#[tauri::command]
fn trigger_kill_switch() -> String {
    let pipe = std::env::var("CTRL_PIPE").unwrap_or_else(|_| "/tmp/trading_ctrl".to_string());
    let _ = std::fs::write(&pipe, "KILL\n");
    "Kill switch activated".to_string()
}

/// Broadcast new XAI log lines to the frontend every 500 ms.
fn start_event_broadcaster(app: AppHandle) {
    std::thread::spawn(move || {
        let mut last_count: usize = 0;
        loop {
            std::thread::sleep(Duration::from_millis(500));
            let log_dir  = std::env::var("XAI_LOG_DIR").unwrap_or_else(|_| "logs/xai".to_string());
            let path     = format!("{log_dir}/reasoning_{}.jsonl", today_yyyymmdd());
            let contents = std::fs::read_to_string(&path).unwrap_or_default();
            let lines: Vec<&str> = contents.lines().collect();
            if lines.len() > last_count {
                for line in &lines[last_count..] {
                    if let Ok(ev) = serde_json::from_str::<AgentEvent>(line) {
                        let _ = app.emit("agent_event", ev);
                    }
                }
                last_count = lines.len();
            }
        }
    });
}

fn today_yyyymmdd() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs  = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let days  = secs / 86400;
    let z     = days + 719_163;
    let era   = z / 146_097;
    let doe   = z - era * 146_097;
    let yoe   = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y     = yoe + era * 400;
    let doy   = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp    = (5 * doy + 2) / 153;
    let d     = doy - (153 * mp + 2) / 5 + 1;
    let m     = if mp < 10 { mp + 3 } else { mp - 9 };
    let y     = if m <= 2 { y + 1 } else { y };
    format!("{y}{m:02}{d:02}")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            start_event_broadcaster(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_reasoning_log,
            trigger_kill_switch,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Tauri application");
}
