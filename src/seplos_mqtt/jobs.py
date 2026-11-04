"""Jobs the settings app starts and the main loop thread runs between two pack polls.

The web thread only submits, reads and cancels jobs; the bus is touched by the main loop thread
alone (``take()`` + ``run_job``), so a job never runs concurrently with a poll.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from typing import Any

from .decoders import identity
from .pack import DEVICE_INFO_LEN
from .transport import BusBusy, SerialBus

logger = logging.getLogger("SeplosBMS.jobs")

KEEP_FINISHED = 5
ADDRESSES = range(16)       # DIP addresses 0..15


class JobBusy(RuntimeError):
    """A job is already queued or running."""


class Job:
    """Mutated by the main loop thread, read by the web thread: every access holds the job's lock."""

    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.created = round(time.time(), 3)
        self._lock = threading.Lock()
        self._state = "queued"
        self._progress = {"done": 0, "total": 0}
        self._result: Any = None
        self._error: str | None = None
        self._finished: float | None = None
        self.cancel_event = threading.Event()
        self.scan: DetectScan | None = None      # position of a detection; main loop thread only

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    def update(self, *, done: int | None = None, total: int | None = None, result: Any = None) -> None:
        with self._lock:
            if done is not None:
                self._progress["done"] = done
            if total is not None:
                self._progress["total"] = total
            if result is not None:
                self._result = copy.deepcopy(result)

    def _set_state(self, state: str, *, result: Any = None, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            if result is not None:
                self._result = copy.deepcopy(result)
            self._error = error
            if state in ("done", "failed", "cancelled"):
                self._finished = round(time.time(), 3)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "state": self._state,
                "progress": dict(self._progress),
                "result": copy.deepcopy(self._result),
                "error": self._error,
                "created": self.created,
                "finished": self._finished,
            }


class JobQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._finished_order: list[str] = []

    def submit(self, kind: str) -> Job:
        with self._lock:
            if any(job.state in ("queued", "running") for job in self._jobs.values()):
                raise JobBusy()
            job = Job(kind)
            self._jobs[job.id] = job
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued job at once, ask a running one to stop. False if unknown or finished."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            state = job.state
            if state == "queued":
                job._set_state("cancelled")
                self._remember_finished(job)
                return True
            if state == "running":
                job.cancel_event.set()
                return True
            return False

    def take(self) -> Job | None:
        """Main loop: the next queued job, marked running."""
        with self._lock:
            for job in self._jobs.values():
                if job.state == "queued":
                    job._set_state("running")
                    return job
            return None

    def current(self) -> Job | None:
        """Main loop: the running job (which keeps its position over loop iterations), else the
        next queued one, marked running."""
        with self._lock:
            for job in self._jobs.values():
                if job.state == "running":
                    return job
        return self.take()

    def complete(self, job: Job, state: str, *, result: Any = None, error: str | None = None) -> None:
        with self._lock:
            job._set_state(state, result=result, error=error)
            self._remember_finished(job)

    def abort_active(self, error: str) -> None:
        """The process is going away: finish queued/running jobs as cancelled (partial results stay)."""
        with self._lock:
            for job in list(self._jobs.values()):
                if job.state in ("queued", "running"):
                    job._set_state("cancelled", error=error)
                    self._remember_finished(job)

    def _remember_finished(self, job: Job) -> None:
        self._finished_order.append(job.id)
        while len(self._finished_order) > KEEP_FINISHED:
            self._jobs.pop(self._finished_order.pop(0), None)


class DetectScan:
    """Position and partial result of a running detection (main loop thread only)."""

    def __init__(self, addresses: Any) -> None:
        self.addresses = list(addresses)
        self.next_index = 0
        self.responding: list[dict[str, Any]] = []
        self.busy: list[int] = []
        self.silent: list[int] = []
        self.final: str | None = None         # "done" / "cancelled" once finished

    def result(self) -> dict[str, Any]:
        found = {entry["address"] for entry in self.responding}
        contiguous = 0
        while contiguous in found:            # independent of the probe order
            contiguous += 1
        return {
            "responding": list(self.responding),
            "busy": list(self.busy),
            "silent": list(self.silent),
            "contiguous_count": contiguous,
        }


def step_detect(bus: SerialBus, job: Job, *, addresses: Any = ADDRESSES) -> bool:
    """One step of the read-only pack detection: exactly one ASCII 0x51 request (single attempt)
    for the next DIP address. Returns True when the job is finished (``job.scan.final`` says how).

    A cancel request is honoured before the step. The loop calls this once per iteration, so the
    scan spreads over the normal polling instead of blocking it. It never changes a setting.
    """
    scan = job.scan
    if scan is None:
        scan = job.scan = DetectScan(addresses)
        job.update(done=0, total=len(scan.addresses))
    if scan.next_index >= len(scan.addresses):
        # nothing (left) to probe: an empty list is an empty, finished scan without any transmission
        scan.final = scan.final or "done"
        job.update(done=scan.next_index, result=scan.result())
        return True
    if job.cancel_requested:
        scan.final = "cancelled"
        job.update(result=scan.result())
        return True
    address = scan.addresses[scan.next_index]
    try:
        payload = bus.request(address, 0x51, DEVICE_INFO_LEN, "DeviceInfo", info=b"", retries=1)
    except BusBusy:
        scan.busy.append(address)
    else:
        if payload is None:
            scan.silent.append(address)
        else:
            normal = identity.decode_device_info(payload)["normal"]
            scan.responding.append(
                {"address": address, "device_name": normal["device_name"], "firmware_version": normal["firmware_version"]}
            )
    scan.next_index += 1
    job.update(done=scan.next_index, result=scan.result())
    if scan.next_index >= len(scan.addresses):
        scan.final = "done"
        return True
    return False


def run_detect(bus: SerialBus, job: Job, *, addresses: Any = ADDRESSES) -> tuple[str, dict[str, Any]]:
    """The whole detection in one go (tests, tools): the final job state and the result."""
    while not step_detect(bus, job, addresses=addresses):
        pass
    assert job.scan is not None and job.scan.final is not None
    return job.scan.final, job.scan.result()
