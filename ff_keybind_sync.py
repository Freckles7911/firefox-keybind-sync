#!/usr/bin/env python3
"""Cross-platform CLI to inspect, export, and import Firefox add-on keybinds,
toolbar layouts, extension permissions, storage-sync data, and Tridactyl config.

Standard library only (Python 3.10+). Supports Windows, Ubuntu WSL accessing
Windows profiles, native Linux / Pop!_OS, and macOS.
"""

from __future__ import annotations

import argparse
import configparser
import copy
import datetime as dt
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if os.name == "posix":
    import fcntl
else:
    fcntl = None  # type: ignore[assignment]

if os.name == "nt":
    import msvcrt
else:
    msvcrt = None  # type: ignore[assignment]


MANIFEST_FILENAME = "firefox_shortcuts_manifest.json"
DEFAULT_BUNDLE_DIR = "./firefox_keybind_bundle"
UI_CUSTOMIZATION_PREF = "browser.uiCustomization.state"
WSL_USERS_ROOT = Path("/mnt/c/Users")

# Minimal fallback names for common extensions if omitted from extensions.json.
KNOWN_EXTENSION_NAMES: dict[str, str] = {
    "{446900e4-71c2-419f-a6a7-df9c091e268b}": "Bitwarden Password Manager",
    "tridactyl.vim@cmcaine.co.uk": "Tridactyl",
    "uBlock0@raymondhill.net": "uBlock Origin",
    "addon@darkreader.org": "Dark Reader",
    "@testpilot-containers": "Firefox Multi-Account Containers",
    "Tab-Session-Manager@sienori": "Tab Session Manager",
    "treestyletab@piro.sakura.ne.jp": "Tree Style Tab",
}

WSL_SKIP_USER_DIRS = {
    "all users",
    "default",
    "default user",
    "defaultuser100000",
    "public",
}


class KeybindSyncError(Exception):
    """Raised on user-facing configuration, discovery, or bundle errors."""


class ProfileLockedError(KeybindSyncError):
    """Raised when a target Firefox profile is actively locked."""


@dataclass(frozen=True)
class ShortcutEntry:
    extension_id: str
    extension_name: str
    command_name: str
    shortcut: str
    description: str
    enabled: bool


def is_wsl(system_name: str | None = None) -> bool:
    """Return True if running inside Windows Subsystem for Linux."""
    sys_name = system_name or platform.system()
    if sys_name != "Linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    if "microsoft" in platform.release().lower():
        return True
    try:
        proc_ver = Path("/proc/version").read_text(encoding="utf-8", errors="ignore")
        return "microsoft" in proc_ver.lower()
    except OSError:
        return False


def _translate_path_str(raw_path: str, *, wsl_mode: bool) -> Path:
    """Translate Windows drive paths to /mnt/<drive>/... when running in WSL."""
    cleaned = raw_path.strip()
    win_drive_match = re.match(r"^([A-Za-z]):[\\/](.*)$", cleaned)
    if win_drive_match and wsl_mode:
        drive = win_drive_match.group(1).lower()
        rest = win_drive_match.group(2).replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}")
    if os.name != "nt":
        cleaned = cleaned.replace("\\", "/")
    return Path(cleaned).expanduser()


