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

from ..monitoring.model import ConfigValidationError, MonitoringConfig
from ..monitoring.store import JsonStore, LoadStatus, StaleRevision, StoreCorrupt

log = logging.getLogger(__name__)

SUPERVISOR_PEER = "172.30.32.2"
MAX_BODY = 64 * 1024

_REVISION_RE = re.compile(r'^(?:W/)?"?([0-9]{1,12})"?$')
_STATIC_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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
    ) -> None:
        """``on_connection_saved`` is called on the web event loop after each successful save, in
        commit order, and must return quickly (step 1c hands the work to the main thread)."""
        self.settings = settings
        self.store = store
        self._on_connection_saved = on_connection_saved
        # serialises PUT /api/connection from the corrupt check through callback and response
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

    async def _mutation(self, request: web.Request) -> tuple[User, Any, int]:
        """Common gate of every changing call: user, CSRF, JSON body and revision."""
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

    async def _get_connection(self, request: web.Request) -> web.Response:
        status, _problem, revision, data = await asyncio.to_thread(self.store.snapshot)
        if status is LoadStatus.CORRUPT:
            raise ApiError("store_corrupt", 409)
        if status is LoadStatus.MISSING:
            raise ApiError("store_missing", 409)
        try:
            config = MonitoringConfig.from_dict(data)
        except ConfigValidationError:
            raise ApiError("store_corrupt", 409) from None
        return json_response(self._connection_view(config, revision))

    async def _put_connection(self, request: web.Request) -> web.Response:
        _user, body, expected = await self._mutation(request)
        if self._closing:
            raise ApiError("shutting_down", 503)
        # The whole save (lock, validation, commit, callback) runs as its own task: if aiohttp
        # cancels this handler, the durable commit and its callback still complete, and the save
        # lock stays held until then.
        task = asyncio.ensure_future(self._save(body, expected))
        self._saves.add(task)
        task.add_done_callback(self._saves.discard)
        return await asyncio.shield(task)

    async def _save(self, body: Any, expected: int) -> web.Response:
        async with self._save_lock:
            status, _problem, _revision, data = await asyncio.to_thread(self.store.snapshot)
            if status is LoadStatus.CORRUPT:
                raise ApiError("store_corrupt", 409)
            if not isinstance(body, dict):
                raise ApiError("invalid_request", field="body")
            unknown = sorted(str(k)[:64] for k in body if k != "connection")
            if unknown:
                raise ApiError("unknown_field", fields=unknown[:10])
            if "connection" not in body:
                raise ApiError("missing_field", fields=["connection"])
            # the body's connection is the complete new object; the document's other keys stay
            data["connection"] = body["connection"]
            try:
                candidate = MonitoringConfig.from_dict(data)
            except ConfigValidationError as exc:
                raise ApiError(
                    "invalid_fields",
                    422,
                    fields=[{"path": path, "key": key} for path, key in exc.problems],
                ) from None
            try:
                await asyncio.to_thread(self.store.commit, candidate.to_dict(), expected)
            except StaleRevision as exc:
                raise ApiError("revision_stale", 412, current=exc.current) from None
            except StoreCorrupt:
                raise ApiError("store_corrupt", 409) from None
            except OSError as exc:
                log.error("saving the monitoring configuration failed (%s)", type(exc).__name__)
                raise ApiError("save_failed", 500) from None
            if self._on_connection_saved is not None:
                try:
                    self._on_connection_saved(candidate)
                except Exception as exc:
                    log.error("connection saved callback failed (%s)", type(exc).__name__)
            # the revision this commit produced, never a later read of the store
            return json_response(self._connection_view(candidate, expected + 1))


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
    """Security headers on every response, ``no-store`` on the API."""
    response.headers.update(_SECURITY_HEADERS)
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


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
