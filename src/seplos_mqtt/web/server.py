"""Ingress web server: security gate, JSON API for the connection settings, static frontend.

Security: connections are accepted only from the Supervisor peer (the TCP peer decides, never a
header); ``X-Remote-User-*`` is read only after that check and a missing user is never an
administrator. Changing calls need a user, a CSRF token bound to that user, a JSON body that
matches a strict schema and the current revision in ``If-Match``. Responses carry text KEYS
(the UI translates); no secrets.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import mimetypes
import re
import secrets
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from aiohttp import web

from .. import timeutil
from ..cells import catalog
from ..cells import validate as cell_validate
from ..jobs import Job, JobBusy, JobQueue
from ..monitoring.model import (
    _PACK_KEYS,
    ConfigValidationError,
    MonitoringConfig,
    Problem,
    changed_fields,
    find_cell,
    set_activity,
)
from ..monitoring.store import JsonStore, LoadStatus, StaleRevision, StoreCorrupt

log = logging.getLogger("SeplosBMS.web")

SUPERVISOR_PEER = "172.30.32.2"
MAX_BODY = 64 * 1024

_REVISION_RE = re.compile(r'^(?:W/)?"?([0-9]{1,12})"?$')
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
_STATIC_PART_RE =re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'self'"
    ),
}


@dataclass(frozen=True)
class WebSettings:
    host: str = "0.0.0.0"
    port: int = 0
    # Only for tests: the Supervisor peer is fixed in production.
    allowed_peers: frozenset[str] = field(default_factory=lambda: frozenset({SUPERVISOR_PEER}))


@dataclass(frozen=True)
class User:
    id: str
    name: str | None
    display_name: str | None


class ApiError(Exception):
    def __init__(self, key: str, status: int = 400, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.status = status
        self.params = params


def _jsonable(data: Any) -> str:
    return json.dumps(data, allow_nan=False, separators=(",", ":"), sort_keys=False)


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_jsonable)


def error_body(key: str, **params: Any) -> dict[str, Any]:
    return {"error": {"key": key, "params": params}}


class WebApp:
    def __init__(
        self,
        settings: WebSettings,
        store: JsonStore,
        *,
        on_connection_saved: Callable[[MonitoringConfig], None] | None = None,
        on_sets_saved: Callable[[MonitoringConfig], None] | None = None,
        on_cells_saved: Callable[[MonitoringConfig], None] | None = None,
        status_provider: Callable[[], dict[str, Any] | None] | None = None,
        jobs: JobQueue | None = None,
    ) -> None:
        """``on_connection_saved`` is called on the web event loop after each successful save, in
        commit order, and must return quickly (step 1c hands the work to the main thread);
        ``on_sets_saved`` is the same for the parallel sets (it must not restart anything) and
        ``on_cells_saved`` for everything about cell types, pack assignments and balancer profiles.
        ``status_provider`` returns the latest status snapshot (a finished dict, read-only here);
        ``jobs`` is the queue the main loop thread works off (None: the job routes answer 503).
        The web thread never touches the serial bus."""
        self.settings = settings
        self.store = store
        self._on_connection_saved = on_connection_saved
        self._on_sets_saved = on_sets_saved
        self._on_cells_saved = on_cells_saved
        self._status_provider = status_provider or (lambda: None)
        self._jobs = jobs
        # serialises PUT /api/connection and /api/sets from the corrupt check through callback and response
        # (binds to the server's loop on first contention)
        self._save_lock = asyncio.Lock()
        self._saves: set[asyncio.Task[web.Response]] = set()
        self._closing = False
        self._secret = secrets.token_bytes(32)
        self._peers = frozenset(_normalise_ip(p) for p in settings.allowed_peers)
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

    # -- lifecycle --------------------------------------------------------------------------

    def make_app(self) -> web.Application:
        app = web.Application(middlewares=[self._peer_check, self._errors], client_max_size=MAX_BODY)
        r = app.router
        r.add_get("/", self._index)
        r.add_get("/static/{path:.+}", self._static)
        r.add_get("/api/session", self._session)
        r.add_get("/api/connection", self._get_connection)
        r.add_put("/api/connection", self._put_connection)
        r.add_get("/api/sets", self._get_sets)
        r.add_put("/api/sets", self._put_sets)
        r.add_get("/api/cells", self._get_cells)
        r.add_put("/api/cells", self._put_cells)
        r.add_post("/api/cells/rename", self._post_rename_cell)
        r.add_get("/api/packs", self._get_packs)
        r.add_put("/api/packs", self._put_packs)
        r.add_post("/api/packs/accept-update", self._post_accept_update)
        r.add_post("/api/packs/confirm-identity", self._post_confirm_identity)
        r.add_get("/api/balancer-profiles", self._get_profiles)
        r.add_put("/api/balancer-profiles", self._put_profiles)
        r.add_get("/api/status", self._get_status)
        r.add_post("/api/jobs/detect", self._post_detect)
        r.add_get("/api/jobs/{job_id}", self._get_job)
        r.add_post("/api/jobs/{job_id}/cancel", self._cancel_job)
        return app

    async def start(self) -> int:
        runner = web.AppRunner(self.make_app(), access_log=None, shutdown_timeout=3.0)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.settings.host, self.settings.port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        sockets = runner.addresses
        self.port = int(sockets[0][1]) if sockets else self.settings.port
        return self.port

    async def stop(self) -> None:
        """Refuse new saves, let running ones finish (commit and callback; a commit is durable and
        bounded by the filesystem, so no timeout), then stop the HTTP server."""
        self._closing = True
        await asyncio.gather(*self._saves, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- middleware -------------------------------------------------------------------------

    @web.middleware
    async def _peer_check(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        # The TCP peer decides, never a header: this runs before any X-Remote-User-* header is read.
        peername = request.transport.get_extra_info("peername") if request.transport else None
        host = peername[0] if isinstance(peername, tuple) and peername else None
        if host is None or _normalise_ip(str(host)) not in self._peers:
            return _finalize(request, json_response(error_body("forbidden_peer"), 403))
        return await handler(request)

    @web.middleware
    async def _errors(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        try:
            response = await handler(request)
        except ApiError as exc:
            response = json_response(error_body(exc.key, **exc.params), exc.status)
        except web.HTTPException as exc:
            response = json_response(error_body(f"http_{exc.status}"), exc.status)
            if exc.status == 405:
                response.headers["Allow"] = exc.headers.get("Allow", "")
        except Exception as exc:
            log.error("web request failed (%s)", type(exc).__name__)
            response = json_response(error_body("internal_error"), 500)
        return _finalize(request, response)

    # -- identity / CSRF --------------------------------------------------------------------

    def _user(self, request: web.Request) -> User | None:
        raw = request.headers.get("X-Remote-User-Id", "").strip()
        if not raw or len(raw) > 128 or not raw.isprintable():
            return None
        return User(
            raw,
            _header(request, "X-Remote-User-Name"),
            _header(request, "X-Remote-User-Display-Name"),
        )

    def _csrf(self, user: User) -> str:
        return hmac.new(self._secret, user.id.encode(), hashlib.sha256).hexdigest()

    async def _mutation(self, request: web.Request, *, revision: bool = True) -> tuple[User, Any, int]:
        """Common gate of every changing call: user, CSRF, JSON body and (unless ``revision`` is
        False, as for jobs) the current revision in ``If-Match``."""
        user = self._user(request)
        if user is None:
            raise ApiError("identity_required", 403)
        given = request.headers.get("X-CSRF-Token", "")
        if not given or not hmac.compare_digest(given.encode(), self._csrf(user).encode()):
            raise ApiError("csrf_invalid", 403)
        if request.content_type != "application/json":
            raise ApiError("json_required", 415)
        try:
            body = await request.json(loads=json.loads)
        except (ValueError, RecursionError):
            raise ApiError("invalid_json") from None
        if not _all_finite(body):
            raise ApiError("invalid_number")
        if not revision:
            return user, body, 0
        header = request.headers.get("If-Match")
        if header is None:
            raise ApiError("revision_required", 428)
        match = _REVISION_RE.match(header.strip())
        if match is None:
            raise ApiError("invalid_request", field="If-Match")
        return user, body, int(match.group(1))

    # -- static files -----------------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        return _package_file(["static", "index.html"])

    async def _static(self, request: web.Request) -> web.Response:
        parts = request.match_info["path"].split("/")
        if len(parts) > 4 or not all(_STATIC_PART_RE.match(p) for p in parts):
            raise web.HTTPNotFound
        return _package_file(["static", *parts])

    # -- API --------------------------------------------------------------------------------

    async def _session(self, request: web.Request) -> web.Response:
        user = self._user(request)
        status, problem, revision, _data = await asyncio.to_thread(self.store.snapshot)
        return json_response(
            {
                "revision": revision,
                "csrf": self._csrf(user) if user else None,
                "user": (
                    {"id": user.id, "name": user.name, "display_name": user.display_name}
                    if user
                    else None
                ),
                "store": {"status": status.value, "problem": problem},
            }
        )

    def _connection_view(self, config: MonitoringConfig, revision: int) -> dict[str, Any]:
        return {"revision": revision, "connection": config.connection.to_dict()}

    def _sets_view(self, config: MonitoringConfig, revision: int) -> dict[str, Any]:
        sets = [
            {**item.to_dict(), "active": active, "reason": reason}
            for item, (active, reason) in zip(config.parallel_sets, set_activity(config), strict=True)
        ]
        return {"revision": revision, "pack_count": config.connection.pack_count, "sets": sets}

    async def _load(self) -> tuple[MonitoringConfig, int]:
        status, _problem, revision, data = await asyncio.to_thread(self.store.snapshot)
        if status is LoadStatus.CORRUPT:
            raise ApiError("store_corrupt", 409)
        if status is LoadStatus.MISSING:
            raise ApiError("store_missing", 409)
        try:
            return MonitoringConfig.from_dict(data), revision
        except ConfigValidationError:
            raise ApiError("store_corrupt", 409) from None

    async def _get_connection(self, request: web.Request) -> web.Response:
        config, revision = await self._load()
        return json_response(self._connection_view(config, revision))

    async def _get_sets(self, request: web.Request) -> web.Response:
        config, revision = await self._load()
        return json_response(self._sets_view(config, revision))

    # -- status and jobs (the web thread only reads snapshots and enqueues jobs) --------------

    async def _get_status(self, request: web.Request) -> web.Response:
        return json_response({"status": self._status_provider()})

    def _job_queue(self) -> JobQueue:
        if self._jobs is None:
            raise ApiError("jobs_unavailable", 503)
        return self._jobs

    async def _job_gate(self, request: web.Request) -> JobQueue:
        """Identity, CSRF and an empty JSON object as body (jobs carry no parameters, no revision)."""
        _user, body, _ = await self._mutation(request, revision=False)
        if body != {}:
            raise ApiError("invalid_request", field="body")
        return self._job_queue()

    def _job_of(self, request: web.Request) -> tuple[JobQueue, Job]:
        queue = self._job_queue()
        job_id = request.match_info["job_id"]
        job = queue.get(job_id) if _JOB_ID_RE.fullmatch(job_id) else None
        if job is None:
            raise ApiError("unknown_job", 404)
        return queue, job

    async def _post_detect(self, request: web.Request) -> web.Response:
        queue = await self._job_gate(request)
        if self._closing:
            raise ApiError("shutting_down", 503)
        try:
            job = queue.submit("detect")
        except JobBusy:
            raise ApiError("job_busy", 409) from None
        return json_response({"job": job.to_dict()}, 202)

    async def _get_job(self, request: web.Request) -> web.Response:
        _queue, job = self._job_of(request)
        return json_response({"job": job.to_dict()})

    async def _cancel_job(self, request: web.Request) -> web.Response:
        queue = await self._job_gate(request)
        _queue, job = self._job_of(request)
        queue.cancel(job.id)
        return json_response({"job": job.to_dict()})

    async def _put_connection(self, request: web.Request) -> web.Response:
        return await self._put(
            request, self._apply_connection, self._connection_view, self._on_connection_saved, "connection"
        )

    async def _put_sets(self, request: web.Request) -> web.Response:
        return await self._put(request, self._apply_sets, self._sets_view, self._on_sets_saved, "sets")

    async def _put(
        self,
        request: web.Request,
        apply: Callable[[Any, LoadStatus, dict[str, Any]], list[Problem]],
        view: Callable[[MonitoringConfig, int], dict[str, Any]],
        callback: Callable[[MonitoringConfig], None] | None,
        what: str,
    ) -> web.Response:
        _user, body, expected = await self._mutation(request)
        if self._closing:
            raise ApiError("shutting_down", 503)
        # The whole save (lock, validation, commit, callback) runs as its own task: if aiohttp
        # cancels this handler, the durable commit and its callback still complete, and the save
        # lock stays held until then.
        task = asyncio.ensure_future(self._save(body, expected, apply, view, callback, what))
        self._saves.add(task)
        task.add_done_callback(self._saves.discard)
        return await asyncio.shield(task)

    @staticmethod
    def _apply_connection(body: Any, _status: LoadStatus, data: dict[str, Any]) -> list[Problem]:
        """Put the body into the document ``data``; returns problems found on the way."""
        if not isinstance(body, dict):
            raise ApiError("invalid_request", field="body")
        unknown = sorted(str(k)[:64] for k in body if k != "connection")
        if unknown:
            raise ApiError("unknown_field", fields=unknown[:10])
        if "connection" not in body:
            raise ApiError("missing_field", fields=["connection"])
        # the body's connection is the complete new object; the document's other keys stay
        data["connection"] = body["connection"]
        return []

    @staticmethod
    def _apply_sets(body: Any, status: LoadStatus, data: dict[str, Any]) -> list[Problem]:
        if not isinstance(body, dict):
            raise ApiError("invalid_request", field="body")
        unknown = sorted(str(k)[:64] for k in body if k != "sets")
        if unknown:
            raise ApiError("unknown_field", fields=unknown[:10])
        if "sets" not in body:
            raise ApiError("missing_field", fields=["sets"])
        if not isinstance(body["sets"], list):
            raise ApiError("invalid_request", field="sets")
        if status is LoadStatus.MISSING:
            raise ApiError("store_missing", 409)
        stored = data.get("parallel_sets")
        stored_ids = {s.get("id") for s in stored if isinstance(s, dict)} if isinstance(stored, list) else set()
        next_id = data.get("next_set_id", 1)
        entries: list[Any] = []
        problems: list[Problem] = []
        for i, entry in enumerate(body["sets"]):
            if isinstance(entry, dict):
                entry = {k: v for k, v in entry.items() if k not in ("active", "reason")}
                if "id" not in entry:
                    entry["id"] = next_id
                    next_id += 1
                elif type(entry["id"]) is int and entry["id"] not in stored_ids:    # other types: model `type`
                    problems.append((f"parallel_sets[{i}].id", "value"))
            entries.append(entry)
        data["parallel_sets"] = entries
        data["next_set_id"] = next_id
        return problems

    # -- cell types, pack assignments, balancer profiles ---------------------------------------

    @staticmethod
    def _cells_view(config: MonitoringConfig, revision: int) -> dict[str, Any]:
        bundle = catalog.current()
        conflicts = config.cell_conflicts()
        cells = [
            {"source": "bundled", "conflict": None, "entry": entry}
            for entry in bundle.entries
            if entry["id"] not in conflicts          # hidden until the own type is renamed
        ]
        cells += [
            {"source": "own", "conflict": "bundled_id" if entry["id"] in conflicts else None, "entry": dict(entry)}
            for entry in config.cell_types
        ]
        return {"revision": revision, "manifest": bundle.manifest, "cells": cells}

    def _live_packs(self) -> dict[int, dict[str, Any]]:
        """The packs of the status snapshot by address (empty before the first poll)."""
        status = self._status_provider()
        packs = status.get("packs") if isinstance(status, dict) else None
        if not isinstance(packs, list):
            return {}
        return {p["address"]: p for p in packs if isinstance(p, dict) and type(p.get("address")) is int}

    def _packs_view(self, config: MonitoringConfig, revision: int) -> dict[str, Any]:
        live = self._live_packs()
        packs = []
        for address in sorted({*config.packs, *range(config.connection.pack_count)}):
            stored = config.packs.get(address)
            now = live.get(address, {})
            cell = None
            if stored is not None and stored.cell is not None:
                item = stored.cell
                found = find_cell(config.cell_types, item.id)
                offer = (
                    changed_fields(item.entry, found[1])
                    if found is not None and item.valid and found[1]["entry_version"] > item.entry_version
                    else None
                )
                cell = {
                    "id": item.id, "entry_version": item.entry_version, "parallel": item.parallel,
                    "entry": dict(item.entry), "valid": item.valid, "source": found[0] if found else None,
                    "update_available": offer is not None, "changed": offer or [],
                }
            assigned = stored is not None and (stored.cell is not None or stored.balancer_profile is not None)
            if not assigned:
                identity = None
            elif stored.serial_mismatch:
                identity = "mismatch"
            else:
                identity = "verified" if stored.bms_serial is not None else "not_verifiable"
            packs.append(
                {
                    "address": address,
                    "configured": address < config.connection.pack_count,
                    "series_count": now.get("cell_count"),
                    "live_bms_serial": now.get("bms_serial"),
                    "cell": cell,
                    "balancer_profile": stored.balancer_profile if stored else None,
                    "bms_serial": stored.bms_serial if stored else None,
                    "serial_mismatch": stored.serial_mismatch if stored else False,
                    "identity": identity,
                }
            )
        return {"revision": revision, "pack_count": config.connection.pack_count, "packs": packs}

    @staticmethod
    def _profiles_view(config: MonitoringConfig, revision: int) -> dict[str, Any]:
        return {"revision": revision, "profiles": [p.to_dict() for p in config.balancer_profiles]}

    async def _get_cells(self, request: web.Request) -> web.Response:
        config, revision = await self._load()
        return json_response(self._cells_view(config, revision))

    async def _get_packs(self, request: web.Request) -> web.Response:
        config, revision = await self._load()
        return json_response(self._packs_view(config, revision))

    async def _get_profiles(self, request: web.Request) -> web.Response:
        config, revision = await self._load()
        return json_response(self._profiles_view(config, revision))

    async def _put_cells(self, request: web.Request) -> web.Response:
        return await self._put(request, self._apply_cells, self._cells_view, self._on_cells_saved, "cells")

    async def _post_rename_cell(self, request: web.Request) -> web.Response:
        return await self._put(request, self._apply_rename, self._cells_view, self._on_cells_saved, "cells")

    async def _put_packs(self, request: web.Request) -> web.Response:
        return await self._put(request, self._apply_packs, self._packs_view, self._on_cells_saved, "cells")

    async def _post_accept_update(self, request: web.Request) -> web.Response:
        return await self._put(request, self._apply_accept, self._packs_view, self._on_cells_saved, "cells")

    async def _post_confirm_identity(self, request: web.Request) -> web.Response:
        return await self._put(request, self._apply_confirm, self._packs_view, self._on_cells_saved, "cells")

    async def _put_profiles(self, request: web.Request) -> web.Response:
        return await self._put(request, self._apply_profiles, self._profiles_view, self._on_cells_saved, "cells")

    @staticmethod
    def _apply_cells(body: Any, status: LoadStatus, data: dict[str, Any]) -> list[Problem]:
        """The whole list of own types. Editing an existing type raises its ``entry_version`` and sets
        ``updated`` (server side); a new id must not be a bundled one."""
        items = _body_value(body, "cells", list, status)
        stored = data.get("cell_types")
        old = {e["id"]: e for e in stored if isinstance(e, dict) and isinstance(e.get("id"), str)} if isinstance(stored, list) else {}
        bundle = catalog.current()
        problems: list[Problem] = []
        entries: list[Any] = []
        for i, raw in enumerate(items):
            if isinstance(raw, dict) and isinstance(raw.get("id"), str):
                before = old.get(raw["id"])
                if before is None:
                    if bundle.get(raw["id"]) is not None:
                        problems.append((f"cell_types[{i}].id", "duplicate"))
                else:
                    ignored = ("entry_version", "updated")
                    if {k: v for k, v in raw.items() if k not in ignored} == {k: v for k, v in before.items() if k not in ignored}:
                        raw = {**raw, "entry_version": before.get("entry_version"), "updated": before.get("updated")}
                    else:
                        version = before.get("entry_version")
                        numeric = isinstance(version, (int, float)) and not isinstance(version, bool)
                        raw = {**raw, "entry_version": int(version) + 1 if numeric else 1,
                               "updated": timeutil.local_now().date().isoformat()}
            entries.append(raw)
        kept = {e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)}      # hashable only
        in_use = [
            {"id": cell_id, "addresses": users}
            for cell_id in old
            if cell_id not in kept and (users := _users(data, lambda p, c=cell_id: _cell_id_of(p) == c))
        ]
        if in_use:
            raise ApiError("in_use", 409, kind="cell_type", items=in_use)
        data["cell_types"] = entries
        return problems

    @staticmethod
    def _apply_rename(body: Any, status: LoadStatus, data: dict[str, Any]) -> list[Problem]:
        """Rename an own type: its id, every pack reference and the id inside those snapshots."""
        if not isinstance(body, dict):
            raise ApiError("invalid_request", field="body")
        unknown = sorted(str(k)[:64] for k in body if k not in ("from", "to"))
        if unknown:
            raise ApiError("unknown_field", fields=unknown[:10])
        missing = [k for k in ("from", "to") if k not in body]
        if missing:
            raise ApiError("missing_field", fields=missing)
        if status is LoadStatus.MISSING:
            raise ApiError("store_missing", 409)
        old_id, new_id = body["from"], body["to"]
        problems: list[Problem] = []
        stored = data.get("cell_types")
        target = next(
            (e for e in stored if isinstance(e, dict) and e.get("id") == old_id), None
        ) if isinstance(stored, list) and isinstance(old_id, str) else None
        if target is None:
            problems.append(("from", "value"))
        pattern = catalog.current().schema["$defs"]["cell"]["properties"]["id"]["pattern"]
        if not isinstance(new_id, str):
            problems.append(("to", "type"))
        elif not re.fullmatch(pattern, new_id):
            problems.append(("to", "format"))
        elif new_id == old_id or catalog.current().get(new_id) is not None or any(
            isinstance(e, dict) and e.get("id") == new_id for e in stored or []
        ):
            problems.append(("to", "duplicate"))
        if problems:
            return problems
        target["id"] = new_id
        packs = data.get("packs")
        for pack in packs.values() if isinstance(packs, dict) else []:
            cell = pack.get("cell") if isinstance(pack, dict) else None
            if isinstance(cell, dict) and cell.get("id") == old_id:
                cell["id"] = new_id
                if isinstance(cell.get("entry"), dict):
                    cell["entry"]["id"] = new_id
        return []

    def _apply_packs(self, body: Any, status: LoadStatus, data: dict[str, Any]) -> list[Problem]:
        """Reference-only patch: a listed address is replaced, an unlisted one stays. The snapshot is
        made only when the cell id changes; the same id keeps the stored one."""
        items = _body_value(body, "packs", dict, status)
        stored = data.get("packs")
        packs: dict[str, Any] = stored if isinstance(stored, dict) else {}
        own = data.get("cell_types") if isinstance(data.get("cell_types"), list) else []
        live = self._live_packs()
        problems: list[Problem] = []
        for key, item in items.items():
            path = f"packs.{key}"
            if not _is_address(key):
                problems.append((path, "value"))
                continue
            if not isinstance(item, dict):
                problems.append((path, "type"))
                continue
            problems.extend((f"{path}.{k}", "unknown_field") for k in item if k not in ("cell", "balancer_profile"))
            absent = [k for k in ("cell", "balancer_profile") if k not in item]
            problems.extend((f"{path}.{k}", "required") for k in absent)
            if absent:
                continue
            before = packs.get(key) if isinstance(packs.get(key), dict) else {}
            old_cell = before.get("cell") if isinstance(before.get("cell"), dict) else None
            cell = None
            reassigned = False
            ref = item["cell"]
            if ref is not None:
                if not isinstance(ref, dict):
                    problems.append((f"{path}.cell", "type"))
                    continue
                problems.extend((f"{path}.cell.{k}", "unknown_field") for k in ref if k not in ("id", "parallel"))
                problems.extend((f"{path}.cell.{k}", "required") for k in ("id", "parallel") if k not in ref)
                if "id" not in ref or "parallel" not in ref:
                    continue
                if not isinstance(ref["id"], str):
                    problems.append((f"{path}.cell.id", "type"))
                    continue
                if old_cell is not None and old_cell.get("id") == ref["id"] and _snapshot_ok(old_cell):
                    # the same type keeps its snapshot, even if the catalog no longer has the entry
                    cell = {**old_cell, "parallel": ref["parallel"]}
                else:
                    found = find_cell(own, ref["id"])
                    if found is None:
                        problems.append((f"{path}.cell.id", "value"))
                        continue
                    entry = cell_validate.normalized(dict(found[1]))
                    cell = {"id": ref["id"], "entry_version": entry["entry_version"], "parallel": ref["parallel"], "entry": entry}
                    reassigned = True
            new = {**before, "cell": cell, "balancer_profile": item["balancer_profile"]}
            had = before.get("cell") is not None or before.get("balancer_profile") is not None
            if cell is None and item["balancer_profile"] is None:
                new["bms_serial"], new["serial_mismatch"] = None, False
                if set(new) <= _PACK_KEYS:
                    packs.pop(key, None)
                    continue
            elif reassigned or not had:
                serial = live.get(int(key), {}).get("bms_serial")
                new["bms_serial"], new["serial_mismatch"] = serial if isinstance(serial, str) else None, False
            packs[key] = new
        data["packs"] = packs
        return problems

    @staticmethod
    def _apply_accept(body: Any, status: LoadStatus, data: dict[str, Any]) -> list[Problem]:
        """Replace the snapshots of the listed packs by the current (own or bundled) entry."""
        addresses = _body_value(body, "addresses", list, status)
        stored = data.get("packs")
        own = data.get("cell_types") if isinstance(data.get("cell_types"), list) else []
        problems: list[Problem] = []
        for i, address in enumerate(addresses):
            pack = stored.get(str(address)) if isinstance(stored, dict) and type(address) is int else None
            cell = pack.get("cell") if isinstance(pack, dict) else None
            found = find_cell(own, cell.get("id")) if isinstance(cell, dict) and isinstance(cell.get("id"), str) else None
            resolved = cell_validate.normalized(dict(found[1])) if found else None      # the raw store may hold 2.0
            version = resolved.get("entry_version") if resolved else None
            if (
                not isinstance(cell, dict) or found is None or not _snapshot_ok(cell)
                or type(version) is not int or version <= cell["entry_version"]
            ):
                problems.append((f"addresses[{i}]", "type" if type(address) is not int else "value"))
                continue
            cell["entry"] = resolved
            cell["entry_version"] = version
        return problems

    def _apply_confirm(self, body: Any, status: LoadStatus, data: dict[str, Any]) -> list[Problem]:
        """Take the serial the pack reports now as the expected one."""
        if not isinstance(body, dict):
            raise ApiError("invalid_request", field="body")
        unknown = sorted(str(k)[:64] for k in body if k != "address")
        if unknown:
            raise ApiError("unknown_field", fields=unknown[:10])
        if "address" not in body:
            raise ApiError("missing_field", fields=["address"])
        if status is LoadStatus.MISSING:
            raise ApiError("store_missing", 409)
        address = body["address"]
        stored = data.get("packs")
        pack = stored.get(str(address)) if isinstance(stored, dict) and type(address) is int else None
        if not isinstance(pack, dict) or (pack.get("cell") is None and pack.get("balancer_profile") is None):
            return [("address", "type" if type(address) is not int else "value")]
        serial = self._live_packs().get(address, {}).get("bms_serial")
        if not isinstance(serial, str):
            raise ApiError("serial_unavailable", 409)
        pack["bms_serial"], pack["serial_mismatch"] = serial, False
        return []

    @staticmethod
    def _apply_profiles(body: Any, status: LoadStatus, data: dict[str, Any]) -> list[Problem]:
        """The whole profile list; ids are assigned here and never reused."""
        items = _body_value(body, "profiles", list, status)
        stored = data.get("balancer_profiles")
        stored_ids = {p.get("id") for p in stored if isinstance(p, dict)} if isinstance(stored, list) else set()
        next_id = data.get("next_profile_id", 1)
        entries: list[Any] = []
        problems: list[Problem] = []
        for i, entry in enumerate(items):
            if isinstance(entry, dict):
                if "id" not in entry:
                    entry = {**entry, "id": next_id}
                    next_id += 1
                elif type(entry["id"]) is int and entry["id"] not in stored_ids:
                    problems.append((f"balancer_profiles[{i}].id", "value"))
            entries.append(entry)
        kept = {e["id"] for e in entries if isinstance(e, dict) and type(e.get("id")) is int}      # hashable only
        in_use = [
            {"id": profile_id, "addresses": users}
            for profile_id in sorted(i for i in stored_ids if type(i) is int)
            if profile_id not in kept and (users := _users(data, lambda p, x=profile_id: p.get("balancer_profile") == x))
        ]
        if in_use:
            raise ApiError("in_use", 409, kind="balancer_profile", items=in_use)
        data["balancer_profiles"] = entries
        data["next_profile_id"] = next_id
        return problems

    async def _save(
        self,
        body: Any,
        expected: int,
        apply: Callable[[Any, LoadStatus, dict[str, Any]], list[Problem]],
        view: Callable[[MonitoringConfig, int], dict[str, Any]],
        callback: Callable[[MonitoringConfig], None] | None,
        what: str,
    ) -> web.Response:
        async with self._save_lock:
            status, _problem, _revision, data = await asyncio.to_thread(self.store.snapshot)
            if status is LoadStatus.CORRUPT:
                raise ApiError("store_corrupt", 409)
            problems = apply(body, status, data)
            candidate = None
            try:
                candidate = MonitoringConfig.from_dict(data)
            except ConfigValidationError as exc:
                problems = list(dict.fromkeys([*problems, *exc.problems]))
            if problems or candidate is None:
                raise ApiError(
                    "invalid_fields",
                    422,
                    fields=[{"path": path, "key": key} for path, key in problems],
                )
            try:
                await asyncio.to_thread(self.store.commit, candidate.to_dict(), expected)
            except StaleRevision as exc:
                raise ApiError("revision_stale", 412, current=exc.current) from None
            except StoreCorrupt:
                raise ApiError("store_corrupt", 409) from None
            except OSError as exc:
                log.error("saving the monitoring configuration failed (%s)", type(exc).__name__)
                raise ApiError("save_failed", 500) from None
            if callback is not None:
                try:
                    callback(candidate)
                except Exception as exc:
                    log.error("%s saved callback failed (%s)", what, type(exc).__name__)
            # the revision this commit produced, never a later read of the store
            return json_response(view(candidate, expected + 1))


class WebThread:
    """Runs a ``WebApp`` on its own event loop in a daemon thread (the main thread owns SIGTERM)."""

    def __init__(self, app: WebApp) -> None:
        self.app = app
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self.port: int | None = None

    def start(self) -> int:
        self._thread = threading.Thread(target=self._run, name="web", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            self._thread.join(5)
            raise self._error
        assert self.port is not None
        return self.port

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            try:
                self.port = loop.run_until_complete(self.app.start())
            except BaseException as exc:
                self._error = exc
                return
            finally:
                self._ready.set()
            loop.run_forever()
        finally:
            loop.close()

    def stop(self, timeout: float = 10.0) -> bool:
        """Shut the server down and join the thread. Returns ``False`` if it did not finish within
        ``timeout``: the thread then completes the cleanup and closes its loop itself."""
        loop, thread = self._loop, self._thread
        if loop is None or thread is None or not thread.is_alive():
            return True
        try:
            loop.call_soon_threadsafe(self._begin_shutdown)
        except RuntimeError:  # the loop closed in the meantime: already stopped
            thread.join(timeout)
            return not thread.is_alive()
        thread.join(timeout)
        if thread.is_alive():
            log.warning("the web server is still shutting down after %.0f s", timeout)
            return False
        return True

    def _begin_shutdown(self) -> None:  # runs on the loop
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.get_running_loop().create_task(self._shutdown())

    async def _shutdown(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await self.app.stop()  # drains running saves, then the HTTP handlers
            await loop.shutdown_asyncgens()
            await loop.shutdown_default_executor()
        except Exception as exc:
            log.warning("web server shutdown failed (%s)", type(exc).__name__)
        finally:
            loop.stop()  # the thread closes the loop after this task finished


def _finalize(request: web.Request, response: web.StreamResponse) -> web.StreamResponse:
    """Security headers on every response, ``no-store`` on the API, ``no-cache`` on the page and its
    static files (the browser revalidates, so an add-on update is not hidden by a cached script)."""
    response.headers.update(_SECURITY_HEADERS)
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _body_value(body: Any, key: str, kind: type, status: LoadStatus) -> Any:
    """The one allowed key of a body, checked for its type."""
    if not isinstance(body, dict):
        raise ApiError("invalid_request", field="body")
    unknown = sorted(str(k)[:64] for k in body if k != key)
    if unknown:
        raise ApiError("unknown_field", fields=unknown[:10])
    if key not in body:
        raise ApiError("missing_field", fields=[key])
    if not isinstance(body[key], kind):
        raise ApiError("invalid_request", field=key)
    if status is LoadStatus.MISSING:
        raise ApiError("store_missing", 409)
    return body[key]


def _is_address(key: Any) -> bool:
    return isinstance(key, str) and key.isascii() and key.isdigit() and str(int(key)) == key and int(key) <= 15


def _cell_id_of(pack: Any) -> Any:
    cell = pack.get("cell") if isinstance(pack, dict) else None
    return cell.get("id") if isinstance(cell, dict) else None


def _users(data: dict[str, Any], matches: Callable[[dict[str, Any]], bool]) -> list[int]:
    """Addresses (any, also above ``pack_count``) of the stored packs for which ``matches`` holds."""
    packs = data.get("packs")
    if not isinstance(packs, dict):
        return []
    return sorted(int(k) for k, p in packs.items() if isinstance(p, dict) and _is_address(k) and matches(p))


def _snapshot_ok(cell: dict[str, Any]) -> bool:
    """The stored snapshot belongs to the reference (id and version)."""
    entry = cell.get("entry")
    return isinstance(entry, dict) and entry.get("id") == cell.get("id") and entry.get("entry_version") == cell.get("entry_version")


def _all_finite(value: Any) -> bool:
    """False if any float in the decoded JSON tree is NaN or infinite (``1e400`` parses to inf).
    Iterative, so deeply nested input cannot exhaust the stack."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return True


def _header(request: web.Request, name: str) -> str | None:
    value = request.headers.get(name, "").strip()
    return value[:128] if value and value.isprintable() else None


def _normalise_ip(value: str) -> str:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return value
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _package_file(parts: list[str]) -> web.Response:
    """A file of the packaged frontend (``seplos_mqtt/web``). Names are validated by the caller;
    nothing outside the package is ever read."""
    node: Any = resources.files("seplos_mqtt.web")
    for part in parts:
        node = node / part
    try:
        data = node.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        raise web.HTTPNotFound from None
    ctype = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
    response = web.Response(body=data, content_type=ctype.split(";")[0])
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        response.charset = "utf-8"
    return response