def _parse_ini(ini_path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # preserve case
    content = ini_path.read_text(encoding="utf-8", errors="replace")
    parser.read_string(content)
    return parser


def _get_ci(section: configparser.SectionProxy, key: str, default: str | None = None) -> str | None:
    """Case-insensitive option lookup on a ConfigParser section."""
    if key in section:
        return section[key]
    key_lower = key.lower()
    for existing_key, value in section.items():
        if existing_key.lower() == key_lower:
            return value
    return default


def _resolve_ini_profile_path(
    firefox_dir: Path,
    raw_path: str,
    *,
    is_relative: bool,
    wsl_mode: bool,
) -> Path:
    if is_relative:
        rel_str = raw_path.strip().replace("\\", "/") if os.name != "nt" else raw_path.strip()
        return firefox_dir / rel_str
    return _translate_path_str(raw_path, wsl_mode=wsl_mode)


def _rank_profile_candidate(profile_dir: Path) -> tuple[int, float]:
    """Prefer directories with prefs.js, sorted by most recent modification time."""
    prefs = profile_dir / "prefs.js"
    try:
        if prefs.is_file():
            return (1, prefs.stat().st_mtime)
        return (0, profile_dir.stat().st_mtime)
    except OSError:
        return (0, 0.0)


def parse_profiles_ini(firefox_dir: Path, *, wsl_mode: bool | None = None) -> Path | None:
    """Resolve the active/default Firefox profile directory inside a Firefox base folder.

    Preference order:
    1. `[Install*]` `Default=...` in `profiles.ini` (or fallback `installs.ini`)
    2. `[Profile*]` entry matching `default-release` in `Path` or `Name`
    3. `[Profile*]` entry with `Default=1`
    4. Any existing `[Profile*]` entry or `*.default-release` / `*.default` directory
    """
    use_wsl = is_wsl() if wsl_mode is None else wsl_mode
    profiles_ini = firefox_dir / "profiles.ini"
    installs_ini = firefox_dir / "installs.ini"

    parser: configparser.ConfigParser | None = None
    if profiles_ini.is_file():
        try:
            parser = _parse_ini(profiles_ini)
        except (configparser.Error, OSError):
            parser = None

    # 1. Prefer [Install*] Default=... in profiles.ini, then installs.ini
    install_candidates: list[tuple[int, int, float, Path]] = []
    ini_sources: list[tuple[int, configparser.ConfigParser]] = []
    if parser is not None:
        ini_sources.append((1, parser))
    if installs_ini.is_file():
        try:
            ini_sources.append((0, _parse_ini(installs_ini)))
        except (configparser.Error, OSError):
            pass

    for source_prio, cfg in ini_sources:
        for sec_name in cfg.sections():
            if not sec_name.lower().startswith("install") and source_prio == 1:
                continue
            section = cfg[sec_name]
            default_val = _get_ci(section, "Default")
            if not default_val:
                continue
            cand = _resolve_ini_profile_path(
                firefox_dir,
                default_val,
                is_relative=not (
                    bool(re.match(r"^[A-Za-z]:[\\/]", default_val.strip()))
                    or default_val.strip().startswith("/")
                ),
                wsl_mode=use_wsl,
            )
            if cand.is_dir():
                locked_flag = 1 if _get_ci(section, "Locked") == "1" else 0
                has_prefs, mtime = _rank_profile_candidate(cand)
                install_candidates.append((source_prio, locked_flag, has_prefs * 1e12 + mtime, cand))

    if install_candidates:
        install_candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return install_candidates[0][3]

    # Collect [Profile*] entries for fallbacks 2, 3, 4
    profile_sections: list[tuple[str, str, bool, Path]] = []
    if parser is not None:
        for sec_name in parser.sections():
            if not sec_name.lower().startswith("profile"):
                continue
            section = parser[sec_name]
            raw_path = _get_ci(section, "Path")
            if not raw_path:
                continue
            is_rel_raw = _get_ci(section, "IsRelative", "1")
            is_rel = is_rel_raw != "0"
            resolved = _resolve_ini_profile_path(
                firefox_dir, raw_path, is_relative=is_rel, wsl_mode=use_wsl
            )
            if resolved.is_dir():
                prof_name = _get_ci(section, "Name", "") or ""
                is_def = _get_ci(section, "Default") == "1"
                profile_sections.append((prof_name, raw_path, is_def, resolved))

    # 2. Fallback to default-release in [Profile*]
    release_candidates = [
        resolved
        for prof_name, raw_path, _, resolved in profile_sections
        if "default-release" in raw_path.lower() or "default-release" in prof_name.lower()
    ]
    if release_candidates:
        release_candidates.sort(key=_rank_profile_candidate, reverse=True)
        return release_candidates[0]

    # Also scan Profiles/ or firefox_dir directly for *.default-release
    for search_dir in (firefox_dir / "Profiles", firefox_dir):
        if search_dir.is_dir():
            try:
                dir_releases = [
                    p
                    for p in search_dir.iterdir()
                    if p.is_dir() and "default-release" in p.name.lower()
                ]
                if dir_releases:
                    dir_releases.sort(key=_rank_profile_candidate, reverse=True)
                    return dir_releases[0]
            except OSError:
                pass

    # 3. Fallback to Default=1
    default_one = [resolved for _, _, is_def, resolved in profile_sections if is_def]
    if default_one:
        default_one.sort(key=_rank_profile_candidate, reverse=True)
        return default_one[0]

    # 4. Last-resort fallback: any existing profile dir
    if profile_sections:
        existing = [resolved for _, _, _, resolved in profile_sections]
        existing.sort(key=_rank_profile_candidate, reverse=True)
        return existing[0]

    for search_dir in (firefox_dir / "Profiles", firefox_dir):
        if search_dir.is_dir():
            try:
                defaults = [
                    p
                    for p in search_dir.iterdir()
                    if p.is_dir() and p.name.lower().endswith(".default")
                ]
                if defaults:
                    defaults.sort(key=_rank_profile_candidate, reverse=True)
                    return defaults[0]
            except OSError:
                pass

    return None


def _is_skippable_wsl_user(name: str) -> bool:
    lower = name.lower()
    if lower in WSL_SKIP_USER_DIRS:
        return True
    return lower.startswith("temp") or lower.startswith("umfd-")


def _iter_wsl_windows_users(wsl_users_root: Path | None = None) -> list[Path]:
    """Return accessible Windows user home directories under /mnt/c/Users."""
    users_root = wsl_users_root if wsl_users_root is not None else WSL_USERS_ROOT
    if not users_root.is_dir():
        return []
    users: list[Path] = []
    current_user = (os.environ.get("USER") or os.environ.get("LOGNAME") or "").lower()
    try:
        entries = list(users_root.iterdir())
    except OSError:
        return []

    for entry in entries:
        if _is_skippable_wsl_user(entry.name):
            continue
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
            # Probe readability safely
            _ = entry.stat()
        except OSError:
            continue
        users.append(entry)

    users.sort(
        key=lambda p: (
            1 if p.name.lower() == current_user else 0,
            1 if (p / "AppData" / "Roaming" / "Mozilla" / "Firefox").is_dir() else 0,
            p.name.lower(),
        ),
        reverse=True,
    )
    return users


def find_default_profile(
    profile_override: str | Path | None = None,
    *,
    home_dir: Path | None = None,
    wsl_users_root: Path | None = None,
    wsl_override: bool | None = None,
    system_name: str | None = None,
) -> Path:
    """Locate the active Firefox profile directory across Windows, WSL, Linux, and macOS."""
    if profile_override is not None:
        explicit = Path(profile_override).expanduser()
        if not explicit.is_dir():
            raise KeybindSyncError(f"Specified --profile directory does not exist: {explicit}")
        return explicit

    home = (home_dir or Path.home()).expanduser()
    sys_name = system_name or platform.system()
    use_wsl = is_wsl(sys_name) if wsl_override is None else wsl_override
    checked_roots: list[Path] = []

    candidate_roots: list[Path] = []
    if sys_name == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else (home / "AppData" / "Roaming")
        candidate_roots.append(base / "Mozilla" / "Firefox")
    elif sys_name == "Darwin":
        candidate_roots.append(home / "Library" / "Application Support" / "Firefox")
    else:
        native_root = home / ".mozilla" / "firefox"
        if native_root.is_dir():
            candidate_roots.append(native_root)
        else:
            checked_roots.append(native_root)

        if use_wsl and not native_root.is_dir():
            for win_user in _iter_wsl_windows_users(wsl_users_root):
                win_ff = win_user / "AppData" / "Roaming" / "Mozilla" / "Firefox"
                try:
                    if win_ff.is_dir():
                        candidate_roots.append(win_ff)
                except OSError:
                    continue

        # Native Linux packaged variants as lower-priority fallbacks
        for extra in (
            home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
            home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        ):
            if extra.is_dir():
                candidate_roots.append(extra)

        # If native ~/.mozilla/firefox existed but had no valid profiles on WSL, still check Windows
        if use_wsl and native_root.is_dir():
            for win_user in _iter_wsl_windows_users(wsl_users_root):
                win_ff = win_user / "AppData" / "Roaming" / "Mozilla" / "Firefox"
                try:
                    if win_ff.is_dir() and win_ff not in candidate_roots:
                        candidate_roots.append(win_ff)
                except OSError:
                    continue

    for root in candidate_roots:
        checked_roots.append(root)
        resolved = parse_profiles_ini(root, wsl_mode=use_wsl)
        if resolved is not None and resolved.is_dir():
            return resolved

    checked_str = ", ".join(str(p) for p in checked_roots) or "(no standard Firefox locations found)"
    raise KeybindSyncError(
        f"Unable to locate a Firefox profile automatically. Checked: {checked_str}. "
        "Pass --profile <path> to specify your profile folder explicitly."
    )


def _find_wsl_windows_home(
    profile_dir: Path | None = None,
    *,
    wsl_users_root: Path | None = None,
) -> Path | None:
    """Infer the active Windows user directory when running under WSL."""
    if profile_dir is not None:
        match = re.match(r"^(/mnt/[a-zA-Z]/Users/[^/]+)(?:/|$)", profile_dir.as_posix())
        if match:
            candidate = Path(match.group(1))
            if candidate.is_dir():
                return candidate

    for win_user in _iter_wsl_windows_users(wsl_users_root):
        ff_dir = win_user / "AppData" / "Roaming" / "Mozilla" / "Firefox"
        trid_dir = win_user / ".config" / "tridactyl"
        try:
            if ff_dir.is_dir() or trid_dir.is_dir():
                return win_user
        except OSError:
            continue
    return None


def find_tridactylrc_paths(
    profile_dir: Path | None = None,
    *,
    home_dir: Path | None = None,
    wsl_users_root: Path | None = None,
    wsl_override: bool | None = None,
    system_name: str | None = None,
) -> tuple[Path | None, list[Path]]:
    """Find an existing tridactylrc and return (detected_path, all_checked_paths)."""
    home = (home_dir or Path.home()).expanduser()
    sys_name = system_name or platform.system()
    use_wsl = is_wsl(sys_name) if wsl_override is None else wsl_override

    candidates: list[Path] = [
        home / ".config" / "tridactyl" / "tridactylrc",
        home / ".tridactylrc",
    ]
    if sys_name == "Windows":
        candidates.append(home / "_tridactylrc")
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "tridactyl" / "tridactylrc")
    elif use_wsl:
        win_home = _find_wsl_windows_home(profile_dir, wsl_users_root=wsl_users_root)
        if win_home is not None:
            candidates.extend(
                [
                    win_home / ".config" / "tridactyl" / "tridactylrc",
                    win_home / ".tridactylrc",
                    win_home / "_tridactylrc",
                ]
            )

    deduped: list[Path] = []
    for cand in candidates:
        if cand not in deduped:
            deduped.append(cand)

    for cand in deduped:
        try:
            if cand.is_file():
                return cand, deduped
        except OSError:
            continue
    return None, deduped


