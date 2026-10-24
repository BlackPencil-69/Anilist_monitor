# -*- coding: utf-8 -*-
"""
tray_icon.py
------------
Builds the system tray icon and menu (pystray).

This module runs in a SEPARATE thread (see main.run_tray). Interaction
with the bot's asyncio loop (running in the main thread) happens ONLY
through thread-safe primitives:
  - asyncio.run_coroutine_threadsafe(...)   - to run a coroutine in another thread;
  - loop.call_soon_threadsafe(...)          - to safely set an asyncio.Event;
  - threading.Event (state.shutdown_complete) - to wait for cleanup to finish.

pystray.Icon.run() can safely be called from a non-main thread on
Windows/Linux (the "main thread only" restriction applies only to macOS/Cocoa).
"""
import asyncio
import logging
import os

import pystray
from pystray import Menu
from pystray import MenuItem as Item

import autostart
from bot_app import check_new_anime
from icon_utils import load_icon_image

logger = logging.getLogger(__name__)


def _on_toggle_autostart(icon, item, state) -> None:
    want_enabled = not state.autostart_enabled
    ok = autostart.enable_autostart() if want_enabled else autostart.disable_autostart()
    if ok:
        state.autostart_enabled = want_enabled
        logger.info("Start with Windows: %s", "enabled" if want_enabled else "disabled")
    else:
        logger.error("Failed to change the Windows autostart state.")
    icon.update_menu()


def _on_check_now(icon, item, state) -> None:
    if not (state.loop and state.loop.is_running()):
        return

    async def _run_check():
        try:
            sent = await check_new_anime(state)
            logger.info("Manual check finished: notifications sent for %d titles.", sent)
        except Exception:
            logger.exception("Error during the manual AniList check.")

    asyncio.run_coroutine_threadsafe(_run_check(), state.loop)


def _on_exit(icon, item, state) -> None:
    logger.info("\"Exit\" clicked - initiating clean shutdown...")
    if state.loop and state.loop.is_running() and state.stop_event is not None:
        # Safely set the asyncio.Event from another thread.
        state.loop.call_soon_threadsafe(state.stop_event.set)
        # Wait (with a timeout) until the async loop closes the httpx/aiogram sessions and saves the cache.
        completed = state.shutdown_complete.wait(timeout=15)
        if not completed:
            logger.warning("Timed out waiting for clean shutdown - forcing exit.")
        icon.stop()
    else:
        # Narrow window right after startup: the tray thread is already running, but the
        # asyncio loop in the main thread hasn't yet set state.loop/state.stop_event.
        # Simply stopping the icon here is not enough - the bot (polling + monitor_loop)
        # would keep living invisibly in the background. So we terminate the process forcibly.
        logger.warning("Async loop not started yet - terminating the process forcibly.")
        icon.stop()
        os._exit(0)


def build_tray_icon(state) -> pystray.Icon:
    image = load_icon_image()

    menu = Menu(
        Item("🟢 Bot is running...", None, enabled=False),
        Menu.SEPARATOR,
        Item(
            "Start with Windows",
            lambda icon, item: _on_toggle_autostart(icon, item, state),
            checked=lambda item: state.autostart_enabled,
        ),
        Item(
            "Check for updates now",
            lambda icon, item: _on_check_now(icon, item, state),
        ),
        Menu.SEPARATOR,
        Item("Exit", lambda icon, item: _on_exit(icon, item, state)),
    )

    return pystray.Icon("anilist_monitor_bot", image, "AniList Monitor Bot", menu)