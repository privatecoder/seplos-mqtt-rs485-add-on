"""Removing the legacy connection options: Supervisor client, startup condition, add-on schema."""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import api
from seplos_mqtt import app, supervisor
from test_app_startup import single_pack_bms

ROOT = Path(__file__).resolve().parent.parent
LEGACY = set(supervisor.LEGACY_OPTIONS)


class FakeSupervisor:
    """A tiny local HTTP server that records requests and answers the options endpoints."""

    def __init__(self, options, get_status=200):
        self.options = options
        self.get_status = get_status
        self.requests: list[tuple[str, str, str | None, dict | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
                self._answer(outer.get_status, {"result": "ok", "data": {"options": outer.options}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                self._answer(200, {"result": "ok", "data": {}})

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def supervisor_api():
    servers = []

    def make(options, **kwargs):
        server = FakeSupervisor(options, **kwargs)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


CURRENT = {"mqtt_topic": "seplos", "enable_ha_discovery": True, "ha_discovery_prefix": "homeassistant",
           "invert_ha_dis_charge_measurements": False, "logging_level": "info"}


def test_options_without_legacy_keys_cause_no_post(supervisor_api):
    server = supervisor_api(dict(CURRENT))
    supervisor.clear_legacy_options("tok", server.url)
    assert [r[0] for r in server.requests] == ["GET"]
    assert server.requests[0][1:3] == ("/addons/self/info", "Bearer tok")


def test_legacy_keys_are_removed_and_the_rest_stays_exactly(supervisor_api, caplog):
    legacy = {key: "x" for key in LEGACY}
    server = supervisor_api({**CURRENT, **legacy})
    caplog.set_level(logging.INFO)
    supervisor.clear_legacy_options("tok", server.url)
    assert [r[0] for r in server.requests] == ["GET", "POST"]
    method, path, auth, body = server.requests[1]
    assert (path, auth) == ("/addons/self/options", "Bearer tok")
    assert body == {"options": CURRENT}
    assert "Removed the legacy connection options" in caplog.text


def test_secret_references_survive_the_cleanup(supervisor_api):
    raw = {**CURRENT, "mqtt_topic": "!secret bank_topic", "number_of_packs": 3}
    server = supervisor_api(raw)
    supervisor.clear_legacy_options("tok", server.url)
    assert server.requests[1][3] == {"options": {**CURRENT, "mqtt_topic": "!secret bank_topic"}}


def test_a_single_legacy_key_is_enough(supervisor_api):
    server = supervisor_api({**CURRENT, "number_of_packs": 3})
    supervisor.clear_legacy_options("tok", server.url)
    assert server.requests[1][3] == {"options": CURRENT}


def test_http_error_is_a_warning_not_an_exception(supervisor_api, caplog):
    server = supervisor_api({**CURRENT, "number_of_packs": 3}, get_status=403)
    caplog.set_level(logging.INFO)
    supervisor.clear_legacy_options("tok", server.url)
    assert [r[0] for r in server.requests] == ["GET"]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_unreachable_supervisor_is_a_warning_not_an_exception(caplog):
    caplog.set_level(logging.INFO)
    supervisor.clear_legacy_options("tok", "http://127.0.0.1:1")      # nothing listens there
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_timeout_is_a_warning_not_an_exception(monkeypatch, caplog):
    def slow(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(supervisor.urllib.request, "urlopen", slow)
    caplog.set_level(logging.INFO)
    supervisor.clear_legacy_options("tok")
    assert any("timed out" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- main()


@pytest.fixture
def calls(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(app, "clear_legacy_options", lambda token: seen.append((token, threading.current_thread())))
    monkeypatch.setenv("MONITORING_FILE", str(tmp_path / "monitoring.json"))
    monkeypatch.delenv("INGRESS_PORT", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    return seen


def run(monkeypatch, **kwargs):
    api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=2, startup=True, **kwargs)
    for thread in threading.enumerate():
        if thread.name == "legacy-options":
            thread.join(5)


def tokens(calls):
    return [token for token, _ in calls]


def test_main_clears_after_the_store_was_created(monkeypatch, calls):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    run(monkeypatch)
    assert tokens(calls) == ["tok"]


def test_main_clears_in_a_background_thread(monkeypatch, calls):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    run(monkeypatch)
    thread = calls[0][1]
    assert thread is not threading.main_thread() and thread.daemon


def test_main_clears_for_an_existing_valid_store(monkeypatch, calls, tmp_path):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    run(monkeypatch)                                   # creates the store
    calls.clear()
    run(monkeypatch)
    assert tokens(calls) == ["tok"]


def test_main_does_not_clear_without_a_token(monkeypatch, calls):
    run(monkeypatch)
    assert calls == []


def test_main_does_not_clear_for_a_corrupt_store(monkeypatch, calls, tmp_path):
    (tmp_path / "monitoring.json").write_bytes(b"{garbage")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    run(monkeypatch)
    assert calls == []


# ---------------------------------------------------------------- setup mode


def test_open_failure_starts_the_app_and_waits_for_a_restart(monkeypatch, calls, caplog):
    started = {}

    def fake_start_web(state, store, port):
        started["state"] = state
        state.update_probe = (state.is_healthy(), state.connection_error)
        state.request_restart()                    # a connection saved in the app

    monkeypatch.setattr(app, "start_web", fake_start_web)
    monkeypatch.setenv("INGRESS_PORT", "8099")
    caplog.set_level(logging.INFO)
    client, _ = api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=5, startup=True,
                             serial_error="cannot open /tmp/vcom0: nope")
    state = started["state"]
    assert api.LAST_RUN["exit"] is None                              # no sys.exit
    assert state.update_probe == (True, "cannot open /tmp/vcom0: nope")   # healthy in setup mode
    assert state.packs == [] and state.status["packs"] == []
    assert state.status["connection_error"] == "cannot open /tmp/vcom0: nope"
    assert len(api.LAST_RUN["execs"]) == 1                           # the restart request left the wait loop
    assert sum("No connection to the BMS" in r.getMessage() for r in caplog.records) == 1
    assert {"topic": "seplos/availability", "payload": "offline"}.items() <= client.as_records()[0].items()


def test_status_has_no_connection_error_normally(monkeypatch, calls):
    api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=3, startup=True)
    assert api.LAST_RUN["states"][0].status["connection_error"] is None


def test_overview_shows_the_connection_error_with_a_settings_link():
    ui = ROOT / "src/seplos_mqtt/web/static"
    js = (ui / "overview.js").read_text(encoding="utf-8")
    assert "status.connection_error" in js and "#/settings" in js
    for lang in ("en", "de"):
        texts = json.loads((ui / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "overview.no_connection" in texts and "overview.check_settings" in texts


def test_setup_mode_retries_the_offline_announcement(monkeypatch):
    from fakes import FakeConnection, FakeMqtt
    from seplos_mqtt.mqtt_client import Publisher

    client = FakeMqtt()
    client.fail_next = 1                                   # the first retained offline is rejected
    state = app.AppState(config=api.config())
    state.mqtt = FakeConnection(client)
    state.publisher = Publisher(client)
    steps = {"n": 0}

    def fake_wait(st, _seconds):
        steps["n"] += 1
        if steps["n"] == 3:
            st.mqtt.resync.set()                           # reconnect: announce again
        return steps["n"] >= 5

    monkeypatch.setattr(app, "wait_or_restart", fake_wait)
    with pytest.raises(app.RestartRequested):
        app.wait_in_setup_mode(state)
    sent = [(m.topic, m.payload, m.retain) for m in client.published]
    assert sent == [("seplos/availability", "offline", True)] * 2     # retried once, again after the resync
    assert not state.mqtt.resync.is_set()
    assert all(payload != "online" for _, payload, _ in sent)


def test_setup_mode_has_no_job_queue(monkeypatch, calls):
    seen = {}

    class FakeWeb:
        def __init__(self, *args, jobs, **kwargs):
            seen["jobs"] = jobs

        def start(self):
            return 1

    import seplos_mqtt.web.server as server

    monkeypatch.setattr(server, "WebApp", lambda settings, store, **kw: seen.setdefault("jobs", kw["jobs"]))
    monkeypatch.setattr(server, "WebThread", lambda web_app: type("T", (), {"start": lambda self: 1})())
    state = app.AppState(config=api.config())
    state.jobs = object()
    state.connection_error = "cannot open /tmp/vcom0"
    app.start_web(state, None, "8099")
    assert seen["jobs"] is None
    state.connection_error = None
    seen.clear()
    app.start_web(state, None, "8099")
    assert seen["jobs"] is state.jobs


def test_constructor_errors_of_the_bridge_enter_setup_mode(monkeypatch, tmp_path):
    from seplos_mqtt.bridge import BridgeError

    class Refusing:
        def __init__(self, *args, **kwargs):
            raise BridgeError("invalid gateway host 'x'")

    seen = {}

    def fake_start_web(state, store, port):
        seen["error"] = state.connection_error
        state.request_restart()

    monkeypatch.setattr(app, "SocatBridge", Refusing)
    monkeypatch.setattr(app, "start_web", fake_start_web)
    monkeypatch.setenv("MONITORING_FILE", str(tmp_path / "monitoring.json"))
    monkeypatch.setenv("RS485_REMOTE_IP", "10.0.0.5")
    monkeypatch.setenv("INGRESS_PORT", "8099")
    api.run_main(monkeypatch, single_pack_bms(), number_of_packs=1, telemetry_polls=4, startup=True)
    assert "invalid gateway host" in seen["error"]
    assert api.LAST_RUN["exit"] is None and len(api.LAST_RUN["execs"]) == 1


@pytest.mark.parametrize("host", ["tcp://10.0.0.5", "a/b", "a b", "", "host\n", "[1.2.3.4]", "a]"])
def test_model_and_bridge_agree_on_hosts(host):
    from seplos_mqtt.bridge import BridgeError, SocatBridge
    from seplos_mqtt.monitoring.model import ConfigValidationError, MonitoringConfig

    with pytest.raises(BridgeError):
        SocatBridge(host, 4196)
    with pytest.raises(ConfigValidationError):
        MonitoringConfig.from_dict({"connection": {"transport": "tcp", "host": host}})


@pytest.mark.parametrize("host", ["10.0.0.5", "gw.local", "fe80::1", "[::1]"])
def test_valid_hosts_pass_both(host):
    from seplos_mqtt.bridge import SocatBridge
    from seplos_mqtt.monitoring.model import MonitoringConfig

    SocatBridge(host, 4196)
    MonitoringConfig.from_dict({"connection": {"transport": "tcp", "host": host}})


def test_jobs_unavailable_text_explains_the_missing_connection():
    ui = ROOT / "src/seplos_mqtt/web/static/i18n"
    for lang in ("en", "de"):
        texts = json.loads((ui / f"{lang}.json").read_text(encoding="utf-8"))
        assert texts["err.jobs_unavailable"].startswith(("No connection", "Keine Verbindung"))


# ---------------------------------------------------------------- add-on schema


def _section(text: str, name: str) -> dict[str, str]:
    block = re.search(rf"^{name}:\n((?:[ ]+.*\n|\n)*)", text, re.MULTILINE).group(1)
    return dict(re.findall(r'^  ([a-z0-9_]+):\s*(.*?)\s*$', block, re.MULTILINE))


def test_legacy_options_have_no_default_and_are_optional():
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    options = _section(text, "options")
    schema = _section(text, "schema")
    assert not LEGACY & set(options)
    for key in LEGACY:
        assert schema[key].strip('"').endswith("?"), key
    assert set(options) == {"mqtt_topic", "enable_ha_discovery", "ha_discovery_prefix",
                            "invert_ha_dis_charge_measurements", "logging_level"}


def test_run_sh_exports_legacy_options_only_when_set():
    text = (ROOT / "run.sh").read_text(encoding="utf-8")
    assert "bashio::config.has_value" in text
    for env_name in ("RS485_REMOTE_IP", "NUMBER_OF_PACKS", "MAINT_FIRMWARE", "MQTT_UPDATE_INTERVAL"):
        assert f"export_legacy_option {env_name} " in text
        assert f"export_option {env_name} " not in text