def tridactylrc_restore_targets(
    profile_dir: Path | None = None,
    *,
    home_dir: Path | None = None,
    wsl_users_root: Path | None = None,
    wsl_override: bool | None = None,
    system_name: str | None = None,
) -> list[Path]:
    """Return destination paths where tridactylrc should be restored on import."""
    home = (home_dir or Path.home()).expanduser()
    sys_name = system_name or platform.system()
    use_wsl = is_wsl(sys_name) if wsl_override is None else wsl_override

    targets: list[Path] = [home / ".config" / "tridactyl" / "tridactylrc"]
    if use_wsl:
        win_home = _find_wsl_windows_home(profile_dir, wsl_users_root=wsl_users_root)
        if win_home is not None:
            win_target = win_home / ".config" / "tridactyl" / "tridactylrc"
            if win_target not in targets:
                targets.append(win_target)
    return targets


def check_profile_lock(profile_dir: Path) -> tuple[bool, str | None]:
    """Return (is_locked, reason) by probing Firefox lock files without modifying them."""
    symlink_lock = profile_dir / "lock"
    if symlink_lock.is_symlink():
        try:
            target = os.readlink(symlink_lock)
        except OSError:
            target = ""
        pid_match = re.search(r":\+(\d+)$", target)
        if pid_match:
            pid = int(pid_match.group(1))
            try:
                os.kill(pid, 0)
                return True, f"lock symlink active (PID {pid})"
            except ProcessLookupError:
                pass
            except PermissionError:
                return True, f"lock symlink active (PID {pid})"
            except OSError:
                return True, f"lock symlink present ({target})"
        else:
            return True, f"lock symlink present ({target or 'active'})"

    for lock_name in ("parent.lock", ".parentlock"):
        lock_path = profile_dir / lock_name
        try:
            exists = lock_path.exists()
        except OSError:
            continue
        if not exists:
            continue

        if os.name == "nt":
            try:
                with open(lock_path, "r+b") as handle:
                    if msvcrt is not None:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except (PermissionError, OSError):
                return True, f"{lock_name} is actively locked"
        else:
            try:
                with open(lock_path, "r+b") as handle:
                    if fcntl is not None:
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except (BlockingIOError, OSError):
                            return True, f"{lock_name} is actively locked (flock)"
                        try:
                            fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except (BlockingIOError, OSError):
                            return True, f"{lock_name} is actively locked (fcntl)"
                        finally:
                            try:
                                fcntl.lockf(handle.fileno(), fcntl.LOCK_UN)
                            except OSError:
                                pass
                            try:
                                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                            except OSError:
                                pass
            except PermissionError:
                # On WSL DrvFS, Windows exclusive locks manifest as Errno 13 Permission denied.
                return True, f"{lock_name} is actively locked (Permission denied)"
            except OSError as exc:
                return True, f"{lock_name} could not be opened safely ({exc})"

    return False, None


