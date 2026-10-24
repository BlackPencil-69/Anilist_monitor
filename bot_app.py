# -*- coding: utf-8 -*-
"""
bot_app.py
----------
The Telegram bot logic itself:
  1. WhitelistMiddleware  - lets messages through only from allowed IDs.
  2. register_handlers    - the /start, /status, /check commands.
  3. check_new_anime      - the main business logic: query AniList,
                             compare with the cache, send nicely formatted
                             HTML messages with a cover image and a button.
"""
import asyncio
import logging
from html import escape as _esc

from aiogram import BaseMiddleware, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

logger = logging.getLogger(__name__)

MAX_CAPTION_LEN = 1024  # Telegram limit for a photo caption
MAX_TITLE_LEN = 200            # title limit (plain text, before escaping)
MAX_GENRES_LEN = 200           # genres string limit (plain text, before escaping)
MAX_DELIVERY_FAILURES = 5      # after this many failed cycles the title is cached despite non-delivery
MAX_RETRY_AFTER = 30           # maximum wait on TelegramRetryAfter, in seconds
CURRENT_BASELINE_VERSION = 2   # increase to force a new silent cache rebaseline

FORMAT_LABELS = {
    "TV": "TV Series",
    "TV_SHORT": "TV Short",
    "MOVIE": "Movie",
    "SPECIAL": "Special",
    "OVA": "OVA",
    "ONA": "ONA",
    "MUSIC": "Music video",
}
STATUS_LABELS = {
    "NOT_YET_RELEASED": "📢 Announced",
    "RELEASING": "📡 Airing now",
}
SEASON_LABELS = {
    "WINTER": "Winter",
    "SPRING": "Spring",
    "SUMMER": "Summer",
    "FALL": "Fall",
}


# ---------------------------------------------------------------------------
# 1. Whitelist middleware
# ---------------------------------------------------------------------------
class WhitelistMiddleware(BaseMiddleware):
    """
    Registered as an OUTER middleware (dp.message.outer_middleware), so it
    runs BEFORE any routing to handlers.

    If user_id is not in WHITELIST_IDS, the event is simply ignored
    (the bot does not respond at all), as required by the spec.
    """

    def __init__(self, whitelist_ids: set[int]):
        super().__init__()
        self.whitelist_ids = whitelist_ids

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None or user.id not in self.whitelist_ids:
            logger.debug("Ignoring message from non-whitelist user_id=%s", getattr(user, "id", None))
            return None  # the handler is NOT called, nothing is sent back
        return await handler(event, data)


# ---------------------------------------------------------------------------
# 2. Bot commands
# ---------------------------------------------------------------------------
def register_handlers(dp: Dispatcher, state) -> None:
    """Registers the /start, /status, /check commands. `state` is a state.AppState object."""

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "👋 <b>AniList Monitor Bot</b> is running.\n"
            f"⏱ Auto-check interval: {state.config.check_interval} s.\n\n"
            "Commands:\n"
            "/status - bot status\n"
            "/check - check AniList now"
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        await message.answer(
            "✅ Bot is running.\n"
            f"📦 Titles in cache: {state.cache.count()}\n"
            f"⏱ Check interval: {state.config.check_interval} s."
        )

    @dp.message(Command("check"))
    async def cmd_check(message: Message) -> None:
        await message.answer("🔄 Checking AniList manually, please wait...")
        sent = await check_new_anime(state)
        await message.answer(f"Done. New notifications sent: {sent}")


# ---------------------------------------------------------------------------
# 3. Message formatting
# ---------------------------------------------------------------------------
def _pick_title(title_obj: dict) -> str:
    return (
        (title_obj or {}).get("english")
        or (title_obj or {}).get("romaji")
        or (title_obj or {}).get("native")
        or "Untitled"
    )


