# Version history

Single source of truth for the release number is the **`VERSION`** file in this directory.

After each **chat/session** that ships meaningful changes to the app, bump the version and append a row below. Keep **`VERSION`**, **`ui/package.json`**, **`ui/src-tauri/tauri.conf.json`**, and **`ui/src-tauri/Cargo.toml`** in sync (same `MAJOR.MINOR.PATCH`).

| Version | Date (UTC) | Summary |
|---------|------------|---------|
| 0.2.0 | 2026-04-09 | UI: Midnight Meridian + experimental themes (CRT / Neon / Raw), theme toggle + `localStorage`, typography and atmosphere updates; version tracking added. |

### How to bump

1. Edit **`VERSION`** (e.g. `0.2.0` → `0.2.1`).
2. Set the same string in:
   - `ui/package.json` → `"version"`
   - `ui/src-tauri/tauri.conf.json` → `"version"`
   - `ui/src-tauri/Cargo.toml` → `version = "..."` under `[package]`
3. Add a row to the table above with today’s date and a one-line summary.

Use **patch** (`0.2.1`) for small fixes; **minor** (`0.3.0`) for features; **major** (`1.0.0`) for breaking or product-level changes.
