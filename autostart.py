# -*- coding: utf-8 -*-
"""
autostart.py
------------
Manages the app's autostart with Windows via the registry:
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run

A REG_SZ value named APP_NAME. This is Unicode-safe (Cyrillic in paths is
not a problem), needs no external processes (cscript/VBS) and no third-party
dependencies.

We also honor the toggle in Task Manager (the "Startup" tab), which is stored in
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run

Note: the Run key cannot specify a working directory (cwd), so the app
must not rely on cwd - paths.get_base_dir() exists for that.

The old .lnk shortcut in the Startup folder (previous implementation) is removed as a migration.
"""
import logging
import os
import sys

try:  # winreg exists only on Windows; on other OSes the module must still import
    import winreg  # type: ignore
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore

logger = logging.getLogger(__name__)

APP_NAME = "AniListMonitorBot"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_startup_folder() -> str:
    """Startup folder (needed only to migrate the old .lnk)."""
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def get_shortcut_path() -> str:
    """Path to the legacy shortcut from the previous version."""
    return os.path.join(get_startup_folder(), f"{APP_NAME}.lnk")


def get_autostart_command() -> str:
    """
    Command to write into Run:
      - built .exe:      "<exe>"
      - development mode: "<pythonw.exe>" "<absolute path to main.py>"
    """
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'

    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    interpreter = pythonw if os.path.isfile(pythonw) else sys.executable
    script = os.path.abspath(sys.argv[0])
    return f'"{interpreter}" "{script}"'


def _remove_legacy_shortcut() -> None:
    """Migration: removes the old .lnk if it was left over from a previous version."""
    shortcut = get_shortcut_path()
    try:
        if os.path.exists(shortcut):
            os.remove(shortcut)
            logger.info("Removed legacy autostart shortcut: %s", shortcut)
    except OSError as e:
        logger.warning("Failed to remove legacy shortcut %s: %s", shortcut, e)


def _is_disabled_in_task_manager() -> bool:
    """
    True if the user disabled the entry in Task Manager.
    StartupApproved format: REG_BINARY; first byte 0x03 (or the even/odd
    variants 0x03/0x07...) = disabled; if there is no entry - enabled.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_READ) as key:
            data, _ = winreg.QueryValueEx(key, APP_NAME)
        if isinstance(data, (bytes, bytearray)) and len(data) > 0:
            return data[0] == 0x03
    except FileNotFoundError:
        pass  # no entry -> not disabled
    except OSError as e:
        logger.warning("Failed to read StartupApproved: %s", e)
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_autostart_enabled() -> bool:
    """True if the value is present in Run and not disabled in Task Manager."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
    except FileNotFoundError:
        return False
    except Exception as e:  # noqa: BLE001 - registry errors must not crash the app
        logger.error("Failed to read the autostart state from the registry: %s", e)
        return False

    try:
        return not _is_disabled_in_task_manager()
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to check StartupApproved: %s", e)
        return True


def enable_autostart() -> bool:
    """Writes/overwrites the value in Run (fixes a stale path). True on success."""
    if winreg is None:
        logger.error("Registry-based autostart is available only on Windows.")
        return False

    command = get_autostart_command()
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        logger.info("Autostart enabled in the registry: %s", command)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to write autostart to the registry: %s", e)
        return False

    # If the entry was previously disabled in Task Manager, clear that flag,
    # otherwise Windows still won't launch it after enabling from the tray.
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Failed to reset StartupApproved: %s", e)

    _remove_legacy_shortcut()
    return True


def disable_autostart() -> bool:
    """Removes the value from Run (absence is not an error) and the old .lnk. True on success."""
    _remove_legacy_shortcut()
    if winreg is None:
        return True
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, APP_NAME)
        logger.info("Autostart disabled: value removed from the registry.")
    except FileNotFoundError:
        logger.info("Autostart already disabled (registry value is absent).")
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to remove autostart from the registry: %s", e)
        return False
    return True


def repair_autostart_path() -> bool:
    """
    If autostart is enabled but the command in the registry doesn't match the
    current one (exe was moved/updated), overwrites it. Returns True if it was fixed.
    """
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            current, _ = winreg.QueryValueEx(key, APP_NAME)
    except Exception:  # noqa: BLE001 - no entry or registry unavailable
        return False

    if current == get_autostart_command():
        return False
    logger.info("Autostart path is stale, fixing: %s", current)
    return enable_autostart()