def is_firefox_running(system_name: str | None = None) -> bool:
    """Best-effort check whether a Firefox process is currently running."""
    sys_name = system_name or platform.system()
    if sys_name == "Linux":
        proc = Path("/proc")
        if proc.is_dir():
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    comm = (entry / "comm").read_text(encoding="utf-8", errors="ignore").strip().lower()
                    if comm in {"firefox", "firefox-bin", "firefox-esr", "firefox.exe"}:
                        return True
                    cmdline = (entry / "cmdline").read_bytes().split(b"\x00", 1)[0]
                    exe_name = Path(cmdline.decode("utf-8", errors="ignore")).name.lower()
                    if exe_name in {"firefox", "firefox-bin", "firefox-esr", "firefox.exe"}:
                        return True
                except OSError:
                    continue
        return False

    if sys_name == "Darwin":
        try:
            res = subprocess.run(
                ["pgrep", "-xi", "firefox"],
                capture_output=True,
                timeout=2,
                check=False,
            )
            return res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    if sys_name == "Windows":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq firefox.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return "firefox.exe" in res.stdout.lower()
        except (OSError, subprocess.SubprocessError):
            return False

    return False


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.is_file():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise KeybindSyncError(f"Failed to parse JSON file {path}: {exc}") from exc


def _write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_extension_names(profile_dir: Path) -> dict[str, str]:
    """Read extensions.json and map add-on IDs to human-readable names."""
    mapping = dict(KNOWN_EXTENSION_NAMES)
    ext_path = profile_dir / "extensions.json"
    if not ext_path.is_file():
        return mapping

    data = _read_json_file(ext_path, {})
    for addon in data.get("addons", []):
        if not isinstance(addon, dict):
            continue
        ext_id = addon.get("id")
        if not ext_id:
            continue
        name = None
        default_locale = addon.get("defaultLocale")
        if isinstance(default_locale, dict):
            name = default_locale.get("name")
        if not name and isinstance(addon.get("locales"), list):
            for loc in addon["locales"]:
                if isinstance(loc, dict) and loc.get("name"):
                    name = loc["name"]
                    break
        if not name:
            name = addon.get("name")
        if name:
            mapping[str(ext_id)] = str(name)
    return mapping


def extract_command_entries(
    commands_obj: dict[str, Any],
    ext_names: dict[str, str],
) -> list[ShortcutEntry]:
    """Flatten extension-settings.json `commands` into ShortcutEntry records."""
    entries: list[ShortcutEntry] = []
    if not isinstance(commands_obj, dict):
        return entries

    for command_name, cmd_data in commands_obj.items():
        if not isinstance(cmd_data, dict):
            continue
        raw_list = cmd_data.get("precedenceList")
        if isinstance(raw_list, list):
            items = raw_list
        else:
            items = [cmd_data]

        for item in items:
            if not isinstance(item, dict):
                continue
            ext_id = str(item.get("id") or "<unknown>")
            val = item.get("value") if isinstance(item.get("value"), dict) else {}
            shortcut = str(val.get("shortcut") or "")
            description = str(val.get("description") or "")
            enabled = bool(item.get("enabled", True))
            entries.append(
                ShortcutEntry(
                    extension_id=ext_id,
                    extension_name=ext_names.get(ext_id, ext_id),
                    command_name=str(command_name),
                    shortcut=shortcut,
                    description=description,
                    enabled=enabled,
                )
            )

    # Active shortcuts first, then alphabetical by add-on name and command
    entries.sort(
        key=lambda e: (
            0 if e.shortcut else 1,
            e.extension_name.lower(),
            e.command_name.lower(),
        )
    )
    return entries


def _consume_js_string(text: str, start: int) -> tuple[str, int] | None:
    """Parse a single- or double-quoted JS string literal starting at `start`."""
    if start >= len(text) or text[start] not in ('"', "'"):
        return None
    quote = text[start]
    i = start + 1
    out: list[str] = []
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 1
            if i >= len(text):
                return None
            esc = text[i]
            simple = {
                '"': '"',
                "'": "'",
                "\\": "\\",
                "/": "/",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
            }
            if esc in simple:
                out.append(simple[esc])
                i += 1
            elif esc == "u" and i + 4 < len(text):
                hex_part = text[i + 1 : i + 5]
                try:
                    out.append(chr(int(hex_part, 16)))
                    i += 5
                except ValueError:
                    out.append(esc)
                    i += 1
            elif esc == "x" and i + 2 < len(text):
                hex_part = text[i + 1 : i + 3]
                try:
                    out.append(chr(int(hex_part, 16)))
                    i += 3
                except ValueError:
                    out.append(esc)
                    i += 1
            else:
                out.append(esc)
                i += 1
        elif ch == quote:
            return "".join(out), i + 1
        else:
            out.append(ch)
            i += 1
    return None


