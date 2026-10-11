"""用户交付 message_core/goose_frame_ref.py 的解码子集。

仅保留纯字节解码函数和字段常量；没有生成器、网络发送或原脚本 CLI。
接口用于核对本次交付样本，不声明覆盖所有 IEC 61850 编码。
"""
from __future__ import annotations
import struct
from typing import Any, Dict, Tuple

ETHERTYPE_GOOSE = 0x88B8

TPID_VLAN = 0x8100

HEADER_LEN = 8

PDU_TAG = 0x61

ALL_DATA_TAG = 0xAB

TAG_GOCBREF = 0x80

TAG_TAL = 0x81

TAG_DATSET = 0x82

TAG_GOID = 0x83

TAG_T = 0x84

TAG_STNUM = 0x85

TAG_SQNUM = 0x86

TAG_SIMULATION = 0x87

TAG_CONFREV = 0x88

TAG_NDSCOM = 0x89

TAG_NUMENTRIES = 0x8A

DATA_TAGS: Dict[int, str] = {
    0xA1: "array", 0xA2: "structure", 0x83: "boolean", 0x84: "bit-string",
    0x85: "integer", 0x86: "unsigned", 0x87: "floating-point", 0x88: "real",
    0x89: "octet-string", 0x8A: "visible-string", 0x8C: "binary-time",
    0x8D: "bcd", 0x8E: "booleanArray", 0x8F: "objId", 0x90: "mMSString",
    0x91: "utc-time",
}

PDU_FIELDS: Tuple[Tuple[int, str], ...] = (
    (TAG_GOCBREF, "gocbRef"), (TAG_TAL, "timeAllowedtoLive"),
    (TAG_DATSET, "datSet"), (TAG_GOID, "goID"), (TAG_T, "t"),
    (TAG_STNUM, "stNum"), (TAG_SQNUM, "sqNum"), (TAG_SIMULATION, "simulation"),
    (TAG_CONFREV, "confRev"), (TAG_NDSCOM, "ndsCom"),
    (TAG_NUMENTRIES, "numDatSetEntries"),
)

def mac_text(raw: bytes) -> str:
    return ":".join("%02x" % b for b in raw)

def read_tlv(data: bytes, pos: int) -> Tuple[int, bytes, int]:
    """读一个 TLV，返回 (tag, value, 下一个位置)。"""
    if pos >= len(data):
        raise ValueError("TLV 越界")
    tag = data[pos]
    pos += 1
    first = data[pos]
    pos += 1
    if first & 0x80:
        count = first & 0x7F
        size = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    else:
        size = first
    value = data[pos:pos + size]
    if len(value) != size:
        raise ValueError("TLV 长度不足: 声明 %d 实际 %d" % (size, len(value)))
    return tag, value, pos + size

def decode_utctime(value: bytes) -> Dict[str, Any]:
    seconds = int.from_bytes(value[0:4], "big")
    fraction = int.from_bytes(value[4:7], "big")
    quality = value[7]
    return {
        "epoch": seconds + fraction / float(1 << 24),
        "clock_failure": bool(quality & 0x40),
        "clock_not_synchronized": bool(quality & 0x20),
    }

def decode_float(value: bytes) -> float:
    """IEC 61850 FloatingPoint：1 字节指数 + 24 位小数尾数（存 4 字节）。"""
    exponent = int.from_bytes(value[0:1], "big", signed=True)
    mantissa = int.from_bytes(value[1:5], "big", signed=True)
    return mantissa * (2.0 ** (exponent - 24))

def decode_data(tag: int, value: bytes) -> Any:
    """按 Data CHOICE 解码一个数据集元素（可递归）。"""
    kind = DATA_TAGS.get(tag, "unknown(0x%02x)" % tag)
    if tag in (0xA1, 0xA2):
        items, pos = [], 0
        while pos < len(value):
            child_tag, child_value, pos = read_tlv(value, pos)
            items.append(decode_data(child_tag, child_value))
        return items
    if tag == 0x83:
        return bool(value[0]) if value else False
    if tag == 0x84:
        unused = value[0]
        return {"kind": "bit-string", "bits": len(value[1:]) * 8 - unused,
                "value": int.from_bytes(value[1:], "big")}
    if tag == 0x85:
        return int.from_bytes(value, "big", signed=True)
    if tag == 0x86:
        return int.from_bytes(value, "big", signed=False)
    if tag == 0x87:
        return decode_float(value)
    if tag == 0x8A:
        return value.decode("ascii", "replace")
    if tag == 0x89:
        return {"kind": "octet-string", "hex": value.hex()}
    if tag == 0x91:
        return decode_utctime(value)
    return {"kind": kind, "hex": value.hex()}

