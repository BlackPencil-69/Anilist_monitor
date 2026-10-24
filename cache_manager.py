# -*- coding: utf-8 -*-
"""
cache_manager.py
----------------
Stores the IDs of already sent (processed) anime titles in a local
JSON file, so that after a restart the app does not spam users
with the same announcements again.

File format:
{
  "sent_ids": [12345, 12346, ...],
  "baseline_version": 2
}
The "baseline_version" field is optional: older files do not have it (treated as 0).
"""
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)


class CacheManager:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._ids: set[int] = set()
        self._baseline_version: int = 0
        self.load()

    def load(self) -> None:
        """Reads the cache from disk. If the file is corrupted or missing, starts with an empty cache."""
        with self._lock:
            if not os.path.exists(self.path):
                self._ids = set()
                self._baseline_version = 0
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._ids = {int(x) for x in data.get("sent_ids", [])}
                # Older files without this field have version 0.
                self._baseline_version = int(data.get("baseline_version", 0))
                logger.info(
                    "Cache loaded: %d titles, baseline_version=%d (%s)",
                    len(self._ids), self._baseline_version, self.path,
                )
            except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError) as e:
                logger.warning("Failed to read cache (%s). Starting with an empty cache.", e)
                self._ids = set()
                self._baseline_version = 0

    def save(self) -> None:
        """Atomically saves the cache to disk (via a temporary file + os.replace)."""
        with self._lock:
            tmp_path = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {"sent_ids": sorted(self._ids), "baseline_version": self._baseline_version},
                        f,
                        ensure_ascii=False,
                    )
                os.replace(tmp_path, self.path)
            except OSError as e:
                logger.error("Failed to save cache to %s: %s", self.path, e)

    @property
    def baseline_version(self) -> int:
        """Cache "baseline" version (0 if the field was missing from the file)."""
        with self._lock:
            return self._baseline_version

    @baseline_version.setter
    def baseline_version(self, value: int) -> None:
        with self._lock:
            self._baseline_version = int(value)

    def is_known(self, anime_id: int) -> bool:
        with self._lock:
            return anime_id in self._ids

    def add(self, anime_id: int) -> None:
        with self._lock:
            self._ids.add(anime_id)

    def count(self) -> int:
        with self._lock:
            return len(self._ids)

    def is_empty(self) -> bool:
        with self._lock:
            return len(self._ids) == 0