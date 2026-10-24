# -*- coding: utf-8 -*-
"""
paths.py
Helper module for determining where the application's files live.

Where the files currently live (in short):
- icon.ico         -> get_base_dir(), i.e. next to the .exe / main.py;
- .env             -> first get_data_dir(), then get_base_dir()
                      (backward compatibility with the old location);
- bot.log          -> get_data_dir()/bot.log;
- cache.json       -> get_data_dir()/cache.json.

Why not everything next to the .exe:
The application folder may be protected (for example
C:\\Program Files\\AniListBot), and writing there fails with PermissionError.
So all files the application WRITES live in the user's data folder.

Why not relative to cwd:
When Windows launches the program from Startup, the current working
directory may be arbitrary (for example C:\\Windows\\System32), NOT the
folder containing the .exe. That's why get_base_dir() uses
sys.executable / sys.argv[0] rather than os.getcwd().
"""
import os
import sys
import tempfile


def get_base_dir() -> str:
    """
    Returns the absolute path to the folder where the application physically lives:
    - if this is a PyInstaller-built .exe (sys.frozen == True) -> the .exe folder;
    - if this is a script run via `python main.py` -> the script folder.

    Used for reading resources: icon.ico and legacy .env/cache.json.
    NOT used for writing.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(sys.argv[0]))


_DATA_DIR_CACHE = None


def _ensure_writable_dir(path: str) -> bool:
    """True if the folder can be created and a file can actually be written to it."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def get_data_dir() -> str:
    """
    Returns the folder for the application's WRITABLE data:
        primary:   %APPDATA%\\AniListMonitorBot
        fallback:  %TEMP%\\AniListMonitorBot

    bot.log and cache.json live here.
    .env is searched here first, then next to the .exe / main.py.

    The result is cached: the write check runs only once.
    If creating/writing is impossible even in the fallback, we don't crash
    and return the fallback path; later errors are handled by the caller
    (in particular, logging has its own try/except).
    """
    global _DATA_DIR_CACHE

    if _DATA_DIR_CACHE is not None:
        return _DATA_DIR_CACHE

    candidates = []

    appdata = os.getenv("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "AniListMonitorBot"))

    candidates.append(os.path.join(tempfile.gettempdir(), "AniListMonitorBot"))

    for path in candidates:
        if _ensure_writable_dir(path):
            _DATA_DIR_CACHE = path
            return path

    # Last resort: even if the write check failed, return a valid path
    # instead of raising an exception during import.
    fallback = candidates[-1]
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass

    _DATA_DIR_CACHE = fallback
    return fallback