"""场景配置读取、校验与设备工厂注册测试。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim2030.contracts import Capability, DeviceSpec, LinkSpec, ScenarioConfig
from sim2030.scenario import (
    DEVICE_TYPES,
    create_device,
    load_scenario,
    register_device,
    validate_scenario,
)

SCENARIO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "substation.json")
LIVE_SCENARIO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scenarios", "substation-live.json")


def make_config() -> ScenarioConfig:
    devices = [
        DeviceSpec(device_id="brk", device_type="switch", layer="process", ports={"control": "control"},
                   capabilities=[Capability(action_type="close"), Capability(action_type="open")]),
        DeviceSpec(device_id="s1", device_type="sensor", layer="process", ports={"up": "up"}),
    ]
    links = [
        LinkSpec(link_id="l1", endpoint_a=("s1", "up"), endpoint_b=("brk", "control"),
                 link_type="hardwire", parameters={"delay_us": 0}),
    ]
    return ScenarioConfig(scenario_id="s", simulation={"dt_us": 100000, "duration_us": 1000000},
                          devices=devices, links=links)


class TestLoadScenario(unittest.TestCase):
    def test_load_substation_scenario(self):
        config = load_scenario(SCENARIO_PATH)
        self.assertEqual(config.scenario_id, "substation-001")
        self.assertEqual(len(config.devices), 15)
        self.assertEqual(len(config.links), 17)
        self.assertTrue(all(isinstance(d, DeviceSpec) for d in config.devices))
        self.assertTrue(all(isinstance(l, LinkSpec) for l in config.links))

    def test_load_resolves_relative_paths(self):
        config = load_scenario(SCENARIO_PATH)
        schema_path = config.observations.get("schema_path", "")
        self.assertTrue(os.path.isabs(schema_path))
        self.assertTrue(schema_path.endswith("monitoring-event.schema.json"))

    def test_load_live_scenario(self):
        config = load_scenario(LIVE_SCENARIO_PATH)
        self.assertEqual(config.scenario_id, "substation-live")
        self.assertEqual(config.name, "变电站场景")
        self.assertEqual(len(config.devices), 21)
        self.assertEqual(len(config.links), 28)

    def test_load_live_scenario_external_layer(self):
        """外界层应包含授时卫星与其他电站，且均通过无线链路接入站内。"""
        config = load_scenario(LIVE_SCENARIO_PATH)
        external = [d for d in config.devices if d.layer == "external"]
        self.assertEqual({d.device_id for d in external}, {"gnss_sat", "peer_station"})
        self.assertEqual({d.device_type for d in external}, {"time_satellite", "remote_substation"})
        self.assertEqual(validate_scenario(config), [])

        wireless = [link for link in config.links if link.link_type == "wireless"]
        self.assertEqual(
            {link.link_id for link in wireless},
            {"L-gnss-time_svc", "L-gnss-time_svc_2", "L-peer-station"},
        )
        endpoints = {frozenset((link.endpoint_a[0], link.endpoint_b[0])) for link in wireless}
        self.assertEqual(endpoints, {
            frozenset(("gnss_sat", "time_svc")),
            frozenset(("gnss_sat", "time_svc_2")),
            frozenset(("peer_station", "station")),
        })

    def test_live_scenario_carries_layout(self):
        """拓扑版式应随场景下发：分层带几何 + 每台设备坐标。"""
        config = load_scenario(LIVE_SCENARIO_PATH)
        layers = config.layout.get("layers") or []
        self.assertTrue(layers)
        for layer in layers:
            for key in ("x", "y", "width", "height"):
                self.assertIsInstance(layer.get(key), (int, float))
        for device in config.devices:
            self.assertIsInstance(device.layout.get("x"), (int, float))
            self.assertIsInstance(device.layout.get("y"), (int, float))

    def test_live_scenario_second_timing_chain(self):
        """复数授时系统与合并单元：第二条链路与首条并列成列，授时卫星居中接入两者。"""
        config = load_scenario(LIVE_SCENARIO_PATH)
        devices = {d.device_id: d for d in config.devices}
        for device_id in ("time_svc_2", "mu_2", "ct_current_2"):
            self.assertIn(device_id, devices)

        column_x = {devices[device_id].layout["x"] for device_id in ("time_svc_2", "mu_2", "ct_current_2")}
        self.assertEqual(len(column_x), 1, "新增授时链路的三台设备必须竖直成列")
        self.assertGreater(column_x.pop(), devices["time_svc"].layout["x"], "新列应位于既有授时列右侧")

        # 授时卫星移到两套授时系统中间，便于对两者广播同一时间基准
        self.assertEqual(
            devices["gnss_sat"].layout["x"],
            (devices["time_svc"].layout["x"] + devices["time_svc_2"].layout["x"]) / 2,
        )

        links = {link.link_id: link for link in config.links}
        self.assertEqual(links["L-gnss-time_svc_2"].link_type, "wireless")
        self.assertEqual(links["L-gnss-time_svc_2"].endpoint_a[0], "gnss_sat")
        self.assertEqual(links["L-gnss-time_svc_2"].endpoint_b[0], "time_svc_2")
        self.assertEqual(links["L-time_svc_2-mu_2"].endpoint_a[0], "time_svc_2")
        self.assertEqual(links["L-time_svc_2-mu_2"].endpoint_b[0], "mu_2")
        self.assertEqual(links["L-ct_current_2-mu_2"].endpoint_a[0], "ct_current_2")
        self.assertEqual(links["L-ct_current_2-mu_2"].endpoint_b[0], "mu_2")

        # 第二套合并单元与首套行为一致：量测同时上送测控装置与过流保护装置。
        self.assertEqual(links["L-mu_2-mc"].endpoint_a[0], "mu_2")
        self.assertEqual(links["L-mu_2-mc"].endpoint_b[0], "mc")
        self.assertEqual(links["L-mu_2-prot"].endpoint_a[0], "mu_2")
        self.assertEqual(links["L-mu_2-prot"].endpoint_b[0], "prot")

        process = next(n for n in config.layout["deployment"]["networks"] if n["id"] == "process")
        mu_2_access = next(c for c in process["connections"] if c["device_id"] == "mu_2")
        self.assertEqual(mu_2_access["business_ports"], ["out"])
        self.assertIn("L-mu_2-mc", process["link_ids"])
        self.assertIn("L-mu_2-prot", process["link_ids"])

    def test_live_scenario_cooling_column_at_process_right(self):
        """冷却接口与冷却风机移到过程层最右列，第二套授时/采集列仍竖直成列。"""
        config = load_scenario(LIVE_SCENARIO_PATH)
        devices = {d.device_id: d for d in config.devices}
        rightmost = max(d.layout["x"] for d in config.devices if d.layer == "process")
        for device_id in ("ctl_cool", "cool"):
            self.assertEqual(devices[device_id].layout["x"], rightmost, "冷却支路应位于过程层最右列")
        self.assertGreater(devices["cool"].layout["y"], devices["ctl_cool"].layout["y"],
                           "冷却风机应排在冷却系统控制接口下方")

        # 冷却支路让出的列由油温传感器接续，变压器热耦合走线随之收窄到新的油温测点。
        coupling = config.layout["deployment"]["direct_links"]["L-tap-oil"]["points"]
        self.assertEqual(coupling[-1]["x"], devices["oil_temp"].layout["x"] + 45)
        self.assertEqual(coupling[-1]["y"], devices["oil_temp"].layout["y"] + 48)

    def test_live_scenario_differential_protection(self):
        """差动保护装置：间隔层保护，接收两套合并单元量测并控制断路器智能终端。"""
        config = load_scenario(LIVE_SCENARIO_PATH)
        devices = {d.device_id: d for d in config.devices}
        diff_prot = devices["diff_prot"]
        self.assertEqual(diff_prot.layer, "bay")
        self.assertEqual(diff_prot.device_type, "differential_protection")
        self.assertEqual(sorted(diff_prot.ports), ["in", "out", "up"])
        self.assertEqual(diff_prot.parameters["value_key"], "ct_current")
        self.assertEqual(diff_prot.parameters["compare_mode"], "absolute")
        self.assertEqual(diff_prot.parameters["differential_threshold_a"], 10.0)

        links = {link.link_id: link for link in config.links}
        for link_id, source in (("L-mu-diff_prot", "mu"), ("L-mu_2-diff_prot", "mu_2")):
            self.assertEqual(links[link_id].endpoint_a, (source, "out"))
            self.assertEqual(links[link_id].endpoint_b, ("diff_prot", "in"))
        self.assertEqual(links["L-diff_prot-brk"].endpoint_a, ("diff_prot", "out"))
        self.assertEqual(links["L-diff_prot-brk"].endpoint_b, ("ctl_brk", "up"))
        self.assertEqual(links["L-diff_prot-station"].endpoint_a, ("diff_prot", "up"))
        self.assertEqual(links["L-diff_prot-station"].endpoint_b, ("station", "diff_prot"))

        networks = {n["id"]: n for n in config.layout["deployment"]["networks"]}
        process_access = next(c for c in networks["process"]["connections"] if c["device_id"] == "diff_prot")
        self.assertEqual(sorted(process_access["business_ports"]), ["in", "out"])
        for link_id in ("L-mu-diff_prot", "L-mu_2-diff_prot", "L-diff_prot-brk"):
            self.assertIn(link_id, networks["process"]["link_ids"])
        station_access = next(c for c in networks["station"]["connections"] if c["device_id"] == "diff_prot")
        self.assertEqual(station_access["business_ports"], ["up"])
        self.assertIn("L-diff_prot-station", networks["station"]["link_ids"])

    def test_invalid_layout_is_rejected(self):
        config = load_scenario(SCENARIO_PATH)
        config.devices[0].layout = {"x": "left", "y": 0}
        errors = validate_scenario(config)
        self.assertTrue(any("layout.x" in error for error in errors))


class TestValidateScenario(unittest.TestCase):
    def test_valid_config(self):
        self.assertEqual(validate_scenario(make_config()), [])

    def test_duplicate_device_id(self):
        config = make_config()
        config.devices.append(DeviceSpec(device_id="brk", device_type="sensor", layer="process"))
        errors = validate_scenario(config)
        self.assertTrue(any("device_id 重复" in e for e in errors))

    def test_unknown_device_type(self):
        config = make_config()
        config.devices.append(DeviceSpec(device_id="x", device_type="not_a_device", layer="process"))
        self.assertTrue(any("类型未知" in e for e in validate_scenario(config)))

    def test_unknown_endpoint(self):
        config = make_config()
        config.links.append(LinkSpec(link_id="bad", endpoint_a=("ghost", "up"),
                                     endpoint_b=("s1", "up"), link_type="wired"))
        self.assertTrue(any("引用未知设备" in e for e in validate_scenario(config)))

    def test_message_link_requires_ports(self):
        config = make_config()
        config.links.append(LinkSpec(link_id="no_port", endpoint_a=("s1", ""),
                                     endpoint_b=("brk", ""), link_type="wired"))
        self.assertTrue(any("都必须填写端口" in e for e in validate_scenario(config)))

    def test_bad_loss_rate(self):
        config = make_config()
        config.links.append(LinkSpec(link_id="lossy", endpoint_a=("s1", "up"),
                                     endpoint_b=("brk", "control"), link_type="wired",
                                     parameters={"loss_rate": 1.5}))
        self.assertTrue(any("loss_rate" in e for e in validate_scenario(config)))

    def test_bad_dt(self):
        config = make_config()
        config.simulation["dt_us"] = 0
        self.assertTrue(any("dt_us" in e for e in validate_scenario(config)))

    def test_bad_operation_time(self):
        config = make_config()
        config.operations.append({"time_us": -1})
        self.assertTrue(any("operations[0]" in e for e in validate_scenario(config)))

    def test_duplicate_capability_action(self):
        config = make_config()
        config.devices[0].capabilities.append(Capability(action_type="close"))
        self.assertTrue(any("能力动作重复" in e for e in validate_scenario(config)))


class TestDeviceFactory(unittest.TestCase):
    def tearDown(self):
        DEVICE_TYPES.pop("test_device", None)

    def test_create_known_device(self):
        device = create_device(DeviceSpec(device_id="brk", device_type="switch", layer="process"))
        self.assertEqual(device.asset_id, "brk")

    def test_unknown_device_raises(self):
        with self.assertRaises(KeyError):
            create_device(DeviceSpec(device_id="x", device_type="no_such", layer="process"))

    def test_register_device(self):
        from sim2030.base.device import Device

        def factory(spec):
            device = Device(spec)
            device.state["custom"] = True
            return device

        register_device("test_device", factory)
        device = create_device(DeviceSpec(device_id="c", device_type="test_device", layer="process"))
        self.assertTrue(device.state.get("custom"))


if __name__ == "__main__":
    unittest.main()
