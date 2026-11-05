"""Discharge settings P52/P53/P55/P56 are published as magnitudes; measurements and the decoder stay signed."""
import pytest

import api
import frames as f
from seplos_mqtt.decoders import parameters
from test_characterization import _maint_responder, _multi_pack_bms, _single_pack_bms

LF280K = "eve-lf280k-rev-b-2021"
FOUR = ("discharging_overcurrent_alarm_a", "discharging_overcurrent_recovery_a",
        "discharging_overcurrent_protection_a", "transient_overcurrent_protection_a")
MAGNITUDES = {FOUR[0]: 150.0, FOUR[1]: 145.0, FOUR[2]: 160.0, FOUR[3]: 300.0}


def negative_block() -> bytes:
    """A 0x47 answer (with address byte) where P52, P53, P55 and P56 are all negative on the wire."""
    block = bytearray(f.parameter_info())
    for index, value in ((55, -16000), (56, -30000)):
        offset = 2 + 2 * index                         # address byte, word count marker, then u16 words
        block[offset:offset + 2] = (value & 0xFFFF).to_bytes(2, "big")
    return bytes(block)


def test_decoder_stays_faithful_to_the_wire():
    decoded = api.decode_parameters(negative_block())
    assert [decoded[k] for k in FOUR] == [-150.0, -145.0, -160.0, -300.0]


def test_public_view_flips_only_the_four_settings_and_keeps_missing_and_null():
    decoded = {**api.decode_parameters(negative_block()), "charging_low_temperature_alarm_c": -5.0}
    view = parameters.public_view(decoded)
    assert {k: view[k] for k in FOUR} == MAGNITUDES
    assert view["charging_low_temperature_alarm_c"] == -5.0              # no blanket abs()
    assert {k: v for k, v in view.items() if k not in FOUR} == {k: v for k, v in decoded.items() if k not in FOUR}
    assert [decoded[k] for k in FOUR] == [-150.0, -145.0, -160.0, -300.0]    # the input is not modified
    partial = parameters.public_view({FOUR[0]: None, FOUR[1]: -1.5})
    assert partial == {FOUR[0]: None, FOUR[1]: 1.5} and FOUR[2] not in partial
    assert parameters.public_view({}) == {}
    assert parameters.public_view({FOUR[0]: 0.0})[FOUR[0]] == 0.0


def test_ascii_transport_publishes_magnitudes_but_keeps_the_internal_sample_signed(monkeypatch):
    api.no_sleep(monkeypatch)
    bms = _single_pack_bms()
    bms.ascii[(0, 0x47)] = negative_block()
    with api.mode(1):
        pack = api.make_pack(0)
        data, ok = api.poll(pack, bms)
        assert ok
        assert {k: data["parameters"][k] for k in FOUR} == MAGNITUDES
        assert data["parameters"]["cells_in_series"] == 16 and "read_at" in data["parameters"]
        assert [pack.parameter_sample().values[k] for k in FOUR] == [-150.0, -145.0, -160.0, -300.0]


def test_maint_transport_publishes_magnitudes_too(monkeypatch):
    api.no_sleep(monkeypatch)
    responder = _maint_responder(storage=f.maint_storage(locks=(1, 1), cache=0x07))
    with api.mode(2, MAINT_FIRMWARE=True):
        bms = _multi_pack_bms()
        bms.ascii[(0, 0x64)] = responder
        bms.binary[(0, 0x47)] = f.parameter_info(switches=bytes([0x5F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x0F, 0x80]))[1:]
        patched = bytearray(bms.binary[(0, 0x47)])
        for index, value in ((55, -16000), (56, -30000)):
            offset = 1 + 2 * index                      # no address byte on the binary path
            patched[offset:offset + 2] = (value & 0xFFFF).to_bytes(2, "big")
        bms.binary[(0, 0x47)] = bytes(patched)
        bms.binary[(0, 0xA2)] = f.serial_info("BAT-SN-0")
        bms.binary[(0, 0xA4)] = f.serial_info("BMS-SN-0")
        pack = api.make_pack(0)
        api.poll(pack, bms)
        data, ok = api.poll(pack, bms)
        assert ok
        assert {k: data["parameters"][k] for k in FOUR} == MAGNITUDES
        assert [pack.parameter_sample().values[k] for k in FOUR] == [-150.0, -145.0, -160.0, -300.0]


def _check(values: dict, key: str) -> dict:
    entry = next(e for e in api.bundled_cells() if e["id"] == LF280K)           # 280 A continuous discharge
    sample = api.checks_sample(values, at=0.0, max_age=None)
    result = api.parameter_checks(entry, 1, sample, 16, now=0.0)
    return next(c for c in result["checks"] if c["key"] == key)


@pytest.mark.parametrize("key", ["discharging_overcurrent_alarm_a", "discharging_overcurrent_protection_a"])
@pytest.mark.parametrize("amps, result", [(280.0, "ok"), (280.01, "over"), (281.0, "over"), (100.0, "ok")])
def test_grades_are_the_same_at_plus_and_minus_the_threshold_and_the_display_is_a_magnitude(key, amps, result):
    for signed in (amps, -amps):
        check = _check({key: signed}, key)
        assert check["result"] == result, (key, signed)
        assert check["value"] == amps and check["limit"] == 280.0 and check["unit"] == "A"


def test_missing_discharge_setting_stays_not_evaluated():
    check = _check({"discharging_overcurrent_alarm_a": None}, "discharging_overcurrent_alarm_a")
    assert check["result"] == "not_evaluated" and check["value"] is None


def test_p53_and_p56_get_no_new_comparison():
    entry = next(e for e in api.bundled_cells() if e["id"] == LF280K)
    sample = api.checks_sample({k: -999.0 for k in FOUR}, at=0.0, max_age=None)
    keys = {c["key"] for c in api.parameter_checks(entry, 1, sample, 16, now=0.0)["checks"]}
    assert FOUR[1] not in keys and FOUR[3] not in keys


def test_measurements_stay_signed_in_mqtt_whatever_the_ha_option(monkeypatch):
    api.no_sleep(monkeypatch)
    with api.mode(1):
        data, ok = api.poll(api.make_pack(0), _single_pack_bms())
    assert ok
    normal = data["telemetry"]["normal"]
    assert normal["current"] < 0 and normal["dis_charge_current"] < 0 and normal["dis_charge_power"] < 0
    # the option only changes the HA templates of exactly the opted-in entities; the payload is never touched
    plain, plain_client = api.discovery(invert=False)
    inverted, inverted_client = api.discovery(invert=True)
    api.discovery_startup(plain, 1)
    api.discovery_startup(inverted, 1)
    templates = {}
    for label, client in (("plain", plain_client), ("inverted", inverted_client)):
        templates[label] = {r["payload"]["uniq_id"]: r["payload"]["val_tpl"] for r in client.as_records()
                            if r["payload"] and "val_tpl" in r["payload"]}
    flipped = [k for k in templates["plain"] if templates["plain"][k] != templates["inverted"][k]]
    assert flipped and all("* -1" in templates["inverted"][k] and "* -1" not in templates["plain"][k] for k in flipped)
    assert not [k for k in flipped if "overcurrent" in k or "parameters" in k]          # the four settings are not among them
