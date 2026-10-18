"""通过既有设备注册、业务消息和记录接口接入；不修改公共模块。"""
from __future__ import annotations

import uuid
from pathlib import Path

from sim2030.application import Application
from sim2030.base.device import Device
from sim2030.base.process import AuxiliaryEquipment, PrimaryEquipment, SensorDevice, AcquisitionDevice, TransformerEquipment
from sim2030.base.bay import MeasurementControlDevice
from sim2030.constants import BusinessType
from sim2030.scenario import register_device

from acoustic_demo.model import DemoModel

SCENARIO = Path(__file__).with_name("scenario.json")


class AcousticFan(AuxiliaryEquipment):
    def physical_outputs(self):
        if not hasattr(self, "model"):
            return super().physical_outputs()
        return {"running": self.model.powered, "speed_ratio": self.model.speed_ratio,
                "motor_current_a": self.model.current_a,
                "rotation_speed_rpm": 1500 * self.model.speed_ratio}

    def check_interlock(self, action_type, parameters, inputs):
        if action_type == "start" and self.model.trip_latched:
            return "振动保护已锁定，请先关闭干扰，待读数恢复后复位"
        return super().check_interlock(action_type, parameters, inputs)

    def _apply_action(self, action_type, parameters, time_us=0):
        super()._apply_action(action_type, parameters, time_us)
        if action_type in ("start", "stop"):
            self.model.set_power(action_type == "start", time_us / 1e6)

    def step(self, time_us, dt_us, inputs):
        self.model.advance(time_us / 1e6)
        messages = super().step(time_us, dt_us, inputs)
        self.state.update(self.physical_outputs())
        return messages


class AcousticTransformer(TransformerEquipment):
    def step(self, time_us, dt_us, inputs):
        # 保留分接动作能力，替换本场景中的通用热模型，避免重复计算温度。
        messages = PrimaryEquipment.step(self, time_us, dt_us, inputs)
        self.model.advance(time_us / 1e6)
        self.state["oil_temp_c"] = self.model.oil_c
        return messages

    def physical_outputs(self):
        result = super().physical_outputs()
        if hasattr(self, "model"):
            result.update(oil_temp_c=self.model.oil_c, oil_sensor_c=self.model.oil_sensor_c)
        return result


class AcousticSensor(SensorDevice):
    def step(self, time_us, dt_us, inputs):
        messages = self._drain_outbox()
        for feature in self.model.sample_to(time_us / 1e6):
            self.state.update(last_value=feature["max_ratio"], last_sample_time_us=time_us, **feature)
            messages.extend(self._new_message(self.up_port, BusinessType.SAMPLING,
                {"sensor_id": self.asset_id, "value": feature["max_ratio"],
                 "unit": "normal RMS ratio", "sample_time_us": time_us,
                 "vibration": feature}, time_us))
        return messages


class AcousticAcquisition(AcquisitionDevice):
    def build_measurement(self, time_us):
        payload = super().build_measurement(time_us)
        payload["vibration"] = dict(self._latest.get("fan_vibration", {}).get("vibration", {}))
        payload["source_samples"] = {key: dict(value) for key, value in self._latest.items()}
        return payload


class AcousticMeasurementControl(MeasurementControlDevice):
    def __init__(self, spec):
        super().__init__(spec)
        self.fan_sample = {}
        self.oil_sample = {}

    def receive(self, message):
        if message.business_type == BusinessType.SAMPLING and message.sender_id == "fan_condition_monitor":
            self.fan_sample = dict(message.payload or {})
        if message.business_type == BusinessType.SAMPLING and message.sender_id == "mu":
            self.oil_sample = dict((message.payload or {}).get("source_samples", {}).get("oil_temp", {}))
        super().receive(message)

    def step(self, time_us, dt_us, inputs):
        messages = super().step(time_us, dt_us, inputs)
        for message in messages:
            if message.business_type == BusinessType.STATUS:
                message.payload["fan_sample"] = self.fan_sample
                message.payload["oil_sample"] = self.oil_sample
        return messages


class AcousticSource(Device):
    def describe_controls(self):
        return [{"kind": "command", "action": action, "label": label, "params": []}
                for action, label in (("start_weak", "原样本幅度 κ=0.30"),
                                      ("start_strong", "增强仿真 κ=0.90"),
                                      ("stop_attack", "关闭虚拟声波"))]


