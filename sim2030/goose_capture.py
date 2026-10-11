"""离线 GOOSE 样本核对、参考检查与只读逐帧回放。"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal
import json
import hashlib
from pathlib import Path
import struct

from sim2030.goose_codec import parse_goose_frame

SAMPLES = {"normal": "正常基线", "forgery": "报文伪造", "suppression": "消息抑制", "dos": "DoS 流量"}


def epoch_us(value):
    return int((Decimal(str(value)) * 1_000_000).to_integral_value())


def iso_time(value):
    return datetime.fromtimestamp(value / 1_000_000, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def read_pcap(data):
    """严格读取经典以太网 PCAP；返回精确时间、文件偏移与原始帧。"""
    formats = {b"\xd4\xc3\xb2\xa1": ("<", 1), b"\xa1\xb2\xc3\xd4": (">", 1),
               b"\x4d\x3c\xb2\xa1": ("<", 1000), b"\xa1\xb2\x3c\x4d": (">", 1000)}
    if len(data) < 24 or data[:4] not in formats:
        raise ValueError("不支持或不完整的 PCAP 文件头")
    order, resolution = formats[data[:4]]
    major, minor, _, _, snaplen, linktype = struct.unpack(order + "HHiIII", data[4:24])
    if (major, minor) != (2, 4) or linktype != 1:
        raise ValueError("需要 PCAP 2.4 以太网数据")
    result, pos = [], 24
    while pos < len(data):
        if pos + 16 > len(data):
            raise ValueError("PCAP 记录头截断")
        sec, fraction, captured, original = struct.unpack(order + "IIII", data[pos:pos + 16])
        pos += 16
        if fraction >= 1_000_000 * resolution or not 14 <= captured <= min(snaplen, 65535) or captured != original:
            raise ValueError("采集帧被截断或长度/时间无效")
        if pos + captured > len(data):
            raise ValueError("PCAP 帧内容截断")
        result.append({"epoch_us": sec * 1_000_000 + fraction // resolution,
                       "offset": pos, "length": captured, "raw": data[pos:pos + captured]})
        pos += captured
        if len(result) > 100000:
            raise ValueError("单个样本超过 100000 帧限制")
    return result


def verify_frames(log_bytes, pcap_bytes, manifest):
    records = [json.loads(line) for line in log_bytes.decode("utf-8-sig").splitlines() if line.strip()]
    packets = read_pcap(pcap_bytes)
    if len(records) != len(packets) or len(records) != manifest["frame_count"]:
        raise ValueError("通信日志、PCAP 与 manifest 帧数不一致")
    origin = epoch_us(manifest["base_time_utc"])
    index = []
    previous = -1
    for number, (record, packet) in enumerate(zip(records, packets), 1):
        decoded = parse_goose_frame(packet["raw"], packet["epoch_us"] / 1_000_000, number, manifest["capture_point"])
        if decoded != record:
            raise ValueError(f"第 {number} 帧的 PCAP 解码与通信日志不一致")
        elapsed = packet["epoch_us"] - origin
        if elapsed < previous or elapsed < 0:
            raise ValueError("采集时间必须单调且不得早于基准时间")
        previous = elapsed
        index.append({"frame_no": number, "time_us": elapsed, "offset": packet["offset"], "length": packet["length"]})
    return records, index


def reference_checks(records, index, baseline):
    """只从正常基线和通信日志取信息；不读标签，不推断订阅端动作。"""
    last_state, window = {}, deque()
    checks, window_counts = [], []
    for frame, point in zip(records, index):
        stream = frame["goose"].get("gocbRef", "")
        st_num = frame["goose"].get("stNum")
        issues = []
        expected = baseline["publishers"].get(stream, [])
        if expected and frame["src_mac"] not in expected:
            issues.append("来源 MAC 偏离正常样本")
        if stream in last_state and st_num < last_state[stream]:
            issues.append("控制块状态号回退")
        last_state[stream] = st_num
        while window and point["time_us"] - window[0] >= 1_000_000:
            window.popleft()
        window.append(point["time_us"])
        window_counts.append(len(window))
        checks.append(issues)
    return checks, window_counts


def normal_baseline(records, index):
    publishers = defaultdict(set)
    for frame in records:
        publishers[frame["goose"]["gocbRef"]].add(frame["src_mac"])
    baseline = {"publishers": {key: sorted(value) for key, value in publishers.items()}}
    _, counts = reference_checks(records, index, baseline)
    baseline["peak_frames_1s"] = max(counts, default=0)
    baseline["rate_threshold_1s"] = max(counts, default=0) * 5
    return baseline


def observation_events(records, index, manifest, sample, scenario_id):
    """按 100ms 采集批次接入 stats，保留 PCAP 证据；检查提示另行展示。"""
    groups = defaultdict(list)
    for record, point in zip(records, index):
        groups[point["time_us"] // 100000].append((record, point))
    origin = epoch_us(manifest["base_time_utc"])
    events = []
    for number, rows in groups.items():
        first, last = rows[0][1], rows[-1][1]
        delivered = (number + 1) * 100000
        appids = sorted({frame["appid"] for frame, _ in rows})
        control_blocks = sorted({frame["goose"]["gocbRef"] for frame, _ in rows})
        events.append({
            "schema_version": "0.1.0", "event_id": f"{sample}-batch-{number}",
            "scenario_id": scenario_id, "provider_id": "message-core", "source_type": "ids",
            "source_id": "message-core-process", "timestamp": iso_time(origin + delivered),
            "observed_at": iso_time(origin + delivered), "time_source": "package-simulation-clock",
            "time_quality": "unknown", "related_asset_ids": ["prot", "ctl_brk"],
            "association_basis": "configured", "status": "unknown", "confidence": None,
            "data_origin": "synthetic", "data_quality": "valid", "missing_reason": None,
            "raw_data_uri": "capture.pcap", "raw_data_format": "pcap", "raw_metadata_uri": "manifest.json",
            "metadata": {"interface_id": "span-process", "asset_mapping_version": "message-core-v1",
                         "capture_point": manifest["capture_point"], "start_time": iso_time(origin + first["time_us"]),
                         "end_time": iso_time(origin + last["time_us"]), "first_frame": first["frame_no"], "last_frame": last["frame_no"]},
            "data": {"event_type": "stats", "protocol": "GOOSE", "packet_count": len(rows),
                     "byte_count": sum(point["length"] for _, point in rows), "ether_type": 0x88B8,
                     "appid": appids[0] if len(appids) == 1 else None, "dst_asset_id": "ctl_brk",
                     "protocol_fields": {"gocbRef": control_blocks[0] if len(control_blocks) == 1 else None,
                                         "appids": appids, "gocbRefs": control_blocks,
                                         "destination_macs": sorted({frame["dst_mac"] for frame, _ in rows}),
                                         "source_macs": sorted({frame["src_mac"] for frame, _ in rows})}},
        })
    return events


class GooseCapture:
    """仅通过已配置的样本目录读帧，不接收浏览器提供的文件路径。"""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.meta = json.loads((self.directory / "capture-index.json").read_text(encoding="utf-8"))
        for filename, key in (("capture.pcap", "pcap_sha256"), ("communication-log.jsonl", "log_sha256")):
            if hashlib.sha256((self.directory / filename).read_bytes()).hexdigest() != self.meta[key]:
                raise ValueError(f"已导入文件发生变化：{filename}，请重新核对")
        self.manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        self.records = [json.loads(line) for line in (self.directory / "communication-log.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.index = self.meta["frames"]
        self.times = [entry["time_us"] for entry in self.index]
        if len(self.index) != len(self.records):
            raise ValueError("回放索引与日志帧数不一致，请重新导入")

    def view(self, time_us, frame_no=None, before_frame=None, limit=12):
        visible = bisect_right(self.times, time_us)
        end = visible if before_frame is None else min(visible, max(0, before_frame - 1))
        limit = max(1, min(50, limit))
        start = max(0, end - limit)
        counts = Counter(issue for point in self.index[:visible] for issue in point["checks"])
        result = {"available": True, "sample": self.meta["sample"], "name": SAMPLES[self.meta["sample"]],
                  "data_origin": "synthetic", "total_frames": len(self.index), "visible_frames": visible,
                  "duration_us": self.times[-1] if self.times else 0, "time_us": time_us,
                  "capture_point": self.meta["capture_point"], "interface_id": "span-process",
                  "check_counts": dict(counts), "baseline": self.meta["baseline"],
                  "goose_config": self.manifest["goose_config"], "dataset_definition": self.manifest["data_set"],
                  "peak_frames_1s": max((p["frames_1s"] for p in self.index[:visible]), default=0),
                  "frames": [self._row(i) for i in range(start, end)][::-1],
                  "has_older": start > 0, "oldest_frame": start + 1 if end else None,
                  "outcome": "unknown", "pcap_verified": True}
        if frame_no is not None:
            if not 1 <= frame_no <= visible:
                return {"error": "该帧尚未回放或不存在"}
            i = frame_no - 1
            point = self.index[i]
            with (self.directory / "capture.pcap").open("rb") as handle:
                handle.seek(point["offset"])
                raw = handle.read(point["length"])
            result["frame"] = {**self._row(i), "record": self.records[i], "raw_hex": raw.hex(" "),
                               "pcap_offset": point["offset"]}
        return result

    def _row(self, index):
        frame, point = self.records[index], self.index[index]
        dataset = frame.get("dataset", [])
        return {"frame_no": frame["frame_no"], "time_us": point["time_us"], "src_mac": frame["src_mac"],
                "capture_time_iso": iso_time(epoch_us(self.manifest["base_time_utc"]) + point["time_us"]),
                "stNum": frame["goose"].get("stNum"), "sqNum": frame["goose"].get("sqNum"),
                "trip": dataset[0][0] if dataset and isinstance(dataset[0], list) else None,
                "parse_status": frame["parse_status"], "checks": point["checks"], "frames_1s": point["frames_1s"]}
