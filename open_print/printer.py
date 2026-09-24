"""BLE transport and print job sequencing for MXW01 printers."""
import asyncio
import time

from bleak import BleakClient, BleakScanner

from . import protocol as p
from .protocol import Cmd, Mode

CONTROL = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000ae02-0000-1000-8000-00805f9b34fb"
DATA = "0000ae03-0000-1000-8000-00805f9b34fb"
# Extra notify/indicate channels; purpose unknown, logged for exploration.
EXTRA_NOTIFY = {"0000ae04-0000-1000-8000-00805f9b34fb": "AE04", "0000ae05-0000-1000-8000-00805f9b34fb": "AE05"}
ADVERTISED_SERVICE = "0000af30-0000-1000-8000-00805f9b34fb"

DEFAULT_INTENSITY = 0x5D  # heat saturates here; higher values print no darker
# Seconds between 182-byte data chunks. At 0.01 the printer drops data after ~19KB
# (garbled rows), 0.015 after ~23KB; 0.02 and 0.03 printed 46KB 4-bit jobs cleanly. 0.025 = margin.
CHUNK_DELAY = 0.025


async def find_printer(timeout: float = 15.0):
    def match(device, adv):
        name = adv.local_name or device.name or ""
        return name.startswith("MXW") or ADVERTISED_SERVICE in adv.service_uuids

    device = await BleakScanner.find_device_by_filter(match, timeout=timeout)
    if device is None:
        raise RuntimeError("No MXW01 printer found. Is it on (green LED flashing) and not connected to a phone?")
    return device


class PrinterError(RuntimeError):
    pass


