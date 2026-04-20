# Version history

Single source of truth for the release number is the **`VERSION`** file in this directory.

After each **chat/session** that ships meaningful changes to the app, bump the version and append a row below. Keep **`VERSION`**, **`ui/package.json`**, **`ui/src-tauri/tauri.conf.json`**, and **`ui/src-tauri/Cargo.toml`** in sync (same `MAJOR.MINOR.PATCH`).

| Version | Date (UTC) | Summary |
|---------|------------|---------|
| 0.2.5 | 2026-04-09 | UI: backup before Atlas zero-match rebuild: `ui/index-v0.2.5-pre-atlas.html`, `ui/src/main-v0.2.5-pre-atlas.js`, `ui/src/terminal-v0.2.5-pre-atlas.css`. |
| 0.2.4 | 2026-04-09 | UI: backup before Control Room (non-terminal) rebuild: `ui/src/terminal-v0.2.4-pre-controlroom.css`, `ui/index-v0.2.4-pre-controlroom.html`. |
| 0.2.3 | 2026-04-09 | UI: backup before Starship‑OS shell rewrite: `ui/src/terminal-v0.2.3-pre-starshipos.css`, `ui/index-v0.2.3-pre-starshipos.html`. |
| 0.2.2 | 2026-04-09 | UI: backup before v4 Spatial HUD rebuild: `ui/src/terminal-v0.2.2-pre-v4-hud.css`, `ui/index-v0.2.2-pre-v4-hud.html`. |
| 0.2.2 | 2026-04-09 | UI: backup before macOS-style Orchard layout revamp: `ui/src/terminal-v0.2.2-pre-orchard-layout.css`, `ui/index-v0.2.2-pre-orchard-layout.html`. |
| 0.2.2 | 2026-04-09 | UI: snapshot before Light/Dark: `ui/src/terminal-v0.2.2-pre-darklight.css`, `ui/src/index-v0.2.2-pre-darklight.html`; then add Light/Dark toggle (persisted) and refine Nuke styling. |
| 0.2.2 | 2026-04-09 | UI: backup before Apple “Liquid Glass” revamp: `ui/src/terminal-v0.2.2-pre-apple-glass.css`, `ui/index-v0.2.2-pre-apple-glass.html`. |
