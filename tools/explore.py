"""Connect to a BLE device and dump its full GATT table.

Usage: .venv/bin/python tools/explore.py <address-or-name>
Readable characteristics are read; notifications are listened to for a few seconds.
"""
import asyncio
import sys

from bleak import BleakClient, BleakScanner


async def resolve(target: str):
    device = await BleakScanner.find_device_by_address(target, timeout=10)
    if device is None:
        device = await BleakScanner.find_device_by_name(target, timeout=10)
    if device is None:
        sys.exit(f"Could not find {target!r}. Is it on and not connected to the phone?")
    return device


async def main(target: str) -> None:
    device = await resolve(target)
    print(f"Connecting to {device.name} ({device.address})...")
    async with BleakClient(device) as client:
        print(f"Connected. MTU={client.mtu_size}\n")
        notify_chars = []
        for service in client.services:
            print(f"Service {service.uuid}  ({service.description})")
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"  Char {char.uuid}  handle={char.handle}  [{props}]  ({char.description})")
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char)
                        print(f"    value: {value.hex(' ')}  {value!r}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"    read failed: {exc}")
                if "notify" in char.properties or "indicate" in char.properties:
                    notify_chars.append(char)
                for desc in char.descriptors:
                    print(f"    Desc {desc.uuid}  handle={desc.handle}")

        def on_notify(char, data: bytearray) -> None:
            print(f"  notify {char.uuid}: {data.hex(' ')}")

        for char in notify_chars:
            try:
                await client.start_notify(char, on_notify)
            except Exception as exc:  # noqa: BLE001
                print(f"start_notify {char.uuid} failed: {exc}")
        if notify_chars:
            print("\nListening for notifications for 5s...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
