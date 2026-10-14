"""安全相关节点：电磁攻击节点、网络攻击节点与频谱检测节点。

三者部署在被攻击对象附近，用于表达“攻击源”和“检测点”的位置关系：

- :class:`ElectromagneticAttackNode` 表示电流互感器附近的电磁干扰源。读数偏差本身由
  场景攻击计划与底座效果模型（``reading_offset``）施加，节点只表达自身状态。
- :class:`NetworkAttackNode` 表示接入过程网交换机的攻击源，攻击报文由它注入过程网。
  报文实例与接收判定仍由 GOOSE 接收实验负责，节点只表达注入来源与归属。
- :class:`SpectrumDetectorNode` 是频谱检测节点的占位实现，暂不产生识别结论。
"""
from __future__ import annotations

from typing import Any, Dict, List

from sim2030.contracts import DeviceSpec, EffectRequest
from sim2030.base.device import Device


class ElectromagneticAttackNode(Device):
    """电磁攻击节点：电流互感器附近的电磁干扰发射源。

    通过演示平面手动“开始攻击/结束攻击”施加或撤销对 ``target_asset_id`` 的读数偏移；
    读数偏差由底座效果模型（默认 ``reading_offset``）计算，节点本身不直接改写设备状态。

    参数：``target_asset_id``（作用目标测点，默认 ``ct_current``）、``reading_offset``
    （读数偏移量，默认 40.0）、``frequency_mhz``（干扰中心频率）、``power_dbm``（等效发射功率）。
    """

    # 手动攻击不设到期时间，由“结束攻击”显式撤销。
    HOLD_FOREVER_US = 2 ** 62

    def __init__(self, spec: DeviceSpec):
        super().__init__(spec)
        self.target_asset_id: str = self.parameters.get("target_asset_id", "ct_current")
        self.reading_offset: float = float(self.parameters.get("reading_offset", 40.0))
        self.effect_type: str = self.parameters.get("effect_type", "reading_offset")
        # 授时攻击等场景下攻击记录使用的类型名，默认电磁攻击。
        self.attack_type: str = self.parameters.get("attack_type", "electromagnetic")
        self.frequency_mhz: float = float(self.parameters.get("frequency_mhz", 0.0))
        self.power_dbm: float = float(self.parameters.get("power_dbm", 0.0))
        self.state.setdefault("emitting", False)

    @property
    def effect_id(self) -> str:
        return f"manual-em-{self.asset_id}"

    @property
    def emitting(self) -> bool:
        return bool(self.state.get("emitting"))

    def set_emitting(self, emitting: bool) -> None:
        self.state["emitting"] = bool(emitting)

    def describe_controls(self) -> List[Dict[str, Any]]:
        """演示平面控制项：随攻击开关状态在“开始攻击/结束攻击”之间切换。"""
        return [{
            "kind": "command",
            "action": "stop_attack" if self.emitting else "start_attack",
            "label": "结束攻击" if self.emitting else "开始攻击",
            "params": [],
        }]

    def build_effect_request(self, time_us: int) -> EffectRequest:
        if self.effect_type == "timing_offset":
            # 授时攻击：对目标设备时钟基准施加固定偏移（微秒），默认 100 ms。
            parameters = {"offset_us": int(self.parameters.get("offset_us", 100_000))}
        else:
            parameters = {"offset": self.reading_offset}
        return EffectRequest(
            effect_id=self.effect_id,
            target_id=self.target_asset_id,
            effect_type=self.effect_type,
            parameters=parameters,
            start_time_us=int(time_us),
            end_time_us=self.HOLD_FOREVER_US,
        )

    def snapshot(self) -> Dict[str, Any]:
        data = super().snapshot()
        data["state"]["target_asset_id"] = self.target_asset_id
        data["state"]["frequency_mhz"] = self.frequency_mhz
        data["state"]["power_dbm"] = self.power_dbm
        return data


class NetworkAttackNode(Device):
    """网络攻击节点：接入过程网交换机的攻击源。

    攻击报文由它注入过程网：接收实验按 ``attack_origin.asset_id`` 取本节点，报文
    ``sender_id`` 也记为本节点，使“谁在发攻击报文”在拓扑与接收记录里保持一致。

    参数：``access_switch_id``（接入的交换机资产，默认 ``sw-process``）、``attack_type``
    （攻击类型名，默认 ``network``）。节点本身不推进报文，也没有可切换的发射状态。
    """

    def __init__(self, spec: DeviceSpec):
        super().__init__(spec)
        self.access_switch_id: str = self.parameters.get("access_switch_id", "sw-process")
        self.attack_type: str = self.parameters.get("attack_type", "network")

    def describe_controls(self) -> List[Dict[str, Any]]:
        """下发项：逐帧注入、状态号投毒、批量洪泛三类攻击报文。

        样本是否已导入、以及洪泛对保护判据的影响，属于运行期实验状态，由演示平面
        在渲染时补充说明；节点自身只声明动作、批量参数与默认值。
        """
        return [
            {"kind": "command", "action": "send_attack_frame", "label": "下发伪造跳闸帧", "params": []},
            {"kind": "command", "action": "send_suppression_frame", "label": "下发消息抑制帧（状态号投毒）", "params": []},
            {"kind": "command", "action": "send_attack_burst", "label": "批量下发（DDoS 演示）",
             "params": [{"key": "count", "label": "批量数量（帧）", "value": 1000,
                         "input": {"min": 1, "max": 5000, "step": 1}}]},
        ]

    def snapshot(self) -> Dict[str, Any]:
        data = super().snapshot()
        data["state"]["access_switch_id"] = self.access_switch_id
        data["state"]["attack_type"] = self.attack_type
        return data


class SpectrumDetectorNode(Device):
    """频谱检测节点：占位实现，暂不产生检测结论（先不设计具体功能）。

    参数：``mode``（展示用检测模式，默认 ``频谱检测``）。
    """

    def __init__(self, spec: DeviceSpec):
        super().__init__(spec)
        self.state.setdefault("mode", self.parameters.get("mode", "频谱检测"))
