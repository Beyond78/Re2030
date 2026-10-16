"""演示平面 HTTP 服务。

``PresentationServer`` 把前端静态页面、运行接口与回放接口合并到一个标准库
HTTP 服务里，不依赖第三方包。所有接口都通过 ``handle_request(method, path, body)``
返回统一响应结构，便于直接用单元测试断言；``serve`` 仅负责绑定端口并轮询。
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from sim2030.application import Application
from sim2030.base.observations import EvidenceReader
from sim2030.evaluation import Evaluator
from sim2030.presentation import views as presentation_views
from sim2030.records import RunReader
from sim2030.scenario import load_scenario, validate_scenario
from sim2030.goose_capture import SAMPLES

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
_EDITOR_LINK_TYPES = {
    "wired", "station", "process", "mirror", "management", "measurement", "timing",
    "wireless", "interference", "attack-access", "hardwire", "electrical", "physical",
}


class PresentationServer:
    """无阻塞地组织页面接口；应用实例同时只承载一个运行。"""

    def __init__(self, scenario_dir: str = "scenarios", output_root: str = "runs") -> None:
        self.scenario_dir = os.path.abspath(scenario_dir)
        self.output_root = os.path.abspath(output_root)
        self.application = Application(output_root=self.output_root)
        self._scenario_paths: Dict[str, str] = {}
        self._seen_request_ids: Dict[str, Dict[str, Any]] = {}
        self._refresh_scenario_paths()

    def close(self) -> None:
        """关闭内部应用持有的文件句柄，便于目录清理与进程退出。"""
        self.application.close()

    # ──────────────────────────────────────────────
    # 场景与运行注册
    # ──────────────────────────────────────────────
    def _refresh_scenario_paths(self) -> None:
        paths: Dict[str, str] = {}
        if os.path.isdir(self.scenario_dir):
            for name in sorted(os.listdir(self.scenario_dir)):
                if not name.lower().endswith(".json"):
                    continue
                full = os.path.join(self.scenario_dir, name)
                if os.path.isfile(full):
                    scenario_id = self._read_scenario_id(full)
                    if scenario_id:
                        paths[scenario_id] = full
        self._scenario_paths = paths

    @staticmethod
    def _read_scenario_id(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                return str(raw.get("scenario_id", ""))
        except (OSError, ValueError, json.JSONDecodeError):
            return ""
        return ""

    def _scenario_path_for(self, scenario_id: str) -> Optional[str]:
        if scenario_id in self._scenario_paths:
            return self._scenario_paths[scenario_id]
        candidate = scenario_id
        if candidate.endswith(".json") and os.path.isfile(candidate):
            return candidate
        for path in self._scenario_paths.values():
            if os.path.basename(path) == scenario_id:
                return path
        return None

    # ──────────────────────────────────────────────
    # 路由分发
    # ──────────────────────────────────────────────
    def handle_request(self, method: str, path: str, body: Optional[bytes] = None) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        """返回 ``(状态码, 响应字典, 响应头)``。

        状态码用于 HTTP 层；响应字典始终包含 ``status``/``error`` 中的一种，便于
        前端和测试直接判断结果。
        """
        method = (method or "GET").upper()
        target = urlsplit(path or "/")
        route = target.path.rstrip("/") or "/"
        query = parse_qs(target.query, keep_blank_values=True)

        try:
            if route == "/" or route == "/index.html":
                return self._static("index.html")
            if route in ("/admin", "/admin.html"):
                return self._static("admin.html")
            if route in ("/app.js", "/static/app.js"):
                return self._static("app.js")
            if route in ("/network-topology.js", "/static/network-topology.js"):
                return self._static("network-topology.js")
            if route in ("/simulation-player.js", "/static/simulation-player.js"):
                return self._static("simulation-player.js")
            if route in ("/topology-layout-editor.js", "/static/topology-layout-editor.js"):
                return self._static("topology-layout-editor.js")
            if route in ("/style.css", "/static/style.css"):
                return self._static("style.css")

            if route == "/api/scenarios" and method == "GET":
                return self._list_scenarios()
            if route == "/api/scenarios/clone" and method == "POST":
                return self._clone_scenario(self._parse_json(body))
            if route.startswith("/api/scenarios/") and method == "DELETE":
                parts = route.split("/")
                if len(parts) == 4 and parts[3]:
                    return self._delete_scenario(unquote(parts[3]))
                return 400, {"error": "缺少场景 ID"}, _JSON_HEADERS
            if route == "/api/runs" and method == "POST":
                return self._create_run(self._parse_json(body))
            if route.startswith("/api/runs/") and route.endswith("/operations") and method == "POST":
                run_id = route.split("/")[3]
                return self._submit_operation(run_id, self._parse_json(body))
            if route.startswith("/api/runs/") and route.endswith("/parameters") and method == "POST":
                parts = route.split("/")
                if len(parts) == 7 and parts[4] == "devices":
                    return self._set_device_parameter(parts[3], parts[5], self._parse_json(body))
            if route.startswith("/api/runs/") and route.endswith("/controls") and method == "GET":
                parts = route.split("/")
                if len(parts) == 7 and parts[4] == "devices":
                    return self._get_device_controls(parts[3], parts[5])
            if route.startswith("/api/runs/") and route.endswith("/views") and method == "GET":
                run_id = route.split("/")[3]
                return self._views(run_id, query)
            if route.startswith("/api/runs/") and route.endswith("/evidence") and method == "GET":
                run_id = route.split("/")[3]
                return self._evidence(run_id, query)

            return 404, {"error": f"未找到接口 {method} {route}"}, _JSON_HEADERS
        except Exception as exc:  # 演示平面不因单次请求异常拖垮服务
            return 500, {"error": f"处理请求失败：{exc}"}, _JSON_HEADERS

    # ──────────────────────────────────────────────
    # 静态资源
    # ──────────────────────────────────────────────
    def _static(self, name: str) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            return 404, {"error": f"缺少静态资源 {name}"}, _JSON_HEADERS
        content_type = "text/html; charset=utf-8" if name.endswith(".html") else (
            "application/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
        )
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        return 200, {"__raw__": text}, {"Content-Type": content_type, "Cache-Control": "no-store"}

    @staticmethod
    def _parse_json(body: Optional[bytes]) -> Dict[str, Any]:
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            raise ValueError("请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    # ──────────────────────────────────────────────
    # 接口实现
    # ──────────────────────────────────────────────
    def _list_scenarios(self) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        self._refresh_scenario_paths()
        scenarios: List[Dict[str, Any]] = []
        for scenario_id, path in sorted(self._scenario_paths.items()):
            hidden_scenarios = {"substation-attack-demo", "substation-network-attack"}
            if scenario_id in hidden_scenarios or ("substation-goose-lab" in self._scenario_paths and scenario_id in {"substation-goose-normal", "substation-goose-forgery", "substation-goose-suppression", "substation-goose-dos"}):
                continue
            try:
                config = load_scenario(path)
                capture_dir = (config.observations or {}).get("provider_dir", "")
                data_available = not config.layout.get("packet_replay") or all(
                    os.path.isfile(os.path.join(capture_dir, name))
                    for name in ("events.jsonl", "capture-index.json", "capture.pcap", "communication-log.jsonl", "manifest.json")
                )
                sample_availability = {}
                if config.layout.get("goose_lab"):
                    for sample in SAMPLES:
                        sample_availability[sample] = all(os.path.isfile(os.path.join(os.path.dirname(capture_dir), sample, name)) for name in ("events.jsonl", "capture-index.json", "capture.pcap", "communication-log.jsonl", "manifest.json"))
                    data_available = any(sample_availability.values())
                scenarios.append({
                    "scenario_id": config.scenario_id,
                    "name": config.name,
                    "goose_lab": bool(config.layout.get("goose_lab")),
                    "goose_samples": sample_availability,
                    "path": path,
                    "observation_mode": "file_replay",
                    "recognition_enabled": bool((config.recognition or {}).get("enabled")),
                    "defense_enabled": bool((config.defense or {}).get("enabled")),
                    "device_count": len(config.devices),
                    "attack_count": len(config.attacks),
                    "data_available": data_available,
                    "unavailable_reason": "" if data_available else "请先导入 message_core.zip",
                })
            except Exception:
                continue
        return 200, {"scenarios": scenarios}, _JSON_HEADERS

    def _delete_scenario(self, scenario_id: str) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        """删除场景配置文件；运行中的当前场景不能被删除。"""
        scenario_id = str(scenario_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", scenario_id):
            return 400, {"error": "场景 ID 无效"}, _JSON_HEADERS

        current_config = self.application.config
        if (
            current_config is not None
            and current_config.scenario_id == scenario_id
            and self.application.run_id
            and self.application.status in {"ready", "running", "paused"}
        ):
            return 409, {"error": "当前场景正在运行，请先停止运行后再删除"}, _JSON_HEADERS

        self._refresh_scenario_paths()
        path = self._scenario_paths.get(scenario_id)
        if path is None:
            return 404, {"error": f"未找到场景 {scenario_id}"}, _JSON_HEADERS

        scenario_root = os.path.realpath(self.scenario_dir)
        resolved_path = os.path.realpath(path)
        if os.path.dirname(resolved_path) != scenario_root:
            return 400, {"error": "场景文件不在场景目录内"}, _JSON_HEADERS
        try:
            os.unlink(resolved_path)
        except FileNotFoundError:
            self._refresh_scenario_paths()
            return 404, {"error": f"未找到场景 {scenario_id}"}, _JSON_HEADERS
        except OSError as exc:
            return 409, {"error": f"删除场景失败：{exc}"}, _JSON_HEADERS
        self._refresh_scenario_paths()
        return 200, {"status": "deleted", "scenario_id": scenario_id, "path": resolved_path}, _JSON_HEADERS

    def _clone_scenario(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        """复制一个已有场景并以独立 JSON 文件发布到场景目录。"""
        source_id = str(body.get("source_scenario_id") or body.get("scenario_id") or "").strip()
        scenario_id = str(body.get("new_scenario_id") or "").strip()
        name = str(body.get("name") or "").strip()
        if not source_id or not scenario_id or not name:
            return 400, {"error": "必须填写原场景、新场景 ID 和场景名称"}, _JSON_HEADERS
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", scenario_id):
            return 400, {"error": "新场景 ID 只能包含字母、数字、下划线和短横线，长度为 2-64"}, _JSON_HEADERS
        if len(name) > 80:
            return 400, {"error": "场景名称不能超过 80 个字符"}, _JSON_HEADERS
        self._refresh_scenario_paths()
        if scenario_id in self._scenario_paths:
            return 409, {"error": f"场景 ID 已存在：{scenario_id}"}, _JSON_HEADERS
        source_path = self._scenario_path_for(source_id)
        if source_path is None:
            return 404, {"error": f"未找到原场景 {source_id}"}, _JSON_HEADERS
        try:
            with open(source_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                return 400, {"error": "原场景配置不是 JSON 对象"}, _JSON_HEADERS
            validation_errors = validate_scenario(load_scenario(source_path))
            if validation_errors:
                return 400, {"error": "原场景校验失败：" + "；".join(validation_errors)}, _JSON_HEADERS
            raw["scenario_id"] = scenario_id
            raw["name"] = name
            if "topology_layout" in body:
                try:
                    editor_layout = self._sanitize_editor_layout(body["topology_layout"])
                except ValueError as exc:
                    return 400, {"error": f"拓扑布局无效：{exc}"}, _JSON_HEADERS
                scenario_layout = dict(raw.get("layout") or {})
                scenario_layout["editor"] = editor_layout
                raw["layout"] = scenario_layout
                runtime_report = self._materialize_editor_runtime(raw, editor_layout)
            else:
                runtime_report = {"devices_added": 0, "links_added": 0, "display_only_modules": [], "display_only_links": []}
            os.makedirs(self.scenario_dir, exist_ok=True)
            target_path = os.path.join(self.scenario_dir, scenario_id + ".json")
            fd, temp_path = tempfile.mkstemp(prefix=f".{scenario_id}.", suffix=".json", dir=self.scenario_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(raw, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                published_config = load_scenario(temp_path)
                published_errors = validate_scenario(published_config)
                if published_errors:
                    return 400, {"error": "发布后的场景校验失败：" + "；".join(published_errors)}, _JSON_HEADERS
                # 链接创建是独占的：不会因为并发发布而替换已有场景文件。
                # 临时文件与目标文件位于同一目录，因此不跨文件系统。
                os.link(temp_path, target_path)
            except FileExistsError:
                return 409, {"error": f"场景 ID 已存在：{scenario_id}"}, _JSON_HEADERS
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            self._refresh_scenario_paths()
            return 201, {"scenario": {"scenario_id": scenario_id, "name": name, "path": target_path},
                         "runtime": runtime_report, "status": "published"}, _JSON_HEADERS
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return 400, {"error": f"复制场景失败：{exc}"}, _JSON_HEADERS

    @staticmethod
    def _sanitize_editor_layout(value: Any) -> Dict[str, Any]:
        """限制后台编辑器布局的形状，避免把任意请求内容写入场景文件。"""
        if not isinstance(value, dict):
            raise ValueError("必须是对象")

        def finite_number(item: Any, label: str, minimum: float, maximum: float) -> int:
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
                raise ValueError(f"{label} 必须是有限数值")
            number = int(round(float(item)))
            if number < minimum or number > maximum:
                raise ValueError(f"{label} 超出允许范围")
            return number

        def positions(name: str) -> Dict[str, Dict[str, int]]:
            source = value.get(name, {})
            if not isinstance(source, dict) or len(source) > 512:
                raise ValueError(f"{name} 必须是对象且不能超过 512 项")
            result = {}
            for item_id, position in source.items():
                if not isinstance(item_id, str) or not item_id or len(item_id) > 120 or not isinstance(position, dict):
                    raise ValueError(f"{name} 包含无效项目")
                result[item_id] = {
                    "x": finite_number(position.get("x"), f"{name}.{item_id}.x", -10000, 10000),
                    "y": finite_number(position.get("y"), f"{name}.{item_id}.y", -10000, 10000),
                }
            return result

        def sizes(name: str) -> Dict[str, Dict[str, int]]:
            source = value.get(name, {})
            if not isinstance(source, dict) or len(source) > 512:
                raise ValueError(f"{name} 必须是对象且不能超过 512 项")
            result = {}
            for item_id, size in source.items():
                if not isinstance(item_id, str) or not item_id or len(item_id) > 120 or not isinstance(size, dict):
                    raise ValueError(f"{name} 包含无效项目")
                result[item_id] = {
                    "width": finite_number(size.get("width"), f"{name}.{item_id}.width", 20, 2000),
                    "height": finite_number(size.get("height"), f"{name}.{item_id}.height", 20, 2000),
                }
            return result

        positions_data = positions("positions")
        sizes_data = sizes("sizes")
        layer_sizes = sizes("layerSizes")

        modules = value.get("customModules", [])
        if not isinstance(modules, list) or len(modules) > 256:
            raise ValueError("customModules 必须是数组且不能超过 256 项")
        custom_modules = []
        for module in modules:
            if not isinstance(module, dict):
                raise ValueError("customModules 包含无效项目")
            module_id = module.get("id")
            if not isinstance(module_id, str) or not module_id or len(module_id) > 120:
                raise ValueError("customModules.id 无效")
            copied = {"id": module_id}
            for field in ("kind", "appearance", "name", "sourceName", "sourceId"):
                if field in module and module[field] is not None:
                    if not isinstance(module[field], str) or len(module[field]) > 120:
                        raise ValueError(f"customModules.{field} 无效")
                    copied[field] = module[field]
            copied["x"] = finite_number(module.get("x"), f"customModules.{module_id}.x", -10000, 10000)
            copied["y"] = finite_number(module.get("y"), f"customModules.{module_id}.y", -10000, 10000)
            copied["width"] = finite_number(module.get("width"), f"customModules.{module_id}.width", 20, 2000)
            copied["height"] = finite_number(module.get("height"), f"customModules.{module_id}.height", 20, 2000)
            custom_modules.append(copied)

        links = value.get("customLinks", [])
        if not isinstance(links, list) or len(links) > 512:
            raise ValueError("customLinks 必须是数组且不能超过 512 项")
        custom_links = []
        for link in links:
            if not isinstance(link, dict) or not all(isinstance(link.get(key), str) and link[key] for key in ("id", "source", "target")):
                raise ValueError("customLinks 包含无效项目")
            link_type = link.get("type", "wired")
            if not isinstance(link_type, str) or link_type not in _EDITOR_LINK_TYPES:
                raise ValueError(f"customLinks.type 无效：{link_type}")
            copied_link = {"id": link["id"][:120], "source": link["source"][:120], "target": link["target"][:120], "type": link_type}
            for field in ("sourcePort", "targetPort"):
                if field in link and link[field] is not None:
                    if not isinstance(link[field], str) or len(link[field]) > 120:
                        raise ValueError(f"customLinks.{field} 无效")
                    copied_link[field] = link[field]
            custom_links.append(copied_link)

        overrides = value.get("nodeOverrides", {})
        if not isinstance(overrides, dict) or len(overrides) > 512:
            raise ValueError("nodeOverrides 必须是对象且不能超过 512 项")
        node_overrides = {}
        for item_id, override in overrides.items():
            if not isinstance(item_id, str) or not item_id or not isinstance(override, dict) or not isinstance(override.get("name"), str):
                raise ValueError("nodeOverrides 包含无效项目")
            node_overrides[item_id[:120]] = {"name": override["name"].strip()[:40]}

        def hidden_ids(name: str) -> List[str]:
            source = value.get(name, [])
            if not isinstance(source, list) or len(source) > 512:
                raise ValueError(f"{name} 必须是数组且不能超过 512 项")
            result = []
            for item_id in source:
                if not isinstance(item_id, str) or not item_id or len(item_id) > 120:
                    raise ValueError(f"{name} 包含无效项目")
                if item_id not in result:
                    result.append(item_id)
            return result

        hidden_nodes = hidden_ids("hiddenNodes")
        hidden_links = hidden_ids("hiddenLinks")
        link_styles_source = value.get("linkStyles", {})
        if not isinstance(link_styles_source, dict) or len(link_styles_source) > 512:
            raise ValueError("linkStyles 必须是对象且不能超过 512 项")
        link_styles = {}
        for item_id, link_type in link_styles_source.items():
            if not isinstance(item_id, str) or not item_id or len(item_id) > 120 or not isinstance(link_type, str) or link_type not in _EDITOR_LINK_TYPES:
                raise ValueError("linkStyles 包含无效项目")
            link_styles[item_id] = link_type

        return {
            "positions": positions_data,
            "sizes": sizes_data,
            "layerSizes": layer_sizes,
            "customModules": custom_modules,
            "customLinks": custom_links,
            "nodeOverrides": node_overrides,
            "hiddenNodes": hidden_nodes,
            "hiddenLinks": hidden_links,
            "linkStyles": link_styles,
        }

    @staticmethod
    def _materialize_editor_runtime(raw: Dict[str, Any], editor: Dict[str, Any]) -> Dict[str, Any]:
        """将后台编辑器中绑定了场景设备的副本/连线纳入运行配置。

        编辑器还允许展示型对象（例如 IDS、交换机图标）存在，但这类对象没有
        ``sourceId`` 或没有对应的运行设备模板时只保留在 layout.editor，不伪装成
        可启动的仿真设备。
        """
        source_devices = {str(item.get("device_id")): item for item in raw.get("devices", [])
                          if isinstance(item, dict) and item.get("device_id")}
        modules = [item for item in editor.get("customModules", []) if isinstance(item, dict)]
        module_by_id = {item.get("id"): item for item in modules if item.get("id")}
        runtime_by_module: Dict[str, str] = {}
        origin_by_module: Dict[str, str] = {}
        display_only_modules: List[str] = []
        used_ids = set(source_devices)

        def safe_id(value: str, index: int) -> str:
            base = re.sub(r"[^A-Za-z0-9_-]", "-", value or "copy").strip("-") or "copy"
            candidate = f"{raw.get('scenario_id', 'scenario')}-{base}"[:110]
            if candidate in used_ids:
                candidate = f"{candidate[:100]}-{index}"
            while candidate in used_ids:
                candidate = f"{candidate[:96]}-{index}-{len(used_ids)}"
            used_ids.add(candidate)
            return candidate

        def resolve_origin(module_id: str, visiting=None) -> Optional[str]:
            if module_id in origin_by_module:
                return origin_by_module[module_id]
            visiting = set(visiting or ())
            if module_id in visiting:
                return None
            visiting.add(module_id)
            module = module_by_id.get(module_id) or {}
            source_id = module.get("sourceId")
            if source_id in source_devices:
                origin = source_id
            elif source_id in module_by_id:
                origin = resolve_origin(source_id, visiting)
            else:
                origin = None
            if origin:
                origin_by_module[module_id] = origin
            return origin

        for index, module in enumerate(modules):
            module_id = module.get("id")
            origin = resolve_origin(module_id)
            if not origin:
                if module_id:
                    display_only_modules.append(module_id)
                continue
            spec = deepcopy(source_devices[origin])
            runtime_id = safe_id(module_id, index)
            spec["device_id"] = runtime_id
            spec["name"] = module.get("name") or f"{spec.get('name') or origin} 副本"
            # 拖动/缩放后的值保存在 editor.positions/editor.sizes；模块对象中的
            # x/y/width/height 只是创建时的快照，发布时必须以映射中的最终值为准。
            position = (editor.get("positions") or {}).get(module_id, {})
            size = (editor.get("sizes") or {}).get(module_id, {})
            module["x"] = position.get("x", module.get("x", 100))
            module["y"] = position.get("y", module.get("y", 100))
            module["width"] = size.get("width", module.get("width", 110))
            module["height"] = size.get("height", module.get("height", 58))
            spec["layout"] = {
                "x": module["x"],
                "y": module["y"],
                "width": module["width"],
                "height": module["height"],
            }
            raw.setdefault("devices", []).append(spec)
            runtime_by_module[module_id] = runtime_id
            # 保留编辑器对象作为后台可追溯元数据，但明确告诉前端它已经
            # 物化成运行设备，避免运行节点和编辑器副本被画成两个对象。
            module["runtimeId"] = runtime_id

        def endpoint_device(item_id: str) -> Optional[str]:
            if item_id in runtime_by_module:
                return runtime_by_module[item_id]
            if item_id in source_devices:
                return item_id
            return None

        def endpoint_origin(item_id: str) -> Optional[str]:
            if item_id in runtime_by_module:
                return origin_by_module.get(item_id)
            if item_id in source_devices:
                return item_id
            return None

        def inferred_port(device: Dict[str, Any], outgoing: bool) -> str:
            parameters = device.get("parameters") or {}
            preferred = (("out_port", "up_port", "wireless_port", "wired_port", "down_port", "control_port")
                         if outgoing else ("in_port", "mu_port", "up_port", "wireless_port", "wired_port", "down_port", "control_port"))
            for key in preferred:
                value = parameters.get(key)
                if isinstance(value, str) and value:
                    return value
            ports = device.get("ports") or {}
            return next((value for value in ports.values() if isinstance(value, str) and value), "")

        def original_ports(origin_a: Optional[str], origin_b: Optional[str]):
            if not origin_a or not origin_b:
                return None
            for link in raw.get("links", []):
                if not isinstance(link, dict):
                    continue
                a, b = link.get("endpoint_a"), link.get("endpoint_b")
                if not (isinstance(a, list) and len(a) == 2 and isinstance(b, list) and len(b) == 2):
                    continue
                if a[0] == origin_a and b[0] == origin_b:
                    return a[1], b[1]
                if a[0] == origin_b and b[0] == origin_a:
                    return b[1], a[1]
            return None

        valid_link_types = {"wired", "hardwire", "wireless", "physical", "electrical"}
        display_only_links: List[str] = []
        used_link_ids = {str(item.get("link_id")) for item in raw.get("links", []) if isinstance(item, dict)}
        for index, link in enumerate(editor.get("customLinks", [])):
            if not isinstance(link, dict):
                continue
            source = endpoint_device(str(link.get("source", "")))
            target = endpoint_device(str(link.get("target", "")))
            if not source or not target:
                display_only_links.append(str(link.get("id", "")))
                continue
            source_spec = next((item for item in raw["devices"] if item.get("device_id") == source), {})
            target_spec = next((item for item in raw["devices"] if item.get("device_id") == target), {})
            ports = original_ports(endpoint_origin(str(link.get("source", ""))), endpoint_origin(str(link.get("target", ""))))
            source_port = link.get("sourcePort") or (ports[0] if ports else inferred_port(source_spec, True))
            target_port = link.get("targetPort") or (ports[1] if ports else inferred_port(target_spec, False))
            if not source_port or not target_port:
                display_only_links.append(str(link.get("id", "")))
                continue
            link_type = link.get("type", "wired")
            if link_type not in valid_link_types:
                link_type = "wired"
            link_id = f"editor-{raw.get('scenario_id', 'scenario')}-{link.get('id', index)}"[:110]
            if link_id in used_link_ids:
                link_id = f"{link_id[:96]}-{index}"
            used_link_ids.add(link_id)
            raw.setdefault("links", []).append({
                "link_id": link_id,
                "endpoint_a": [source, source_port],
                "endpoint_b": [target, target_port],
                "link_type": link_type,
                "parameters": {"delay_us": 0},
            })
            # Keep the editor-to-runtime mapping so the UI renders this link once.
            link["runtimeId"] = link_id

        return {
            "devices_added": len(runtime_by_module),
            "links_added": len(editor.get("customLinks", [])) - len(display_only_links),
            "display_only_modules": display_only_modules,
            "display_only_links": display_only_links,
        }

    def _create_run(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        scenario_id = body.get("scenario_id") or body.get("scenario") or body.get("path")
        if not scenario_id:
            return 400, {"error": "缺少 scenario_id"}, _JSON_HEADERS
        path = self._scenario_path_for(str(scenario_id))
        if path is None:
            return 404, {"error": f"未找到场景 {scenario_id}"}, _JSON_HEADERS
        try:
            run_id = self.application.load(path, body.get("goose_options"))
        except (ValueError, OSError) as exc:
            return 400, {"error": str(exc)}, _JSON_HEADERS
        self._seen_request_ids.clear()
        return 201, {
            "run_id": run_id,
            "scenario_id": self.application.config.scenario_id if self.application.config else scenario_id,
            "status": self.application.status,
            "mode": body.get("mode", "live"),
        }, _JSON_HEADERS

    def _submit_operation(self, run_id: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        if not self.application.run_id:
            return 404, {"error": "当前没有活动运行"}, _JSON_HEADERS
        if self.application.run_id != run_id:
            return 404, {"error": f"运行 {run_id} 不存在或已结束"}, _JSON_HEADERS

        request_id = body.get("request_id") or "req-" + str(len(self._seen_request_ids) + 1)
        body["request_id"] = request_id
        if request_id in self._seen_request_ids:
            previous = self._seen_request_ids[request_id]
            return 200, {"request_id": request_id, "status": "duplicate", "previous": previous}, _JSON_HEADERS

        receipt = self.application.submit_operation(body)
        self._seen_request_ids[request_id] = receipt
        return 200, receipt, _JSON_HEADERS

    def _get_device_controls(self, run_id: str, device_id: str) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        if not self.application.run_id:
            return 404, {"error": "当前没有活动运行"}, _JSON_HEADERS
        if self.application.run_id != run_id:
            return 404, {"error": f"运行 {run_id} 不存在或已结束"}, _JSON_HEADERS
        result = self.application.get_device_controls(device_id)
        if result.get("status") == "not_found":
            return 404, {"error": result.get("reason", "目标不存在")}, _JSON_HEADERS
        return 200, result, _JSON_HEADERS

    def _set_device_parameter(self, run_id: str, device_id: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        if not self.application.run_id:
            return 404, {"error": "当前没有活动运行"}, _JSON_HEADERS
        if self.application.run_id != run_id:
            return 404, {"error": f"运行 {run_id} 不存在或已结束"}, _JSON_HEADERS
        key = body.get("key") or body.get("parameter")
        if not key:
            return 400, {"error": "缺少参数名 key"}, _JSON_HEADERS
        if "value" not in body:
            return 400, {"error": "缺少参数值 value"}, _JSON_HEADERS
        result = self.application.set_device_parameter(device_id, key, body["value"])
        if result.get("status") == "not_found":
            return 404, {"error": result.get("reason", "目标不存在")}, _JSON_HEADERS
        return 200, result, _JSON_HEADERS

    def _views(self, run_id: str, query: Dict[str, List[str]]) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        view_name = self._first(query, "view", "system")
        goose_options = {}
        if view_name == "goose":
            try:
                goose_options = {key: int(self._first(query, key, None)) for key in ("frame_no", "before_frame", "limit") if key in query}
            except (ValueError, TypeError):
                return 400, {"error": "报文查询参数必须是整数"}, _JSON_HEADERS

        receiver_options = {}
        if view_name == "receiver":
            try:
                receiver_options = {key: int(self._first(query, key, None)) for key in ("before", "limit") if key in query}
                if any(value <= 0 for value in receiver_options.values()):
                    raise ValueError()
            except (ValueError, TypeError):
                return 400, {"error": "队列查询参数须为正整数"}, _JSON_HEADERS

        # 快照视图必须真正按 time_us 读取历史记录，而不是返回当前状态。
        if view_name == "snapshot":
            time_us = int(self._first(query, "time_us", 0) or 0)
            if self.application.run_id == run_id:
                replayed = self.application.replay(run_id, time_us)
                data = {
                    "time_us": replayed.get("time_us", time_us),
                    "snapshots": replayed.get("device_snapshots", {}),
                }
                return 200, {"run_id": run_id, "view": view_name, "data": data}, _JSON_HEADERS
            run_dir = os.path.join(self.output_root, run_id)
            if not os.path.isdir(run_dir):
                return 404, {"error": f"运行 {run_id} 不存在"}, _JSON_HEADERS
            return 200, {"run_id": run_id, "view": view_name,
                         "data": self._replay_view(view_name, run_dir, query)}, _JSON_HEADERS

        if self.application.run_id == run_id:
            options: Dict[str, Any] = {}
            if "target_ids" in query:
                options["target_ids"] = query["target_ids"]
            if view_name == "goose":
                options = goose_options
            if view_name == "receiver":
                options = receiver_options
            data = self.application.get_view(view_name, options)
            if view_name == "evaluation":
                data = self._evaluation_payload(data)
            return 200, {"run_id": run_id, "view": view_name, "data": data}, _JSON_HEADERS

        run_dir = os.path.join(self.output_root, run_id)
        if not os.path.isdir(run_dir):
            return 404, {"error": f"运行 {run_id} 不存在"}, _JSON_HEADERS
        return 200, {"run_id": run_id, "view": view_name, "data": self._replay_view(view_name, run_dir, query)}, _JSON_HEADERS

    def _replay_view(self, view_name: str, run_dir: str, query: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
        reader = RunReader(run_dir)
        manifest = reader.manifest()
        if view_name == "status":
            snapshot = reader.latest("truth")
            return {"run_id": manifest.get("run_id"), "scenario_id": manifest.get("scenario_id"),
                    "time_us": snapshot.get("time_us", 0) if snapshot else 0, "finished": True}
        if view_name == "system":
            return presentation_views.build_system_view(run_dir)
        if view_name == "goose":
            return presentation_views.build_goose_view(run_dir, {
                key: int(self._first(query or {}, key, None)) for key in ("frame_no", "before_frame", "limit") if key in (query or {})
            })
        if view_name == "receiver":
            return presentation_views.build_receiver_view(run_dir, {key: int(self._first(query or {}, key, None)) for key in ("before", "limit") if key in (query or {})})
        if view_name == "timeline":
            return presentation_views.build_timeline(run_dir)
        if view_name == "observations":
            return {"observations": {"events": reader.evidence()}}
        if view_name == "recognition":
            return {"recognition": reader.read("recognition")}
        if view_name == "defense":
            return {"defense": reader.read("defense")}
        if view_name == "evaluation":
            return self._evaluation_payload({"evaluation": asdict(Evaluator().evaluate(run_dir))})
        if view_name == "snapshot":
            time_us = int(self._first(query or {}, "time_us", 0) or 0)
            return {"snapshots": reader.snapshot_at(time_us)}
        return {"error": f"未知视图 {view_name}"}

    @staticmethod
    def _evaluation_payload(data: Dict[str, Any]) -> Dict[str, Any]:
        """统一评估页协议：前端只依赖 comparison/runs/reason。"""
        evaluation = data.get("evaluation")
        if evaluation is None:
            return {"comparison": None, "runs": [], "reason": data.get("reason", "暂无评估结果")}
        return presentation_views.build_comparison([evaluation])

    def _evidence(self, run_id: str, query: Dict[str, List[str]]) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        run_dir = os.path.join(self.output_root, run_id)
        if not os.path.isdir(run_dir):
            return 404, {"error": f"运行 {run_id} 不存在"}, _JSON_HEADERS
        provider_id = self._first(query, "provider_id", None)
        event_id = self._first(query, "event_id", None)
        start = int(self._first(query, "start", 0) or 0)
        count = int(self._first(query, "count", 10) or 10)

        reader = RunReader(run_dir)
        manifest = reader.manifest()
        base_dir = manifest.get("observation_base_dir") or run_dir
        evidence_reader = EvidenceReader(base_dir)
        events = reader.evidence(provider_id=provider_id, event_id=event_id)
        segments: List[Dict[str, Any]] = []
        for event in events:
            raw_event = event.get("raw") or event
            segment = evidence_reader.read_segment(raw_event, start=start, count=count)
            segments.append({
                "provider_id": event.get("provider_id"),
                "event_id": event.get("event_id"),
                "source_type": event.get("source_type"),
                "raw_data_uri": raw_event.get("raw_data_uri"),
                "raw_data_format": raw_event.get("raw_data_format"),
                "segment": segment,
            })
        return 200, {"run_id": run_id, "evidence": segments}, _JSON_HEADERS

    @staticmethod
    def _first(query: Dict[str, List[str]], key: str, default: Any) -> Any:
        values = query.get(key)
        if not values:
            return default
        return values[0]

    # ──────────────────────────────────────────────
    # HTTP 服务入口
    # ──────────────────────────────────────────────
    def serve(self, application: Optional[Application] = None, host: str = "127.0.0.1", port: int = 8000) -> None:
        """启动阻塞式 HTTP 服务；``application`` 可选，缺省沿用内部应用。"""
        if application is not None:
            self.application = application
        self._refresh_scenario_paths()

        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._dispatch()

            def do_POST(self):
                self._dispatch()

            def do_DELETE(self):
                self._dispatch()

            def _dispatch(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length else None
                status, payload, headers = server.handle_request(self.command, self.path, body)
                raw = payload.get("__raw__")
                if raw is not None:
                    data = raw.encode("utf-8")
                else:
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                pass

        httpd = ThreadingHTTPServer((host, port), Handler)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()


__all__ = ["PresentationServer"]
