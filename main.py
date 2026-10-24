# -*- coding: utf-8 -*-
"""
main.py
Application entry point. Responsible for:
- configuring logging (to a file when running as a --windowed .exe with no console);
- creating the aiogram bot/dispatcher, the AniList client and the cache;
- starting the system tray in a separate thread (pystray);
- running the bot's asyncio loop (aiogram polling + the background AniList
  monitor) in the main thread;
- clean, safe shutdown on a command from the system tray.

Thread architecture:
    Main thread   -> asyncio.run(async_main()) -> aiogram polling + monitor_loop
    Second thread -> pystray Icon.run()         -> system tray and its menu

Both sides are connected through the AppState object (state.py) and
thread-safe primitives (run_coroutine_threadsafe / call_soon_threadsafe)
and threading.Event.

Data files (bot.log, cache.json) live in get_data_dir(), i.e.
%APPDATA%\\AniListMonitorBot, not next to the .exe, because of possible
access-rights restrictions.
"""
import asyncio
import logging
import os
import sys
import threading

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from anilist_client import AniListClient
from bot_app import WhitelistMiddleware, check_new_anime, register_handlers
from cache_manager import CacheManager
from config import load_config
from paths import get_base_dir, get_data_dir
from state import AppState
from tray_icon import build_tray_icon
import autostart


