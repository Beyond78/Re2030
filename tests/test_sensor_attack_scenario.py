"""风机专用传感器与合并单元的接入测试。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim2030.application import Application
from sim2030.records import RunReader
from sim2030.scenario import load_scenario, validate_scenario


SCENARIO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scenarios",
    "substation-sensor-attack.json",
)


class TestSensorAttackScenario(unittest.TestCase):
    def test_fan_sensors_feed_a_dedicated_merging_unit(self):
        config = load_scenario(SCENARIO_PATH)
        self.assertEqual(validate_scenario(config), [])

        devices = {device.device_id: device for device in config.devices}
        self.assertEqual(
            [devices[key].name for key in ("fan_vibration", "fan_current", "fan_speed")],
            ["风机振动传感器", "风机电流传感器", "风机转速传感器"],
        )
        self.assertEqual(devices["fan_condition_monitor"].name, "风机状态监测采集装置")
        self.assertEqual(devices["fan_condition_monitor"].device_type, "acquisition")
        fan_sensor_links = {
            link.link_id for link in config.links
            if link.endpoint_b[0] == "fan_condition_monitor"
        }
        self.assertEqual(fan_sensor_links, {
            "L-fan-vibration-mu",
            "L-fan-current-mu",
            "L-fan-speed-mu",
        })

    def test_running_fan_readings_are_collected_by_its_merging_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Application(output_root=os.path.join(tmp, "runs"))
            app.load(SCENARIO_PATH)
            app.start()
            receipt = app.submit_operation({
                "request_id": "start-fan",
                "target_asset_id": "cool",
                "action_type": "start",
            })
            self.assertEqual(receipt["status"], "queued")
            for _ in range(12):
                app.step()
            merged_samples = app.engine.get_device("fan_condition_monitor")._latest
            self.assertTrue({"fan_vibration", "fan_current", "fan_speed"}.issubset(merged_samples))
            run_id = app.run_id
            app.close()

            records = list(RunReader(os.path.join(tmp, "runs", run_id)).read("business"))
            sensor_ids = {"fan_vibration", "fan_current", "fan_speed"}
            sensor_values = {
                (record.get("payload") or {}).get("sensor_id"): (record.get("payload") or {}).get("value")
                for record in records
                if (record.get("payload") or {}).get("sensor_id") in sensor_ids
            }
            self.assertEqual(set(sensor_values), sensor_ids)
            self.assertGreater(sensor_values["fan_vibration"], 0)
            self.assertGreater(sensor_values["fan_current"], 0)
            self.assertGreater(sensor_values["fan_speed"], 0)

if __name__ == "__main__":
    unittest.main()
