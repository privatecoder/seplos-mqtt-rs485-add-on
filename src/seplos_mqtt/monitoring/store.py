"""Small atomic, versioned JSON store for the persisted monitoring configuration.

A missing file is a first installation (``MISSING``); a present file that cannot be read, has an
unknown schema, an invalid revision or an invalid body is ``CORRUPT`` and is never silently
replaced. Writes are atomic for the process (temporary file, fsync, rename, fsync of the
directory) and the in-memory document only changes after the write succeeded. Every commit
carries the revision the caller based its change on (optimistic concurrency). No power-loss
guarantee is claimed.

File layout: ``{"schema_version": <int>, "revision": <int >= 1>, "data": {...}}``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Validator = Callable[[dict[str, Any]], bool]


class LoadStatus(StrEnum):
    MISSING = "missing"  # first installation
    VALID = "valid"
    CORRUPT = "corrupt"  # unreadable, invalid JSON, unknown schema, revision or content


class StaleRevision(Exception):
    """The document changed since the caller read it."""

    def __init__(self, current: int) -> None:
        super().__init__(f"stale revision, current is {current}")
        self.current = current


class StoreCorrupt(Exception):
    """The document on disk is unusable; only ``reset`` may replace it."""

    def __init__(self, problem: str | None) -> None:
        super().__init__(f"store is corrupt ({problem})")
        self.problem = problem


class JsonStore:
    """One JSON object per file, versioned. ``data`` is read-only for callers: change it with
    ``commit`` (which persists first)."""

    def __init__(
        self, path: Path, *, schema_version: int = 1, validate: Validator | None = None
    ) -> None:
        self.path = path
        self.schema_version = schema_version
        self._validate = validate
        self.data: dict[str, Any] = {}
        self.revision = 0  # 0 while MISSING
        self.status = LoadStatus.MISSING
        self.problem: str | None = None
        self._lock = threading.Lock()
        self._load()

    @property
    def corrupt(self) -> bool:
        return self.status is LoadStatus.CORRUPT

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError):
            self.mark_corrupt("unreadable")
            return
        try:
            raw = json.loads(text)
        except ValueError:
            self.mark_corrupt("invalid JSON")
            return
        if not isinstance(raw, dict) or (
            type(raw.get("schema_version")) is not int or raw["schema_version"] != self.schema_version
        ):
            self.mark_corrupt("unknown schema")
            return
        revision = raw.get("revision")
        if type(revision) is not int or revision < 1:
            self.mark_corrupt("invalid revision")
            return
        body = raw.get("data")
        if not isinstance(body, dict) or (self._validate is not None and not self._validate(body)):
            self.mark_corrupt("invalid content")
            return
        self.data = body
        self.revision = revision
        self.status = LoadStatus.VALID

    def snapshot(self) -> tuple[LoadStatus, str | None, int, dict[str, Any]]:
        """Coherent ``(status, problem, revision, deep copy of data)`` taken under the store lock
        (blocks while a commit is in progress: call it off the event loop)."""
        with self._lock:
            return self.status, self.problem, self.revision, copy.deepcopy(self.data)

    def mark_corrupt(self, problem: str) -> None:
        """Flag the loaded document as unusable (the file itself is left untouched)."""
        self.status = LoadStatus.CORRUPT
        self.problem = problem
        self.data = {}
        self.revision = 0
        log.error("state file %s is not usable (%s)", self.path.name, problem)

    def _write(self, new: dict[str, Any], revision: int) -> None:
        """Write the document atomically. Adopts nothing: the caller updates the in-memory state
        after this returned."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(
            {"schema_version": self.schema_version, "revision": revision, "data": new},
            indent=1,
            allow_nan=False,  # ValueError for NaN/inf: nothing is written or adopted
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        _fsync_directory(self.path.parent)

    def _adopt(self, new: dict[str, Any], revision: int) -> None:
        self.data = copy.deepcopy(new)
        self.revision = revision
        self.status = LoadStatus.VALID
        self.problem = None

    def commit(self, new: dict[str, Any], expected_revision: int) -> None:
        """Persist ``new`` atomically as ``revision + 1``; on success it becomes ``data``.

        Raises ``StoreCorrupt`` for a corrupt document, ``StaleRevision`` if ``expected_revision``
        is not the current revision (check through adoption is one critical section) and
        ``OSError`` if writing fails (the old document stays in place).
        """
        if type(expected_revision) is not int:
            raise TypeError("expected_revision must be an int")
        with self._lock:
            if self.corrupt:
                raise StoreCorrupt(self.problem)
            if expected_revision != self.revision:
                raise StaleRevision(self.revision)
            revision = self.revision + 1
            self._write(new, revision)
            self._adopt(new, revision)

    def save(self) -> None:
        """Write the current document again (no change, new revision)."""
        with self._lock:
            if self.corrupt:
                raise StoreCorrupt(self.problem)
            revision = self.revision + 1
            data = copy.deepcopy(self.data)
            self._write(data, revision)
            self._adopt(data, revision)

    def reset(self, new: dict[str, Any] | None = None) -> None:
        """Explicit user reset: keep a corrupt file as ``<name>.corrupt`` (abort if that fails)
        and start a new document with revision 1."""
        data = new if new is not None else {}
        with self._lock:
            backed_up = False
            if self.corrupt and self.path.exists():
                backup = self.path.with_name(self.path.name + ".corrupt")
                os.replace(self.path, backup)  # failure aborts: the original stays untouched
                backed_up = True
            try:
                self._write(data, 1)
            except BaseException:
                if backed_up:
                    self.problem = "reset failed"
                raise
            self._adopt(data, 1)


def _fsync_directory(directory: Path) -> None:
    """Make the rename durable where the platform allows it (best effort)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
