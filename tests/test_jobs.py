"""Job queue, read-only pack detection and the job hand-over to the main loop thread."""
from __future__ import annotations

import threading

import pytest

import api
import frames as f
from fakes import FakeBms, FakeSerial
from seplos_mqtt.jobs import KEEP_FINISHED, JobBusy, JobQueue, run_detect, step_detect
from seplos_mqtt.transport import BusBusy, SerialBus

# ---------------------------------------------------------------- queue


def test_submit_take_complete():
    q = JobQueue()
    job = q.submit("detect")
    assert len(job.id) == 32 and job.kind == "detect"
    assert job.to_dict()["state"] == "queued"
    assert job.to_dict()["progress"] == {"done": 0, "total": 0}
    assert q.get(job.id) is job
    assert q.get("0" * 32) is None
    taken = q.take()
    assert taken is job and job.state == "running"
    assert q.take() is None                     # nothing else queued
    q.complete(job, "done", result={"a": 1})
    d = job.to_dict()
    assert (d["state"], d["result"], d["error"]) == ("done", {"a": 1}, None)
    assert d["finished"] is not None and d["created"] <= d["finished"]


def test_only_one_job_at_a_time():
    q = JobQueue()
    job = q.submit("detect")
    with pytest.raises(JobBusy):
        q.submit("detect")
    q.take()
    with pytest.raises(JobBusy):                # running counts as busy
        q.submit("detect")
    q.complete(job, "failed", error="job_failed")
    assert job.to_dict()["error"] == "job_failed"
    q.submit("detect")                          # free again


def test_cancel_queued_running_and_finished():
    q = JobQueue()
    assert q.cancel("f" * 32) is False
    queued = q.submit("detect")
    assert q.cancel(queued.id) is True
    assert queued.state == "cancelled" and queued.to_dict()["finished"] is not None
    assert q.take() is None                      # a cancelled job is never run
    running = q.submit("detect")
    q.take()
    assert q.cancel(running.id) is True
    assert running.cancel_requested and running.state == "running"   # the job stops itself
    q.complete(running, "cancelled")
    assert q.cancel(running.id) is False         # finished: nothing to cancel


def test_only_the_last_finished_jobs_are_kept():
    q = JobQueue()
    ids = []
    for _ in range(KEEP_FINISHED + 3):
        job = q.submit("detect")
        q.take()
        q.complete(job, "done")
        ids.append(job.id)
    assert [q.get(i) is not None for i in ids] == [False] * 3 + [True] * KEEP_FINISHED


def _race_submit() -> list:
    q = JobQueue()
    barrier = threading.Barrier(2)
    results: list = []

    def worker():
        barrier.wait()
        try:
            results.append(q.submit("detect"))
        except JobBusy:
            results.append("busy")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_two_threads_submit_exactly_one_wins():
    for _ in range(20):
        assert _race_submit().count("busy") == 1


def test_to_dict_is_a_copy():
    q = JobQueue()
    job = q.submit("detect")
    q.take()
    job.update(done=1, total=2, result={"x": [1]})
    d = job.to_dict()
    d["result"]["x"].append(2)
    d["progress"]["done"] = 99
    assert job.to_dict()["result"] == {"x": [1]}
    assert job.to_dict()["progress"] == {"done": 1, "total": 2}


# ---------------------------------------------------------------- detection


def make_bus(bms: FakeBms, cls=SerialBus) -> SerialBus:
    return cls(FakeSerial(bms), clock=lambda: 0.0, sleep=lambda s: None)


def bms_with(*addresses: int) -> FakeBms:
    bms = FakeBms()
    for adr in addresses:
        bms.ascii[(adr, 0x51)] = f.device_info_info(name=f"PACK{adr}")
    return bms


def run(bms: FakeBms, bus=None):
    q = JobQueue()
    job = q.submit("detect")
    q.take()
    final, result = run_detect(bus or make_bus(bms), job)
    return job, final, result


def test_detect_two_packs():
    bms = bms_with(0, 1)
    job, final, result = run(bms)
    assert final == "done"
    assert result["responding"] == [
        {"address": 0, "device_name": "PACK0", "firmware_version": "16.06"},
        {"address": 1, "device_name": "PACK1", "firmware_version": "16.06"},
    ]
    assert result["silent"] == list(range(2, 16))
    assert result["busy"] == []
    assert result["contiguous_count"] == 2
    assert job.to_dict()["progress"] == {"done": 16, "total": 16}
    # read-only: exactly one 0x51 request per address, nothing else, all valid frames
    assert [(r.adr, r.cid2) for r in bms.requests] == [(a, 0x51) for a in range(16)]
    assert not bms.protocol_errors


