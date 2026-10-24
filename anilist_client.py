# -*- coding: utf-8 -*-
"""
anilist_client.py
------------------
A lightweight async client for the public AniList GraphQL API
(https://graphql.anilist.co), without the official SDK - httpx only.

Makes two queries for ANIME-type titles:
  1) new announcements: NOT_YET_RELEASED + RELEASING, sorted by ID (descending);
  2) older entries that have started airing: RELEASING only, sorted by start
     date (descending).
The results are merged without duplicates.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"

# How many pages to fetch for the "older entries that have started airing" query.
RELEASING_PAGES = 2
# Delay between requests to AniList (to avoid hitting the rate limit).
REQUEST_DELAY = 0.7
# Upper bound for waiting on a 429, in seconds.
MAX_RETRY_AFTER = 60.0

# Parameterized query: sort order and statuses are passed as variables.
# MediaSort (ID_DESC, START_DATE_DESC) and MediaStatus (NOT_YET_RELEASED, RELEASING) are valid AniList enums.
_QUERY = """
query ($page: Int, $perPage: Int, $sort: [MediaSort], $statusIn: [MediaStatus]) {
  Page(page: $page, perPage: $perPage) {
    media(
      type: ANIME
      sort: $sort
      status_in: $statusIn
    ) {
      id
      title {
        romaji
        english
        native
      }
      coverImage {
        extraLarge
        large
      }
      format
      status
      season
      seasonYear
      genres
      siteUrl
    }
  }
}
"""


class AniListClient:
    """A thin wrapper around httpx.AsyncClient for calls to the AniList GraphQL API."""

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _fetch_page(self, variables: dict, max_retries: int) -> list[dict] | None:
        """
        Performs a single request (one page) with retries.
        Returns a list of media (possibly empty), or None if the request ultimately failed.
        """
        for attempt in range(1, max_retries + 1):
            is_last = attempt == max_retries
            try:
                resp = await self._client.post(
                    ANILIST_URL,
                    json={"query": _QUERY, "variables": variables},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
            except httpx.HTTPError as e:
                logger.warning("AniList connection error (attempt %d/%d): %s", attempt, max_retries, e)
                if not is_last:
                    await asyncio.sleep(2 * attempt)
                continue

            if resp.status_code == 429:
                # AniList reports the wait time in the Retry-After header; cap it from above.
                retry_after = min(_safe_float(resp.headers.get("Retry-After"), default=5.0), MAX_RETRY_AFTER)
                logger.warning("AniList rate limit (429). Attempt %d/%d.", attempt, max_retries)
                if not is_last:
                    logger.warning("Waiting %.1f s.", retry_after)
                    await asyncio.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                logger.warning("AniList returned server error %s (attempt %d/%d).",
                               resp.status_code, attempt, max_retries)
                if not is_last:
                    await asyncio.sleep(2 * attempt)
                continue

            if resp.status_code != 200:
                logger.warning("AniList returned status %s: %s", resp.status_code, resp.text[:200])
                return None

            try:
                data = resp.json()
            except ValueError:
                logger.warning("Invalid (non-JSON) response from AniList.")
                return None

            if "errors" in data:
                logger.warning("AniList GraphQL error: %s", data["errors"])
                return None

            return (data.get("data") or {}).get("Page", {}).get("media", []) or []

        logger.error("Failed to fetch data from AniList after %d attempts.", max_retries)
        return None

    async def fetch_latest(self, per_page: int = 20, max_retries: int = 3, pages: int = 3) -> list[dict]:
        """
        Returns titles from two queries, merged without duplicates (order preserved):
          1) new announcements: NOT_YET_RELEASED + RELEASING statuses, ID_DESC, pages 1..pages;
          2) older entries that have started airing: RELEASING only, START_DATE_DESC, RELEASING_PAGES pages.
        If the first page fails, returns []. If a later page fails,
        returns what has already been collected (safe: notifications are sent only for new IDs).
        """
        per_page = max(1, min(per_page, 50))  # AniList limit
        pages = max(1, pages)

        plans = [
            ("ID_DESC", ["NOT_YET_RELEASED", "RELEASING"], pages),
            ("START_DATE_DESC", ["RELEASING"], RELEASING_PAGES),
        ]

        collected: list[dict] = []
        first_request = True
        failed = False

        for sort, statuses, page_count in plans:
            for page in range(1, page_count + 1):
                if not first_request:
                    await asyncio.sleep(REQUEST_DELAY)
                first_request = False

                variables = {"page": page, "perPage": per_page, "sort": [sort], "statusIn": statuses}
                items = await self._fetch_page(variables, max_retries)

                if items is None:
                    failed = True
                    break
                collected.extend(items)
                if len(items) < per_page:
                    break  # ran out of pages - nothing more in this query
            if failed:
                break

        if failed:
            if not collected:
                return []
            logger.warning("One of the AniList pages failed - returning the %d records collected so far.", len(collected))

        # Remove duplicates by id, keeping the order of first appearance.
        seen: set = set()
        result: list[dict] = []
        for m in collected:
            mid = m.get("id")
            if mid is not None:
                if mid in seen:
                    continue
                seen.add(mid)
            result.append(m)
        return result


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default