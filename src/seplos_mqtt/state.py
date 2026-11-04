"""
Small persistent state across add-on restarts (the add-on's /data volume), e.g. the last valid
energy counters so the 0x42 fallback value can be recognized after a restart.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("SeplosBMS.state")

DEFAULT_PATH = "/data/seplos_state.json"


class StateStore:
    """JSON key/value store; writes are throttled and atomic. path None = memory only."""

    MIN_WRITE_INTERVAL = 300

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.data: dict = {}
        self._dirty = False
        self._last_write: float | None = None
        self._durable_pending: set[str] = set()
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                logger.warning("Ignoring unreadable state file %s: %s", self.path, e)
            else:
                if isinstance(data, dict):
                    self.data = data
                else:
                    logger.warning("Ignoring state file %s: not a JSON object", self.path)

    @classmethod
    def default(cls, path: str | None) -> StateStore:
        """Use `path`, else /data/seplos_state.json if /data is writable, else memory only."""
        if path:
            return cls(path)
        if os.path.isdir("/data") and os.access("/data", os.W_OK):
            return cls(DEFAULT_PATH)
        return cls(None)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        """Store a value; written to disk throttled."""
        if self.data.get(key) != value:
            self.data[key] = value
            self._dirty = True
            self.flush(force=False)

    def set_durable(self, key: str, value) -> bool:
        """
        Store a value and write it to disk right away (not throttled). True once it is on disk (or
        there is no file); a failed write stays pending and is retried by the next call.
        """
        if self.data.get(key) == value and key not in self._durable_pending:
            return True
        self.data[key] = value
        self._dirty = True
        self._durable_pending.add(key)
        self.flush(force=True)
        if self.path is None or not self._dirty:
            self._durable_pending.discard(key)
        return key not in self._durable_pending

    def flush(self, force: bool = True) -> None:
        if not self._dirty or self.path is None:
            return
        now = time.monotonic()
        if not force and self._last_write is not None and now - self._last_write < self.MIN_WRITE_INTERVAL:
            return
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".seplos_state")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
            tmp = None
            self._dirty = False
            self._durable_pending.clear()
            self._last_write = now
        except OSError as e:
            logger.warning("Could not write state file %s: %s", self.path, e)
        finally:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)
