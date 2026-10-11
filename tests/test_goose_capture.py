import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from sim2030.goose_codec import parse_goose_frame
from sim2030.goose_capture import GooseCapture, read_pcap, reference_checks, normal_baseline
from sim2030.base.observations import FileObservationSource, validate_observation_event
from sim2030.application import Application
from sim2030.presentation.server import PresentationServer
from sim2030.presentation.views import build_goose_view
from tools.import_message_core import import_package

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 1789084800


def tlv(tag, value):
    length = bytes([len(value)]) if len(value) < 128 else b"\x81" + bytes([len(value)])
    return bytes([tag]) + length + value


def make_frame(st=1, sq=1, source="000c29100001", trip=False, appid=12289):
    clock = struct.pack(">I", ORIGIN) + b"\x00" * 4
    dataset = tlv(0xA2, tlv(0x83, bytes([trip])) + tlv(0x84, b"\x03\x00\x00") + tlv(0x91, clock))
    parts = [(0x80, b"PROT/LLN0$GO$gcbTrip"), (0x81, b"\x01\xf4"), (0x82, b"PROT/LLN0$dsTrip"),
             (0x83, b"test-goose"), (0x84, clock), (0x85, bytes([st])), (0x86, bytes([sq])),
             (0x87, b"\0"), (0x88, b"\1"), (0x89, b"\0"), (0x8A, b"\1"), (0xAB, dataset)]
    pdu = tlv(0x61, b"".join(tlv(tag, value) for tag, value in parts))
    ethernet = bytes.fromhex("010ccd013001" + source) + struct.pack(">HHH", 0x8100, 0x8003, 0x88B8)
    return ethernet + struct.pack(">HHHH", appid, len(pdu) + 8, 0, 0) + pdu


def sample_package(path, corrupt=False):
    contents = {}
    for sample in ("normal", "forgery", "suppression", "dos"):
        packets = [(0, make_frame()), (200000, make_frame(sq=2)), (400000, make_frame(st=2))]
        if sample != "normal":
            packets.insert(1, (500, make_frame(st=7, source="000c29660066", trip=True, appid=12291)))
        pcap = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
        records = []
        for i, (us, raw) in enumerate(packets, 1):
            pcap += struct.pack("<IIII", ORIGIN, us, len(raw), len(raw)) + raw
            records.append(parse_goose_frame(raw, ORIGIN + us / 1000000, i, "test/CB-IN"))
        log = ("\n".join(json.dumps(row) for row in records) + "\n").encode()
        manifest = {"frame_count": len(records), "base_time_utc": ORIGIN, "capture_point": "test/CB-IN",
                    "goose_config": {"publisher": "保护", "subscriber": "智能终端"}, "data_set": [{"name": "trip"}]}
        contents[f"communication-log-{sample}.jsonl"] = log
        contents[f"readback-{sample}.jsonl"] = log
        contents[f"goose-{sample}.pcap"] = pcap
        contents[f"manifest-{sample}.json"] = json.dumps(manifest).encode()
        contents[f"labels-{sample}.jsonl"] = b'{"truth":"DO_NOT_USE_THIS_LABEL","outcome":"unknown"}\n'
    manifest = "\n".join(f"{hashlib.sha256(data).hexdigest()} {len(data)} {name}" for name, data in contents.items())
    manifest += "\n" + "0" * 64 + " 1 absent-document.md"
    if corrupt:
        contents["goose-forgery.pcap"] += b"bad"
    with zipfile.ZipFile(path, "w") as package:
        for name, data in contents.items():
            package.writestr("message_core/" + name, data)
        package.writestr("message_core/MANIFEST_sha256.txt", manifest)


class TestGooseCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / "samples.zip"
        sample_package(self.archive)
        self.data = self.root / "data"
        self.report = import_package(self.archive, self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def scene(self, sample="forgery"):
        scene = json.loads((ROOT / "scenarios/substation-live.json").read_text(encoding="utf-8"))
        scene["scenario_id"] = "test-goose"
        scene["simulation"]["duration_us"] = 600000
        scene["layout"]["packet_replay"] = {"sample": sample}
        scene["observations"] = {"provider_dir": str(self.data / sample), "file_pattern": "events.jsonl"}
        path = self.root / "scene.json"
        path.write_text(json.dumps(scene), encoding="utf-8")
        return path

    def test_import_preserves_packet_log_and_keeps_labels_separate(self):
        self.assertEqual(self.report["missing_manifest_entries"], ["absent-document.md"])
        capture = GooseCapture(self.data / "forgery")
        frame = capture.view(1000, frame_no=2)["frame"]
        self.assertEqual(frame["capture_time_iso"], "2026-09-11T00:00:00.000500Z")
        self.assertEqual(frame["record"]["src_mac"], "00:0c:29:66:00:66")
        self.assertTrue(frame["trip"])
        self.assertIn("来源 MAC 偏离正常样本", frame["checks"])
        for file in (self.data / "forgery").iterdir():
            self.assertNotIn(b"DO_NOT_USE_THIS_LABEL", file.read_bytes())
        self.assertIn(b"DO_NOT_USE_THIS_LABEL", (self.data / "evaluation/labels-forgery.jsonl").read_bytes())

    def test_frame_visibility_is_causal_and_pagination_bounded(self):
        capture = GooseCapture(self.data / "forgery")
        self.assertEqual(capture.view(-1)["visible_frames"], 0)
        self.assertIn("error", capture.view(0, frame_no=2))
        self.assertEqual(capture.view(500)["visible_frames"], 2)
        self.assertEqual([f["frame_no"] for f in capture.view(500000, before_frame=3)["frames"]], [2, 1])
        self.assertEqual(capture.view(0)["check_counts"], {})

    def test_source_mapping_and_batch_counts(self):
        capture = GooseCapture(self.data / "forgery")
        config = {"provider_dir": str(self.data / "forgery"), "file_pattern": "events.jsonl",
                  "time_mapping": {"origin": capture.meta["base_time_iso"]}}
        source = FileObservationSource(config)
        self.assertEqual(source.read_available(0), [])
        events = source.read_available(1000000)
        self.assertEqual(sum(e["data"]["packet_count"] for e in events), 4)
        self.assertIsNone(events[0]["data"]["appid"])
        self.assertEqual(events[0]["data"]["protocol_fields"]["appids"], [12289, 12291])
        for event in events:
            self.assertEqual(validate_observation_event(event), [])
            self.assertEqual(event["metadata"]["interface_id"], "span-process")
            self.assertEqual(event["data"]["event_type"], "stats")
            self.assertEqual(event["status"], "unknown")

    def test_reference_checks_use_only_headers_and_baseline(self):
        normal = GooseCapture(self.data / "normal")
        baseline = normal_baseline(normal.records, normal.index)
        self.assertFalse(any(reference_checks(normal.records, normal.index, baseline)[0]))
        forged = GooseCapture(self.data / "forgery")
        checks, _ = reference_checks(forged.records, forged.index, baseline)
        self.assertEqual(checks[1], ["来源 MAC 偏离正常样本"])
        self.assertEqual(checks[2], ["控制块状态号回退"])

    def test_modified_or_truncated_files_are_rejected(self):
        pcap = (self.data / "normal/capture.pcap").read_bytes()
        with self.assertRaises(ValueError): read_pcap(pcap[:-1])
        with self.assertRaises(ValueError): read_pcap(pcap + b"x")
        (self.data / "normal/capture.pcap").write_bytes(pcap + b"x")
        with self.assertRaises(ValueError): GooseCapture(self.data / "normal")

    def test_bad_checksum_aborts_before_writing_data(self):
        broken = self.root / "broken.zip"
        sample_package(broken, corrupt=True)
        with self.assertRaises(ValueError): import_package(broken, self.root / "broken-output")
        self.assertFalse((self.root / "broken-output").exists())

    def test_existing_data_is_not_overwritten(self):
        with self.assertRaises(ValueError): import_package(self.archive, self.data)

    def test_application_and_finished_record_replay(self):
        app = Application(output_root=str(self.root / "runs"))
        try:
            app.load(str(self.scene()))
            self.assertEqual(app.get_view("goose")["visible_frames"], 0)
            app.duration_us = 2_000_000
            app.run_to_end()
            view = app.get_view("system")
            self.assertEqual(view["packet_capture"]["visible_frames"], 4)
            self.assertEqual(view["recognition"], [])
            self.assertEqual(view["observations"]["late"], [])
            self.assertFalse(any(item["effects"] for item in view["management"].values()))
            replay = build_goose_view(self.root / "runs" / app.run_id)
            self.assertEqual(replay["visible_frames"], 4)
        finally:
            app.close()

    def test_failed_sample_load_preserves_existing_run(self):
        app = Application(output_root=str(self.root / "runs"))
        try:
            path = self.scene()
            previous = app.load(str(path))
            config = json.loads(path.read_text())
            config["observations"]["provider_dir"] = str(self.root / "not-imported")
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "尚未导入"): app.load(str(path))
            self.assertEqual(app.run_id, previous)
            self.assertIsNotNone(app.recorder)
        finally:
            app.close()

    def test_http_frame_query_does_not_accept_paths_or_future_frames(self):
        self.scene()
        server = PresentationServer(str(self.root), str(self.root / "runs"))
        try:
            _, result, _ = server.handle_request("POST", "/api/runs", b'{"scenario_id":"test-goose"}')
            run = result["run_id"]
            code, _, _ = server.handle_request("GET", f"/api/runs/{run}/views?view=goose&frame_no=../../secret")
            self.assertEqual(code, 400)
            _, body, _ = server.handle_request("GET", f"/api/runs/{run}/views?view=goose&frame_no=1")
            self.assertIn("error", body["data"])
            server.application.run_to_end()
            _, body, _ = server.handle_request("GET", f"/api/runs/{run}/views?view=goose&frame_no=2")
            self.assertEqual(body["data"]["frame"]["frame_no"], 2)
            server.application.load(str(self.scene("normal")))
            _, archived, _ = server.handle_request("GET", f"/api/runs/{run}/views?view=goose&frame_no=2")
            self.assertTrue(archived["data"]["frame"]["trip"])
            code, _, _ = server.handle_request("GET", f"/api/runs/{run}/views?view=goose&frame_no=invalid")
            self.assertEqual(code, 400)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