def _scan_user_pref_spans(text: str, target_pref: str) -> list[tuple[int, int, str]]:
    """Find active (non-commented) `user_pref(target_pref, <string>);` spans."""
    spans: list[tuple[int, int, str]] = []
    n = len(text)
    i = 0

    while i < n:
        # Skip line comments
        if text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        # Skip block comments
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        # Skip standalone string literals outside user_pref
        if text[i] in ('"', "'"):
            parsed = _consume_js_string(text, i)
            i = parsed[1] if parsed else i + 1
            continue

        if text.startswith("user_pref", i) and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            j = i + len("user_pref")
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "(":
                k = j + 1
                while k < n and text[k].isspace():
                    k += 1
                first_arg = _consume_js_string(text, k)
                if first_arg is not None and first_arg[0] == target_pref:
                    k = first_arg[1]
                    while k < n and text[k].isspace():
                        k += 1
                    if k < n and text[k] == ",":
                        k += 1
                        while k < n and text[k].isspace():
                            k += 1
                        second_arg = _consume_js_string(text, k)
                        if second_arg is not None:
                            k = second_arg[1]
                            while k < n and text[k].isspace():
                                k += 1
                            if k < n and text[k] == ")":
                                k += 1
                                while k < n and text[k].isspace() and text[k] != "\n":
                                    k += 1
                                if k < n and text[k] == ";":
                                    k += 1
                                spans.append((i, k, second_arg[0]))
                                i = k
                                continue
        i += 1

    return spans


def extract_ui_customization_state(profile_dir: Path) -> dict[str, str] | None:
    """Extract `browser.uiCustomization.state` from prefs.js (falling back to user.js)."""
    for filename in ("prefs.js", "user.js"):
        pref_file = profile_dir / filename
        if not pref_file.is_file():
            continue
        content = pref_file.read_text(encoding="utf-8", errors="replace")
        spans = _scan_user_pref_spans(content, UI_CUSTOMIZATION_PREF)
        if spans:
            _, _, value = spans[-1]
            canonical = f'user_pref("{UI_CUSTOMIZATION_PREF}", {json.dumps(value, ensure_ascii=False)});'
            return {
                "source_file": filename,
                "raw_pref_line": canonical,
                "value": value,
            }
    return None


def upsert_user_pref(user_js_path: Path, pref_name: str, new_line: str) -> str:
    """Insert or update `user_pref(pref_name, ...);` in user.js without clobbering other prefs."""
    if not user_js_path.exists():
        content = (
            "// Managed by firefox-keybind-sync (applies on next Firefox launch)\n"
            f"{new_line}\n"
        )
        user_js_path.write_text(content, encoding="utf-8")
        return "created"

    text = user_js_path.read_text(encoding="utf-8", errors="replace")
    spans = _scan_user_pref_spans(text, pref_name)
    if not spans:
        sep = "" if (not text or text.endswith("\n")) else "\n"
        updated = f"{text}{sep}{new_line}\n"
        user_js_path.write_text(updated, encoding="utf-8")
        return "appended"

    # Replace first span with new_line and drop any duplicate occurrences cleanly
    pieces: list[str] = []
    cursor = 0
    for idx, (start, end, _) in enumerate(spans):
        pieces.append(text[cursor:start])
        if idx == 0:
            pieces.append(new_line)
            cursor = end
        else:
            # Skip any trailing newline belonging strictly to the removed duplicate
            next_cursor = end
            if text[next_cursor:].startswith("\r\n"):
                next_cursor += 2
            elif next_cursor < len(text) and text[next_cursor] == "\n":
                next_cursor += 1
            cursor = next_cursor
    pieces.append(text[cursor:])
    updated = "".join(pieces)
    if not updated.endswith("\n"):
        updated += "\n"
    user_js_path.write_text(updated, encoding="utf-8")
    return "updated"


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def fmt_row(cols: list[str]) -> str:
        return " | ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def _create_backup(target_path: Path, timestamp: str) -> Path | None:
    """Create a timestamped `.bak` copy of `target_path` if it exists."""
    if not target_path.exists():
        return None
    backup_path = target_path.with_name(f"{target_path.name}.{timestamp}.bak")
    counter = 1
    while backup_path.exists():
        backup_path = target_path.with_name(f"{target_path.name}.{timestamp}.{counter}.bak")
        counter += 1
    shutil.copy2(target_path, backup_path)
    return backup_path


def _merge_commands(target_commands: dict[str, Any], imported_commands: dict[str, Any]) -> int:
    """Surgically merge imported `commands` into target `extension-settings.json` commands."""
    merged_count = 0
    for cmd_name, src_cmd in imported_commands.items():
        if not isinstance(src_cmd, dict):
            continue
        src_list = src_cmd.get("precedenceList")
        if not isinstance(src_list, list):
            target_commands[cmd_name] = copy.deepcopy(src_cmd)
            merged_count += 1
            continue

        dst_cmd = target_commands.get(cmd_name)
        if not isinstance(dst_cmd, dict) or not isinstance(dst_cmd.get("precedenceList"), list):
            target_commands[cmd_name] = copy.deepcopy(src_cmd)
            merged_count += len(src_list)
            continue

        dst_list: list[dict[str, Any]] = dst_cmd["precedenceList"]
        by_id: dict[str, int] = {
            str(entry["id"]): idx
            for idx, entry in enumerate(dst_list)
            if isinstance(entry, dict) and "id" in entry
        }
        for src_entry in src_list:
            if not isinstance(src_entry, dict):
                continue
            ext_id = src_entry.get("id")
            if ext_id is not None and str(ext_id) in by_id:
                dst_idx = by_id[str(ext_id)]
                existing = dst_list[dst_idx]
                updated = copy.deepcopy(src_entry)
                if "installDate" in existing:
                    updated["installDate"] = existing["installDate"]
                dst_list[dst_idx] = updated
            else:
                dst_list.append(copy.deepcopy(src_entry))
                if ext_id is not None:
                    by_id[str(ext_id)] = len(dst_list) - 1
            merged_count += 1
    return merged_count


