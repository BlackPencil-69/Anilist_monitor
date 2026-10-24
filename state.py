# -*- coding: utf-8 -*-
"""
state.py
--------
Shared application state object. A single AppState instance is created
in main.py and passed to both the async bot loop and the system tray
thread - it is the only "junction" between the two worlds (asyncio + threading).
"""
import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AppState:
    config: Any            # config.Config
    cache: Any              # cache_manager.CacheManager
    anilist: Any             # anilist_client.AniListClient
    bot: Any                  # aiogram.Bot
    dispatcher: Any            # aiogram.Dispatcher

    # Populated inside the asyncio loop (main.async_main):
    loop: Optional[asyncio.AbstractEventLoop] = None
    stop_event: Optional[asyncio.Event] = None

    # Thread-safe flag: signals the system tray thread that the async loop
    # has finished a clean shutdown (httpx/aiogram sessions closed,
    # cache saved) and it is safe to stop the tray icon and exit the process.
    shutdown_complete: threading.Event = field(default_factory=threading.Event)

    tray_icon: Optional[Any] = None
    autostart_enabled: bool = False

    # In-memory only (reset on restart): a counter of failed delivery cycles
    # per title, so an undeliverable title isn't retried forever.
    delivery_failures: dict[int, int] = field(default_factory=dict)

    # Created lazily inside the asyncio loop (in check_new_anime):
    # keeps the three check sources (loop, /check, system tray) from interleaving.
    check_lock: Optional[asyncio.Lock] = None