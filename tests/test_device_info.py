"""HA device info: product facts on physical packs, honest virtual devices, observed BMS serial."""
import inspect

import api
from seplos_mqtt import app
from seplos_mqtt.monitoring.model import ParallelSet


def set_item():
    return ParallelSet(id=2, name="Left", packs=(0, 1), wiring="diagonal", plus=0, minus=1)


PHYSICAL = {"mf": "Seplos", "mdl": "BMS 2.0 / EMU1101", "mdl_id": "1101-10E", "hw": "V16"}


def device_blocks(client, node):
    """The full device block of the configs of one node (those carrying more than the ids)."""
    return [r["payload"]["dev"] for r in client.as_records()
            if f"/{node}/" in r["topic"] and r["payload"] and set(r["payload"]["dev"]) != {"ids"}]


def test_physical_pack_device_carries_the_product_facts_and_no_firmware_guess():
    disc, client = api.discovery()
    disc.publish_pack(0, pack_bus=True)
    block = device_blocks(client, "seplos-mqtt-pack-0")[0]
    assert block == {**PHYSICAL, "sw": "", "name": "Seplos BMS Pack-0 (Master)", "ids": "seplos_bms_pack_0"}
    assert "sn" not in block


def test_slave_pack_device_keeps_its_name_and_via_device():
    disc, client = api.discovery()
    disc.publish_pack(1, pack_bus=False)
    block = device_blocks(client, "seplos-mqtt-pack-1")[0]
    assert block["name"] == "Seplos BMS Pack-1 (Slave)" and block["via_device"] == "seplos_bms_pack_0"
    assert {k: block[k] for k in PHYSICAL} == PHYSICAL


def test_real_firmware_replaces_the_empty_version():
    disc, client = api.discovery()
    disc.publish_pack(0, pack_bus=True)
    client.published.clear()
    api.firmware_refresh(disc, 0, {"normal": {"firmware_version": "16.06", "firmware_patch": "16.06.13+"}})
    assert device_blocks(client, "seplos-mqtt-pack-0")[0]["sw"] == "16.06.13+"


def test_serial_is_the_battery_serial_as_reported_and_republished_once():
    disc, client = api.discovery()
    disc.publish_pack(0, pack_bus=True)
    info = {"normal": {"firmware_version": "16.06", "firmware_patch": "16.06.13+"}}
    api.firmware_refresh(disc, 0, info)                       # no serial yet
    assert "sn" not in device_blocks(client, "seplos-mqtt-pack-0")[-1]
    client.published.clear()
    disc.update_firmware(0, "16.06.13+", "  SEPLOS-1  ")
    blocks = device_blocks(client, "seplos-mqtt-pack-0")
    assert len(blocks) == 1 and blocks[0]["sn"] == "SEPLOS-1"
    client.published.clear()
    disc.update_firmware(0, "16.06.13+", "SEPLOS-1")          # unchanged: nothing is sent
    assert client.as_records() == []
    disc.update_firmware(0, "16.06.13+", "SEPLOS-2")          # changed: sent again
    assert device_blocks(client, "seplos-mqtt-pack-0")[0]["sn"] == "SEPLOS-2"


def test_blank_serial_is_not_published():
    disc, client = api.discovery()
    disc.publish_pack(0, pack_bus=True)
    disc.update_firmware(0, "16.06", "   ")
    assert all("sn" not in b for b in device_blocks(client, "seplos-mqtt-pack-0"))


def test_placeholder_and_duplicate_serials_are_published_as_reported():
    disc, client = api.discovery()
    for pack_no in (0, 1):
        disc.publish_pack(pack_no, pack_bus=True)
        disc.update_firmware(pack_no, "16.06", "00000000000000000000")
    assert device_blocks(client, "seplos-mqtt-pack-0")[-1]["sn"] == "00000000000000000000"
    assert device_blocks(client, "seplos-mqtt-pack-1")[-1]["sn"] == "00000000000000000000"


def retained(client):
    """What the broker holds: the last payload per topic (empty payload = cleared)."""
    topics = {}
    for record in client.as_records():
        topics[record["topic"]] = record["payload"]
    return {t: p for t, p in topics.items() if p}


def registry(configs):
    """HA applies the device fields each config supplies, in the order it processes them."""
    devices = {}
    for config in configs:
        dev = dict(config["dev"])
        devices.setdefault(dev["ids"], {}).update(dev)
    return devices


def test_retained_snapshot_has_one_full_block_per_device_and_any_replay_order_agrees():
    disc, client = api.discovery(maint_enabled=True)
    disc.publish_pack(0, pack_bus=True)
    disc.publish_pack(1, pack_bus=True)
    disc.publish_system()
    disc.publish_set(set_item())
    disc.update_firmware(0, "16.06.13", "BAT-A")
    disc.update_firmware(1, "16.06", "BAT-1")
    disc.set_capabilities(0, {"maint"}, pack_bus=True)                  # capability republish
    disc.update_cell_count(0, 15)
    disc.publish_balancer(0)                                           # conditional discovery
    disc.update_firmware(0, "16.06.13", "BAT-B")                       # later serial change
    disc.update_firmware(0, "16.06.14", "BAT-B")                       # and firmware change
    disc.publish_pack(0, pack_bus=True)                                # resync
    snapshot = retained(client)
    configs = list(snapshot.values())
    full = {}
    for topic, config in snapshot.items():
        if "name" in config["dev"]:
            full.setdefault(config["dev"]["ids"], []).append(topic)
    assert sorted(full) == ["seplos_bms_pack_0", "seplos_bms_pack_1", "seplos_bms_set_2", "seplos_bms_system"]
    assert all(len(topics) == 1 for topics in full.values())           # exactly one full block per device
    forward, backward = registry(configs), registry(reversed(configs))
    assert forward == backward
    assert forward["seplos_bms_pack_0"]["sw"] == "16.06.14" and forward["seplos_bms_pack_0"]["sn"] == "BAT-B"
    assert forward["seplos_bms_pack_1"]["sn"] == "BAT-1"
    assert forward["seplos_bms_system"]["sw"] == "" and "sn" not in forward["seplos_bms_system"]


def test_a_later_blank_serial_keeps_the_last_one():
    disc, client = api.discovery()
    disc.publish_pack(0, pack_bus=True)
    disc.update_firmware(0, "16.06", "BAT-1")
    client.published.clear()
    disc.update_firmware(0, "16.06", " ")
    disc.update_firmware(0, "16.06", None)
    assert client.as_records() == []


def test_bms_serial_never_reaches_the_device_info():
    source = inspect.getsource(app.poll_pack)
    assert '"serial_number"' in source
    assert source.count("bms_serial_number") == 0


def test_system_and_set_devices_are_virtual_and_do_not_copy_firmware():
    disc, client = api.discovery()
    api.discovery_startup(disc, 2)
    api.firmware_refresh(disc, 0, {"normal": {"firmware_version": "16.06", "firmware_patch": "16.06.13+"}})
    disc.update_firmware(0, "16.06.13+", "SEPLOS-1")
    system = device_blocks(client, "seplos-mqtt-system")
    assert system and all(b == {"mf": "Seplos", "mdl": "Bank aggregate (virtual)", "hw": "", "sw": "",
                                "name": "Seplos BMS System", "ids": "seplos_bms_system",
                                "via_device": "seplos_bms_pack_0"} for b in system)