def _configure_logging() -> None:
    """
    In a PyInstaller build with --windowed/--noconsole the process has
    NO standard output (sys.stdout/sys.stderr are None).
    Writing logs to a regular StreamHandler in that case would crash
    the application.

    Therefore:
    - in frozen mode, or when there is no stderr, we write to bot.log;
    - bot.log lives in get_data_dir(), not next to the .exe;
    - we use RotatingFileHandler: 1 MB x 3 backups, utf-8;
    - if the log file is unavailable, we don't crash but fall back to
      StreamHandler (if stderr exists) or NullHandler.
    """
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = []
    is_frozen_windowed = getattr(sys, "frozen", False) or sys.stderr is None

    if is_frozen_windowed:
        try:
            log_path = os.path.join(get_data_dir(), "bot.log")
            handlers.append(
                RotatingFileHandler(
                    log_path,
                    maxBytes=1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
            )
        except Exception:
            # The application must start even if the log file can't be created.
            if sys.stderr is not None:
                handlers.append(logging.StreamHandler())
            else:
                handlers.append(logging.NullHandler())
    else:
        # Regular `python main.py` run with a console.
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


_configure_logging()
logger = logging.getLogger("main")

# Polling restart and network wait parameters (seconds).
_BACKOFF_MIN = 5
_BACKOFF_MAX = 60
_STABLE_RUN_SECONDS = 60  # how long polling must run for the start to count as successful
_NETWORK_PROBE_URLS = ("https://api.telegram.org", "https://graphql.anilist.co")


async def _sleep_or_stop(state: AppState, seconds: float) -> bool:
    """Waits `seconds` s. Returns True if a stop command arrived in the meantime."""
    try:
        await asyncio.wait_for(state.stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback: no background task should die silently."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.critical(
            "Background task '%s' terminated unexpectedly with an error.",
            task.get_name(),
            exc_info=exc,
        )


async def _probe_host(client: httpx.AsyncClient, url: str) -> bool:
    """A host is reachable if any HTTP response came back (even 404/405)."""
    try:
        await client.get(url)
        return True
    except httpx.HTTPError:
        return False


async def wait_for_network(state: AppState) -> bool:
    """
    Waits until Telegram and AniList become reachable (5->60 s backoff).
    True - the network is up; False - a stop command arrived.
    """
    delay = _BACKOFF_MIN
    attempt = 0

    async with httpx.AsyncClient(timeout=5.0) as client:
        while not state.stop_event.is_set():
            attempt += 1
            results = await asyncio.gather(
                *(_probe_host(client, u) for u in _NETWORK_PROBE_URLS)
            )
            if all(results):
                if attempt > 1:
                    logger.info("Network is available (attempt %d).", attempt)
                return True

            logger.warning(
                "Network is still unavailable (attempt %d), retrying in %d s.",
                attempt,
                delay,
            )
            if await _sleep_or_stop(state, delay):
                return False
            delay = min(delay * 2, _BACKOFF_MAX)

    return False


async def polling_supervisor(state: AppState) -> None:
    """Keeps aiogram polling alive: on failure, restarts it with exponential backoff."""
    loop = asyncio.get_running_loop()
    delay = _BACKOFF_MIN

    while not state.stop_event.is_set():
        started_at = loop.time()
        try:
            await state.dispatcher.start_polling(
                state.bot, handle_signals=False, close_bot_session=False
            )
            if state.stop_event.is_set():
                break
            logger.warning(
                "Polling finished without an error, but no stop command was given."
            )
        except TelegramUnauthorizedError:
            logger.critical(
                "Telegram rejected the bot token (Unauthorized). Check the token in the configuration. "
                "Polling restarts stopped; the application and system tray remain active."
            )
            return
        except Exception:
            logger.exception("Polling crashed.")

        # If polling ran stably for long enough, reset the backoff delay.
        if loop.time() - started_at >= _STABLE_RUN_SECONDS:
            delay = _BACKOFF_MIN

        logger.warning("Restarting polling in %d s.", delay)
        if await _sleep_or_stop(state, delay):
            return
        delay = min(delay * 2, _BACKOFF_MAX)


async def monitor_loop(state: AppState) -> None:
    """Background loop: polls AniList for new titles once every CHECK_INTERVAL seconds."""
    # Windows autostart: the network may not be up yet - wait for it before the first check.
    # This only happens in this task, so the system tray and polling are not blocked.
    if not await wait_for_network(state):
        return

    while not state.stop_event.is_set():
        try:
            sent = await check_new_anime(state)
            if sent:
                logger.info(
                    "Scheduled check: sent notifications for %d new titles.",
                    sent,
                )
        except Exception:
            logger.exception("Error during the scheduled AniList check.")

        # Wait for the interval, but exit immediately if a shutdown command arrives -
        # this lets the app close instantly instead of waiting up to CHECK_INTERVAL.
        try:
            await asyncio.wait_for(
                state.stop_event.wait(), timeout=state.config.check_interval
            )
        except asyncio.TimeoutError:
            pass  # normal case: the wait interval simply elapsed


async def async_main(state: AppState) -> None:
    state.loop = asyncio.get_running_loop()
    state.stop_event = asyncio.Event()

    register_handlers(state.dispatcher, state)

    # outer_middleware runs before routing to handlers, so users
    # outside the whitelist cannot interact with the bot at all.
    state.dispatcher.message.outer_middleware(
        WhitelistMiddleware(state.config.whitelist_ids)
    )

    polling_task = asyncio.create_task(
        polling_supervisor(state), name="polling_supervisor"
    )
    monitor_task = asyncio.create_task(monitor_loop(state), name="monitor_loop")
    polling_task.add_done_callback(_log_task_exception)
    monitor_task.add_done_callback(_log_task_exception)

    logger.info(
        "Bot started. Watching AniList for new announcements/airing titles every %d s.",
        state.config.check_interval,
    )

    await state.stop_event.wait()
    logger.info("Shutdown signal received - stopping background tasks...")

    # Graceful polling stop first. A RuntimeError ("Polling is not started") is fine
    # if the supervisor is in a pause between restarts; the timeout prevents hanging.
    try:
        await asyncio.wait_for(state.dispatcher.stop_polling(), timeout=5)
    except Exception as e:
        logger.info("Graceful polling stop skipped: %r", e)

    polling_task.cancel()
    monitor_task.cancel()
    await asyncio.gather(polling_task, monitor_task, return_exceptions=True)

    # Cleanly close all network sessions and save the cache.
    await state.anilist.close()
    await state.bot.session.close()
    state.cache.save()

    logger.info("All resources released, cache saved. Shutdown complete.")
    state.shutdown_complete.set()


def run_tray(state: AppState) -> None:
    """Runs in a separate thread: builds the tray icon and runs it (blocking)."""
    icon = build_tray_icon(state)
    state.tray_icon = icon
    icon.run()  # blocks this thread until icon.stop() is called


# We keep the named mutex handle in a module-level variable so it isn't released
# until the process exits.
_instance_mutex_handle = None


def _acquire_single_instance() -> bool:
    """True - we are the first instance (or the check isn't possible); False - another one is already running."""
    global _instance_mutex_handle

    if sys.platform != "win32":
        return True

    import ctypes
    from ctypes import wintypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateMutexW(None, False, "Local\\AniListMonitorBot")
    last_error = ctypes.get_last_error()

    if not handle:
        logger.warning(
            "Failed to create mutex (code %d) - starting without duplicate-instance protection.",
            last_error,
        )
        return True

    if last_error == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    _instance_mutex_handle = handle
    return True


def _show_fatal_message(text: str) -> None:
    """
    Shows an error window if the application runs without a console (Windows).

    The call is deliberately wrapped in try/except: even if MessageBox
    fails, the application must not crash because of that.
    On non-Windows platforms nothing is shown.
    """
    if sys.platform != "win32":
        return

    try:
        import ctypes

        MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(
            0,
            text,
            "AniList Monitor Bot",
            MB_ICONERROR,
        )
    except Exception:
        pass


def main() -> None:
    if not _acquire_single_instance():
        logger.info(
            "AniList Monitor Bot is already running - this instance exits quietly."
        )
        sys.exit(0)

    try:
        config = load_config()
    except RuntimeError as e:
        message = str(e)

        # Say exactly where .env is expected so the user doesn't have to guess.
        try:
            message += (
                f"\n\nFolder where .env is expected: {get_data_dir()} "
                f"(alternatively: {get_base_dir()})."
            )
        except Exception:
            pass

        # There is no console in --windowed mode: without a window the user would see nothing.
        if getattr(sys, "frozen", False) or sys.stderr is None:
            _show_fatal_message(message)

        logger.error(message)
        sys.exit(1)

    cache = CacheManager(config.cache_file)
    anilist = AniListClient()
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()

    state = AppState(
        config=config, cache=cache, anilist=anilist, bot=bot, dispatcher=dispatcher
    )

    # If autostart is enabled but the registry path is stale (the exe was moved), fix it.
    # The is_autostart_enabled() check guarantees we don't resurrect an entry
    # that the user disabled in Task Manager.
    if autostart.is_autostart_enabled():
        autostart.repair_autostart_path()

    # Set the correct checkbox in the system tray based on the registry state.
    state.autostart_enabled = autostart.is_autostart_enabled()

    tray_thread = threading.Thread(
        target=run_tray, args=(state,), daemon=True, name="TrayThread"
    )
    tray_thread.start()

    try:
        asyncio.run(async_main(state))
    except KeyboardInterrupt:
        # Only relevant for manual `python main.py` runs in a console during development.
        logger.info("Ctrl+C received, shutting down.")

    logger.info("Program finished.")


if __name__ == "__main__":
    main()