class Printer:
    def __init__(self, address: str | None = None, verbose: bool = False, on_disconnect=None):
        self.address = address
        self.verbose = verbose
        self.on_disconnect = on_disconnect
        self.client: BleakClient | None = None
        self._replies: asyncio.Queue[p.Reply] = asyncio.Queue()
        self.extra_log: list[tuple[float, str, bytes]] = []
        self.transfer_log: list[tuple[float, int]] = []
        self._cancelled = False

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    async def connect(self) -> None:
        target = self.address or await find_printer()
        self.client = BleakClient(target, disconnected_callback=lambda _c: self.on_disconnect and self.on_disconnect())
        await self.client.connect()
        await self.client.start_notify(NOTIFY, self._on_notify)
        for uuid, name in EXTRA_NOTIFY.items():
            try:
                await self.client.start_notify(uuid, lambda _c, d, n=name: self._on_extra(n, bytes(d)))
            except Exception as exc:  # noqa: BLE001
                if self.verbose:
                    print(f"  could not subscribe {name}: {exc}")
        self._replies = asyncio.Queue()

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.disconnect()

    async def __aenter__(self) -> "Printer":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    def _on_notify(self, _char, data: bytearray) -> None:
        if self.verbose:
            print(f"  <- {bytes(data).hex(' ')}", flush=True)
        reply = p.Reply.parse(bytes(data))
        if reply:
            self._replies.put_nowait(reply)

    def _on_extra(self, name: str, data: bytes) -> None:
        self.extra_log.append((time.monotonic(), name, data))
        if self.verbose:
            print(f"  <- [{name}] {data.hex(' ')}", flush=True)

    async def send(self, pkt: bytes) -> None:
        if self.verbose:
            print(f"  -> {pkt.hex(' ')}", flush=True)
        await self.client.write_gatt_char(CONTROL, pkt, response=False)

    async def command(self, cmd: int, payload: bytes = b"\x00", expect: bool = True, timeout: float = 5.0):
        await self.send(p.packet(cmd, payload))
        return await self.wait_for(cmd, timeout) if expect else None

    async def wait_for(self, cmd: int, timeout: float) -> p.Reply:
        async def loop():
            while True:
                reply = await self._replies.get()
                if reply.cmd == cmd:
                    return reply

        return await asyncio.wait_for(loop(), timeout)

    async def raw(self, cmd: int, payload: bytes, listen: float = 1.5) -> list[p.Reply]:
        """Send an arbitrary command and return every reply seen within `listen` seconds."""
        while not self._replies.empty():
            self._replies.get_nowait()
        await self.send(p.packet(cmd, payload))
        await asyncio.sleep(listen)
        replies = []
        while not self._replies.empty():
            replies.append(self._replies.get_nowait())
        return replies

    async def status(self) -> p.Status:
        return p.Status.from_payload((await self.command(Cmd.STATUS)).payload)

    async def battery(self) -> int:
        return (await self.command(Cmd.BATTERY)).payload[0]

    async def version(self) -> str:
        payload = (await self.command(Cmd.VERSION)).payload
        return payload.split(b"\x00")[0].decode(errors="replace")

    async def print_type(self) -> int:
        return (await self.command(Cmd.PRINT_TYPE)).payload[0]

    async def info(self) -> tuple[p.Status, str, int]:
        return await self.status(), await self.version(), await self.print_type()

    async def wait_idle(self, timeout: float = 30.0) -> p.Status:
        """Poll until the printer is back in standby. Motion commands sent while it's busy are
        acknowledged but silently ignored."""
        deadline = time.monotonic() + timeout
        while True:
            st = await self.status()
            if st.state == 0 or time.monotonic() > deadline:
                return st
            await asyncio.sleep(0.3)

    async def cancel(self) -> None:
        """Send AC without waiting; safe to call while print_rows() is in flight. Also stops
        streaming the rest of the job."""
        self._cancelled = True
        await self.send(p.packet(Cmd.CANCEL))

    async def feed(self, mm: int) -> None:
        await self.wait_idle()
        await self.command(Cmd.FEED, p.u16(mm))  # printer acks with 00, even with no paper
        await self.wait_idle()

    async def retract(self, mm: int) -> None:
        await self.wait_idle()
        await self.command(Cmd.RETRACT, p.u16(mm))
        await self.wait_idle()

    async def print_rows(
        self, rows: bytes, mode: Mode = Mode.MONO, intensity: int = DEFAULT_INTENSITY,
        chunk_delay: float = CHUNK_DELAY,
    ) -> float:
        """Print rows of any length as one job; returns seconds from flush to completion.

        The printer starts printing on its own once ~48 KB is buffered and keeps consuming data as it
        arrives, so long jobs stream fine (verified: 1400-row / 67 KB mono job printed continuously).
        """
        self._cancelled = False
        return await self._print_job(rows, mode, intensity, chunk_delay)

    async def _print_job(self, rows: bytes, mode: Mode, intensity: int, chunk_delay: float) -> float:
        lines = len(rows) // p.bytes_per_line(mode)
        await self.send(p.intensity(intensity))
        st = await self.wait_idle()
        while not self._replies.empty():  # drop stale replies (e.g. an AA from a cancelled job)
            self._replies.get_nowait()
        if not st.ok:
            raise PrinterError(f"Printer not ready: {st.error_name} (status {st.raw.hex(' ')})")
        await self.send(p.print_request(lines, mode))
        reply = await self.wait_for(Cmd.PRINT_REQUEST, 5.0)
        if reply.payload[:1] != b"\x00":
            code = reply.payload[0] if reply.payload else -1
            reason = p.PRINT_REJECT.get(code, f"code {code}")
            raise PrinterError(f"Print request rejected: {reason}")

        chunk = max(20, (self.client.mtu_size or 23) - 3)
        t0 = time.monotonic()
        self.transfer_log = []  # (seconds since A9 accepted, bytes sent) about every 0.5 s
        next_mark = 0.0
        for i in range(0, len(rows), chunk):
            if self._cancelled:
                break
            await self.client.write_gatt_char(DATA, rows[i : i + chunk], response=False)
            await asyncio.sleep(chunk_delay)  # don't overrun the printer's buffer
            now = time.monotonic() - t0
            if now >= next_mark:
                self.transfer_log.append((now, i + chunk))
                next_mark = now + 0.5
        if self._cancelled:
            return 0.0  # AC already sent; don't flush a partial buffer

        start = time.monotonic()
        await self.send(p.packet(Cmd.FLUSH))
        await self.wait_for(Cmd.PRINT_COMPLETE, timeout=30 + lines * 0.1)
        return time.monotonic() - start