def test_detect_with_a_gap():
    _, final, result = run(bms_with(0, 2))
    assert final == "done"
    assert [e["address"] for e in result["responding"]] == [0, 2]
    assert result["contiguous_count"] == 1


def test_contiguous_count_does_not_depend_on_the_probe_order():
    q = JobQueue()
    job = q.submit("detect")
    q.take()
    final, result = run_detect(make_bus(bms_with(0, 1)), job, addresses=[1, 0])
    assert final == "done"
    assert result["contiguous_count"] == 2


def test_detect_with_no_addresses_sends_nothing():
    bms = bms_with(0, 1)
    job, final, result = run_empty(bms)
    assert final == "done"
    assert result == {"responding": [], "busy": [], "silent": [], "contiguous_count": 0}
    assert bms.requests == []                              # not a single transmission
    assert job.to_dict()["progress"] == {"done": 0, "total": 0}
    # stepping a finished scan again is harmless and still sends nothing
    assert step_detect(make_bus(bms), job, addresses=[]) is True
    assert bms.requests == []


def run_empty(bms: FakeBms):
    q = JobQueue()
    job = q.submit("detect")
    q.take()
    final, result = run_detect(make_bus(bms), job, addresses=[])
    return job, final, result


def test_detect_without_address_0():
    _, _, result = run(bms_with(1, 2))
    assert result["contiguous_count"] == 0


def test_detect_nothing_answers():
    _, final, result = run(FakeBms())
    assert final == "done"
    assert result == {"responding": [], "busy": [], "silent": list(range(16)), "contiguous_count": 0}


def test_detect_uses_a_single_attempt_per_address(caplog):
    bms = FakeBms()
    run(bms)
    assert len(bms.requests) == 16                       # no retries for silent addresses
    assert "Failed to read" not in caplog.text           # not an error for a probe


def test_detect_cancel_after_three_addresses():
    bms = bms_with(0, 1, 2, 3)
    q = JobQueue()
    job = q.submit("detect")
    q.take()
    inner = bms.ascii[(2, 0x51)]
    bms.ascii[(2, 0x51)] = lambda req: (job.cancel_event.set(), f.ascii_frame(2, 0x00, inner))[1]
    final, result = run_detect(make_bus(bms), job)
    assert final == "cancelled"
    assert [e["address"] for e in result["responding"]] == [0, 1, 2]   # partial result is kept
    assert job.to_dict()["progress"]["done"] == 3
    assert len(bms.requests) == 3                                    # nothing sent after the cancel
    assert job.to_dict()["result"]["contiguous_count"] == 3


def test_detect_bus_busy_is_recorded_and_the_scan_continues():
    class Busy(SerialBus):
        def request(self, address, *args, **kwargs):
            if address == 3:
                raise BusBusy("no quiet gap")
            return super().request(address, *args, **kwargs)

    bms = bms_with(0, 1, 2, 3, 4)
    _, final, result = run(bms, make_bus(bms, Busy))
    assert final == "done"
    assert result["busy"] == [3]
    assert [e["address"] for e in result["responding"]] == [0, 1, 2, 4]
    assert 3 not in result["silent"]
    assert result["contiguous_count"] == 3


def test_request_retries_parameter():
    bms = FakeBms()
    bus = make_bus(bms)
    assert bus.request(0, 0x51, 32, "DeviceInfo", info=b"", retries=1) is None
    assert len(bms.requests) == 1
    bms2 = FakeBms()
    assert make_bus(bms2).request(0, 0x51, 32, "DeviceInfo", info=b"") is None
    assert len(bms2.requests) == SerialBus.FRAME_READ_RETRIES      # the default is unchanged


# ---------------------------------------------------------------- main loop


def single_bms() -> FakeBms:
    bms = FakeBms()
    bms.ascii[(0, 0x42)] = f.telemetry_info(adr=0)
    bms.ascii[(0, 0x44)] = f.telesignal_info(adr=0)
    bms.ascii[(0, 0x51)] = f.device_info_info()
    return bms


def probe_indices(bms: FakeBms) -> list[int]:
    """Positions of the 16 detection probes: the last 0x51 requests (the pack's own device info and
    patch probing come first, during its first poll)."""
    return [i for i, r in enumerate(bms.requests) if r.cid2 == 0x51][-16:]


