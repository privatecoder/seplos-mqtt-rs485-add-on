"""Ingress web server: peer check, headers, CSRF/mutation gate, connection API, static files."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.request

import pytest
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from seplos_mqtt.monitoring.migration import load_or_migrate
from seplos_mqtt.monitoring.model import MonitoringConfig
from seplos_mqtt.monitoring.store import JsonStore
from seplos_mqtt.web.server import MAX_BODY, WebApp, WebSettings, WebThread, _normalise_ip

LOCAL = WebSettings(host="127.0.0.1", port=0, allowed_peers=frozenset({"127.0.0.1"}))
USER = {"X-Remote-User-Id": "u1", "X-Remote-User-Name": "max", "X-Remote-User-Display-Name": "Max"}
CONN = {"transport": "tcp", "host": "10.0.0.5", "port": 4196, "pack_count": 2}


def make_store(tmp_path, extra=None):
    path = tmp_path / "monitoring.json"
    cfg, store = load_or_migrate(path, {"RS485_REMOTE_IP": "10.0.0.1"})
    assert cfg is not None
    if extra:
        doc = cfg.to_dict()
        doc.update(extra)
        store.commit(doc, store.revision)
    return store


def run_with(app, scenario):
    """Run ``scenario(client)`` against a test server for ``app``."""

    async def main():
        async with TestServer(app.make_app()) as server, TestClient(server) as client:
            return await scenario(client)

    return asyncio.run(main())


async def csrf_of(client, headers=USER):
    res = await client.get("/api/session", headers=headers)
    return (await res.json())["csrf"]


async def put(client, body, *, revision="1", csrf=True, content_type="application/json", user=True, raw=None):
    headers = dict(USER) if user else {}
    if csrf:
        headers["X-CSRF-Token"] = await csrf_of(client)
    if revision is not None:
        headers["If-Match"] = revision
    headers["Content-Type"] = content_type
    data = raw if raw is not None else json.dumps(body)
    return await client.put("/api/connection", data=data, headers=headers)


def assert_secure(res):
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]


# ---------------------------------------------------------------- peer / headers


def test_foreign_peer_forbidden_even_with_valid_headers(tmp_path):
    app = WebApp(WebSettings(host="127.0.0.1", allowed_peers=frozenset({"10.0.0.1"})), make_store(tmp_path))

    async def scenario(client):
        token = hmac.new(app._secret, b"u1", hashlib.sha256).hexdigest()
        headers = {**USER, "X-CSRF-Token": token, "If-Match": "1", "Content-Type": "application/json"}
        res = await client.put("/api/connection", data=json.dumps({"connection": CONN}), headers=headers)
        assert res.status == 403
        assert (await res.json())["error"]["key"] == "forbidden_peer"
        assert_secure(res)
        assert res.headers["Cache-Control"] == "no-store"
        res = await client.get("/api/session", headers=USER)
        assert res.status == 403
        assert (await res.json())["error"]["key"] == "forbidden_peer"
        assert_secure(res)
        assert res.headers["Cache-Control"] == "no-store"
        res = await client.get("/")
        assert res.status == 403
        assert_secure(res)
        assert "Cache-Control" not in res.headers

    run_with(app, scenario)


def test_normalise_ip():
    assert _normalise_ip("::ffff:172.30.32.2") == "172.30.32.2"
    assert _normalise_ip("172.30.32.2") == "172.30.32.2"
    assert _normalise_ip("fe80::1%eth0") == "fe80::1"


def test_security_headers_and_no_store(tmp_path):
    app = WebApp(LOCAL, make_store(tmp_path))

    async def scenario(client):
        ok = await client.get("/api/session")
        assert ok.status == 200
        assert ok.headers["Cache-Control"] == "no-store"
        assert_secure(ok)
        err = await client.put("/api/connection", data="{}", headers={"Content-Type": "application/json"})
        assert err.status == 403
        assert err.headers["Cache-Control"] == "no-store"
        assert_secure(err)
        missing = await client.get("/api/nothing")
        assert missing.status == 404
        assert (await missing.json())["error"]["key"] == "http_404"
        assert missing.headers["Cache-Control"] == "no-store"
        assert_secure(missing)
        page = await client.get("/")
        assert_secure(page)
        assert "Cache-Control" not in page.headers

    run_with(app, scenario)


def test_session_user_and_csrf(tmp_path):
    app = WebApp(LOCAL, make_store(tmp_path))

    async def scenario(client):
        anon = await (await client.get("/api/session")).json()
        assert anon["csrf"] is None
        assert anon["user"] is None
        assert anon["revision"] == 1
        assert anon["store"] == {"status": "valid", "problem": None}
        data = await (await client.get("/api/session", headers=USER)).json()
        assert data["csrf"] == hmac.new(app._secret, b"u1", hashlib.sha256).hexdigest()
        assert data["user"] == {"id": "u1", "name": "max", "display_name": "Max"}

    run_with(app, scenario)


# ---------------------------------------------------------------- connection API


def test_get_connection(tmp_path):
    app = WebApp(LOCAL, make_store(tmp_path))

    async def scenario(client):
        res = await client.get("/api/connection")
        body = await res.json()
        assert res.status == 200
        assert body["revision"] == 1
        assert body["connection"]["host"] == "10.0.0.1"

    run_with(app, scenario)


def test_put_gate_errors(tmp_path):
    app = WebApp(LOCAL, make_store(tmp_path))
    good = {"connection": CONN}

    async def key(res):
        return (await res.json())["error"]["key"]

    async def scenario(client):
        res = await put(client, good, user=False, csrf=False)
        assert (res.status, await key(res)) == (403, "identity_required")
        res = await client.put(
            "/api/connection",
            data=json.dumps(good),
            headers={**USER, "X-CSRF-Token": "wrong", "If-Match": "1", "Content-Type": "application/json"},
        )
        assert (res.status, await key(res)) == (403, "csrf_invalid")
        res = await put(client, good, content_type="text/plain")
        assert (res.status, await key(res)) == (415, "json_required")
        res = await put(client, None, raw="{nope")
        assert (res.status, await key(res)) == (400, "invalid_json")
        res = await put(client, good, revision=None)
        assert (res.status, await key(res)) == (428, "revision_required")
        res = await put(client, good, revision="abc")
        assert (res.status, await key(res)) == (400, "invalid_request")
        res = await put(client, good, revision="7")
        body = await res.json()
        assert (res.status, body["error"]["key"], body["error"]["params"]) == (412, "revision_stale", {"current": 1})
        res = await put(client, {"connection": CONN, "other": 1})
        body = await res.json()
        assert (res.status, body["error"]["key"], body["error"]["params"]) == (400, "unknown_field", {"fields": ["other"]})
        res = await put(client, {})
        assert (res.status, await key(res)) == (400, "missing_field")
        res = await put(client, [1])
        assert (res.status, await key(res)) == (400, "invalid_request")
        res = await put(client, {"connection": {"transport": "tcp", "host": "", "port": 0}})
        body = await res.json()
        assert res.status == 422
        assert body["error"]["key"] == "invalid_fields"
        assert body["error"]["params"]["fields"] == [
            {"path": "connection.host", "key": "format"},
            {"path": "connection.port", "key": "range"},
        ]
        big = {"connection": {**CONN, "pad": "x" * (MAX_BODY + 10)}}
        res = await put(client, big)
        assert res.status == 413
        assert app.store.revision == 1

    run_with(app, scenario)


def test_corrupt_store(tmp_path):
    path = tmp_path / "monitoring.json"
    path.write_bytes(b"{garbage")
    _cfg, store = load_or_migrate(path, {})
    app = WebApp(LOCAL, store)

    async def scenario(client):
        session = await (await client.get("/api/session")).json()
        assert session["store"] == {"status": "corrupt", "problem": "invalid JSON"}
        res = await client.get("/api/connection")
        assert (res.status, (await res.json())["error"]["key"]) == (409, "store_corrupt")
        res = await put(client, {"connection": CONN}, revision="0")
        assert (res.status, (await res.json())["error"]["key"]) == (409, "store_corrupt")

    run_with(app, scenario)
    assert path.read_bytes() == b"{garbage"


def test_successful_put(tmp_path):
    store = make_store(tmp_path, extra={"future": {"x": [1]}})
    saved = []
    app = WebApp(LOCAL, store, on_connection_saved=saved.append)
    revision = store.revision

    async def scenario(client):
        res = await put(client, {"connection": {**CONN, "future_conn_key": 5}}, revision=str(revision))
        body = await res.json()
        assert res.status == 200
        assert body["revision"] == revision + 1
        assert body["connection"]["pack_count"] == 2
        again = await (await client.get("/api/connection")).json()
        assert again == body

    run_with(app, scenario)
    disk = json.loads((tmp_path / "monitoring.json").read_text())
    assert disk["revision"] == revision + 1
    assert disk["data"]["future"] == {"x": [1]}
    assert disk["data"]["connection"]["pack_count"] == 2
    assert disk["data"]["connection"]["future_conn_key"] == 5
    assert len(saved) == 1
    assert isinstance(saved[0], MonitoringConfig)
    assert saved[0].connection.pack_count == 2


def test_raising_callback_still_returns_200(tmp_path):
    store = make_store(tmp_path)
    calls = []

    def boom(cfg):
        calls.append(cfg)
        raise RuntimeError("callback failed")

    app = WebApp(LOCAL, store, on_connection_saved=boom)

    async def scenario(client):
        res = await put(client, {"connection": CONN})
        assert res.status == 200

    run_with(app, scenario)
    assert len(calls) == 1
    assert store.revision == 2


# ---------------------------------------------------------------- static files


def test_static_files(tmp_path):
    app = WebApp(LOCAL, make_store(tmp_path))

    async def scenario(client):
        res = await client.get("/")
        assert res.status == 200
        assert res.headers["Content-Type"].startswith("text/html")
        text = await res.text()
        assert "<title>Seplos BMS</title>" in text
        assert 'src="static/app.js"' in text
        js = await client.get("/static/app.js")
        assert js.status == 200
        assert "javascript" in js.headers["Content-Type"]
        assert "api/session" in await js.text()
        for path in ("/static/../x", "/static/a/b/c/d/e", "/static/missing.js", "/static/.hidden", "/static/static"):
            assert (await client.get(path)).status == 404, path

    run_with(app, scenario)


# ---------------------------------------------------------------- thread


def test_web_thread(tmp_path):
    thread = WebThread(WebApp(LOCAL, make_store(tmp_path)))
    port = thread.start()
    try:
        assert port > 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session", timeout=5) as res:
            assert res.status == 200
            assert json.load(res)["store"]["status"] == "valid"
    finally:
        thread.stop()
    assert thread._thread is not None
    assert not thread._thread.is_alive()


def test_web_thread_bind_error_is_raised(tmp_path):
    first = WebThread(WebApp(LOCAL, make_store(tmp_path)))
    port = first.start()
    try:
        taken = WebSettings(host="127.0.0.1", port=port, allowed_peers=frozenset({"127.0.0.1"}))
        second = WebThread(WebApp(taken, make_store(tmp_path / "other")))
        try:
            second.start()
        except OSError:
            pass
        else:
            raise AssertionError("expected a bind error")
        assert second._thread is not None
        assert not second._thread.is_alive()
    finally:
        first.stop()


# ---------------------------------------------------------------- review fixes


async def wait_set(event):
    while not event.is_set():
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
def test_put_rejects_non_finite_numbers(tmp_path, literal):
    store = make_store(tmp_path)
    saved = []
    app = WebApp(LOCAL, store, on_connection_saved=saved.append)
    path = tmp_path / "monitoring.json"
    before = path.read_bytes()
    raw = '{"connection": {"transport": "tcp", "host": "new", "future": {"deep": [1, ' + literal + "]}}}"

    async def scenario(client):
        res = await put(client, None, raw=raw)
        assert res.status == 400
        assert (await res.json())["error"]["key"] == "invalid_number"
        res = await client.get("/api/connection")
        assert res.status == 200

    run_with(app, scenario)
    assert store.revision == 1
    assert path.read_bytes() == before
    assert saved == []


def test_missing_store(tmp_path):
    store = JsonStore(tmp_path / "monitoring.json")
    saved = []
    app = WebApp(LOCAL, store, on_connection_saved=saved.append)

    async def scenario(client):
        res = await client.get("/api/connection")
        assert (res.status, (await res.json())["error"]["key"]) == (409, "store_missing")
        session = await (await client.get("/api/session")).json()
        assert session["store"]["status"] == "missing"
        assert session["revision"] == 0
        res = await put(client, {"connection": CONN}, revision="0")
        body = await res.json()
        assert res.status == 200
        assert body["revision"] == 1

    run_with(app, scenario)
    assert json.loads((tmp_path / "monitoring.json").read_text())["revision"] == 1
    assert len(saved) == 1


@pytest.mark.parametrize(
    "path",
    ["/static/%2e%2e/server.py", "/static/..%2fserver.py", "/static/%2fetc%2fpasswd", "/static/%2e%2e%2f%2e%2e%2fconfig.py"],
)
def test_encoded_static_traversal(tmp_path, path):
    app = WebApp(LOCAL, make_store(tmp_path))

    async def scenario(client):
        res = await client.get(URL(path, encoded=True))
        assert res.status == 404
        assert "def " not in await res.text()

    run_with(app, scenario)


def test_csrf_token_of_another_user_rejected(tmp_path):
    app = WebApp(LOCAL, make_store(tmp_path))

    async def scenario(client):
        token_a = await csrf_of(client, {"X-Remote-User-Id": "userA"})
        headers = {
            "X-Remote-User-Id": "userB",
            "X-CSRF-Token": token_a,
            "If-Match": "1",
            "Content-Type": "application/json",
        }
        res = await client.put("/api/connection", data=json.dumps({"connection": CONN}), headers=headers)
        assert (res.status, (await res.json())["error"]["key"]) == (403, "csrf_invalid")

    run_with(app, scenario)


def test_save_oserror_maps_to_500(tmp_path, monkeypatch):
    from seplos_mqtt.monitoring import store as store_mod

    store = make_store(tmp_path)
    saved = []
    app = WebApp(LOCAL, store, on_connection_saved=saved.append)
    path = tmp_path / "monitoring.json"
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    async def scenario(client):
        monkeypatch.setattr(store_mod.os, "replace", boom)
        res = await put(client, {"connection": CONN})
        monkeypatch.undo()
        assert (res.status, (await res.json())["error"]["key"]) == (500, "save_failed")

    run_with(app, scenario)
    assert store.revision == 1
    assert path.read_bytes() == before
    assert saved == []


def test_overlapping_saves_are_serialised(tmp_path):
    store = make_store(tmp_path)
    saved = []
    app = WebApp(LOCAL, store, on_connection_saved=lambda c: saved.append(c.connection.pack_count))
    real_commit = store.commit
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow_commit(new, expected):
        real_commit(new, expected)
        calls.append(expected)
        if len(calls) == 1:  # pause the first commit after the store returned
            started.set()
            release.wait(5)

    store.commit = slow_commit

    async def scenario(client):
        a = asyncio.create_task(put(client, {"connection": {**CONN, "pack_count": 2}}, revision="1"))
        await wait_set(started)
        # a read during the paused commit sees a coherent (revision, connection) pair
        got = await (await client.get("/api/connection")).json()
        assert got["revision"] == 2
        assert got["connection"]["pack_count"] == 2
        b = asyncio.create_task(put(client, {"connection": {**CONN, "pack_count": 3}}, revision="2"))
        await asyncio.sleep(0.2)
        assert not b.done()  # waits for a's callback and response
        assert saved == []
        release.set()
        ra, rb = await a, await b
        return await ra.json(), await rb.json()

    body_a, body_b = run_with(app, scenario)
    assert saved == [2, 3]
    assert (body_a["revision"], body_a["connection"]["pack_count"]) == (2, 2)
    assert (body_b["revision"], body_b["connection"]["pack_count"]) == (3, 3)
    disk = json.loads((tmp_path / "monitoring.json").read_text())
    assert disk["revision"] == 3
    assert disk["data"]["connection"]["pack_count"] == 3


# ---------------------------------------------------------------- shutdown


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as res:
        return json.load(res)


# ---------------------------------------------------------------- jobs


async def post(client, path, body=None, *, csrf=True, user=True, content_type="application/json", raw=None):
    headers = dict(USER) if user else {}
    if csrf:
        headers["X-CSRF-Token"] = await csrf_of(client)
    headers["Content-Type"] = content_type
    data = raw if raw is not None else json.dumps({} if body is None else body)
    return await client.post(path, data=data, headers=headers)


async def key_of(res):
    return (await res.json())["error"]["key"]


def job_app(tmp_path, jobs="queue"):
    from seplos_mqtt.jobs import JobQueue

    queue = JobQueue() if jobs == "queue" else None
    return WebApp(LOCAL, make_store(tmp_path), jobs=queue), queue


def test_detect_job_gate(tmp_path):
    app, queue = job_app(tmp_path)

    async def scenario(client):
        res = await post(client, "/api/jobs/detect", user=False, csrf=False)
        assert (res.status, await key_of(res)) == (403, "identity_required")
        res = await client.post(
            "/api/jobs/detect", data="{}", headers={**USER, "X-CSRF-Token": "wrong", "Content-Type": "application/json"}
        )
        assert (res.status, await key_of(res)) == (403, "csrf_invalid")
        res = await post(client, "/api/jobs/detect", content_type="text/plain")
        assert (res.status, await key_of(res)) == (415, "json_required")
        res = await post(client, "/api/jobs/detect", raw="{nope")
        assert (res.status, await key_of(res)) == (400, "invalid_json")
        res = await post(client, "/api/jobs/detect", {"addresses": [1]})
        assert (res.status, await key_of(res)) == (400, "invalid_request")
        res = await post(client, "/api/jobs/detect", raw="[]")
        assert (res.status, await key_of(res)) == (400, "invalid_request")

    run_with(app, scenario)
    assert queue.take() is None                 # nothing was submitted


def test_detect_job_lifecycle(tmp_path):
    app, queue = job_app(tmp_path)

    async def scenario(client):
        res = await post(client, "/api/jobs/detect")
        body = await res.json()
        assert res.status == 202
        job = body["job"]
        assert job["kind"] == "detect" and job["state"] == "queued" and len(job["id"]) == 32
        assert res.headers["Cache-Control"] == "no-store"
        # a second one while the first is queued
        res = await post(client, "/api/jobs/detect")
        assert (res.status, await key_of(res)) == (409, "job_busy")
        # reading needs no identity
        res = await client.get(f"/api/jobs/{job['id']}")
        assert res.status == 200 and (await res.json())["job"]["state"] == "queued"
        # what the main loop thread does
        taken = queue.take()
        queue.complete(taken, "done", result={"responding": []})
        res = await client.get(f"/api/jobs/{job['id']}")
        done = (await res.json())["job"]
        assert done["state"] == "done" and done["result"] == {"responding": []}
        # busy is over, a new one is accepted
        res = await post(client, "/api/jobs/detect")
        assert res.status == 202

    run_with(app, scenario)


def test_job_ids_must_be_known_and_well_formed(tmp_path):
    app, _ = job_app(tmp_path)

    async def scenario(client):
        for job_id in ("0" * 32, "x" * 32, "ABCDEF" + "0" * 26, "abc", "0" * 33):
            res = await client.get(f"/api/jobs/{job_id}")
            assert (res.status, await key_of(res)) == (404, "unknown_job"), job_id
            res = await post(client, f"/api/jobs/{job_id}/cancel")
            assert (res.status, await key_of(res)) == (404, "unknown_job"), job_id

    run_with(app, scenario)


def test_cancel_job(tmp_path):
    app, queue = job_app(tmp_path)

    async def scenario(client):
        job = (await (await post(client, "/api/jobs/detect")).json())["job"]
        # identity / CSRF are required
        res = await post(client, f"/api/jobs/{job['id']}/cancel", user=False, csrf=False)
        assert (res.status, await key_of(res)) == (403, "identity_required")
        res = await client.post(
            f"/api/jobs/{job['id']}/cancel", data="{}",
            headers={**USER, "X-CSRF-Token": "wrong", "Content-Type": "application/json"},
        )
        assert (res.status, await key_of(res)) == (403, "csrf_invalid")
        assert queue.get(job["id"]).state == "queued"
        res = await post(client, f"/api/jobs/{job['id']}/cancel")
        assert res.status == 200 and (await res.json())["job"]["state"] == "cancelled"
        # cancelling again is harmless
        res = await post(client, f"/api/jobs/{job['id']}/cancel")
        assert res.status == 200 and (await res.json())["job"]["state"] == "cancelled"
        # a running job is only asked to stop
        again = (await (await post(client, "/api/jobs/detect")).json())["job"]
        running = queue.take()
        res = await post(client, f"/api/jobs/{again['id']}/cancel")
        assert (await res.json())["job"]["state"] == "running"
        assert running.cancel_requested

    run_with(app, scenario)


def test_job_routes_without_a_queue(tmp_path):
    app, _ = job_app(tmp_path, jobs=None)

    async def scenario(client):
        res = await post(client, "/api/jobs/detect")
        assert (res.status, await key_of(res)) == (503, "jobs_unavailable")
        res = await client.get("/api/jobs/" + "0" * 32)
        assert (res.status, await key_of(res)) == (503, "jobs_unavailable")
        res = await post(client, "/api/jobs/" + "0" * 32 + "/cancel")
        assert (res.status, await key_of(res)) == (503, "jobs_unavailable")
        res = await post(client, "/api/jobs/detect", user=False, csrf=False)      # the gate still comes first
        assert (res.status, await key_of(res)) == (403, "identity_required")

    run_with(app, scenario)


def test_detect_is_refused_while_closing(tmp_path):
    app, queue = job_app(tmp_path)
    app._closing = True

    async def scenario(client):
        res = await post(client, "/api/jobs/detect")
        assert (res.status, await key_of(res)) == (503, "shutting_down")

    run_with(app, scenario)
    assert queue.take() is None


def test_job_routes_from_a_foreign_peer(tmp_path):
    from seplos_mqtt.jobs import JobQueue

    app = WebApp(WebSettings(host="127.0.0.1", allowed_peers=frozenset({"10.0.0.1"})), make_store(tmp_path), jobs=JobQueue())

    async def scenario(client):
        res = await post(client, "/api/jobs/detect", csrf=False)       # no token can be fetched from a foreign peer
        assert (res.status, await key_of(res)) == (403, "forbidden_peer")
        res = await client.get("/api/status")
        assert (res.status, await key_of(res)) == (403, "forbidden_peer")

    run_with(app, scenario)


def test_get_waits_for_paused_adoption(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    app = WebApp(LOCAL, store)
    in_adopt, release = threading.Event(), threading.Event()
    real_adopt = store._adopt

    def slow_adopt(new, revision):
        store.data = {**new}  # data first ...
        in_adopt.set()
        release.wait(5)  # ... revision only after the release
        real_adopt(new, revision)

    monkeypatch.setattr(store, "_adopt", slow_adopt)

    async def scenario(client):
        saver = asyncio.create_task(put(client, {"connection": {**CONN, "pack_count": 2}}, revision="1"))
        await wait_set(in_adopt)
        reader = asyncio.create_task(client.get("/api/connection"))
        await asyncio.sleep(0.3)
        assert not reader.done()  # waits for the store lock
        release.set()
        res = await reader
        await saver
        return await res.json()

    body = run_with(app, scenario)
    assert body["revision"] == 2
    assert body["connection"]["pack_count"] == 2


def test_put_while_closing_is_refused(tmp_path):
    store = make_store(tmp_path)
    saved = []
    app = WebApp(LOCAL, store, on_connection_saved=saved.append)
    app._closing = True

    async def scenario(client):
        res = await put(client, {"connection": CONN})
        assert (res.status, (await res.json())["error"]["key"]) == (503, "shutting_down")
        assert (await client.get("/api/connection")).status == 200  # reading stays available

    run_with(app, scenario)
    assert store.revision == 1
    assert saved == []


def test_slow_commit_survives_aiohttp_cancellation(tmp_path):
    store = make_store(tmp_path)
    saved = []
    app = WebApp(LOCAL, store, on_connection_saved=saved.append)
    real_commit = store.commit
    entered, release = threading.Event(), threading.Event()

    def held(new, expected):
        entered.set()
        release.wait(20)
        real_commit(new, expected)

    store.commit = held
    thread = WebThread(app)
    port = thread.start()
    base = f"http://127.0.0.1:{port}"
    token = _get_json(f"{base}/api/session", USER)["csrf"]
    result = {}

    def client():
        req = urllib.request.Request(
            f"{base}/api/connection",
            data=json.dumps({"connection": CONN}).encode(),
            method="PUT",
            headers={**USER, "X-CSRF-Token": token, "If-Match": "1", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                result["status"] = res.status
        except Exception as exc:  # pragma: no cover - failure path
            result["error"] = repr(exc)

    c = threading.Thread(target=client)
    c.start()
    assert entered.wait(5)
    assert thread.stop(timeout=0.1) is False  # still draining the running save
    time.sleep(4.5)  # longer than aiohttp's shutdown_timeout (3 s) plus a margin
    assert store.revision == 1
    assert saved == []
    release.set()
    c.join(15)
    assert thread.stop(timeout=15) is True
    assert result == {"status": 200}
    assert store.revision == 2
    assert len(saved) == 1  # exactly once
    assert not thread._thread.is_alive()
    assert not asyncio.all_tasks(thread._loop)


def test_stop_waits_for_running_commit(tmp_path):
    store = make_store(tmp_path)
    saved = []
    app = WebApp(LOCAL, store, on_connection_saved=saved.append)
    real_commit = store.commit
    entered, release = threading.Event(), threading.Event()

    def held(new, expected):
        entered.set()
        release.wait(10)
        real_commit(new, expected)

    store.commit = held
    thread = WebThread(app)
    port = thread.start()
    base = f"http://127.0.0.1:{port}"
    token = _get_json(f"{base}/api/session", USER)["csrf"]
    result = {}

    def client():
        req = urllib.request.Request(
            f"{base}/api/connection",
            data=json.dumps({"connection": CONN}).encode(),
            method="PUT",
            headers={**USER, "X-CSRF-Token": token, "If-Match": "1", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                result["status"] = res.status
        except Exception as exc:  # pragma: no cover - failure path
            result["error"] = repr(exc)

    c = threading.Thread(target=client)
    c.start()
    assert entered.wait(5)
    stopped = []
    s = threading.Thread(target=lambda: stopped.append(thread.stop()))
    s.start()
    time.sleep(0.3)
    assert s.is_alive()  # stop is waiting for the commit
    release.set()
    s.join(15)
    c.join(15)
    assert stopped == [True]
    assert result == {"status": 200}
    assert store.revision == 2
    assert len(saved) == 1
    assert not thread._thread.is_alive()
    assert not asyncio.all_tasks(thread._loop)


def test_stop_does_not_wait_for_partial_request(tmp_path):
    app = WebApp(LOCAL, make_store(tmp_path))
    thread = WebThread(app)
    port = thread.start()
    token = _get_json(f"http://127.0.0.1:{port}/api/session", USER)["csrf"]
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        head = (
            "PUT /api/connection HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n"
            f"Content-Type: application/json\r\nX-Remote-User-Id: u1\r\nX-CSRF-Token: {token}\r\n"
            "If-Match: 1\r\n\r\n"
        )
        sock.sendall(head.encode() + b'{"conn')
        time.sleep(0.3)
        started = time.monotonic()
        assert thread.stop(timeout=8.0) is True
        assert time.monotonic() - started < 8.0
    finally:
        sock.close()
    assert not thread._thread.is_alive()
    assert not asyncio.all_tasks(thread._loop)
