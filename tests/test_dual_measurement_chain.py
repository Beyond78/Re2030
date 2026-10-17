"""复数授时与采集链路：第二套互感器／合并单元的量测与保护行为。

覆盖：量测通道归并、测控装置按来源保留并上送量测、站端如实看到两套数值、
以及过流保护“任一来源过流即出口并按来源归属”的判据。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim2030.base.bay import DifferentialProtectionDevice
from sim2030.base.engine import SimulationEngine
from sim2030.constants import BusinessType
from sim2030.contracts import DeviceSpec, EffectRequest
from sim2030.scenario import load_scenario

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_SCENARIO_PATH = os.path.join(ROOT, "scenarios", "substation-live.json")
DT_US = 100000


def build_engine() -> SimulationEngine:
    """装配基准场景并合闸，使线路有电流。"""
    config = load_scenario(LIVE_SCENARIO_PATH)
    engine = SimulationEngine()
    engine.build(config.devices, config.links, config.environment, entry_id="station")
    engine.submit_business_operation(
        {"request_id": "close-1", "time_us": 0, "target_asset_id": "brk", "action_type": "close"}
    )
    return engine


def run(engine: SimulationEngine, steps: int = 12, on_step=None):
    messages = []
    for index in range(steps):
        time_us = index * DT_US
        if on_step is not None:
            on_step(engine, time_us)
        messages.extend(engine.step(time_us, DT_US).messages)
    return messages


def tamper(engine: SimulationEngine, sensor_id: str, offset: float, window_us: int = 2 ** 62) -> None:
    """对指定互感器施加读数偏移，模拟该套采样通道被篡改。"""
    engine.submit_effect(EffectRequest(
        effect_id=f"tamper-{sensor_id}", target_id=sensor_id, effect_type="reading_offset",
        parameters={"offset": offset}, start_time_us=0, end_time_us=window_us,
    ))


def silence_differential(engine: SimulationEngine) -> None:
    """抬高差动整定值，用于单独验证过流保护。

    只篡改一套互感器必然同时形成差流，差动保护会先于过流保护出口，
    因此验证过流判据时需要先把差动保护静默。
    """
    engine.set_device_parameter("diff_prot", "differential_threshold_a", 10000.0)


def protections(messages, sender=None):
    """保护事件；``sender`` 用于只看某一台保护装置（场景中同时存在过流与差动保护）。"""
    return [
        m for m in messages
        if m.business_type == BusinessType.PROTECTION and (sender is None or m.sender_id == sender)
    ]


class TestDualMeasurementChain(unittest.TestCase):
    def test_second_ct_uses_same_measurement_channel(self):
        """第二套互感器测同一电气量，量测通道名与首套一致，便于下游统一识别。"""
        engine = build_engine()
        run(engine, steps=6)
        self.assertEqual(sorted(engine._devices["mu"]._latest), ["ct_current", "oil_temp", "vt_voltage"])
        self.assertEqual(sorted(engine._devices["mu_2"]._latest), ["ct_current"])
        self.assertEqual(engine._devices["mu_2"]._latest["ct_current"]["sensor_id"], "ct_current_2")

    def test_measurement_control_keeps_every_source(self):
        """测控装置按来源分别保留量测，两套采集单元互不覆盖。"""
        engine = build_engine()
        run(engine, steps=8)
        telemetry = engine._devices["mc"].build_telemetry()
        self.assertEqual(telemetry["measurement_id"], "mc")
        self.assertEqual(sorted(telemetry["sources"]), ["mu", "mu_2"])
        self.assertAlmostEqual(
            telemetry["sources"]["mu"]["samples"]["ct_current"],
            telemetry["sources"]["mu_2"]["samples"]["ct_current"],
            places=6,
        )
        overview = engine._devices["mc"].snapshot()["overview"]
        self.assertEqual(sorted(overview["samples_by_source"]), ["mu", "mu_2"])
        self.assertNotIn("latest_sample", overview, "单值覆盖字段应已由按来源分组取代")

    def test_tampered_second_ct_diverges_at_station(self):
        """只篡改第二套互感器时，站端同时收到两套不同的量测值，而不是被覆盖成一路。"""
        engine = build_engine()
        tamper(engine, "ct_current_2", 20.0)
        run(engine, steps=8)
        sources = engine._devices["station"]._overview["mc"]["sources"]
        self.assertEqual(sorted(sources), ["mu", "mu_2"])
        self.assertAlmostEqual(sources["mu"]["samples"]["ct_current"], 115.47, places=1)
        self.assertAlmostEqual(sources["mu_2"]["samples"]["ct_current"], 135.47, places=1)

    def test_both_sources_normal_no_trip(self):
        """两套量测均正常时不动作。"""
        engine = build_engine()
        messages = run(engine, steps=12)
        self.assertEqual(protections(messages), [])
        self.assertEqual(engine._devices["brk"].state["position"], "closed")

    def test_either_source_overcurrent_trips_and_records_source(self):
        """任一来源持续过流即出口，并在保护事件中标注触发来源。"""
        engine = build_engine()
        silence_differential(engine)
        tamper(engine, "ct_current_2", 200.0)
        events = protections(run(engine, steps=12), sender="prot")
        self.assertTrue(events, "第二套互感器过流应触发保护动作")
        self.assertEqual(events[0].payload["action"], "trip")
        self.assertEqual(events[0].payload["source"], "mu_2")
        self.assertAlmostEqual(events[0].payload["sample"]["samples"]["ct_current"], 315.47, places=1)
        self.assertEqual(engine._devices["brk"].state["position"], "open")

    def test_first_source_still_trips_on_its_own(self):
        """首套互感器单独过流时同样出口，来源标注为首套。"""
        engine = build_engine()
        silence_differential(engine)
        tamper(engine, "ct_current", 200.0)
        events = protections(run(engine, steps=12), sender="prot")
        self.assertTrue(events)
        self.assertTrue(all(event.payload["source"] == "mu" for event in events))

    def test_single_tick_spike_does_not_trip_overcurrent(self):
        """过流保护的持续时间按来源独立整定：单次越限达不到整定次数，不应出口。"""
        engine = build_engine()
        silence_differential(engine)

        def on_step(engine, time_us):
            if time_us == 4 * DT_US:
                tamper(engine, "ct_current_2", 200.0, window_us=time_us)
            elif time_us > 4 * DT_US:
                engine.clear_effect("tamper-ct_current_2")

        events = protections(run(engine, steps=12, on_step=on_step), sender="prot")
        self.assertEqual(events, [])

    def test_per_source_counters_are_independent(self):
        """两套来源各自累计越限次数，互不覆盖。"""
        engine = build_engine()
        run(engine, steps=8)
        ticks = engine._devices["prot"]._persistent_ticks
        self.assertEqual(sorted(ticks), ["mu", "mu_2"])
        self.assertEqual(set(ticks.values()), {0})


class TestDifferentialProtection(unittest.TestCase):
    """差动保护装置：比较两套合并单元上报电流的绝对差，越限即跳断路器。"""

    def test_divergence_within_threshold_does_not_trip(self):
        engine = build_engine()
        tamper(engine, "ct_current_2", 5.0)
        messages = run(engine, steps=12)
        self.assertEqual(protections(messages, sender="diff_prot"), [])
        self.assertEqual(engine._devices["brk"].state["position"], "closed")

    def test_absolute_divergence_trips_breaker(self):
        engine = build_engine()
        tamper(engine, "ct_current_2", 20.0)
        events = protections(run(engine, steps=12), sender="diff_prot")
        self.assertTrue(events, "差流越限应触发差动保护")
        payload = events[0].payload
        self.assertEqual(payload["action"], "trip")
        self.assertEqual(payload["source"], "differential")
        self.assertAlmostEqual(payload["differential"]["difference_a"], 20.0, places=1)
        self.assertAlmostEqual(payload["differential"]["threshold_a"], 10.0, places=1)
        self.assertEqual(payload["differential"]["compare_mode"], "absolute")
        self.assertEqual(payload["differential"]["low_source"], "mu")
        self.assertEqual(payload["differential"]["high_source"], "mu_2")
        self.assertEqual(sorted(payload["sources"]), ["mu", "mu_2"])
        self.assertEqual(engine._devices["brk"].state["position"], "open")

    def test_trip_command_reaches_breaker_terminal(self):
        """跳闸命令经过程网下发到断路器智能终端。"""
        engine = build_engine()
        tamper(engine, "ct_current_2", 20.0)
        messages = run(engine, steps=12)
        commands = [m for m in messages if m.sender_id == "diff_prot" and m.business_type == BusinessType.COMMAND]
        self.assertTrue(commands)
        self.assertEqual(commands[0].receiver_id, "ctl_brk")
        self.assertEqual(commands[0].payload["action_type"], "trip")
        self.assertEqual(commands[0].payload["parameters"]["source"], "differential")

    def test_trip_is_latched_until_differential_recovers(self):
        """差流未复归前只出口一次，避免每个判断周期重复下发相同动作。"""
        engine = build_engine()
        tamper(engine, "ct_current_2", 20.0)
        messages = run(engine, steps=20)
        trips = {
            m.message_id for m in messages
            if m.sender_id == "diff_prot" and m.business_type == BusinessType.COMMAND
        }
        self.assertEqual(len(trips), 1)
        self.assertTrue(engine._devices["diff_prot"]._latched)

    def test_trip_rearms_after_recovery(self):
        """差流复归后重新允许动作。"""
        engine = build_engine()
        tamper(engine, "ct_current_2", 20.0)

        def on_step(engine, time_us):
            if time_us == 12 * DT_US:
                engine.clear_effect("tamper-ct_current_2")
            elif time_us == 16 * DT_US:
                tamper(engine, "ct_current_2", 20.0)

        messages = run(engine, steps=20, on_step=on_step)
        trips = {
            m.message_id for m in messages
            if m.sender_id == "diff_prot" and m.business_type == BusinessType.COMMAND
        }
        self.assertEqual(len(trips), 2, "复归后再次越限应重新出口")

    def test_threshold_is_adjustable_at_runtime(self):
        """整定值可在运行期修改并立即影响判断。"""
        engine = build_engine()
        tamper(engine, "ct_current_2", 20.0)
        self.assertEqual(
            engine.set_device_parameter("diff_prot", "differential_threshold_a", 50.0)["status"], "updated"
        )
        messages = run(engine, steps=12)
        self.assertEqual(protections(messages, sender="diff_prot"), [])
        self.assertEqual(engine._devices["brk"].state["position"], "closed")
        self.assertAlmostEqual(engine._devices["diff_prot"]._last_differential["difference"], 20.0, places=1)

    def test_threshold_is_exposed_as_control(self):
        """整定值通过能力接口下发给演示平面，前端无需硬编码。"""
        engine = build_engine()
        controls = engine.get_device_controls("diff_prot")["controls"]
        self.assertEqual(len(controls), 1)
        control = controls[0]
        self.assertEqual(control["kind"], "parameter")
        self.assertEqual(control["key"], "differential_threshold_a")
        self.assertEqual(control["unit"], "A")
        self.assertAlmostEqual(control["value"], 10.0, places=6)

    def test_single_source_does_not_trip(self):
        """只有一个来源时无法比较，不应动作。"""
        device = DifferentialProtectionDevice(
            DeviceSpec(device_id="d", device_type="differential_protection", layer="bay")
        )
        self.assertIsNone(device.differential())
        self.assertEqual(device.evaluate_differential(), [])

    def test_unsupported_compare_mode_does_not_trip(self):
        """未实现的比较方式明确不动作，而不是悄悄退回绝对值。"""
        engine = build_engine()
        engine.set_device_parameter("diff_prot", "compare_mode", "relative")
        tamper(engine, "ct_current_2", 200.0)
        messages = run(engine, steps=12)
        self.assertEqual(protections(messages, sender="diff_prot"), [])
        self.assertTrue(engine._devices["diff_prot"]._last_differential["unsupported_mode"])


if __name__ == "__main__":
    unittest.main()
