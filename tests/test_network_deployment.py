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
                status, editor, headers = server.handle_request("GET", "/topology-layout-editor.js")
                self.assertEqual(status, 200)
                self.assertIn("application/javascript", headers["Content-Type"])
                self.assertIn("class TopologyLayoutEditor", editor["__raw__"])
                _, page, _ = server.handle_request("GET", "/")
                self.assertLess(page["__raw__"].index('src="/topology-layout-editor.js'), page["__raw__"].index('src="/app.js'))
                self.assertLess(page["__raw__"].index('src="/network-topology.js'), page["__raw__"].index('src="/app.js'))
                status, admin, headers = server.handle_request("GET", "/admin")
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertIn("拓扑后台管理", admin["__raw__"])
                self.assertIn('id="topology-edit-toggle"', admin["__raw__"])
                self.assertIn('id="topology-property-panel"', admin["__raw__"])
                self.assertNotIn('id="topology-edit-toggle"', page["__raw__"])
                self.assertNotIn('id="topology-property-panel"', page["__raw__"])
            finally:
                server.close()

    def test_clone_and_publish_scenario_persists_and_is_listed_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.json"
            source.write_text((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"), encoding="utf-8")
            server = PresentationServer(str(root), str(root / "runs"))
            try:
                status, payload, _ = server.handle_request("POST", "/api/scenarios/clone", json.dumps({
                    "source_scenario_id": "substation-live",
                    "new_scenario_id": "demo-copy",
                    "name": "变电站复制场景",
                    "topology_layout": {
                        "positions": {"station": {"x": 300, "y": 200}},
                        "sizes": {"station": {"width": 140, "height": 72}},
                        "layerSizes": {"layer:process": {"width": 900, "height": 420}},
                        "customModules": [{"id": "copy-1", "kind": "sensor", "name": "复制传感器", "x": 500, "y": 600, "width": 120, "height": 60}],
                        "customLinks": [{"id": "link-1", "source": "copy-1", "target": "station", "type": "measurement"}],
                        "nodeOverrides": {"station": {"name": "站控系统副本"}},
                        "hiddenNodes": ["ids-process"],
                        "hiddenLinks": ["process:mirror"],
                        "linkStyles": {"process:mirror": "mirror"},
                    },
                }).encode("utf-8"))
                self.assertEqual(status, 201)
                self.assertEqual(payload["status"], "published")
                published = root / "demo-copy.json"
                self.assertTrue(published.is_file())
                raw = json.loads(published.read_text(encoding="utf-8"))
                self.assertEqual(raw["scenario_id"], "demo-copy")
                self.assertEqual(raw["name"], "变电站复制场景")
                self.assertEqual(raw["devices"], json.loads(source.read_text(encoding="utf-8"))["devices"])
                self.assertEqual(raw["layout"]["editor"]["customModules"][0]["name"], "复制传感器")
                self.assertEqual(raw["layout"]["editor"]["customLinks"][0]["type"], "measurement")
                self.assertEqual(raw["layout"]["editor"]["nodeOverrides"]["station"]["name"], "站控系统副本")
                self.assertEqual(raw["layout"]["editor"]["hiddenNodes"], ["ids-process"])
                self.assertEqual(raw["layout"]["editor"]["hiddenLinks"], ["process:mirror"])
                self.assertEqual(raw["layout"]["editor"]["linkStyles"], {"process:mirror": "mirror"})
                listed = server.handle_request("GET", "/api/scenarios")[1]["scenarios"]
                self.assertIn("demo-copy", {item["scenario_id"] for item in listed})
                status, _, _ = server.handle_request("POST", "/api/scenarios/clone", json.dumps({
                    "source_scenario_id": "substation-live",
                    "new_scenario_id": "demo-copy",
                    "name": "重复发布",
                }).encode("utf-8"))
                self.assertEqual(status, 409)
            finally:
                server.close()
            reloaded = PresentationServer(str(root), str(root / "runs"))
            try:
                listed = reloaded.handle_request("GET", "/api/scenarios")[1]["scenarios"]
                self.assertIn("demo-copy", {item["scenario_id"] for item in listed})
                status, payload, _ = reloaded.handle_request("POST", "/api/runs", json.dumps({"scenario_id": "demo-copy"}).encode("utf-8"))
                self.assertEqual(status, 201)
                self.assertEqual(payload["scenario_id"], "demo-copy")
            finally:
                reloaded.close()

    def test_clone_materializes_bound_editor_nodes_and_links_for_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.json"
            source.write_text((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"), encoding="utf-8")
            server = PresentationServer(str(root), str(root / "runs"))
            try:
                layout = {
                    "positions": {"copy-ct": {"x": 760, "y": 840}},
                    "sizes": {"copy-ct": {"width": 156, "height": 82}},
                    "layerSizes": {},
                    "customModules": [{"id": "copy-ct", "sourceId": "ct_current", "sourceName": "电流传感器",
                                       "kind": "sensor", "name": "复制电流传感器", "x": 500, "y": 500,
                                       "width": 110, "height": 58}],
                    "customLinks": [{"id": "copy-link", "source": "copy-ct", "target": "mu",
                                     "type": "wired", "sourcePort": "up", "targetPort": "in"}],
                    "nodeOverrides": {}, "hiddenNodes": [], "hiddenLinks": [], "linkStyles": {},
                }
                status, payload, _ = server.handle_request("POST", "/api/scenarios/clone", json.dumps({
                    "source_scenario_id": "substation-live", "new_scenario_id": "runtime-copy",
                    "name": "运行复制场景", "topology_layout": layout,
                }).encode("utf-8"))
                self.assertEqual(status, 201)
                self.assertEqual(payload["runtime"]["devices_added"], 1)
                self.assertEqual(payload["runtime"]["links_added"], 1)
                config = load_scenario(str(root / "runtime-copy.json"))
                copied_id = "runtime-copy-copy-ct"
                self.assertIn(copied_id, {device.device_id for device in config.devices})
                self.assertIn((copied_id, "up"), {(link.endpoint_a[0], link.endpoint_a[1]) for link in config.links})
                published = json.loads((root / "runtime-copy.json").read_text(encoding="utf-8"))
                published_module = published["layout"]["editor"]["customModules"][0]
                published_link = published["layout"]["editor"]["customLinks"][0]
                self.assertEqual(published_module["runtimeId"], copied_id)
                self.assertEqual(published_module["runtimeId"], published["devices"][-1]["device_id"])
                self.assertEqual(published_module["x"], 760)
                self.assertEqual(published_module["y"], 840)
                self.assertEqual(published_module["width"], 156)
                self.assertEqual(published_module["height"], 82)
                self.assertEqual(published["devices"][-1]["layout"], {"x": 760, "y": 840, "width": 156, "height": 82})
                self.assertEqual(published_link["runtimeId"], published["links"][-1]["link_id"])
                self.assertEqual(validate_scenario(config), [])
                run_id = server.application.load(str(root / "runtime-copy.json"))
                self.assertIn(copied_id, server.application.engine._devices)
                server.application.start()
                for _ in range(3):
                    server.application.step()
                received = server.application.engine._devices["mu"]._latest
                self.assertIn(copied_id, received)
                self.assertEqual(received[copied_id]["sensor_id"], copied_id)
                status, system_payload, _ = server.handle_request("GET", f"/api/runs/{run_id}/views?view=system")
                self.assertEqual(status, 200)
                system = system_payload["data"]
                runtime_devices = [device for device in system["topology"]["devices"] if device["device_id"] == copied_id]
                self.assertEqual(len(runtime_devices), 1)
                self.assertEqual(runtime_devices[0]["layout"], {"x": 760, "y": 840, "width": 156, "height": 82})
                runtime_links = [link for link in system["topology"]["links"] if link["link_id"] == published_link["runtimeId"]]
                self.assertEqual(len(runtime_links), 1)
                self.assertEqual(runtime_links[0]["endpoint_a"][0], copied_id)
            finally:
                server.close()

    def test_delete_scenario_removes_file_and_refreshes_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "delete-me.json"
            raw = json.loads((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"))
            raw["scenario_id"] = "delete-me"
            raw["name"] = "待删除场景"
            source.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            server = PresentationServer(str(root), str(root / "runs"))
            try:
                status, payload, _ = server.handle_request("DELETE", "/api/scenarios/delete-me")
                self.assertEqual(status, 200)
                self.assertEqual(payload["status"], "deleted")
                self.assertFalse(source.exists())
                listed = server.handle_request("GET", "/api/scenarios")[1]["scenarios"]
                self.assertNotIn("delete-me", {item["scenario_id"] for item in listed})
                status, payload, _ = server.handle_request("DELETE", "/api/scenarios/delete-me")
                self.assertEqual(status, 404)
                self.assertIn("未找到场景", payload["error"])
            finally:
                server.close()

    def test_delete_rejects_current_running_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "active.json"
            raw = json.loads((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"))
            raw["scenario_id"] = "active"
            source.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            server = PresentationServer(str(root), str(root / "runs"))
            try:
                status, created, _ = server.handle_request("POST", "/api/runs", json.dumps({"scenario_id": "active"}).encode("utf-8"))
                self.assertEqual(status, 201)
                self.assertEqual(created["scenario_id"], "active")
                status, payload, _ = server.handle_request("DELETE", "/api/scenarios/active")
                self.assertEqual(status, 409)
                self.assertIn("正在运行", payload["error"])
                self.assertTrue(source.exists())
            finally:
                server.close()

    def test_clone_removes_temp_file_when_published_runtime_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "substation-live.json"
            source.write_text((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"), encoding="utf-8")
            server = PresentationServer(str(root), str(root / "runs"))
            try:
                layout = {
                    "positions": {}, "sizes": {}, "layerSizes": {},
                    "customModules": [{"id": "copy-ct", "sourceId": "ct_current", "name": "复制电流传感器",
                                       "kind": "sensor", "x": 500, "y": 500, "width": 110, "height": 58}],
                    "customLinks": [{"id": "invalid-link", "source": "copy-ct", "target": "mu",
                                     "type": "wired", "sourcePort": "missing-port", "targetPort": "in"}],
                    "nodeOverrides": {}, "hiddenNodes": [], "hiddenLinks": [], "linkStyles": {},
                }
                status, payload, _ = server.handle_request("POST", "/api/scenarios/clone", json.dumps({
                    "source_scenario_id": "substation-live", "new_scenario_id": "invalid-runtime-copy",
                    "name": "非法运行场景", "topology_layout": layout,
                }).encode("utf-8"))
                self.assertEqual(status, 400)
                self.assertIn("发布后的场景校验失败", payload["error"])
                self.assertFalse((root / "invalid-runtime-copy.json").exists())
                self.assertFalse(any(path.name.startswith(".invalid-runtime-copy.") for path in root.iterdir()))
            finally:
                server.close()

    def test_clone_rejects_invalid_editor_layout_without_creating_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.json"
            source.write_text((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"), encoding="utf-8")
            server = PresentationServer(str(root), str(root / "runs"))
            try:
                status, payload, _ = server.handle_request("POST", "/api/scenarios/clone", json.dumps({
                    "source_scenario_id": "substation-live",
                    "new_scenario_id": "invalid-layout-copy",
                    "name": "非法布局",
                    "topology_layout": {"positions": {"station": {"x": "bad", "y": 20}}},
                }).encode("utf-8"))
                self.assertEqual(status, 400)
                self.assertIn("拓扑布局无效", payload["error"])
                self.assertFalse((root / "invalid-layout-copy.json").exists())
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