def _merge_extension_preferences(
    target_prefs: dict[str, Any],
    imported_prefs: dict[str, Any],
) -> int:
    """Surgically merge extension permissions/preferences without dropping unrelated extensions."""
    merged_count = 0
    for ext_id, src_val in imported_prefs.items():
        if ext_id not in target_prefs or not isinstance(target_prefs[ext_id], dict) or not isinstance(src_val, dict):
            target_prefs[ext_id] = copy.deepcopy(src_val)
            merged_count += 1
            continue

        dst_val = target_prefs[ext_id]
        for key, item_val in src_val.items():
            if isinstance(item_val, list) and isinstance(dst_val.get(key), list):
                combined = list(dst_val[key])
                for elem in item_val:
                    if elem not in combined:
                        combined.append(copy.deepcopy(elem))
                dst_val[key] = combined
            else:
                dst_val[key] = copy.deepcopy(item_val)
        merged_count += 1
    return merged_count


def _copy_sqlite_cleanly(src_sqlite: Path, dst_sqlite: Path) -> str:
    """Copy storage-sync-v2.sqlite, folding any non-empty WAL via a temp copy."""
    wal_path = src_sqlite.with_name(f"{src_sqlite.name}-wal")
    shm_path = src_sqlite.with_name(f"{src_sqlite.name}-shm")
    wal_has_data = False
    try:
        wal_has_data = wal_path.is_file() and wal_path.stat().st_size > 0
    except OSError:
        wal_has_data = False

    if not wal_has_data:
        shutil.copy2(src_sqlite, dst_sqlite)
        return "copied"

    try:
        with tempfile.TemporaryDirectory(prefix="ff_keybind_sqlite_") as tmp_dir:
            tmp_dir_path = Path(tmp_dir)
            tmp_db = tmp_dir_path / src_sqlite.name
            shutil.copy2(src_sqlite, tmp_db)
            if wal_path.is_file():
                shutil.copy2(wal_path, tmp_dir_path / wal_path.name)
            if shm_path.is_file():
                shutil.copy2(shm_path, tmp_dir_path / shm_path.name)

            conn = sqlite3.connect(str(tmp_db))
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.commit()
            finally:
                conn.close()

            shutil.copy2(tmp_db, dst_sqlite)
            return "checkpointed-and-copied"
    except (sqlite3.Error, OSError):
        shutil.copy2(src_sqlite, dst_sqlite)
        return "copied-raw-wal-unmerged"


def cmd_inspect(args: argparse.Namespace) -> int:
    profile_dir = find_default_profile(args.profile)
    locked, lock_reason = check_profile_lock(profile_dir)
    ff_running = is_firefox_running()
    ext_names = load_extension_names(profile_dir)

    ext_settings_path = profile_dir / "extension-settings.json"
    ext_settings = _read_json_file(ext_settings_path, {}) if ext_settings_path.is_file() else {}
    commands_obj = ext_settings.get("commands", {}) if isinstance(ext_settings, dict) else {}
    entries = extract_command_entries(commands_obj, ext_names)

    tridactyl_found, tridactyl_checked = find_tridactylrc_paths(profile_dir)
    sqlite_path = profile_dir / "storage-sync-v2.sqlite"
    wal_path = profile_dir / "storage-sync-v2.sqlite-wal"
    ui_state = extract_ui_customization_state(profile_dir)

    print(f"Profile:                 {profile_dir}")
    lock_status = f"LOCKED ({lock_reason})" if locked else "unlocked"
    if ff_running and not locked:
        lock_status += " [Firefox process detected]"
    print(f"Lock status:             {lock_status}")

    if tridactyl_found is not None:
        print(f"tridactylrc:             {tridactyl_found}")
    else:
        checked_fmt = ", ".join(str(p) for p in tridactyl_checked)
        print(f"tridactylrc:             not found (checked: {checked_fmt})")

    if sqlite_path.is_file():
        size_bytes = sqlite_path.stat().st_size
        wal_note = ""
        if wal_path.is_file() and wal_path.stat().st_size > 0:
            wal_note = f" (+WAL {_format_size(wal_path.stat().st_size)})"
        print(f"storage-sync-v2.sqlite:  present ({_format_size(size_bytes)}, {size_bytes} bytes){wal_note}")
    else:
        print("storage-sync-v2.sqlite:  missing")

    prefs_has_ui = False
    prefs_path = profile_dir / "prefs.js"
    if prefs_path.is_file():
        prefs_text = prefs_path.read_text(encoding="utf-8", errors="replace")
        prefs_has_ui = bool(_scan_user_pref_spans(prefs_text, UI_CUSTOMIZATION_PREF))

    if prefs_has_ui and ui_state is not None:
        detail = ""
        try:
            parsed_ui = json.loads(ui_state["value"])
            placements = parsed_ui.get("placements", {})
            nav_count = len(placements.get("nav-bar", []))
            unified_count = len(placements.get("unified-extensions-area", []))
            detail = f" (nav-bar: {nav_count} widgets, unified-extensions: {unified_count} widgets)"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        print(f"uiCustomization.state:   present in prefs.js{detail}")
    elif ui_state is not None:
        print(f"uiCustomization.state:   not in prefs.js (found in {ui_state['source_file']})")
    else:
        print("uiCustomization.state:   missing from prefs.js")

    active_count = sum(1 for e in entries if e.shortcut)
    print(f"\nExtension Shortcuts ({active_count} assigned / {len(entries)} command entries):")
    if entries:
        rows = [
            [
                e.extension_name,
                e.command_name,
                e.shortcut if e.shortcut else "(unset)",
                e.description if e.description else "-",
                "yes" if e.enabled else "no",
                e.extension_id,
            ]
            for e in entries
        ]
        print(
            _render_table(
                ["Add-on", "Command", "Shortcut", "Description", "Enabled", "Extension ID"],
                rows,
            )
        )
    else:
        print("  No command entries found in extension-settings.json.")

    return 0


