"""MXW01 command framing.

Control packets go to AE01, replies arrive on AE02:
    22 21 CMD 00 LEN_LO LEN_HI PAYLOAD... CRC8(payload) FF
Replies use the same header but carry no CRC/trailer.
Raw bitmap rows are written to AE03 (no framing).
"""
from dataclasses import dataclass
from enum import IntEnum

PRINTER_WIDTH = 384  # dots (48mm at 203 dpi)
MIN_LINES = 0  # docs claim 90 rows minimum, but a 30-row unpadded job printed fine (fw 1.9.3.1.2)


class Cmd(IntEnum):
    STATUS = 0xA1
    INTENSITY = 0xA2
    FEED = 0xA3  # payload: u16 LE distance, measured ~1 mm per unit (80 -> 81 mm)
    RETRACT = 0xA4  # payload: u16 LE distance, measured ~1 mm per unit (40 -> ~38.5 mm)
    QUERY_COUNT = 0xA7
    PRINT_REQUEST = 0xA9
    PRINT_COMPLETE = 0xAA
    BATTERY = 0xAB
    CANCEL = 0xAC
    FLUSH = 0xAD
    PRINT_TYPE = 0xB0
    VERSION = 0xB1


class Mode(IntEnum):
    MONO = 0x00  # 1 bit/dot, 48 bytes/line, LSB = leftmost dot, 1 = black
    MONO_ALT = 0x01  # also 1bpp per community docs; purpose unknown
    GRAY4 = 0x02  # 4 bits/dot, 192 bytes/line, high nibble = left dot, 0xF = black


def bytes_per_line(mode: Mode) -> int:
    return PRINTER_WIDTH // 2 if mode == Mode.GRAY4 else PRINTER_WIDTH // 8


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def packet(cmd: int, payload: bytes = b"\x00") -> bytes:
    n = len(payload)
    return bytes([0x22, 0x21, cmd, 0x00, n & 0xFF, n >> 8]) + payload + bytes([crc8(payload), 0xFF])


def u16(n: int) -> bytes:
    return bytes([n & 0xFF, (n >> 8) & 0xFF])


def print_request(lines: int, mode: Mode = Mode.MONO) -> bytes:
    return packet(Cmd.PRINT_REQUEST, u16(lines) + bytes([0x30, mode]))


def intensity(level: int) -> bytes:
    return packet(Cmd.INTENSITY, bytes([max(0, min(255, level))]))


@dataclass
class Reply:
    cmd: int
    payload: bytes
    raw: bytes

    @classmethod
    def parse(cls, data: bytes) -> "Reply | None":
        if len(data) < 6 or data[0] != 0x22 or data[1] != 0x21:
            return None
        length = data[4] | (data[5] << 8)
        return cls(data[2], bytes(data[6 : 6 + length]), bytes(data))


STATES = {0: "standby", 1: "printing", 2: "feeding", 3: "retracting"}
ERRORS = {1: "no paper or lid open", 9: "no paper", 4: "overheated", 8: "low battery"}  # only 1 verified
# A9 print-request reply codes (payload[0]). 0 = accepted; 2 verified with paper removed.
PRINT_REJECT = {2: "no paper"}


@dataclass
class Status:
    state: int
    battery: int
    temperature: int
    error_flag: int
    error_code: int
    sensor: int  # u16 at bytes 8-9; ~0x0106-0x010D with paper, ~0x08A6-0x0C44 without (paper sensor?)
    raw: bytes

    @property
    def ok(self) -> bool:
        return self.error_flag == 0

    @property
    def state_name(self) -> str:
        return STATES.get(self.state, f"unknown({self.state})")

    @property
    def error_name(self) -> str:
        if self.ok:
            return "none"
        return ERRORS.get(self.error_code, f"unknown({self.error_code})")

    @classmethod
    def from_payload(cls, p: bytes) -> "Status":
        # Idle:     00 00 00 56 14 00 00 00 09 01
        # No paper: 00 00 00 54 1d 00 01 01 a6 08
        #           ^state   ^batt ^temp  ^flag ^code ^sensor(u16 LE)
        return cls(
            state=p[0], battery=p[3], temperature=p[4], error_flag=p[6], error_code=p[7],
            sensor=p[8] | (p[9] << 8), raw=p,
        )