def _truncate(text: str, limit: int = MAX_CAPTION_LEN) -> str:
    """Truncates PLAIN text (before html.escape) so an entity or tag is not cut in half."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_caption(media: dict) -> str:
    """Builds a nicely formatted HTML caption for a message about a single title."""
    # Truncate the plain text first, and only then escape it.
    title = _esc(_truncate(_pick_title(media.get("title") or {}), MAX_TITLE_LEN))
    status_label = STATUS_LABELS.get(media.get("status"), _esc(str(media.get("status") or "—")))
    format_label = FORMAT_LABELS.get(media.get("format"), _esc(str(media.get("format") or "—")))

    season = SEASON_LABELS.get(media.get("season"))
    year = media.get("seasonYear")
    if season and year:
        season_str = f"{season} {year}"
    elif year:
        season_str = str(year)
    else:
        season_str = "—"

    genres = media.get("genres") or []
    genres_str = _esc(_truncate(", ".join(genres), MAX_GENRES_LEN)) if genres else "—"

    def _build(with_genres: bool) -> str:
        lines = [
            f"<b>{title}</b>",
            status_label,
            f"🎞 Format: {format_label}",
            f"📅 Season: {season_str}",
        ]
        if with_genres:
            lines.append(f"🏷 Genres: {genres_str}")
        return "\n".join(lines)

    caption = _build(True)
    if len(caption) > MAX_CAPTION_LEN:
        # Do not cut the finished HTML - drop the genres line entirely.
        caption = _build(False)
    return caption


def _build_keyboard(site_url: str | None) -> InlineKeyboardMarkup | None:
    if not site_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Open on AniList", url=site_url)]]
    )


# ---------------------------------------------------------------------------
# 4. Sending notifications
# ---------------------------------------------------------------------------
async def _call_with_retry(send, *args, **kwargs):
    """Performs a send; on TelegramRetryAfter waits (max 30 s) and retries once."""
    try:
        return await send(*args, **kwargs)
    except TelegramRetryAfter as e:
        delay = min(e.retry_after, MAX_RETRY_AFTER)
        logger.warning("Telegram asks to wait %s s, waiting %s s and retrying.", e.retry_after, delay)
        await asyncio.sleep(delay)
        return await send(*args, **kwargs)


async def _send_notification(bot, user_id: int, covers: list[str], caption: str, keyboard) -> bool:
    """
    Sends a notification to a single user. Order of attempts:
    each cover from the `covers` list (extraLarge -> large), then text without a photo.
    Returns True if anything was delivered.
    """
    attempts: list[tuple[str, str | None]] = [("photo", url) for url in covers] + [("text", None)]
    for kind, url in attempts:
        try:
            if url:
                await _call_with_retry(bot.send_photo, user_id, photo=url, caption=caption, reply_markup=keyboard)
            else:
                await _call_with_retry(bot.send_message, user_id, caption, reply_markup=keyboard)
            return True
        except TelegramForbiddenError as e:
            # The user has blocked the bot - a fallback is pointless.
            logger.warning("User user_id=%s has blocked the bot, skipping: %s", user_id, e)
            return False
        except TelegramRetryAfter as e:
            # RetryAfter again after waiting - give up until the next cycle.
            logger.warning("Telegram rate limit for user_id=%s has not expired after waiting: %s", user_id, e)
            return False
        except TelegramAPIError as e:
            logger.warning("Failed to send (%s) to user_id=%s: %s", kind, user_id, e)
    return False


# ---------------------------------------------------------------------------
# 5. Main monitoring logic
# ---------------------------------------------------------------------------
async def check_new_anime(state) -> int:
    """
    Queries AniList for the latest titles, compares them with the cache and sends
    notifications about new ones to all users in WHITELIST_IDS.

    Returns the number of titles for which at least one user was notified.

    Special case - "first run" (empty cache): to avoid spamming the user
    with dozens of old announcements right after installing the bot,
    all fetched IDs are simply written to the cache WITHOUT sending
    any messages. Real notifications start arriving from the next check
    cycle, once genuinely new entries appear on AniList.

    A title is added to the cache only after successful delivery (to at least one
    user) or after MAX_DELIVERY_FAILURES consecutive failed cycles.
    Concurrent calls (the loop, /check, the tray) are serialized via check_lock.
    """
    if state.check_lock is None:
        state.check_lock = asyncio.Lock()

    async with state.check_lock:
        media_list = await state.anilist.fetch_latest(per_page=20)
        if not media_list:
            return 0

        # Silent rebaseline: empty cache (first run) or a cache of an older version
        # (the query set was extended). All fetched IDs are recorded without notifications.
        if state.cache.baseline_version < CURRENT_BASELINE_VERSION:
            was_empty = state.cache.is_empty()
            added = 0
            for m in media_list:
                mid = m.get("id")
                if mid is not None and not state.cache.is_known(mid):
                    state.cache.add(mid)
                    added += 1
            state.cache.baseline_version = CURRENT_BASELINE_VERSION
            await asyncio.to_thread(state.cache.save)
            logger.info(
                "Cache rebaseline (%s): added %d titles without notifications, baseline_version=%d.",
                "first run" if was_empty else "migration",
                added, CURRENT_BASELINE_VERSION,
            )
            return 0

        new_items = [m for m in media_list if m.get("id") is not None and not state.cache.is_known(m["id"])]
        if not new_items:
            return 0

        sent_count = 0
        cache_dirty = False
        # Go from the oldest new ID to the newest - a more natural chronological order in the chat.
        for m in reversed(new_items):
            anime_id = m["id"]
            caption = format_caption(m)
            cover_obj = m.get("coverImage") or {}
            covers: list[str] = []
            for key in ("extraLarge", "large"):
                url = cover_obj.get(key)
                if url and url not in covers:
                    covers.append(url)
            keyboard = _build_keyboard(m.get("siteUrl"))

            delivered_any = False
            for user_id in state.config.whitelist_ids:
                if await _send_notification(state.bot, user_id, covers, caption, keyboard):
                    delivered_any = True

            if delivered_any:
                state.cache.add(anime_id)
                state.delivery_failures.pop(anime_id, None)
                cache_dirty = True
                sent_count += 1
            else:
                failures = state.delivery_failures.get(anime_id, 0) + 1
                if failures >= MAX_DELIVERY_FAILURES:
                    logger.error(
                        "Title id=%s was not delivered for %d consecutive cycles - adding to cache, notification lost.",
                        anime_id, failures,
                    )
                    state.cache.add(anime_id)
                    state.delivery_failures.pop(anime_id, None)
                    cache_dirty = True
                else:
                    state.delivery_failures[anime_id] = failures
                    logger.warning(
                        "Title id=%s was not delivered to anyone (attempt %d/%d), will retry in the next cycle.",
                        anime_id, failures, MAX_DELIVERY_FAILURES,
                    )

        if cache_dirty:
            await asyncio.to_thread(state.cache.save)
        return sent_count