# Agent Instructions — `firefox-keybind-sync`

## 1. Authoritative Brain Context
Before making any changes in this repository, read the project's brain files:
- @[Steerage](~/brain/projects/firefox-keybind-sync/STEERAGE.md)
- @[Plan](~/brain/projects/firefox-keybind-sync/PLAN.md)
- @[State Log](~/brain/projects/firefox-keybind-sync/STATE_LOG.md)
- @[Global Agent Instructions](~/brain/shared/GLOBAL_AGENTS.md)

### Project Overview
- **Goal**: Standalone, zero-dependency Python 3.10+ CLI (`ff_keybind_sync.py`) for inspecting, exporting, and surgically importing Firefox extension shortcuts (`extension-settings.json`), extension private-browsing permissions (`extension-preferences.json`), toolbar/pop-out layouts (`browser.uiCustomization.state` via `user.js`), `xulstore.json`, `storage-sync-v2.sqlite`, and `tridactylrc` across Windows, WSL, Linux, and macOS.

## 2. Session Handoff
Always update @[State Log](~/brain/projects/firefox-keybind-sync/STATE_LOG.md) and check off completed items in @[Plan](~/brain/projects/firefox-keybind-sync/PLAN.md) before ending a session or when `/session-handoff` is invoked.