def test_a_submitted_job_is_spread_over_the_polling(monkeypatch):
    bms = single_bms()
    client, _ = api.run_main(monkeypatch, bms, number_of_packs=1, telemetry_polls=40, job_after_polls=2)
    d = api.LAST_RUN["job"].to_dict()
    assert d["state"] == "done"
    assert d["progress"] == {"done": 16, "total": 16}
    assert [e["address"] for e in d["result"]["responding"]] == [0]
    assert d["result"]["silent"] == list(range(1, 16))
    assert d["result"]["contiguous_count"] == 1
    probes = probe_indices(bms)
    assert [bms.requests[i].adr for i in probes] == list(range(16))
    # one probe per loop iteration: a normal poll (0x42) lies between any two probes ...
    for a, b in zip(probes, probes[1:], strict=False):
        assert any(r.cid2 == 0x42 for r in bms.requests[a + 1:b])
    # ... and the polling goes on after the last one
    assert any(r.cid2 == 0x42 for r in bms.requests[probes[-1] + 1:])
    assert [r for r in client.as_records() if r["topic"] == "seplos/pack-0/sensors"]
    assert api.LAST_RUN["exit"] is None


def _health_recorder(monkeypatch, probe_cost_ticks: int = 3) -> list[bool]:
    """Record is_healthy() after every loop iteration; each probe costs `probe_cost_ticks` x 0.5 s of fake time."""
    import time

    from seplos_mqtt import app

    health: list[bool] = []
    real_update, real_step = app.update_status, app.step_detect

    def update(state):
        real_update(state)
        health.append(state.is_healthy())

    def step(bus, job, **kwargs):
        finished = real_step(bus, job, **kwargs)
        for _ in range(probe_cost_ticks):
            time.monotonic()                  # the fake clock advances 0.5 s per call
        return finished

    monkeypatch.setattr(app, "update_status", update)
    monkeypatch.setattr(app, "step_detect", step)
    return health


def test_health_stays_ok_during_a_slow_scan(monkeypatch):
    health = _health_recorder(monkeypatch)                      # 1.5 s per probe
    api.run_main(monkeypatch, single_bms(), number_of_packs=1, telemetry_polls=40, job_after_polls=2)
    assert api.LAST_RUN["job"].state == "done"
    assert len(health) >= 18 and all(health)