def parse_goose_frame(raw: bytes, capture_ts: float, frame_no: int,
                      capture_point: str = "substation-attack-demo/CB-TERMINAL-IN"
                      ) -> Dict[str, Any]:
    """把一帧原始以太网报文解析为一条通信日志记录。

    解析失败不丢帧：保留采集信息与原始内容，parse_status 记为 failed。
    """
    record: Dict[str, Any] = {
        "frame_no": frame_no, "capture_point": capture_point,
        "capture_time": round(capture_ts, 6), "frame_length": len(raw),
        "src_mac": None, "dst_mac": None, "vlan": None,
        "appid": None, "length": None, "reserve1": None,
        "reserve1_s_bit": None, "reserve2": None,
        "goose": {}, "dataset": [], "parse_status": "failed", "parse_error": None,
    }
    try:
        if len(raw) < 14:
            raise ValueError("帧长不足以太网头")
        record["dst_mac"] = mac_text(raw[0:6])
        record["src_mac"] = mac_text(raw[6:12])
        offset = 12
        ethertype = struct.unpack(">H", raw[offset:offset + 2])[0]
        if ethertype == TPID_VLAN:
            tci = struct.unpack(">H", raw[offset + 2:offset + 4])[0]
            record["vlan"] = {"vid": tci & 0x0FFF, "pcp": (tci >> 13) & 0x07,
                              "dei": (tci >> 12) & 0x01}
            offset += 4
            ethertype = struct.unpack(">H", raw[offset:offset + 2])[0]
        offset += 2
        if ethertype != ETHERTYPE_GOOSE:
            raise ValueError("非 GOOSE 以太网类型: 0x%04x" % ethertype)
        record["appid"] = struct.unpack(">H", raw[offset:offset + 2])[0]
        record["length"] = struct.unpack(">H", raw[offset + 2:offset + 4])[0]
        reserve1 = struct.unpack(">H", raw[offset + 4:offset + 6])[0]
        record["reserve1"] = reserve1
        record["reserve1_s_bit"] = bool(reserve1 & 0x8000)
        record["reserve2"] = struct.unpack(">H", raw[offset + 6:offset + 8])[0]
        payload = raw[offset + HEADER_LEN:]

        pdu_tag, pdu_value, _ = read_tlv(payload, 0)
        if pdu_tag != PDU_TAG:
            raise ValueError("应用层标签不是 0x61: 0x%02x" % pdu_tag)

        goose: Dict[str, Any] = {}
        data_bytes = b""
        pos = 0
        while pos < len(pdu_value):
            tag, value, pos = read_tlv(pdu_value, pos)
            if tag == ALL_DATA_TAG:
                data_bytes = value
                continue
            name = dict(PDU_FIELDS).get(tag, "tag_0x%02x" % tag)
            if tag in (TAG_GOCBREF, TAG_DATSET, TAG_GOID):
                goose[name] = value.decode("ascii", "replace")
            elif tag == TAG_T:
                goose["t"] = decode_utctime(value)
            elif tag in (TAG_SIMULATION, TAG_NDSCOM):
                goose[name] = bool(value[0]) if value else False
            else:
                goose[name] = int.from_bytes(value, "big", signed=False)

        dataset, pos = [], 0
        while pos < len(data_bytes):
            tag, value, pos = read_tlv(data_bytes, pos)
            dataset.append(decode_data(tag, value))
        goose["allData_length"] = len(data_bytes)
        record["goose"] = goose
        record["dataset"] = dataset
        declared = goose.get("numDatSetEntries")
        if isinstance(declared, int) and declared != len(dataset):
            record["parse_status"] = "inconsistent"
            record["parse_error"] = ("numDatSetEntries=%d 与实际条目 %d 不一致"
                                     % (declared, len(dataset)))
        else:
            record["parse_status"] = "ok"
    except Exception as exc:                                  # noqa: BLE001
        record["parse_status"] = "failed"
        record["parse_error"] = "%s: %s" % (type(exc).__name__, exc)
    return record
