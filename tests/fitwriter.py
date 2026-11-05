"""Minimal FIT encoder used only to build synthetic test fixtures.

Supports what the importer reads: file_id, developer_data_id,
field_description, session, activity, plus developer fields on any message.
Written from the FIT protocol spec (little-endian, protocol 2.0).
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

# base type id -> (struct code, size)
BASE = {
    0x00: ("B", 1),  # enum
    0x02: ("B", 1),  # uint8
    0x84: ("H", 2),  # uint16
    0x86: ("I", 4),  # uint32
    0x8C: ("I", 4),  # uint32z
    0x88: ("f", 4),  # float32
    0x07: (None, 0),  # string (size given per field)
    0x0D: (None, 0),  # byte array (size given per field)
}
ENUM, UINT8, UINT16, UINT32, UINT32Z, FLOAT32, STRING, BYTE = (
    0x00, 0x02, 0x84, 0x86, 0x8C, 0x88, 0x07, 0x0D)

_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def fit_time(dt: datetime) -> int:
    return int((dt - FIT_EPOCH).total_seconds())


def _encode(base: int, value, size: int | None = None) -> bytes:
    if base in (STRING, BYTE):
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if base == STRING:
            raw += b"\x00"
        assert size is not None and len(raw) <= size
        return raw.ljust(size, b"\x00")
    code, _ = BASE[base]
    return struct.pack("<" + code, value)


def _size(base: int, value) -> int:
    if base == STRING:
        return len(value.encode("utf-8")) + 1
    if base == BYTE:
        return len(value)
    return BASE[base][1]


class FitWriter:
    def __init__(self):
        self.body = bytearray()
        self.dev_fields: dict[str, tuple[int, int, int]] = {}  # name -> (idx, num, base)
        self._local = 0

    def message(self, global_num: int, fields: list[tuple[int, int, object]],
                dev: dict[str, object] | None = None) -> None:
        """fields: list of (field_def_num, base_type, value)."""
        dev = dev or {}
        local = self._local % 16
        self._local += 1
        has_dev = bool(dev)
        header = 0x40 | local | (0x20 if has_dev else 0)
        defn = bytearray([header, 0, 0])
        defn += struct.pack("<HB", global_num, len(fields))
        data = bytearray([local])
        for num, base, value in fields:
            size = _size(base, value)
            defn += bytes([num, size, base])
            data += _encode(base, value, size)
        if has_dev:
            defn += bytes([len(dev)])
            for name, value in dev.items():
                idx, num, base = self.dev_fields[name]
                size = _size(base, value)
                defn += bytes([num, size, idx])
                data += _encode(base, value, size)
        self.body += defn + data

    def declare_dev_field(self, name: str, num: int, base: int,
                          dev_index: int = 0, units: str = "") -> None:
        fields = [(0, UINT8, dev_index), (1, UINT8, num), (2, UINT8, base),
                  (3, STRING, name)]
        if units:
            fields.append((8, STRING, units))
        self.message(206, fields)
        self.dev_fields[name] = (dev_index, num, base)

    def developer_data_id(self, dev_index: int = 0) -> None:
        app_id = bytes(range(16))
        self.message(207, [(1, BYTE, app_id), (3, UINT8, dev_index)])

    def to_bytes(self) -> bytes:
        header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(self.body), b".FIT")
        header += struct.pack("<H", crc16(header))
        payload = header + bytes(self.body)
        return payload + struct.pack("<H", crc16(payload))


BREATHE_FIELDS = [
    ("pattern", 0, STRING), ("minutes", 1, UINT8), ("breaths", 2, UINT16),
    ("hr_pre", 3, FLOAT32), ("hr_post", 4, FLOAT32),
    ("rmssd_pre", 5, FLOAT32), ("rmssd_post", 6, FLOAT32),
]


def activity_fit(start: datetime, sport: int, sub_sport: int, duration_s: float,
                 distance_m: float | None = None, avg_hr: int | None = None,
                 max_hr: int | None = None, calories: int | None = None,
                 breathe: dict | None = None, file_type: int = 4,
                 utc_offset_s: int = 0, serial: int = 1234) -> bytes:
    """Build an activity FIT file. sport/sub_sport are FIT enum ints
    (running=1, walking=11, training=10, meditation=67; breathing=62)."""
    w = FitWriter()
    w.message(0, [(0, ENUM, file_type), (1, UINT16, 1), (2, UINT16, 3703),
                  (3, UINT32Z, serial), (4, UINT32, fit_time(start))])
    if file_type != 4:
        return w.to_bytes()
    dev = {}
    if breathe is not None:
        w.developer_data_id(0)
        for name, num, base in BREATHE_FIELDS:
            w.declare_dev_field(name, num, base)
        dev = {k: v for k, v in breathe.items() if k in w.dev_fields}
    end = fit_time(start) + int(duration_s)
    fields = [(253, UINT32, end), (2, UINT32, fit_time(start)),
              (5, ENUM, sport), (6, ENUM, sub_sport),
              (7, UINT32, int(duration_s * 1000)), (8, UINT32, int(duration_s * 1000))]
    if distance_m is not None:
        fields.append((9, UINT32, int(distance_m * 100)))
    if calories is not None:
        fields.append((11, UINT16, calories))
    if avg_hr is not None:
        fields.append((16, UINT8, avg_hr))
    if max_hr is not None:
        fields.append((17, UINT8, max_hr))
    w.message(18, fields, dev)
    # activity: timestamp, total_timer_time, num_sessions, type, event, event_type, local_timestamp
    w.message(34, [(253, UINT32, end), (0, UINT32, int(duration_s * 1000)),
                   (1, UINT16, 1), (2, ENUM, 0), (3, ENUM, 26), (4, ENUM, 1),
                   (5, UINT32, end + utc_offset_s)])
    return w.to_bytes()
