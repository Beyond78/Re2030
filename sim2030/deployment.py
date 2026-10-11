"""校验场景中的网络部署、接口映射和旁路采集关系。"""
import math


def validate_deployment(config, errors):
    deployment = config.layout.get("deployment")
    if deployment is None:
        return
    if not isinstance(deployment, dict):
        errors.append("layout.deployment 必须是对象")
        return
    networks = deployment.get("networks", [])
    if not isinstance(networks, list):
        errors.append("deployment.networks 必须是列表")
        return
    devices = {d.device_id: d for d in config.devices}
    links = {link.link_id: link for link in config.links}
    asset_ids, network_ids, capture_ids, physical_ports, assigned = set(devices), set(), set(), set(), set()

    def unique(value, seen, label):
        if not isinstance(value, str) or not value or value in seen:
            errors.append(f"{label} 缺失或重复：{value}")
        else:
            seen.add(value)

    def geometry(asset):
        if not isinstance(asset, dict):
            errors.append("部署节点必须是对象")
            return False
        unique(asset.get("id"), asset_ids, "部署节点 ID")
        for key in ("x", "y", "width", "height"):
            value = asset.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or value < 0 or (key in ("width", "height") and value == 0)):
                errors.append(f"部署节点 {asset.get('id')} 的 {key} 无效")
        return True

    for network in networks:
        if not isinstance(network, dict):
            errors.append("network 必须是对象")
            continue
        unique(network.get("id"), network_ids, "网络 ID")
        sw, probe = network.get("switch", {}), network.get("probe", {})
        if not geometry(sw) or not geometry(probe):
            continue
        unique(probe.get("interface_id"), capture_ids, "IDS 采集口标识")
        if not probe.get("source_id") or not probe.get("capture_interface") or not probe.get("management_interface"):
            errors.append("IDS 必须声明事件来源、采集接口和管理接口")
        if probe.get("capture_interface") == probe.get("management_interface"):
            errors.append("IDS 采集接口和管理接口必须分开")
        if network.get("mirror_direction") != "both":
            errors.append("镜像采集必须明确包含收发双向流量")
        switch_ports = set()
        unique(network.get("mirror_port"), switch_ports, "镜像端口")
        connections = network.get("connections", [])
        if not isinstance(connections, list):
            errors.append("network.connections 必须是列表")
            continue
        access = {}
        for connection in connections:
            if not isinstance(connection, dict):
                errors.append("网络接口连接必须是对象")
                continue
            asset_id = connection.get("device_id")
            if asset_id not in devices or asset_id in access:
                errors.append(f"网络接入设备未知或重复：{asset_id}")
            access[asset_id] = connection
            unique(connection.get("switch_port"), switch_ports, "交换机端口")
            interface = connection.get("interface_id")
            if not interface or (asset_id, interface) in physical_ports:
                errors.append(f"同一设备接口不能接入两张网：{asset_id}/{interface}")
            physical_ports.add((asset_id, interface))
            ports = connection.get("business_ports")
            if not isinstance(ports, list) or not ports:
                errors.append(f"{asset_id}/{interface} 缺少业务端口映射")
            elif asset_id in devices and any(p not in devices[asset_id].ports for p in ports):
                errors.append(f"{asset_id}/{interface} 引用未知业务端口")
            for axis, bound in (("anchor_x", 90), ("anchor_y", 48)):
                value = connection.get(axis)
                if not isinstance(value, (int, float)) or not 0 <= value <= bound:
                    errors.append(f"{asset_id} 的接口坐标 {axis} 无效")
        link_ids = network.get("link_ids", [])
        if not isinstance(link_ids, list):
            errors.append("network.link_ids 必须是列表")
            continue
        for link_id in link_ids:
            if link_id in assigned:
                errors.append(f"业务连接不能同时归属两张网：{link_id}")
            assigned.add(link_id)
            link = links.get(link_id)
            if link is None or link.link_type != "wired":
                errors.append(f"网络引用不存在或非以太网业务连接：{link_id}")
                continue
            for asset_id, port in (link.endpoint_a, link.endpoint_b):
                if port not in access.get(asset_id, {}).get("business_ports", []):
                    errors.append(f"业务连接 {link_id} 端点 {asset_id}/{port} 没有接入本网络")
    geometry(deployment.get("manager", {}))
    direct = deployment.get("direct_links", {})
    if not isinstance(direct, dict):
        errors.append("deployment.direct_links 必须是对象")
        return
    for link_id, display in direct.items():
        if link_id not in links or link_id in assigned:
            errors.append(f"直连接线未知或已归属交换网络：{link_id}")
        if not isinstance(display, dict) or display.get("role") not in ("measurement", "timing", "physical"):
            errors.append(f"直连接线 {link_id} 类型无效")
            continue
        points = display.get("points", [])
        if not isinstance(points, list):
            errors.append(f"直连接线 {link_id} 路径必须是列表")
            continue
        for point in points:
            if not isinstance(point, dict) or any(
                not isinstance(point.get(axis), (int, float)) or not math.isfinite(point[axis])
                for axis in ("x", "y")
            ):
                errors.append(f"直连接线 {link_id} 坐标无效")
