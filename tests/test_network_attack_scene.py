import json
from pathlib import Path
import tempfile
import unittest

from sim2030.application import Application
from sim2030.scenario import load_scenario, validate_scenario
from sim2030.presentation.server import PresentationServer

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "scenarios/substation-network-attack.json"


class TestNetworkAttackPresentation(unittest.TestCase):
    def test_scene_keeps_all_baseline_simulation_settings(self):
        baseline = json.loads((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"))
        demonstration = json.loads(SCENE.read_text(encoding="utf-8"))
        demonstration["layout"].pop("cyber_demo")
        for item in (baseline, demonstration):
            item.pop("scenario_id")
            item.pop("name")
        self.assertEqual(demonstration, baseline)

    def test_scene_validates_and_is_listed(self):
        self.assertEqual(validate_scenario(load_scenario(str(SCENE))), [])
        with tempfile.TemporaryDirectory() as tmp:
            server = PresentationServer(str(ROOT / "scenarios"), tmp)
            try:
                code, body, _ = server.handle_request("GET", "/api/scenarios")
                self.assertEqual(code, 200)
                item = next(s for s in body["scenarios"] if s["scenario_id"] == "substation-network-attack")
                self.assertEqual(item["attack_count"], 0)
            finally:
                server.close()

    def test_running_scene_does_not_inject_network_attacks_or_ids_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Application(output_root=tmp)
            try:
                app.load(str(SCENE))
                app.start()
                for _ in range(10):
                    app.step()
                view = app.get_view("system")
                self.assertEqual(view["topology"]["layout"]["cyber_demo"]["mode"], "presentation_only")
                self.assertEqual(view["recognition"], [])
                self.assertFalse(any(event["source_type"] == "ids" for event in view["observations"]["events"]))
                self.assertFalse(any(d["effects"] for d in view["management"].values()))
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
