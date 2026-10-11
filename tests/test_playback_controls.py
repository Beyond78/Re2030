import json
from pathlib import Path
import tempfile
import unittest

from sim2030.presentation.server import PresentationServer


class TestPlaybackControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fixtures = Path(__file__).resolve().parent / "fixtures"
        self.server = PresentationServer(str(fixtures), self.tmp.name)
        scenario = json.loads((fixtures / "substation.json").read_text(encoding="utf-8"))
        code, payload, _ = self.server.handle_request("POST", "/api/runs", json.dumps({"scenario_id": scenario["scenario_id"]}).encode())
        self.assertEqual(code, 201)
        self.run_id = payload["run_id"]
        self.sequence = 0

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def operation(self, action, request_id=None):
        self.sequence += 1
        _, receipt, _ = self.server.handle_request("POST", f"/api/runs/{self.run_id}/operations", json.dumps({
            "request_id": request_id or str(self.sequence), "target_asset_id": "run", "operation_type": action,
        }).encode())
        return receipt

    def test_manual_step_from_ready_and_paused_advances_exactly_once(self):
        app = self.server.application
        for step in range(1, 4):
            receipt = self.operation("step_once")
            self.assertEqual(receipt["summary"]["status"], "paused")
            self.assertEqual(receipt["summary"]["time_us"], step * app.dt_us)

    def test_duplicate_manual_request_is_not_repeated(self):
        self.operation("step_once", "same")
        receipt = self.operation("step_once", "same")
        self.assertEqual(receipt["status"], "duplicate")
        self.assertEqual(self.server.application.current_time_us, self.server.application.dt_us)

    def test_manual_step_during_automatic_run_is_rejected(self):
        self.operation("start")
        receipt = self.operation("step_once")
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(self.server.application.current_time_us, 0)

    def test_finish_is_preserved_after_last_manual_step(self):
        app = self.server.application
        app.duration_us = app.dt_us
        receipt = self.operation("step_once")
        self.assertEqual(receipt["summary"]["status"], "finished")
        self.assertEqual(self.operation("step_once")["status"], "rejected")

    def test_control_receipts_expose_authoritative_state(self):
        for action, status in (("start", "running"), ("pause", "paused"), ("stop", "finished")):
            self.assertEqual(self.operation(action)["summary"]["status"], status)

    def test_system_view_exposes_actual_step_size(self):
        self.server.application.dt_us = 250000
        _, payload, _ = self.server.handle_request("GET", f"/api/runs/{self.run_id}/views?view=system")
        self.assertEqual(payload["data"]["dt_us"], 250000)

    def test_player_module_is_served_before_the_application(self):
        code, data, headers = self.server.handle_request("GET", "/simulation-player.js")
        self.assertEqual(code, 200)
        self.assertIn("application/javascript", headers["Content-Type"])
        self.assertIn("class SimulationPlayer", data["__raw__"])
        _, page, _ = self.server.handle_request("GET", "/")
        self.assertLess(page["__raw__"].index('src="/simulation-player.js'), page["__raw__"].index('src="/app.js'))


if __name__ == "__main__":
    unittest.main()
