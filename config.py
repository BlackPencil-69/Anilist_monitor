# -*- coding: utf-8 -*-
"""
config.py
Reading and validating the application configuration from the `.env` file.

.env search order:
    1. get_data_dir()/.env  -> %APPDATA%\\AniListMonitorBot\\.env
    2. get_base_dir()/.env  -> next to the .exe / main.py (backward compatibility)

The cache (cache.json) is always stored in get_data_dir(), because the
.exe folder may be write-protected. An old cache next to the .exe is
migrated automatically by copying (the original is not deleted).
"""
import logging
import os
import shutil
from dataclasses import dataclass

from dotenv import load_dotenv

from paths import get_base_dir, get_data_dir

logger = logging.getLogger(__name__)


@dataclass
class Config:
    bot_token: str
    whitelist_ids: set[int]
    check_interval: int
    cache_file: str


def load_config() -> Config:
    """
    Loads the configuration from .env and returns a Config object.
    Raises RuntimeError with a human-friendly explanation if required fields are missing.
    """
    base_dir = get_base_dir()
    data_dir = get_data_dir()

    # The modern location first, then the legacy one next to the .exe.
    env_candidates = [
        os.path.join(data_dir, ".env"),
        os.path.join(base_dir, ".env"),
    ]
    env_path = next(
        (path for path in env_candidates if os.path.exists(path)), None
    )

    if env_path is not None:
        load_dotenv(dotenv_path=env_path)
    else:
        # In case the user supplies environment variables another way (development/CI).
        load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token.upper().startswith("PASTE_YOUR"):
        raise RuntimeError(
            "BOT_TOKEN is not set or has not been changed. "
            f"Create a .env file in the folder {data_dir} "
            f"(or next to the program: {base_dir}) based on .env.example "
            "and set the token you got from @BotFather in Telegram."
        )

    raw_whitelist = os.getenv("WHITELIST_IDS", "")
    raw_items = [x.strip() for x in raw_whitelist.split(",") if x.strip()]
    whitelist_ids: set[int] = set()
    invalid_items: list[str] = []

    for item in raw_items:
        try:
            whitelist_ids.add(int(item))
        except ValueError:
            invalid_items.append(item)

    if invalid_items:
        logger.warning(
            "Ignored invalid values in WHITELIST_IDS: %s",
            ", ".join(invalid_items),
        )

    if not whitelist_ids:
        raise RuntimeError(
            "WHITELIST_IDS is not set in .env. Specify at least one Telegram User ID "
            "(for example WHITELIST_IDS=123456789), otherwise the bot will ignore all users."
        )

    try:
        check_interval = int(os.getenv("CHECK_INTERVAL", "1800"))
    except ValueError:
        check_interval = 1800

    if check_interval < 60:
        # Protection against overly aggressive AniList polling (rate limit).
        check_interval = 60

    # The cache is always in the user's data folder - not next to the .exe.
    cache_file = os.path.join(data_dir, "cache.json")

    # Migration: old cache next to the .exe -> the new data folder.
    # It must be copied BEFORE the first monitoring cycle, otherwise the bot
    # would treat all titles as new and send duplicate notifications.
    old_cache_file = os.path.join(base_dir, "cache.json")
    if os.path.exists(old_cache_file) and not os.path.exists(cache_file):
        try:
            shutil.copy2(old_cache_file, cache_file)
            logger.info(
                "Old cache migrated from %s to %s.",
                old_cache_file,
                cache_file,
            )
        except Exception:
            # A migration failure must not stop the application.
            logger.exception(
                "Failed to copy the old cache from %s to %s.",
                old_cache_file,
                cache_file,
            )

    return Config(
        bot_token=token,
        whitelist_ids=whitelist_ids,
        check_interval=check_interval,
        cache_file=cache_file,
    )