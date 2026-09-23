"""Send non-printing query commands to an MXW01 and dump the replies.

Usage: .venv/bin/python tools/probe.py <address>
Packet format (community-documented): 22 21 CMD 00 LEN_LO LEN_HI PAYLOAD CRC8(payload) FF
"""
import asyncio
import sys

from bleak import BleakClient

CONTROL = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000ae02-0000-1000-8000-00805f9b34fb"


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def packet(cmd: int, payload: bytes) -> bytes:
    n = len(payload)
    return bytes([0x22, 0x21, cmd, 0x00, n & 0xFF, n >> 8]) + payload + bytes([crc8(payload), 0xFF])


QUERIES = {
    "status (A1)": (0xA1, b"\x00"),
    "battery (AB)": (0xAB, b"\x00"),
    "version (B1)": (0xB1, b"\x00"),
}


async def main(address: str) -> None:
    async with BleakClient(address) as client:
        await client.start_notify(NOTIFY, lambda _c, d: print(f"  <- {d.hex(' ')}"))
        for label, (cmd, payload) in QUERIES.items():
            pkt = packet(cmd, payload)
            print(f"{label}: -> {pkt.hex(' ')}")
            await client.write_gatt_char(CONTROL, pkt, response=False)
            await asyncio.sleep(1.5)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
