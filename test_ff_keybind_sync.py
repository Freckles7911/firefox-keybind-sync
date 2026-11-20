#!/usr/bin/env python3
"""Unit and integration tests for ff_keybind_sync.py (standard library only)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ff_keybind_sync as fks


def _create_sample_profile(
    profile_dir: Path,
    *,
    include_tridactylrc_at: Path | None = None,
    with_wal: bool = False,
) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    extensions = {
        "schemaVersion": 36,
        "addons": [
            {
                "id": "{446900e4-71c2-419f-a6a7-df9c091e268b}",
                "defaultLocale": {"name": "Bitwarden Password Manager"},
            },
            {
                "id": "tridactyl.vim@cmcaine.co.uk",
                "defaultLocale": {"name": "Tridactyl"},
            },
            {
                "id": "@testpilot-containers",
                "defaultLocale": {"name": "Firefox Multi-Account Containers"},
            },
        ],
    }
    (profile_dir / "extensions.json").write_text(json.dumps(extensions), encoding="utf-8")

    ext_settings = {
        "version": 1,
        "url_overrides": {"newTabURL": {"precedenceList": []}},
        "commands": {
            "_execute_browser_action": {
                "precedenceList": [
                    {
                        "id": "{446900e4-71c2-419f-a6a7-df9c091e268b}",
                        "installDate": 1700000000000,
                        "value": {"shortcut": "Ctrl+Shift+F"},
                        "enabled": True,
                    }
                ]
            },
            "autofill_login": {
                "precedenceList": [
                    {
                        "id": "{446900e4-71c2-419f-a6a7-df9c091e268b}",
                        "installDate": 1700000000000,
                        "value": {"shortcut": "Ctrl+Shift+L"},
                        "enabled": True,
                    }
                ]
            },
            "open_container_0": {
                "precedenceList": [
                    {
                        "id": "@testpilot-containers",
                        "installDate": 1700000000000,
                        "value": {"shortcut": "Alt+1", "description": "Container shortcut 1"},
                        "enabled": True,
                    }
                ]
            },
        },
    }
    (profile_dir / "extension-settings.json").write_text(
        json.dumps(ext_settings, indent=2), encoding="utf-8"
    )

    ext_prefs = {
        "{446900e4-71c2-419f-a6a7-df9c091e268b}": {
            "permissions": ["internal:privateBrowsingAllowed"],
            "origins": [],
        }
    }
    (profile_dir / "extension-preferences.json").write_text(
        json.dumps(ext_prefs, indent=2), encoding="utf-8"
    )

    ui_obj = {
        "placements": {
            "nav-bar": ["back-button", "_446900e4-71c2-419f-a6a7-df9c091e268b_-browser-action"],
            "unified-extensions-area": ["tridactyl_vim_cmcaine_co_uk-browser-action"],
        },
        "currentVersion": 28,
    }
    prefs_content = (
        '// Mozilla User Preferences\n'
        'user_pref("browser.startup.homepage", "about:home");\n'
        f'user_pref("browser.uiCustomization.state", {json.dumps(json.dumps(ui_obj))});\n'
    )
    (profile_dir / "prefs.js").write_text(prefs_content, encoding="utf-8")

    (profile_dir / "xulstore.json").write_text(
        json.dumps({"chrome://browser/content/browser.xhtml": {"main-window": {"sizemode": "maximized"}}}),
        encoding="utf-8",
    )

    db_path = profile_dir / "storage-sync-v2.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        if with_wal:
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("CREATE TABLE IF NOT EXISTS storage_sync_data (ext_id TEXT PRIMARY KEY, data TEXT);")
        conn.execute(
            "INSERT OR REPLACE INTO storage_sync_data VALUES (?, ?);",
            ("tridactyl.vim@cmcaine.co.uk", '{"nmaps":{"J":"tabprev"}}'),
        )
        conn.commit()
    finally:
        conn.close()

    if include_tridactylrc_at is not None:
        include_tridactylrc_at.parent.mkdir(parents=True, exist_ok=True)
        include_tridactylrc_at.write_text("bind J tabprev\nbind K tabnext\n", encoding="utf-8")


class ProfileDiscoveryTests(unittest.TestCase):
    def test_profiles_ini_priority_install_over_default_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ff_root = Path(tmp) / "Firefox"
            p_work = ff_root / "Profiles" / "abc.default"
            p_personal = ff_root / "Profiles" / "xyz.default-release"
            p_work.mkdir(parents=True)
            p_personal.mkdir(parents=True)
            (ff_root / "profiles.ini").write_text(
                "[Install308046B0AF4A39CB]\n"
                "Default=Profiles/xyz.default-release\n"
                "Locked=1\n\n"
                "[Profile1]\n"
                "Name=work\n"
                "IsRelative=1\n"
                "Path=Profiles/abc.default\n"
                "Default=1\n",
                encoding="utf-8",
            )
            found = fks.parse_profiles_ini(ff_root, wsl_mode=False)
            self.assertEqual(found, p_personal)

    def test_profiles_ini_fallbacks_release_then_default_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ff_root = Path(tmp) / "Firefox"
            p_legacy = ff_root / "Profiles" / "aaa.default"
            p_rel = ff_root / "Profiles" / "bbb.default-release"
            p_legacy.mkdir(parents=True)
            p_rel.mkdir(parents=True)
            (ff_root / "profiles.ini").write_text(
                "[Profile0]\nIsRelative=1\nPath=Profiles/aaa.default\nDefault=1\n\n"
                "[Profile1]\nName=default-release\nIsRelative=1\nPath=Profiles/bbb.default-release\n",
                encoding="utf-8",
            )
            self.assertEqual(fks.parse_profiles_ini(ff_root, wsl_mode=False), p_rel)

            p_rel.rmdir()
            self.assertEqual(fks.parse_profiles_ini(ff_root, wsl_mode=False), p_legacy)

    def test_wsl_auto_discovery_when_linux_mozilla_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            linux_home = root / "home" / "user"
            linux_home.mkdir(parents=True)
            win_users = root / "mnt" / "c" / "Users"
            (win_users / "Default").mkdir(parents=True)
            (win_users / "TEMP").mkdir(parents=True)
            win_ff = win_users / "kevin" / "AppData" / "Roaming" / "Mozilla" / "Firefox"
            win_prof = win_ff / "Profiles" / "v244.default-release"
            win_prof.mkdir(parents=True)
            (win_ff / "profiles.ini").write_text(
                "[Install1]\nDefault=Profiles/v244.default-release\nLocked=1\n",
                encoding="utf-8",
            )

            found = fks.find_default_profile(
                home_dir=linux_home,
                wsl_users_root=win_users,
                wsl_override=True,
                system_name="Linux",
            )
            self.assertEqual(found, win_prof)

    def test_windows_and_macos_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            win_home = root / "win_home"
            win_prof = win_home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles" / "p1.default-release"
            win_prof.mkdir(parents=True)
            (win_prof.parents[1] / "profiles.ini").write_text(
                "[Profile0]\nIsRelative=1\nPath=Profiles/p1.default-release\nDefault=1\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"APPDATA": str(win_home / "AppData" / "Roaming")}):
                self.assertEqual(
                    fks.find_default_profile(home_dir=win_home, system_name="Windows", wsl_override=False),
                    win_prof,
                )

            mac_home = root / "mac_home"
            mac_prof = mac_home / "Library" / "Application Support" / "Firefox" / "Profiles" / "m1.default-release"
            mac_prof.mkdir(parents=True)
            (mac_prof.parents[1] / "profiles.ini").write_text(
                "[InstallA]\nDefault=Profiles/m1.default-release\n",
                encoding="utf-8",
            )
            self.assertEqual(
                fks.find_default_profile(home_dir=mac_home, system_name="Darwin", wsl_override=False),
                mac_prof,
            )

    def test_profile_override_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            valid = Path(tmp) / "prof"
            valid.mkdir()
            self.assertEqual(fks.find_default_profile(valid), valid)
            with self.assertRaises(fks.KeybindSyncError):
                fks.find_default_profile(Path(tmp) / "missing")


class PrefAndLockTests(unittest.TestCase):
    def test_user_js_upsert_handles_semicolons_comments_and_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            user_js = Path(tmp) / "user.js"
            line1 = 'user_pref("browser.uiCustomization.state", "{\\"k\\":\\"a);b\\"}");'
            self.assertEqual(fks.upsert_user_pref(user_js, fks.UI_CUSTOMIZATION_PREF, line1), "created")
            self.assertIn(line1, user_js.read_text(encoding="utf-8"))

            # Append unrelated pref and comment, then update
            user_js.write_text(
                '// user_pref("browser.uiCustomization.state", "commented");\n'
                f"{line1}\n"
                'user_pref("other.pref", 42);\n',
                encoding="utf-8",
            )
            line2 = 'user_pref("browser.uiCustomization.state", "{\\"k\\":\\"updated\\"}");'
            self.assertEqual(fks.upsert_user_pref(user_js, fks.UI_CUSTOMIZATION_PREF, line2), "updated")
            content = user_js.read_text(encoding="utf-8")
            self.assertIn('// user_pref("browser.uiCustomization.state", "commented");', content)
            self.assertIn('user_pref("other.pref", 42);', content)
            self.assertIn(line2, content)
            self.assertNotIn("a);b", content)

    def test_lock_symlink_and_permission_error_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "prof"
            prof.mkdir()
            symlink_lock = prof / "lock"
            symlink_lock.symlink_to(f"127.0.1.1:+{os.getpid()}")
            locked, reason = fks.check_profile_lock(prof)
            self.assertTrue(locked)
            self.assertIn(str(os.getpid()), reason or "")
            symlink_lock.unlink()

            # Simulate WSL DrvFS exclusive Windows parent.lock raising PermissionError
            (prof / "parent.lock").write_bytes(b"")
            with mock.patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
                locked_w, reason_w = fks.check_profile_lock(prof)
            self.assertTrue(locked_w)
            self.assertIn("parent.lock", reason_w or "")


class InspectExportImportTests(unittest.TestCase):
    def test_inspect_output_and_tridactyl_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            prof = root / "profile"
            trid = home / ".config" / "tridactyl" / "tridactylrc"
            _create_sample_profile(prof, include_tridactylrc_at=trid)

            buf = io.StringIO()
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch("ff_keybind_sync.is_wsl", return_value=False),
                mock.patch("ff_keybind_sync.is_firefox_running", return_value=False),
                contextlib.redirect_stdout(buf),
            ):
                rc = fks.main(["--profile", str(prof), "inspect"])

            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Bitwarden Password Manager", out)
            self.assertIn("Ctrl+Shift+F", out)
            self.assertIn("Container shortcut 1", out)
            self.assertIn(str(trid), out)
            self.assertIn("storage-sync-v2.sqlite:  present", out)
            self.assertIn("uiCustomization.state:   present in prefs.js", out)

    def test_export_and_surgical_import_with_wsl_tridactyl_and_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_home = root / "src_home"
            src_trid = src_home / ".tridactylrc"
            src_prof = root / "src_prof"
            _create_sample_profile(src_prof, include_tridactylrc_at=src_trid, with_wal=True)

            bundle = root / "firefox_keybind_bundle"

            with (
                mock.patch.object(Path, "home", return_value=src_home),
                mock.patch("ff_keybind_sync.is_wsl", return_value=False),
                mock.patch("ff_keybind_sync.is_firefox_running", return_value=False),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rc_exp = fks.main(["export", "--profile", str(src_prof), "--bundle", str(bundle)])
            self.assertEqual(rc_exp, 0)

            manifest_path = bundle / fks.MANIFEST_FILENAME
            self.assertTrue(manifest_path.is_file())
            self.assertTrue((bundle / "xulstore.json").is_file())
            self.assertTrue((bundle / "storage-sync-v2.sqlite").is_file())
            self.assertTrue((bundle / "tridactylrc").is_file())

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIn("autofill_login", manifest["commands"])
            self.assertIn("{446900e4-71c2-419f-a6a7-df9c091e268b}", manifest["extension_preferences"])
            self.assertIn("nav-bar", manifest["ui_customization_state"]["value"])

            # Prepare target WSL-mounted Windows profile with pre-existing machine-specific state
            dst_home = root / "dst_home"
            win_users = root / "mnt" / "c" / "Users"
            win_home = win_users / "kevin"
            dst_prof = win_home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles" / "tgt.default-release"
            dst_prof.mkdir(parents=True)

            (dst_prof / "extension-settings.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "homepageNotification": {"keep": True},
                        "commands": {
                            "_execute_browser_action": {
                                "precedenceList": [
                                    {
                                        "id": "uBlock0@raymondhill.net",
                                        "installDate": 1600000000000,
                                        "value": {"shortcut": "Ctrl+Alt+U"},
                                        "enabled": True,
                                    }
                                ]
                            },
                            "custom-local-cmd": {
                                "precedenceList": [
                                    {
                                        "id": "local@example.com",
                                        "installDate": 1600000000000,
                                        "value": {"shortcut": "Alt+9"},
                                        "enabled": True,
                                    }
                                ]
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (dst_prof / "extension-preferences.json").write_text(
                json.dumps(
                    {
                        "{446900e4-71c2-419f-a6a7-df9c091e268b}": {
                            "permissions": ["clipboardWrite"],
                            "origins": ["https://vault.local/*"],
                        },
                        "other@addon": {"permissions": ["activeTab"]},
                    }
                ),
                encoding="utf-8",
            )
            (dst_prof / "user.js").write_text(
                '// user_pref("browser.uiCustomization.state", "commented-should-stay");\n'
                'user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);\n'
                'user_pref("browser.uiCustomization.state", "old-state");\n',
                encoding="utf-8",
            )
            (dst_prof / "storage-sync-v2.sqlite").write_bytes(b"stale-db")
            (dst_prof / "storage-sync-v2.sqlite-wal").write_bytes(b"stale-wal")
            (dst_prof / "storage-sync-v2.sqlite-shm").write_bytes(b"stale-shm")

            with (
                mock.patch.object(Path, "home", return_value=dst_home),
                mock.patch("ff_keybind_sync.is_wsl", return_value=True),
                mock.patch("ff_keybind_sync.is_firefox_running", return_value=False),
                mock.patch("ff_keybind_sync.WSL_USERS_ROOT", win_users),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                rc_imp = fks.main(["import", "--profile", str(dst_prof), "--bundle", str(bundle)])

            self.assertEqual(rc_imp, 0)

            # Verify backups exist
            baks = [p.name for p in dst_prof.glob("*.bak")]
            self.assertTrue(any(n.startswith("extension-settings.json.") for n in baks))
            self.assertTrue(any(n.startswith("extension-preferences.json.") for n in baks))

            # Verify surgical merge in extension-settings.json
            merged_es = json.loads((dst_prof / "extension-settings.json").read_text(encoding="utf-8"))
            self.assertEqual(merged_es["homepageNotification"], {"keep": True})
            self.assertIn("custom-local-cmd", merged_es["commands"])
            exec_ids = [x["id"] for x in merged_es["commands"]["_execute_browser_action"]["precedenceList"]]
            self.assertEqual(exec_ids, ["uBlock0@raymondhill.net", "{446900e4-71c2-419f-a6a7-df9c091e268b}"])

            # Verify surgical merge in extension-preferences.json
            merged_ep = json.loads((dst_prof / "extension-preferences.json").read_text(encoding="utf-8"))
            self.assertIn("other@addon", merged_ep)
            bw_perms = merged_ep["{446900e4-71c2-419f-a6a7-df9c091e268b}"]["permissions"]
            self.assertIn("clipboardWrite", bw_perms)
            self.assertIn("internal:privateBrowsingAllowed", bw_perms)

            # Verify user.js update preserved commented line & unrelated pref
            user_js = (dst_prof / "user.js").read_text(encoding="utf-8")
            self.assertIn('// user_pref("browser.uiCustomization.state", "commented-should-stay");', user_js)
            self.assertIn('user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);', user_js)
            self.assertNotIn('"old-state"', user_js)
            extracted = fks.extract_ui_customization_state(dst_prof)
            self.assertIsNotNone(extracted)
            assert extracted is not None
            self.assertIn("nav-bar", extracted["value"])

            # Verify stale WAL/SHM deleted and SQLite database valid
            self.assertFalse((dst_prof / "storage-sync-v2.sqlite-wal").exists())
            self.assertFalse((dst_prof / "storage-sync-v2.sqlite-shm").exists())
            conn = sqlite3.connect(str(dst_prof / "storage-sync-v2.sqlite"))
            try:
                row = conn.execute(
                    "SELECT data FROM storage_sync_data WHERE ext_id = ?",
                    ("tridactyl.vim@cmcaine.co.uk",),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertIn("tabprev", row[0])
            finally:
                conn.close()

            # Verify tridactylrc restored to Linux ~/.config AND WSL Windows %USERPROFILE%\.config
            linux_trid = dst_home / ".config" / "tridactyl" / "tridactylrc"
            win_trid = win_home / ".config" / "tridactyl" / "tridactylrc"
            self.assertTrue(linux_trid.is_file())
            self.assertTrue(win_trid.is_file())
            self.assertIn("bind J tabprev", linux_trid.read_text(encoding="utf-8"))
            self.assertIn("bind J tabprev", win_trid.read_text(encoding="utf-8"))

    def test_import_refuses_when_profile_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prof = root / "prof"
            prof.mkdir()
            lock_file = prof / ".parentlock"
            lock_file.write_bytes(b"")

            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / fks.MANIFEST_FILENAME).write_text(
                json.dumps({"commands": {}, "extension_preferences": {}}),
                encoding="utf-8",
            )

            with subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl, sys, time; "
                        "f = open(sys.argv[1], 'r+b'); "
                        "fcntl.lockf(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); "
                        "print('LOCKED', flush=True); "
                        "time.sleep(10)"
                    ),
                    str(lock_file),
                ],
                stdout=subprocess.PIPE,
                text=True,
            ) as holder:
                try:
                    assert holder.stdout is not None
                    ready = holder.stdout.readline().strip()
                    self.assertEqual(ready, "LOCKED")
                    with contextlib.redirect_stderr(io.StringIO()):
                        rc = fks.main(["import", "--profile", str(prof), "--bundle", str(bundle)])
                    self.assertEqual(rc, 1)
                finally:
                    holder.terminate()
                    holder.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
