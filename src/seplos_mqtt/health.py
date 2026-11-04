"""/health endpoint for the Supervisor watchdog (config.yaml `watchdog`)."""
from __future__ import annotations

import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8080


def max_poll_age(number_of_packs: int, update_interval: int) -> int:
    """Acceptable age of the last successful poll, derived from the polling cadence."""
    # The main loop sleeps 1 s after each pack poll, plus an optional pause after a full cycle.
    expected_cycle = number_of_packs * 1 + max(update_interval, 0)
    # a little slack for serial hiccups and startup
    return max(15, int(expected_cycle * 3 + 5))


def make_handler(is_healthy: Callable[[float], bool]) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            ok = is_healthy(time.time())
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok" if ok else b"unhealthy")

        def log_message(self, format, *args):  # noqa: A002 - keep add-on logs clean
            return

    return HealthHandler


def serve(is_healthy: Callable[[float], bool], port: int = PORT) -> None:
    HTTPServer(("0.0.0.0", port), make_handler(is_healthy)).serve_forever()
