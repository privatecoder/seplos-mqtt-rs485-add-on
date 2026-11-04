"""/health endpoint for the Supervisor watchdog (config.yaml `watchdog`)."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("SeplosBMS.health")

PORT = 8080
# requests per pack and poll incl. the 1 s bus gap: 0x42/0x44 (+ 0x5A on RS485-1/2), with slack for retries
SECONDS_PER_PACK_SINGLE = 4
SECONDS_PER_PACK_MULTI = 3
SYSTEM_SECONDS = 3


def max_poll_age(number_of_packs: int, update_interval: int, multi_pack: bool) -> float:
    """Acceptable age of the last successful poll (of any pack), derived from the polling cadence."""
    per_pack = SECONDS_PER_PACK_MULTI if multi_pack else SECONDS_PER_PACK_SINGLE
    cycle = max(number_of_packs, 1) * per_pack + max(update_interval, 0) + (SYSTEM_SECONDS if multi_pack else 0)
    return max(60.0, cycle * 3 + 10.0)


def is_healthy(mqtt_connected: bool, serial_open: bool, poll_age: float, max_age: float) -> bool:
    return mqtt_connected and serial_open and poll_age < max_age


def make_handler(check: Callable[[], bool]) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        timeout = 5

        def do_GET(self):  # noqa: N802 - http.server API
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            ok = check()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok" if ok else b"unhealthy")

        def log_message(self, format, *args):  # noqa: A002 - keep add-on logs clean
            return

    return HealthHandler


def start(check: Callable[[], bool], port: int = PORT) -> ThreadingHTTPServer | None:
    """Bind synchronously (so a port conflict is reported), then serve in a daemon thread."""
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(check))
    except OSError as e:
        logger.error("Health endpoint could not bind port %s: %s (the Supervisor watchdog will restart the add-on)", port, e)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server
