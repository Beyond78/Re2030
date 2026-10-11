"""把用户交付的 message_core.zip 导入本地只读回放库（标准库）。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim2030.goose_capture import SAMPLES, epoch_us, iso_time, normal_baseline, observation_events, reference_checks, verify_frames


def import_package(archive, destination):
    archive, destination = Path(archive), Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("目标目录已有数据，请使用一个新的空目录")
    entries, payloads = {}, {}
    with zipfile.ZipFile(archive) as package:
        files = [entry for entry in package.infolist() if not entry.is_dir()]
        if sum(entry.file_size for entry in files) > 64 * 1024 * 1024 or any(entry.file_size > 32 * 1024 * 1024 for entry in files):
            raise ValueError("交付包超过导入大小限制")
        checksums = package.read("message_core/MANIFEST_sha256.txt").decode("utf-8-sig")
        for line in checksums.splitlines():
            match = re.match(r"([0-9a-fA-F]{64})\s+(\d+)\s+(.+)", line)
            if match:
                entries[match[3]] = (match[1].lower(), int(match[2]))
        actual = {}
        for entry in files:
            data = package.read(entry)
            actual[(hashlib.sha256(data).hexdigest(), len(data))] = entry.filename
        missing = [name for name, expected in entries.items() if expected not in actual]
        for sample in SAMPLES:
            names = [f"communication-log-{sample}.jsonl", f"goose-{sample}.pcap", f"manifest-{sample}.json",
                     f"readback-{sample}.jsonl", f"labels-{sample}.jsonl"]
            blobs = {}
            for name in names:
                data = package.read("message_core/" + name)
                if (hashlib.sha256(data).hexdigest(), len(data)) != entries.get(name):
                    raise ValueError(f"数据完整性核对失败：{name}")
                blobs[name] = data
            if blobs[names[0]] != blobs[names[3]]:
                raise ValueError(f"回读日志不一致：{sample}")
            manifest = json.loads(blobs[names[2]].decode("utf-8-sig"))
            records, index = verify_frames(blobs[names[0]], blobs[names[1]], manifest)
            payloads[sample] = (blobs, manifest, records, index)

    normal = payloads["normal"]
    baseline = normal_baseline(normal[2], normal[3])
    report = {"archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "actual_file_count": len(files),
              "verified_manifest_entries": len(entries) - len(missing), "missing_manifest_entries": missing,
              "decoder": "provided-reference-decoder-subset", "data_origin": "synthetic", "samples": {}}
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "evaluation").mkdir()
    for sample, (blobs, manifest, records, index) in payloads.items():
        folder = destination / sample
        folder.mkdir()
        checks, rates = reference_checks(records, index, baseline)
        for point, issues, count in zip(index, checks, rates):
            point.update(checks=issues, frames_1s=count)
        meta = {"sample": sample, "capture_point": manifest["capture_point"], "baseline": baseline,
                "base_time_iso": iso_time(epoch_us(manifest["base_time_utc"])), "frames": index}
        for source, target in ((f"communication-log-{sample}.jsonl", "communication-log.jsonl"),
                               (f"goose-{sample}.pcap", "capture.pcap"), (f"manifest-{sample}.json", "manifest.json")):
            (folder / target).write_bytes(blobs[source])
        meta["pcap_sha256"] = hashlib.sha256((folder / "capture.pcap").read_bytes()).hexdigest()
        meta["log_sha256"] = hashlib.sha256((folder / "communication-log.jsonl").read_bytes()).hexdigest()
        (folder / "capture-index.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        events = observation_events(records, index, manifest, sample, f"substation-goose-{sample}")
        (folder / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
        # 标签只封存于评价目录，不解析，不进入上面的检查与观测生成。
        (destination / "evaluation" / f"labels-{sample}.jsonl").write_bytes(blobs[f"labels-{sample}.jsonl"])
        report["samples"][sample] = {"frames": len(records), "observations": len(events), "pcap_matches_log": True,
                                      "duration_us": index[-1]["time_us"], "peak_frames_1s": max(rates),
                                      "checks": {issue: sum(issue in row for row in checks) for issue in sorted({s for row in checks for s in row})}}
    (destination / "import-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/message_core")
    args = parser.parse_args()
    print(json.dumps(import_package(args.archive, args.output), ensure_ascii=False, indent=2))