def cmd_export(args: argparse.Namespace) -> int:
    profile_dir = find_default_profile(args.profile)
    bundle_dir = Path(args.bundle).expanduser()
    bundle_dir.mkdir(parents=True, exist_ok=True)

    locked, lock_reason = check_profile_lock(profile_dir)
    if locked or is_firefox_running():
        reason = lock_reason or "Firefox process appears to be running"
        print(
            f"Warning: {reason}. Close Firefox before exporting for the cleanest snapshot "
            "of prefs.js and SQLite databases.",
            file=sys.stderr,
        )

    ext_names = load_extension_names(profile_dir)
    ext_settings_path = profile_dir / "extension-settings.json"
    ext_prefs_path = profile_dir / "extension-preferences.json"

    ext_settings = _read_json_file(ext_settings_path, {}) if ext_settings_path.is_file() else {}
    commands = ext_settings.get("commands", {}) if isinstance(ext_settings, dict) else {}
    ext_prefs = _read_json_file(ext_prefs_path, {}) if ext_prefs_path.is_file() else {}
    ui_state = extract_ui_customization_state(profile_dir)

    copied_files: dict[str, bool] = {
        "xulstore.json": False,
        "storage-sync-v2.sqlite": False,
        "tridactylrc": False,
    }

    xulstore_src = profile_dir / "xulstore.json"
    if xulstore_src.is_file():
        shutil.copy2(xulstore_src, bundle_dir / "xulstore.json")
        copied_files["xulstore.json"] = True

    sqlite_src = profile_dir / "storage-sync-v2.sqlite"
    sqlite_status = "missing"
    if sqlite_src.is_file():
        sqlite_status = _copy_sqlite_cleanly(sqlite_src, bundle_dir / "storage-sync-v2.sqlite")
        copied_files["storage-sync-v2.sqlite"] = True
        if sqlite_status == "copied-raw-wal-unmerged":
            print(
                "Warning: storage-sync-v2.sqlite-wal had uncommitted data that could not be "
                "checkpointed cleanly; close Firefox and re-export if add-on data is missing.",
                file=sys.stderr,
            )

    tridactyl_src, _ = find_tridactylrc_paths(profile_dir)
    if tridactyl_src is not None:
        shutil.copy2(tridactyl_src, bundle_dir / "tridactylrc")
        copied_files["tridactylrc"] = True

    manifest = {
        "schema_version": 1,
        "exported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_platform": "WSL" if is_wsl() else platform.system(),
        "source_profile": str(profile_dir),
        "extensions": ext_names,
        "commands": commands,
        "extension_preferences": ext_prefs,
        "ui_customization_state": ui_state,
        "tridactylrc_source": str(tridactyl_src) if tridactyl_src else None,
        "copied_files": copied_files,
    }
    manifest_path = bundle_dir / MANIFEST_FILENAME
    _write_json_file(manifest_path, manifest)

    entries = extract_command_entries(commands, ext_names)
    active_count = sum(1 for e in entries if e.shortcut)
    print(f"Exported bundle to:      {bundle_dir.resolve()}")
    print(f"Source profile:          {profile_dir}")
    print(f"Manifest:                {manifest_path}")
    print(f"Commands exported:       {len(commands)} commands ({active_count} active shortcuts)")
    print(f"Extension permissions:   {len(ext_prefs)} add-ons")
    print(f"uiCustomization.state:   {'included' if ui_state else 'not found'}")
    print(f"xulstore.json:           {'copied' if copied_files['xulstore.json'] else 'not found'}")
    print(f"storage-sync-v2.sqlite:  {sqlite_status}")
    print(
        f"tridactylrc:             {'copied from ' + str(tridactyl_src) if tridactyl_src else 'not found'}"
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    bundle_dir = Path(args.bundle).expanduser()
    manifest_path = bundle_dir / MANIFEST_FILENAME
    if not bundle_dir.is_dir():
        raise KeybindSyncError(f"Bundle directory does not exist: {bundle_dir}")
    if not manifest_path.is_file():
        raise KeybindSyncError(f"Missing bundle manifest: {manifest_path}")

    manifest = _read_json_file(manifest_path, {})
    if not isinstance(manifest, dict):
        raise KeybindSyncError(f"Invalid manifest structure in {manifest_path}")

    profile_dir = find_default_profile(args.profile)

    locked, lock_reason = check_profile_lock(profile_dir)
    if locked:
        raise ProfileLockedError(
            f"Refusing to import because target profile is actively locked ({lock_reason}): "
            f"{profile_dir}. Close Firefox completely and try again."
        )

    if is_firefox_running():
        print(
            "Warning: A Firefox process appears to be running on this system. "
            "Ensure all Firefox windows using this profile are closed before launching.",
            file=sys.stderr,
        )

    timestamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backups: list[Path] = []

    def track_backup(path: Path) -> None:
        bak = _create_backup(path, timestamp)
        if bak is not None:
            backups.append(bak)

    # 1. Surgically merge "commands" into extension-settings.json
    ext_settings_path = profile_dir / "extension-settings.json"
    track_backup(ext_settings_path)
    target_ext_settings = _read_json_file(ext_settings_path, {"version": 1, "commands": {}})
    if not isinstance(target_ext_settings, dict):
        target_ext_settings = {"version": 1, "commands": {}}
    if not isinstance(target_ext_settings.get("commands"), dict):
        target_ext_settings["commands"] = {}

    imported_commands = manifest.get("commands") or {}
    merged_cmd_entries = 0
    if isinstance(imported_commands, dict):
        merged_cmd_entries = _merge_commands(target_ext_settings["commands"], imported_commands)
    _write_json_file(ext_settings_path, target_ext_settings)

    # 2. Surgically merge keys into extension-preferences.json
    ext_prefs_path = profile_dir / "extension-preferences.json"
    track_backup(ext_prefs_path)
    target_ext_prefs = _read_json_file(ext_prefs_path, {})
    if not isinstance(target_ext_prefs, dict):
        target_ext_prefs = {}

    imported_prefs = manifest.get("extension_preferences") or {}
    merged_pref_addons = 0
    if isinstance(imported_prefs, dict):
        merged_pref_addons = _merge_extension_preferences(target_ext_prefs, imported_prefs)
    _write_json_file(ext_prefs_path, target_ext_prefs)

    # 3. Inject/update browser.uiCustomization.state in target user.js
    ui_action = "skipped (not in manifest)"
    ui_state = manifest.get("ui_customization_state")
    if isinstance(ui_state, dict) and "value" in ui_state:
        user_js_path = profile_dir / "user.js"
        track_backup(user_js_path)
        pref_line = f'user_pref("{UI_CUSTOMIZATION_PREF}", {json.dumps(ui_state["value"], ensure_ascii=False)});'
        ui_action = upsert_user_pref(user_js_path, UI_CUSTOMIZATION_PREF, pref_line)

    # 4. Restore xulstore.json
    xul_action = "skipped (not in bundle)"
    xul_bundle = bundle_dir / "xulstore.json"
    if xul_bundle.is_file():
        xul_target = profile_dir / "xulstore.json"
        track_backup(xul_target)
        shutil.copy2(xul_bundle, xul_target)
        xul_action = "restored"

    # 5. Remove stale WAL/SHM and restore storage-sync-v2.sqlite
    sqlite_action = "skipped (not in bundle)"
    sqlite_bundle = bundle_dir / "storage-sync-v2.sqlite"
    if sqlite_bundle.is_file():
        sqlite_target = profile_dir / "storage-sync-v2.sqlite"
        track_backup(sqlite_target)
        removed_companions: list[str] = []
        for suffix in ("-wal", "-shm"):
            companion = profile_dir / f"storage-sync-v2.sqlite{suffix}"
            if companion.exists():
                companion.unlink()
                removed_companions.append(companion.name)
        shutil.copy2(sqlite_bundle, sqlite_target)
        if removed_companions:
            sqlite_action = f"restored (removed stale {', '.join(removed_companions)})"
        else:
            sqlite_action = "restored"

    # 6. Restore tridactylrc to ~/.config/tridactyl/tridactylrc (and Windows %USERPROFILE% on WSL)
    trid_targets_restored: list[str] = []
    trid_bundle = bundle_dir / "tridactylrc"
    if trid_bundle.is_file():
        for dst in tridactylrc_restore_targets(profile_dir):
            dst.parent.mkdir(parents=True, exist_ok=True)
            track_backup(dst)
            shutil.copy2(trid_bundle, dst)
            trid_targets_restored.append(str(dst))

    print(f"Imported bundle from:    {bundle_dir.resolve()}")
    print(f"Target profile:          {profile_dir}")
    print(f"extension-settings.json: merged {merged_cmd_entries} command entry/entries")
    print(f"extension-preferences:   merged {merged_pref_addons} add-on permission record(s)")
    print(f"user.js uiCustomization: {ui_action}")
    print(f"xulstore.json:           {xul_action}")
    print(f"storage-sync-v2.sqlite:  {sqlite_action}")
    if trid_targets_restored:
        print(f"tridactylrc restored:    {', '.join(trid_targets_restored)}")
    else:
        print("tridactylrc restored:    skipped (not in bundle)")
    if backups:
        print(f"Backups created ({len(backups)}):")
        for bak in backups:
            print(f"  - {bak}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ff_keybind_sync.py",
        description=(
            "Inspect, export, and import Firefox add-on shortcuts, private-browsing "
            "permissions, toolbar layouts, storage-sync data, and Tridactyl config."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Explicit path to a Firefox profile directory (overrides profiles.ini auto-discovery).",
    )

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_inspect = subparsers.add_parser(
        "inspect",
        help="Read-only inspection of profile add-on shortcuts, toolbar state, and Tridactyl config.",
    )
    p_inspect.add_argument(
        "--profile",
        default=argparse.SUPPRESS,
        help="Explicit path to a Firefox profile directory.",
    )
    p_inspect.set_defaults(func=cmd_inspect)

    p_export = subparsers.add_parser(
        "export",
        help="Export keybinds, permissions, toolbar layout, storage-sync, and tridactylrc into a bundle.",
    )
    p_export.add_argument(
        "--profile",
        default=argparse.SUPPRESS,
        help="Explicit path to a Firefox profile directory.",
    )
    p_export.add_argument(
        "--bundle",
        default=DEFAULT_BUNDLE_DIR,
        help=f"Output bundle directory (default: {DEFAULT_BUNDLE_DIR}).",
    )
    p_export.set_defaults(func=cmd_export)

    p_import = subparsers.add_parser(
        "import",
        help="Safely import a bundle into the target Firefox profile (requires Firefox to be closed).",
    )
    p_import.add_argument(
        "--profile",
        default=argparse.SUPPRESS,
        help="Explicit path to a Firefox profile directory.",
    )
    p_import.add_argument(
        "--bundle",
        default=DEFAULT_BUNDLE_DIR,
        help=f"Input bundle directory (default: {DEFAULT_BUNDLE_DIR}).",
    )
    p_import.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeybindSyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
