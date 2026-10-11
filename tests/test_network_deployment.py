import copy
import json
from pathlib import Path
import tempfile
import unittest

from sim2030.scenario import load_scenario, validate_scenario
from sim2030.presentation.server import PresentationServer
from sim2030.presentation.views import build_system_view
from sim2030.contracts import event_key
from sim2030.application import Application
from tests.support import make_observation_event, write_jsonl, OBS_TIME_ORIGIN

ROOT = Path(__file__).resolve().parents[1]


def write_ids_scene(directory):
    """只供自动测试与浏览器验收使用，事件明确标为 synthetic。"""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    scene = json.loads((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"))
    scene["scenario_id"] = "ids-ui-test"
    scene["name"] = "IDS 界面测试（合成事件）"
    scene["recognition"] = {"enabled": True, "window_size_us": 1_000_000}
    scene["defense"] = {"enabled": False}
    scene["observations"] = {"provider_dir": str(root / "provider"), "time_mapping": {"origin": OBS_TIME_ORIGIN, "unit": "us"}}
    events = []
    for network, kind in (("station", "flow"), ("process", "alert")):
        events.append(make_observation_event(
            scenario_id=scene["scenario_id"], provider_id="test-ids", event_id=network,
            source_type="ids", source_id=f"ids-{network}", timestamp=OBS_TIME_ORIGIN,
            status="anomaly" if kind == "alert" else "normal", data_origin="synthetic",
            related_asset_ids=["mc"], raw_data_uri="evidence/ids.jsonl", raw_data_format="eve_json",
            metadata={"start_time": OBS_TIME_ORIGIN, "end_time": OBS_TIME_ORIGIN,
                      "interface_id": f"span-{network}", "asset_mapping_version": "test-v1"},
            data={"event_type": kind, "protocol": "GOOSE" if network == "process" else "MMS",
                  "src_asset_id": "mu" if network == "process" else "station", "dst_asset_id": "mc",
                  **({"alert_type": "测试：异常发布源", "severity": 2} if kind == "alert" else {})},
        ))
    write_jsonl(str(root / "provider/events.jsonl"), events)
    write_jsonl(str(root / "provider/evidence/ids.jsonl"), events)
    path = root / "ids-ui-test.json"
    path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    return path


class TestNetworkDeployment(unittest.TestCase):
    def setUp(self):
        self.config = load_scenario(str(ROOT / "scenarios/substation-live.json"))

    def test_all_scenarios_have_valid_separate_networks(self):
        for path in (ROOT / "scenarios").glob("*.json"):
            with self.subTest(path=path.name):
                self.assertEqual(validate_scenario(load_scenario(str(path))), [])

    def test_same_device_interface_cannot_bridge_networks(self):
        networks = self.config.layout["deployment"]["networks"]
        networks[1]["connections"][0]["interface_id"] = "ETH-ST"
        self.assertTrue(any("两张网" in e for e in validate_scenario(self.config)))

    def test_mirror_port_cannot_be_used_by_business_device(self):
        network = self.config.layout["deployment"]["networks"][0]
        network["mirror_port"] = "P1"
        self.assertTrue(any("交换机端口" in e for e in validate_scenario(self.config)))

    def test_control_hardwire_cannot_be_assigned_to_ethernet(self):
        self.config.layout["deployment"]["networks"][0]["link_ids"].append("L-ctl-brk-down")
        self.assertTrue(any("非以太网" in e for e in validate_scenario(self.config)))

    def test_business_route_requires_both_physical_endpoints(self):
        self.config.layout["deployment"]["networks"][0]["link_ids"].append("L-mu-mc")
        errors = validate_scenario(self.config)
        self.assertTrue(any("没有接入" in e for e in errors))
        self.assertTrue(any("同时归属" in e for e in errors))

    def test_ids_capture_and_management_interfaces_are_separate(self):
        probe = self.config.layout["deployment"]["networks"][0]["probe"]
        probe["management_interface"] = probe["capture_interface"]
        self.assertTrue(any("必须分开" in e for e in validate_scenario(self.config)))

    def test_legacy_scenario_remains_valid(self):
        self.config.layout.pop("deployment")
        self.assertEqual(validate_scenario(self.config), [])

    def test_ids_provider_events_reach_system_view_and_recognition(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = write_ids_scene(tmp)
            app = Application(output_root=str(Path(tmp) / "runs"))
            try:
                app.load(str(scenario))
                app.start()
                app.step()
                data = app.get_view("system")
                events = data["observations"]["events"]
                self.assertEqual({e["raw"]["metadata"]["interface_id"] for e in events}, {"span-station", "span-process"})
                self.assertEqual(len(data["recognition"]), 1)
                self.assertIn("mc", data["recognition"][0]["target_asset_ids"])
                self.assertTrue(all(e["raw"]["data_origin"] == "synthetic" for e in events))
            finally:
                app.close()

    def test_static_module_served_and_referenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = PresentationServer(str(ROOT / "scenarios"), tmp)
            try:
                status, payload, headers = server.handle_request("GET", "/network-topology.js")
                self.assertEqual(status, 200)
                self.assertIn("application/javascript", headers["Content-Type"])
                self.assertIn("const NetworkTopology", payload["__raw__"])
                _, page, _ = server.handle_request("GET", "/")
                self.assertLess(page["__raw__"].index('src="/network-topology.js'), page["__raw__"].index('src="/app.js'))
            finally:
                server.close()

    def test_finished_view_preserves_deployment_and_only_latest_window_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"layout": self.config.layout}), encoding="utf-8")
            old = {"source_type": "ids", "provider_id": "p", "event_id": "old", "raw": {"data": {"event_type": "alert"}}}
            current = copy.deepcopy(old)
            current["event_id"] = "current"
            batch = {"type": "observation_batch", "event_keys": [event_key("p", "current")]}
            (root / "observation.jsonl").write_text("\n".join(json.dumps(e) for e in [old, current, batch]), encoding="utf-8")
            view = build_system_view(root)
            self.assertEqual(view["topology"]["layout"], self.config.layout)
            self.assertEqual(view["observations"]["ids_events"], [current])


if __name__ == "__main__":
    unittest.main()
