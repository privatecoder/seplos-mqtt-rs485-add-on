"""socat bridge from a remote RS485-to-Ethernet gateway to a local pty (owned by Python)."""
from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
import time
from collections.abc import Callable
from typing import Any

from .monitoring.model import TCP_PTY_PATH, valid_gateway_host

logger = logging.getLogger("SeplosBMS")


class BridgeError(RuntimeError):
    pass


def _socat_host(host: str) -> str:
    """The host as socat's tcp address wants it: IPv6 literals in brackets, the rest unchanged."""
    if ":" not in host:
        if "[" in host or "]" in host:
            raise BridgeError(f"invalid gateway host {host!r}")
        return host
    inner = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.IPv6Address(inner)
    except ValueError:
        raise BridgeError(f"invalid gateway host {host!r}") from None
    return f"[{inner}]"


class SocatBridge:
    def __init__(
        self,
        host: str,
        port: int,
        link: str = TCP_PTY_PATH,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        wait_s: float = 5.0,
    ) -> None:
        # the host ends up in a socat address: only plain host names / IP literals
        if not valid_gateway_host(host):
            raise BridgeError(f"invalid gateway host {host!r}")
        self._address = _socat_host(host)
        self.host = host
        self.port = port
        self.link = link
        self._popen = popen
        self._wait_s = wait_s
        self._proc: Any = None

    def start(self) -> None:
        """Spawn socat and wait (bounded) for the pty link. Raises ``BridgeError``."""
        self.stop()
        # A link left behind by a dead socat would make the wait below succeed too early, so it must
        # go before the spawn; anything else at that path is not ours to replace.
        if os.path.islink(self.link):
            try:
                os.unlink(self.link)
            except OSError as e:
                raise BridgeError(f"cannot remove the stale link {self.link}: {e}") from e
        elif os.path.exists(self.link):
            raise BridgeError(f"refusing to replace {self.link}")
        argv = ["socat", f"pty,link={self.link},raw", f"tcp:{self._address}:{self.port},retry,interval=.2,forever"]
        logger.info("Bridging RS485 gateway %s:%s to %s", self.host, self.port, self.link)
        try:
            proc = self._popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise BridgeError(f"could not start socat: {e}") from e
        self._proc = proc
        deadline = time.monotonic() + self._wait_s
        while True:
            if proc.poll() is not None:
                self._proc = None
                raise BridgeError(f"socat exited with code {proc.poll()} before {self.link} appeared")
            if os.path.exists(self.link):
                return
            if time.monotonic() >= deadline:
                self.stop()
                raise BridgeError(f"socat did not create {self.link} within {self._wait_s:g} s (process terminated)")
            time.sleep(0.1)

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        """Terminate socat (kill after 3 s). Idempotent, never raises."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is not None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        except Exception as e:
            logger.warning("Stopping socat failed: %s", e)
