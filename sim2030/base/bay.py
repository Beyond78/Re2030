"""间隔层设备：状态监测、保护、测控。

三者都继承 ``Device``，通过消息与过程层/站控层交互。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sim2030.constants import BusinessType
from sim2030.contracts import DeviceSpec, Message
from sim2030.base.device import Device


class ConditionMonitor(Device):
    """状态监测装置：汇集设备状态与诊断结果，向站端上报异常和告警。"""

    def __init__(self, spec: DeviceSpec):
        super().__init__(spec)
        self.up_port: str = self.parameters.get("up_port", "up")
        self.in_port: str = self.parameters.get("in_port", "in")
        self.report_interval_us: int = int(self.parameters.get("report_interval_us", 1_000_000))
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._next_report_us: int = 0

    def receive(self, message: Message) -> None:
        if message.business_type in (BusinessType.STATUS, BusinessType.SAMPLING):
            self._latest[message.sender_id] = message.payload or {}
        else:
            self._inbox.append(message)

    def summarize(self, measurements: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "monitor_id": self.asset_id,
            "measurements": measurements,
            "alarm": self.parameters.get("alarm_rule", ""),
        }

    def step(self, time_us: int, dt_us: int, inputs: Dict[str, Any]) -> List[Message]:
        self._drain_inbox()
        messages = self._drain_outbox()
        if time_us < self._next_report_us or not self._latest:
            return messages
        self._next_report_us = time_us + self.report_interval_us
        messages.extend(
            self._new_message(self.up_port, BusinessType.STATUS, self.summarize(dict(self._latest)), time_us)
        )
        return messages


class ProtectionDevice(Device):
    """保护装置：根据电气采样值判断故障，发出跳闸指令并上报保护事件。"""

    def __init__(self, spec: DeviceSpec):
        super().__init__(spec)
        self.out_port: str = self.parameters.get("out_port", "out")
        self.in_port: str = self.parameters.get("in_port", "in")
        self.up_port: str = self.parameters.get("up_port", "up")
        self.eval_interval_us: int = int(self.parameters.get("eval_interval_us", 1_000_000))
        self._next_eval_us: int = 0
        # 按采集来源（合并单元）分别保留最近采样：复数采集单元之间不得相互覆盖。
        self._samples_by_source: Dict[str, Dict[str, Any]] = {}
        self._window: List[Dict[str, Any]] = []

    def receive(self, message: Message) -> None:
        if message.business_type == BusinessType.SAMPLING:
            payload = message.payload or {}
            source = payload.get("acquisition_id") or message.sender_id
            self._samples_by_source[source] = payload
        else:
            self._inbox.append(message)

    def evaluate_protection(self, samples: Dict[str, Any], time_us: int) -> List[Dict[str, Any]]:
        """针对单个来源的采样返回需要执行的保护动作列表（如 trip）。

        ``samples`` 是该来源的一份采样载荷（含 ``acquisition_id``）；复数采集单元
        时由 :meth:`step` 逐来源调用，实现类需按来源自行区分判断状态。
        """
        return []

    def evaluate_differential(self) -> List[Dict[str, Any]]:
        """跨来源判断（如差动保护）：默认不动作，由需要比较各采集来源的实现覆盖。

        返回的动作除 ``action_type`` / ``parameters`` 外，可附带 ``source``、``sample``、
        ``sources``、``differential``，用于在保护事件里说明触发依据。
        """
        return []

    def _emit_action(self, action: Dict[str, Any], messages: List[Message], time_us: int) -> None:
        """输出一次保护动作：跳闸命令 + 保护事件，事件保留触发来源与依据。"""
        payload = {
            "request_id": action.get("request_id", f"prot-{time_us}"),
            "action_type": action["action_type"],
            "parameters": dict(action.get("parameters") or {}),
        }
        messages.extend(
            self._new_message(self.out_port, BusinessType.COMMAND, payload, time_us, related_request=payload["request_id"])
        )
        event: Dict[str, Any] = {"action": action["action_type"]}
        for key in ("source", "sample", "sources", "differential"):
            if action.get(key) is not None:
                event[key] = action[key]
        messages.extend(self._new_message(self.up_port, BusinessType.PROTECTION, event, time_us))

    def step(self, time_us: int, dt_us: int, inputs: Dict[str, Any]) -> List[Message]:
        self._drain_inbox()
        messages = self._drain_outbox()
        if self.parameters.get("external_trip_replay", False):
            return messages
        if time_us < self._next_eval_us:
            return messages
        self._next_eval_us = time_us + self.eval_interval_us
        if not self._samples_by_source:
            return messages

        # 逐个来源独立判断：任一来源满足动作条件即出口，并在保护事件中标注来源。
        for source, sample in list(self._samples_by_source.items()):
            for action in self.evaluate_protection(sample, time_us):
                self._emit_action({**action, "source": source, "sample": sample}, messages, time_us)

        # 跨来源判断（如差动保护）：需要同时看到多套采集单元的量测。
        for action in self.evaluate_differential():
            self._emit_action(action, messages, time_us)

        return messages


class OverCurrentProtectionDevice(ProtectionDevice):
    """线路过流/过压保护示例实现，按配置阈值与持续时间判断。"""

    def __init__(self, spec: DeviceSpec):
        super().__init__(spec)
        # 每个来源单独累计持续越限次数：任一来源达到整定次数即可动作。
        self._persistent_ticks: Dict[str, int] = {}

    def evaluate_protection(self, samples: Dict[str, Any], time_us: int) -> List[Dict[str, Any]]:
        """按单一来源的采样判断：该来源持续越限达到整定次数即出口。"""
        threshold = float(self.parameters.get("overcurrent_threshold_a", 1000.0))
        duration_ticks = int(self.parameters.get("duration_ticks", 3))
        source = samples.get("acquisition_id") or "unknown"
        value = self._read_value(samples)
        if value is None:
            return []
        if value > threshold:
            self._persistent_ticks[source] = self._persistent_ticks.get(source, 0) + 1
        else:
            self._persistent_ticks[source] = 0
        if self._persistent_ticks[source] >= duration_ticks:
            self._persistent_ticks[source] = 0
            return [{"action_type": "trip", "parameters": {"source": source}}]
        return []

    def _read_value(self, samples: Dict[str, Any]):
        key = self.parameters.get("value_key", "current_a")
        s = samples.get("samples", samples)
        if isinstance(s, dict):
            return s.get(key)
        return None

    def describe_controls(self) -> List[Dict[str, Any]]:
        return [{
            "kind": "parameter",
            "key": "overcurrent_threshold_a",
            "label": "过流判断阈值",
            "unit": "A",
            "value": float(self.parameters.get("overcurrent_threshold_a", 1000.0)),
            "input": {"type": "number", "min": 0, "step": 1},
        }]

    def snapshot(self) -> Dict[str, Any]:
        snap = super().snapshot()
        snap.setdefault("overview", {})["overcurrent_threshold_a"] = float(
            self.parameters.get("overcurrent_threshold_a", 1000.0)
        )
        return snap


class DifferentialProtectionDevice(ProtectionDevice):
    """差动保护装置：比较各套合并单元上报的同一电气量，差值越限即出口跳闸。

    与逐来源判断的过流保护不同，本装置做**跨来源**判断：只有同时拿到至少两套采集
    单元的量测才比较，任一来源缺失时不动作。

    参数：``value_key``（参与比较的通道，默认 ``ct_current``）、
    ``differential_threshold_a``（差值整定值，默认 10.0）、
    ``compare_mode``（比较方式，当前仅实现 ``absolute`` 绝对值差）、
    ``duration_ticks``（所需连续越限次数，默认 1，即单次越限即出口）、
    ``eval_interval_us``（判断周期）。动作后保持，差流复归后才允许再次动作。
    """

    def __init__(self, spec: DeviceSpec):
        super().__init__(spec)
        self._persistent_ticks: int = 0
        # 动作后保持：差流复归前不重复下发跳闸命令，避免每个判断周期都刷一条相同动作。
        self._latched: bool = False
        self._last_differential: Dict[str, Any] = {}

    def differential(self) -> Optional[Dict[str, Any]]:
        """比较各来源在同一通道上的数值；来源不足两个或数值缺失时返回 ``None``。"""
        key = self.parameters.get("value_key", "ct_current")
        readings: Dict[str, float] = {}
        for source, sample in self._samples_by_source.items():
            value = ((sample or {}).get("samples") or {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                readings[source] = float(value)
        if len(readings) < 2:
            return None

        low_source = min(readings, key=lambda name: readings[name])
        high_source = max(readings, key=lambda name: readings[name])
        mode = str(self.parameters.get("compare_mode", "absolute"))
        threshold = float(self.parameters.get("differential_threshold_a", 10.0))
        difference = readings[high_source] - readings[low_source]
        if mode != "absolute":
            # 其他比较方式（如相对差）尚未实现，明确不动作而不是退回绝对值。
            return {
                "mode": mode, "readings": readings, "difference": difference,
                "threshold": threshold, "exceeded": False,
                "low_source": low_source, "high_source": high_source,
                "unsupported_mode": True,
            }
        return {
            "mode": mode,
            "readings": readings,
            "difference": difference,
            "threshold": threshold,
            "exceeded": difference > threshold,
            "low_source": low_source,
            "high_source": high_source,
        }

    def evaluate_differential(self) -> List[Dict[str, Any]]:
        report = self.differential()
        if report is None:
            self._persistent_ticks = 0
            return []
        self._last_differential = report

        if not report["exceeded"]:
            # 差流已复归：清零并解除保持，允许下一次越限重新动作。
            self._persistent_ticks = 0
            self._latched = False
            return []

        duration_ticks = max(1, int(self.parameters.get("duration_ticks", 1)))
        if self._latched:
            return []
        self._persistent_ticks += 1
        if self._persistent_ticks < duration_ticks:
            return []

        self._persistent_ticks = 0
        self._latched = True
        return [{
            "action_type": "trip",
            "parameters": {
                "source": "differential",
                "difference_a": round(report["difference"], 6),
                "threshold_a": report["threshold"],
                "compare_mode": report["mode"],
            },
            "source": "differential",
            # 保护事件带上两套原始量测，站端可如实并列表述是哪两套读数不一致。
            "sources": {name: dict(self._samples_by_source[name]) for name in report["readings"]},
            "differential": {
                "difference_a": round(report["difference"], 6),
                "threshold_a": report["threshold"],
                "compare_mode": report["mode"],
                "low_source": report["low_source"],
                "high_source": report["high_source"],
                "readings": dict(report["readings"]),
            },
        }]

    def describe_controls(self) -> List[Dict[str, Any]]:
        return [{
            "kind": "parameter",
            "key": "differential_threshold_a",
            "label": "差流整定值",
            "unit": "A",
            "value": float(self.parameters.get("differential_threshold_a", 10.0)),
            "input": {"type": "number", "min": 0, "step": 0.1},
        }]

    def snapshot(self) -> Dict[str, Any]:
        snap = super().snapshot()
        overview = snap.setdefault("overview", {})
        overview["differential_threshold_a"] = float(self.parameters.get("differential_threshold_a", 10.0))
        overview["compare_mode"] = str(self.parameters.get("compare_mode", "absolute"))
        if self._last_differential:
            overview["last_differential"] = dict(self._last_differential)
        return snap


class MeasurementControlDevice(Device):
    """测控装置：形成业务量测，检查并下发操作命令，跟踪执行状态。"""

    def __init__(self, spec: DeviceSpec):
        super().__init__(spec)
        self.up_port: str = self.parameters.get("up_port", "up")
        self.mu_port: str = self.parameters.get("mu_port", "mu")
        self.command_ports: Dict[str, str] = self.parameters.get("command_ports", {"*": "control"})
        self.report_interval_us: int = int(self.parameters.get("report_interval_us", 1_000_000))
        # 按来源（合并单元）分别保留最近量测：多套采集单元的量测互不覆盖，便于如实上送。
        self._samples_by_source: Dict[str, Dict[str, Any]] = {}
        self._last_commands: List[Dict[str, Any]] = []
        self._last_feedback: List[Dict[str, Any]] = []
        self._next_report_us: int = 0

    def receive(self, message: Message) -> None:
        if message.business_type == BusinessType.SAMPLING:
            payload = message.payload or {}
            source = payload.get("acquisition_id") or message.sender_id
            self._samples_by_source[source] = payload
        elif message.business_type == BusinessType.COMMAND:
            payload = message.payload or {}
            ok, reason = self.check_command(payload)
            self._last_commands.append({
                "time_us": message.created_time_us,
                "action": payload.get("action_type", ""),
                "status": "dispatched" if ok else "rejected",
                "reason": reason,
                "request_id": payload.get("request_id", ""),
                "target": (payload.get("parameters") or {}).get("target", ""),
            })
            self._last_commands = self._last_commands[-8:]
            if not ok:
                self._queue(
                    self._new_message(
                        self.up_port, BusinessType.FEEDBACK,
                        {"request_id": payload.get("request_id", ""), "status": "rejected", "reason": reason},
                        message.created_time_us, related_request=payload.get("request_id", ""),
                    )
                )
            else:
                action_type = payload.get("action_type", "")
                port = self.command_ports.get(action_type, self.command_ports.get("*", "control"))
                self._queue(
                    self._new_message(port, BusinessType.COMMAND, payload, message.created_time_us,
                                      related_request=payload.get("request_id", ""))
                )
        elif message.business_type == BusinessType.FEEDBACK:
            self._last_feedback.append({
                "time_us": message.created_time_us,
                "request_id": message.related_request or (message.payload or {}).get("request_id", ""),
                "payload": message.payload or {},
            })
            self._last_feedback = self._last_feedback[-8:]
            self._queue(
                self._new_message(self.up_port, BusinessType.FEEDBACK, message.payload, message.created_time_us,
                                  related_request=message.related_request)
            )
        else:
            self._inbox.append(message)

    def check_command(self, command: Dict[str, Any]):
        """检查操作条件；返回 (是否允许, 拒绝原因)。"""
        action_type = command.get("action_type", "")
        if not action_type:
            return False, "缺少 action_type"
        allowed = self.parameters.get("allowed_actions", None)
        if allowed is not None and action_type not in allowed:
            return False, f"不允许动作 {action_type}"
        return True, ""

    def build_telemetry(self) -> Dict[str, Any]:
        """按来源分组上送量测：保留每一套采集单元各自的量测值，不做合并或择优。"""
        return {
            "measurement_id": self.asset_id,
            "sources": {source: dict(payload) for source, payload in self._samples_by_source.items()},
        }

    def snapshot(self) -> Dict[str, Any]:
        data = super().snapshot()
        data["overview"] = {
            "samples_by_source": {source: dict(payload) for source, payload in self._samples_by_source.items()},
            "last_commands": list(self._last_commands),
            "last_feedback": list(self._last_feedback),
        }
        return data

    def step(self, time_us: int, dt_us: int, inputs: Dict[str, Any]) -> List[Message]:
        self._drain_inbox()
        messages = self._drain_outbox()
        if time_us >= self._next_report_us and self._samples_by_source:
            self._next_report_us = time_us + self.report_interval_us
            messages.extend(self._new_message(self.up_port, BusinessType.STATUS, self.build_telemetry(), time_us))
        return messages