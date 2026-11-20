# firefox-keybind-sync

Zero-dependency Python 3.10+ CLI to inspect, export, and surgically import Firefox extension keyboard shortcuts, private-browsing permissions, toolbar/pop-out button layouts, `storage-sync-v2.sqlite`, and Tridactyl configuration across **Windows**, **WSL**, **Linux (Ubuntu / Pop!_OS)**, and **macOS**.

## Why Firefox Sync Misses These Settings

Even with Firefox Sync enabled for add-ons, critical customization state stays machine-local or breaks across installs:

1. **`extension-settings.json` (`commands`) is not synced**: Firefox stores custom add-on shortcuts configured under `about:addons` -> **Manage Extension Shortcuts** (such as Bitwarden `Ctrl+Shift+F` / `Ctrl+Shift+L` or uBlock Origin element picker shortcuts) in `extension-settings.json` via `ExtensionSettingsStore`. Firefox Sync installs the add-ons on a new machine, but leaves custom command bindings blank.
2. **`extension-preferences.json` stays local**: Private-window execution permissions (`internal:privateBrowsingAllowed`) and granted optional origins live here and must be re-granted on each profile unless transferred.
3. **Toolbar & Unified Extensions pop-out placement (`browser.uiCustomization.state`)**: Firefox saves pinned toolbar icons and the Unified Extensions pop-out list inside `prefs.js` as `user_pref("browser.uiCustomization.state", "...");`. Writing that preference into **`user.js`** forces Firefox to lock in your exact toolbar and pop-out placements on next launch without manually clobbering `prefs.js`.

## Requirements

- **Python 3.10+** (standard library only — no `pip install` or virtual environment required)

## Profile Auto-Discovery

`ff_keybind_sync.py` automatically locates your active profile via `profiles.ini` in priority order:

1. `[Install*]` `Default=...` (matches what Firefox actually launches on modern profiles)
2. `[Profile*]` entry matching `default-release`
3. `[Profile*]` entry with `Default=1`

Platform paths checked automatically:

| Platform | Default Firefox Base Path |
| :--- | :--- |
| **Windows 10/11** | `%APPDATA%\Mozilla\Firefox` |
| **Ubuntu WSL** | `~/.mozilla/firefox`, or automatically `/mnt/c/Users/<win_user>/AppData/Roaming/Mozilla/Firefox` when `~/.mozilla/firefox` does not exist |
| **Linux (Pop!_OS / Ubuntu)** | `~/.mozilla/firefox` (with Flatpak/Snap fallbacks) |
| **macOS** | `~/Library/Application Support/Firefox` |

Use `--profile /path/to/profile` on any subcommand to target a specific profile explicitly.

## Usage

### 1. Inspect (read-only)

Lists resolved profile path, lock state, detected `tridactylrc`, `storage-sync-v2.sqlite` size, `browser.uiCustomization.state` presence, and a clean table of extension shortcuts mapped to human-readable add-on names via `extensions.json`:

```bash
# Linux / macOS / WSL
python3 ff_keybind_sync.py inspect

# Windows (PowerShell / Command Prompt)
py -3 ff_keybind_sync.py inspect

# Target an explicit profile
python3 ff_keybind_sync.py inspect --profile /path/to/profile
```

### 2. Export

Close Firefox first for the cleanest snapshot of `prefs.js` and SQLite data, then export a portable bundle:

```bash
python3 ff_keybind_sync.py export --bundle ./firefox_keybind_bundle
```

Bundle contents:
- `firefox_shortcuts_manifest.json`: `"commands"` from `extension-settings.json`, full `extension-preferences.json`, and `user_pref("browser.uiCustomization.state", ...);`
- `xulstore.json` (window/sidebar/toolbar state)
- `storage-sync-v2.sqlite` (check-pointed from WAL into a self-contained SQLite file when possible)
- `tridactylrc` (if found at `~/.config/tridactyl/tridactylrc`, `~/.tridactylrc`, or `%USERPROFILE%\.config\tridactyl\tridactylrc`)

> **Note:** `firefox_keybind_bundle/`, `*.sqlite*`, and `*.bak` are excluded in `.gitignore` so personal browser databases and add-on storage never end up in Git.

### 3. Import

**Close Firefox completely** on the target machine before importing. `import` refuses to run if `parent.lock` / `.parentlock` / `lock` is actively held:

```bash
python3 ff_keybind_sync.py import --bundle ./firefox_keybind_bundle
```

What `import` does safely:
1. Creates timestamped `.bak` backups of `extension-settings.json`, `extension-preferences.json`, `user.js`, `xulstore.json`, `storage-sync-v2.sqlite`, and `tridactylrc` (if present).
2. **Surgically merges** `"commands"` into `extension-settings.json` (preserving unrelated settings and other extensions in shared `precedenceList` entries) and merges extension permissions into `extension-preferences.json`.
3. Injects or updates `user_pref("browser.uiCustomization.state", ...);` in `user.js` so toolbar and pop-out placements apply on the next Firefox launch.
4. Restores `xulstore.json` and deletes stale `storage-sync-v2.sqlite-wal` / `storage-sync-v2.sqlite-shm` files before copying `storage-sync-v2.sqlite`.
5. Restores `tridactylrc` to `~/.config/tridactyl/tridactylrc` (and on WSL, also restores to `/mnt/c/Users/<win_user>/.config/tridactyl/tridactylrc`).

## Tridactyl Portability Workflow

1. **Before exporting**, dump your live `:bind`, `:set`, and runtime configuration from Tridactyl's in-browser storage to disk by running inside Firefox/Tridactyl:
   ```text
   :mktridactylrc!
   ```
   *(Requires the Tridactyl native messenger; writes to `~/.config/tridactyl/tridactylrc` on Linux/macOS or `%USERPROFILE%\.config\tridactyl\tridactylrc` on Windows.)*
2. **Export** with `ff_keybind_sync.py export` so both `tridactylrc` and `storage-sync-v2.sqlite` are captured in the bundle.
3. **On machines without the Tridactyl native messenger** (where Firefox add-ons cannot read `tridactylrc` directly from disk), load your config into Tridactyl's browser storage directly with `:sourceurl`:
   ```text
   :sourceurl https://raw.githubusercontent.com/<user>/<dotfiles>/main/tridactylrc
   ```
   Or copy `tridactylrc` into place with `ff_keybind_sync.py import` alongside `storage-sync-v2.sqlite`, or serve it locally (`python3 -m http.server`) and run `:sourceurl http://127.0.0.1:8000/tridactylrc`.

## Testing

Run the offline standard-library test suite against synthetic profiles:

```bash
python3 -m unittest -v test_ff_keybind_sync.py
```