def test_a_real_loss_of_core_data_during_a_scan_is_still_unhealthy(monkeypatch):
    bms = single_bms()
    good, calls = f.telemetry_info(adr=0), {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return f.ascii_frame(0, 0x00, good) if calls["n"] <= 3 else b""     # the pack stops answering

    bms.ascii[(0, 0x42)] = flaky
    health = _health_recorder(monkeypatch)
    api.run_main(monkeypatch, bms, number_of_packs=1, telemetry_polls=120, job_after_polls=1)
    assert all(health[:2])
    assert not health[-1]                                                    # no core data for > 60 s
    assert api.LAST_RUN["job"].to_dict()["progress"]["done"] > 3             # the scan was running meanwhile


def test_bus_busy_on_one_address_does_not_stop_scan_or_polling(monkeypatch):
    real = SerialBus.request

    def patched(self, address, cid2, *args, **kwargs):
        if cid2 == 0x51 and address == 3:
            raise BusBusy("no quiet gap")
        return real(self, address, cid2, *args, **kwargs)

    monkeypatch.setattr(SerialBus, "request", patched)
    bms = single_bms()
    api.run_main(monkeypatch, bms, number_of_packs=1, telemetry_polls=40, job_after_polls=2)
    d = api.LAST_RUN["job"].to_dict()
    assert d["state"] == "done" and d["progress"]["done"] == 16
    assert d["result"]["busy"] == [3] and 3 not in d["result"]["silent"]
    assert sum(1 for r in bms.requests if r.cid2 == 0x42) >= 16             # polling went on throughout


def test_a_restart_during_the_scan_stops_it_at_once(monkeypatch):
    bms = single_bms()
    bms.ascii[(3, 0x51)] = lambda req: (
        api.LAST_RUN["states"][0].request_restart(),
        f.ascii_frame(3, 0x00, f.device_info_info()),
    )[1]
    client, port = api.run_main(monkeypatch, bms, number_of_packs=1, telemetry_polls=60, job_after_polls=2)
    d = api.LAST_RUN["job"].to_dict()
    assert d["state"] == "cancelled" and d["error"] == "restarting"
    assert d["progress"]["done"] == 4
    assert [e["address"] for e in d["result"]["responding"]] == [0, 3]       # the partial result is kept
    assert d["result"]["silent"] == [1, 2]
    assert bms.requests[-1].adr == 3 and bms.requests[-1].cid2 == 0x51       # no probe of 4, no further poll
    assert not any(r.adr == 4 for r in bms.requests)
    assert len(api.LAST_RUN["execs"]) == 1                                   # cleanup + exactly one exec
    assert any(r["topic"] == "seplos/availability" and r["payload"] == "offline" for r in client.as_records())
    assert port.is_open is False


def test_sigterm_during_a_scan_leaves_the_partial_result(monkeypatch):
    bms = single_bms()
    api.run_main(monkeypatch, bms, number_of_packs=1, telemetry_polls=60, job_after_polls=2, signal_after_polls=6)
    d = api.LAST_RUN["job"].to_dict()
    assert d["state"] == "cancelled" and d["error"] == "shutting_down"
    assert 0 < d["progress"]["done"] < 16
    assert api.LAST_RUN["execs"] == [] and api.LAST_RUN["exit"] == 0


def test_a_job_submitted_over_http_runs_on_the_loop_thread(monkeypatch, tmp_path):
    import json
    import urllib.request

    from seplos_mqtt.web import server

    monkeypatch.setattr(server, "SUPERVISOR_PEER", "127.0.0.1")      # the default allowed peer of WebSettings
    monkeypatch.setenv("MONITORING_FILE", str(tmp_path / "monitoring.json"))
    monkeypatch.setenv("INGRESS_PORT", "0")
    monkeypatch.delenv("RS485_REMOTE_IP", raising=False)
    loop_thread = threading.get_ident()                              # run_main runs main() on this thread
    seen: list[int] = []
    posted: dict = {}
    bms = single_bms()

    def prober(adr):
        def respond(req):
            seen.append(threading.get_ident())
            return f.ascii_frame(adr, 0x00, f.device_info_info()) if adr == 0 else b""
        return respond

    for adr in range(16):
        bms.ascii[(adr, 0x51)] = prober(adr)

    def submit_over_http():
        web = api.LAST_RUN["states"][0].web
        base, user = f"http://127.0.0.1:{web.port}", {"X-Remote-User-Id": "u1"}
        with urllib.request.urlopen(urllib.request.Request(f"{base}/api/session", headers=user), timeout=5) as res:
            token = json.load(res)["csrf"]
        req = urllib.request.Request(
            f"{base}/api/jobs/detect", data=b"{}", method="POST",
            headers={**user, "X-CSRF-Token": token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as res:
            posted["status"], posted["job"] = res.status, json.load(res)["job"]["id"]
        posted["thread"] = threading.get_ident()

    good, calls = f.telemetry_info(adr=0), {"n": 0}

    def telemetry(req):
        calls["n"] += 1
        if calls["n"] == 3:
            t = threading.Thread(target=submit_over_http)
            t.start()
            t.join(10)
        return f.ascii_frame(0, 0x00, good)

    bms.ascii[(0, 0x42)] = telemetry
    api.run_main(monkeypatch, bms, number_of_packs=1, telemetry_polls=40, startup=True)
    assert posted["status"] == 202 and posted["thread"] != loop_thread
    job = api.LAST_RUN["states"][0].jobs.get(posted["job"])
    assert job.state == "done"
    assert len(seen) >= 17                                           # the pack's own 0x51 requests + 16 probes
    assert set(seen) == {loop_thread}                                # every bus request on the main loop thread


def test_an_unknown_job_kind_fails_cleanly(monkeypatch):
    from seplos_mqtt import app

    state = app.AppState(config=api.config())
    state.jobs = JobQueue()
    job = state.jobs.submit("format-disk")
    state.jobs.take()
    app.run_job(state, job)
    assert job.to_dict()["state"] == "failed" and job.to_dict()["error"] == "unknown_job_kind"


def test_a_crashing_job_is_marked_failed(monkeypatch):
    from seplos_mqtt import app

    def boom(bus, job):
        raise RuntimeError("bus exploded")

    monkeypatch.setattr(app, "step_detect", boom)
    state = app.AppState(config=api.config())
    state.jobs = JobQueue()
    job = state.jobs.submit("detect")
    state.jobs.take()
    app.run_job(state, job)
    assert job.to_dict()["state"] == "failed" and job.to_dict()["error"] == "job_failed"