class AcousticApplication(Application):
    def __init__(self, output_root="runs"):
        for name, factory in {
            "acoustic_fan": AcousticFan, "acoustic_transformer": AcousticTransformer,
            "acoustic_sensor": AcousticSensor, "acoustic_acquisition": AcousticAcquisition,
            "acoustic_measurement_control": AcousticMeasurementControl,
            "acoustic_source": AcousticSource,
        }.items():
            register_device(name, factory)
        super().__init__(output_root)
        self.acoustic = None

    def load(self, config_path, goose_options=None):
        self.acoustic = None
        run_id = super().load(config_path, goose_options)
        if self.config.scenario_id != "substation-acoustic-demo":
            return run_id
        self.acoustic = DemoModel()
        self.acoustic.trip_latched = False
        for device in self.engine._devices.values():
            if device.spec.device_type.startswith("acoustic_"):
                device.model = self.acoustic
        self.rule = dict(self.engine.get_device("station").parameters["acoustic_protection"])
        self.events, self.history = [], []
        self.attack_preview = None
        self.consecutive, self.last_window = 0, 0
        self.last_oil_sample_us = -1
        self.last_measurement = {}
        self.command_due_s = None
        self.trip_request_id = None
        self.engine.message_handler = self._observe_delivery
        self.recorder.append("acoustic_meta", {"time_us": 0, "provenance": self.acoustic.provenance,
            "rule": self.rule, "data_origin": "SIMULATION_NOT_HARDWARE", "model_parameters": {
                "nominal_current_a": 2, "nominal_speed_rpm": 1500,
                "current_lag_s": .25, "speed_coast_s": 5, "oil_sensor_lag_s": 5,
                "P_loss_w": 30000, "C_j_per_k": 3600000, "G0_w_per_k": 500,
                "Gf_w_per_k": 500, "ambient_c": 25, "initial_oil_c": 55,
                "all_parameters_validated": False}})
        return run_id

    def _event(self, kind, time_s, **details):
        record = {"kind": kind, "time_us": round(time_s * 1e6),
                  "observed_time_s": time_s, "data_origin": "APP_SIM_NOT_PCAP", **details}
        if kind == "measurement_received":
            self.last_measurement = record
        else:
            self.events.append(record)
        self.events = self.events[-100:]
        if self.recorder:
            self.recorder.append("acoustic_path", record)

    def _observe_delivery(self, message, time_us):
        payload, now = message.payload or {}, time_us / 1e6
        if message.business_type in (BusinessType.COMMAND, BusinessType.FEEDBACK):
            if message.sender_id in {"station", "mc", "ctl_cool", "cool"} and message.receiver_id in {"station", "mc", "ctl_cool", "cool"}:
                self._event("message_delivered", now, src=message.sender_id, dst=message.receiver_id,
                    business_type=message.business_type, message_id=message.message_id,
                    request_id=message.related_request, source_time_s=message.created_time_us / 1e6,
                    scheduled_delivery_s=message.deliver_time_us / 1e6, payload=payload)
        if message.receiver_id == "station" and message.business_type == BusinessType.STATUS:
            sample = payload.get("fan_sample", {})
            vibration = sample.get("vibration", {})
            end = vibration.get("window_end_s", 0)
            if end > self.last_window:
                if end != self.last_window + 1:
                    self.consecutive = 0
                self.last_window = end
                exceeded = vibration["max_ratio"] > self.rule["threshold_ratio"]
                self.consecutive = self.consecutive + 1 if exceeded else 0
                self._event("measurement_received", now, src="mc", dst="station",
                    source_time_s=end, rms_ratio_xyz=vibration["rms_ratio_xyz"],
                    consecutive_windows=self.consecutive,
                    fan_current_a=sample.get("samples", {}).get("fan_current"),
                    fan_speed_rpm=sample.get("samples", {}).get("fan_speed"))
                if self.consecutive >= self.rule["consecutive_windows"] and not self.acoustic.trip_latched:
                    self.acoustic.trip_latched = True
                    self.command_due_s = now + self.rule["delay_s"]
                    self.trip_request_id = "acoustic-trip-" + uuid.uuid4().hex[:12]
                    self._event("threshold_trip", now, source_time_s=end,
                        request_id=self.trip_request_id, command_due_s=self.command_due_s)
            oil = payload.get("oil_sample", {})
            source_us = oil.get("sample_time_us", -1)
            if source_us > self.last_oil_sample_us:
                self.last_oil_sample_us = source_us
                if self.recorder:
                    self.recorder.append("acoustic_path", {"kind": "oil_measurement_received",
                        "time_us": time_us, "observed_time_s": now,
                        "src": "mc", "dst": "station", "path": ["oil_temp", "mu", "mc", "station"],
                        "source_time_s": source_us / 1e6, "value": oil.get("value"), "unit": "C",
                        "data_origin": "APP_SIM_NOT_PCAP"})
        return False  # 只旁观投递，继续交给真实的仿真设备处理。

    def _submit_operation_locked(self, request):
        if self.acoustic is not None and request.get("target_asset_id") == "sensor_attack_node":
            if self.status not in {"ready", "paused", "running"}:
                return {"status": "rejected", "reason": "运行已结束，请新建运行"}
            action = request.get("action_type")
            if action not in {"start_weak", "start_strong", "start_attack", "stop_attack", "reset_protection"}:
                return {"status": "rejected", "reason": "不支持的声波操作"}
            now = self.current_time_us / 1e6
            if action == "reset_protection":
                if self.acoustic.response.gain or not self.acoustic.feature or self.acoustic.feature["max_ratio"] > self.rule["threshold_ratio"]:
                    return {"status": "rejected", "reason": "先关闭干扰，并推进到一个正常振动窗口后再复位"}
                self.acoustic.trip_latched = False
                self.command_due_s = None
                self.consecutive = 0
            else:
                gain = {"start_weak": .30, "start_strong": .90, "start_attack": .30, "stop_attack": 0}[action]
                try:
                    self.acoustic.response.switch(gain, now)
                except ValueError as exc:
                    return {"status": "rejected", "reason": str(exc)}
                self.engine.get_device("sensor_attack_node").state.update(emitting=bool(gain), normalized_gain=gain)
            self._event(action, now, request_id=request.get("request_id", ""))
            return {"status": "accepted", "action_type": action, "time_us": self.current_time_us}
        return super()._submit_operation_locked(request)

    def _step_locked(self):
        if self.acoustic is not None and self.status == "running" and self.command_due_s is not None:
            now = self.current_time_us / 1e6
            if now + 1e-9 >= self.command_due_s:
                receipt = self.engine.submit_business_operation({"time_us": self.current_time_us,
                    "request_id": self.trip_request_id, "target_asset_id": "cool", "action_type": "stop",
                    "parameters": {"reason": "simulated_vibration_threshold"}})
                self._event("stop_command_issued", now, src="station", dst="mc",
                            request_id=self.trip_request_id, receipt=receipt)
                self.command_due_s = None
        return super()._step_locked()

    def _record_step(self, result, *args):
        super()._record_step(result, *args)
        if self.acoustic is not None and result.time_us % 1_000_000 == 0:
            if (self.acoustic.powered and self.acoustic.response.gain and self.acoustic.last_wave
                    and any(abs(row[5]) > 1e-8 for row in self.acoustic.last_wave)):
                self.attack_preview = {"time_s": result.time_us / 1e6,
                    "gain": self.acoustic.response.gain,
                    "wave": list(self.acoustic.last_wave),
                    "source_wave": self._source_wave(self.acoustic.time_s)}
            row = {"time_s": result.time_us / 1e6, "powered": self.acoustic.powered,
                   "current_a": self.acoustic.current_a, "speed_rpm": 1500 * self.acoustic.speed_ratio,
                   "oil_c": self.acoustic.oil_sensor_c, "oil_reference_c": self.acoustic.oil_c,
                   "rms_ratio_xyz": (self.acoustic.feature or {}).get("rms_ratio_xyz", [None] * 3)}
            self.history.append(row)
            self.history = self.history[-1801:]
            self.recorder.append("acoustic_series", {"time_us": result.time_us, **row})

    def _source_wave(self, time_s):
        return [[t, self.acoustic.response.input_at(t)] for t in
                (max(0, time_s - .004) + i / 88000 for i in range(352)) if t <= time_s]

    def _get_view_locked(self, view_name, query=None):
        if view_name == "acoustic":
            if self.acoustic is None:
                return {"available": False}
            return {"available": True, "run_id": self.run_id, "time_s": self.current_time_us / 1e6,
                "model_time_s": self.acoustic.time_s,
                "source_gain": self.acoustic.response.gain, "powered": self.acoustic.powered,
                "current_a": self.acoustic.current_a, "speed_rpm": 1500 * self.acoustic.speed_ratio,
                "oil_c": self.acoustic.oil_sensor_c, "cutoff_s": self.acoustic.cutoff_s,
                "trip_latched": self.acoustic.trip_latched, "consecutive": self.consecutive,
                "rule": self.rule, "feature": self.acoustic.feature,
                "source_wave": self._source_wave(self.acoustic.time_s),
                "attack_preview": self.attack_preview,
                "last_received": self.last_measurement,
                "wave_columns": ["time_s", "excitation_norm", "base_x", "base_y", "base_z",
                                 "delta_x", "delta_y", "delta_z", "observed_x", "observed_y", "observed_z"],
                "wave": self.acoustic.last_wave, "history": self.history, "events": self.events,
                "provenance": self.acoustic.provenance}
        return super()._get_view_locked(view_name, query)
