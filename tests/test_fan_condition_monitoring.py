"""基准场景的风机状态监测组：冷却风机附属传感器与风机状态监测采集装置。

覆盖：停机时读数为 0、启动后三台传感器读到风机的振动/电流/转速、采集装置把三路
读数汇集到本地，且该组只做本地监测、不让测控装置多出一个业务量测来源。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim2030.base.engine import SimulationEngine
from sim2030.scenario import load_scenario

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_SCENARIO_PATH = os.path.join(ROOT, "scenarios", "substation-live.json")
DT_US = 100000
FAN_CHANNELS = {"fan_vibration", "fan_current", "fan_speed"}


def build_engine() -> SimulationEngine:
    config = load_scenario(LIVE_SCENARIO_PATH)
    engine = SimulationEngine()
    engine.build(config.devices, config.links, config.environment, entry_id="station")
    return engine


def operate(engine: SimulationEngine, action_type: str, target: str = "cool", time_us: int = 0) -> None:
    engine.submit_business_operation({
        "request_id": f"{action_type}-{target}", "time_us": time_us,
        "target_asset_id": target, "action_type": action_type,
    })


def run(engine: SimulationEngine, steps: int, start_index: int = 0) -> None:
    for index in range(start_index, start_index + steps):
        engine.step(index * DT_US, DT_US)


def merged_values(engine: SimulationEngine) -> dict:
    latest = engine.get_device("fan_condition_monitor")._latest
    return {channel: payload.get("value") for channel, payload in latest.items()}


class TestFanConditionMonitoring(unittest.TestCase):
    def test_stopped_fan_sensors_report_zero(self):
        """风机停机时三台传感器如实上报 0，采集装置仍汇集三路读数。"""
        engine = build_engine()
        run(engine, 20)
        values = merged_values(engine)
        self.assertEqual(set(values), FAN_CHANNELS)
        self.assertEqual([values[channel] for channel in sorted(values)], [0.0, 0.0, 0.0])

    def test_running_fan_readings_reach_the_monitor(self):
        """启动风机后读到额定转速、电机电流与振动，并经采集接线汇入采集装置。"""
        engine = build_engine()
        operate(engine, "start")
        run(engine, 20)
        values = merged_values(engine)
        self.assertAlmostEqual(values["fan_speed"], 1480.0, places=6)
        self.assertAlmostEqual(values["fan_current"], 8.5, places=6)
        self.assertAlmostEqual(values["fan_vibration"], 3.2, places=6)

    def test_readings_follow_the_fan_state(self):
        """停止风机后读数回到 0，说明读数确实来自冷却风机的运行状态。"""
        engine = build_engine()
        operate(engine, "start")
        run(engine, 20)
        self.assertGreater(engine.get_device("fan_speed").state["last_value"], 0)

        operate(engine, "stop", time_us=20 * DT_US)
        run(engine, 40, start_index=20)
        self.assertEqual(engine.get_device("cool").state["running"], False)
        values = merged_values(engine)
        self.assertEqual([values[channel] for channel in sorted(values)], [0.0, 0.0, 0.0])

    def test_monitor_does_not_add_a_business_measurement_source(self):
        """采集装置只做本地监测：测控装置的量测来源仍是两套合并单元。"""
        engine = build_engine()
        operate(engine, "start")
        run(engine, 20)
        sources = engine.get_device("mc").build_telemetry()["sources"]
        self.assertEqual(sorted(sources), ["mu", "mu_2"])


class TestCoolingAffectsTransformerHeat(unittest.TestCase):
    """冷却风机的启停经负载率与热模型作用到变压器油温。"""

    def test_load_pu_comes_from_rated_load(self):
        """合闸后负载率为 负荷/额定负荷；断路器分闸时线路无流，负载率为 0。"""
        engine = build_engine()
        operate(engine, "close", target="brk")
        run(engine, 20)
        self.assertAlmostEqual(engine._environment.snapshot()["load_pu"], 0.5, places=6)

        open_engine = build_engine()
        run(open_engine, 20)
        self.assertEqual(open_engine._environment.snapshot()["load_pu"], 0.0)

    def test_fan_lowers_steady_oil_temperature(self):
        """不通风机时油温稳定在约 42.5 ℃，通风机后降到约 35.6 ℃。"""
        without = build_engine()
        operate(without, "close", target="brk")
        run(without, 1200)  # 120 s，热时间常数 20 s
        idle_temp = without.get_device("tap").state["oil_temp_c"]
        self.assertAlmostEqual(idle_temp, 42.5, delta=0.2)

        with_fan = build_engine()
        operate(with_fan, "close", target="brk")
        operate(with_fan, "start")
        run(with_fan, 1200)
        cooled_temp = with_fan.get_device("tap").state["oil_temp_c"]
        self.assertAlmostEqual(cooled_temp, 35.625, delta=0.2)
        self.assertGreater(idle_temp - cooled_temp, 6.0, "开风机应明显压低油温")

    def test_cooling_response_is_fast(self):
        """热时间常数被缩短：开风机 30 s 内油温已下降 3 ℃ 以上。"""
        engine = build_engine()
        operate(engine, "close", target="brk")
        operate(engine, "start")
        run(engine, 300)  # 30 s
        temp = engine.get_device("tap").state["oil_temp_c"]
        self.assertLess(temp, 37.2, "30 s 内应已明显低于停机时的 42.5 ℃")
        self.assertGreater(temp, 35.5, "尚未完全稳定，说明是一段瞬态过程")


if __name__ == "__main__":
    unittest.main()
