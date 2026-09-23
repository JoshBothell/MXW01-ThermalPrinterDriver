"""Scan for BLE advertisements and print everything we can see.

Usage: .venv/bin/python tools/scan.py [seconds] [--all]
By default hides unnamed devices with no service UUIDs to cut noise.
"""
import asyncio
import sys

from bleak import BleakScanner


async def main(duration: float, show_all: bool) -> None:
    print(f"Scanning for {duration:.0f}s...")
    found = await BleakScanner.discover(timeout=duration, return_adv=True)
    rows = sorted(found.values(), key=lambda da: -da[1].rssi)
    for device, adv in rows:
        name = adv.local_name or device.name
        if not show_all and not name and not adv.service_uuids:
            continue
        print(f"\n{name or '<no name>'}  addr={device.address}  rssi={adv.rssi}")
        for uuid in adv.service_uuids:
            print(f"  service: {uuid}")
        for cid, data in adv.manufacturer_data.items():
            print(f"  manufacturer 0x{cid:04x}: {data.hex(' ')}")
        for uuid, data in adv.service_data.items():
            print(f"  service_data {uuid}: {data.hex(' ')}")
        if adv.tx_power is not None:
            print(f"  tx_power: {adv.tx_power}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(main(float(args[0]) if args else 10.0, "--all" in sys.argv))
