"""Long-running service that holds the BLE connection so the printer stays awake.

Protocol: one JSON object per line over a Unix socket; one JSON reply per request.
    {"op": "status"} | {"op": "feed", "mm": N} | {"op": "retract", "mm": N} | {"op": "cancel"}
    {"op": "print", "mode": 0|2, "intensity": N, "rows": "<base64>"}
Replies: {"ok": true, ...} or {"ok": false, "error": "..."}
"""
import asyncio
import base64
import json
import os
from pathlib import Path

from .printer import CHUNK_DELAY, Printer, PrinterError
from .protocol import Mode, Status

SOCKET_PATH = Path(os.environ.get("OPEN_PRINT_SOCKET", Path.home() / ".open_print" / "printer.sock"))
KEEPALIVE_SECONDS = 30
TEAR_FEED_MM = 14  # a normal job auto-feeds ~13.5 mm to the tear bar; a cancelled one doesn't
RECONNECT_SECONDS = 5


def log(msg: str) -> None:
    print(msg, flush=True)


class PrinterService:
    def __init__(self, address: str | None = None, verbose: bool = False):
        self._disconnected = asyncio.Event()
        self.printer = Printer(address, verbose=verbose, on_disconnect=self._disconnected.set)
        self.lock = asyncio.Lock()  # one BLE conversation at a time
        self.printing = False

    async def connection_loop(self) -> None:
        while True:
            try:
                log("Looking for printer...")
                await self.printer.connect()
                self._disconnected.clear()
                log(f"Connected to {self.printer.client.address}. Holding connection.")
                await self._disconnected.wait()
                log("Printer disconnected.")
            except Exception as exc:  # noqa: BLE001
                log(f"Connect failed: {exc}")
            await asyncio.sleep(RECONNECT_SECONDS)

    async def keepalive_loop(self) -> None:
        # Periodic status poll: keeps the link active and surfaces errors in the log.
        last = None
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            if not self.printer.is_connected:
                continue
            try:
                async with self.lock:
                    st = await self.printer.status()
                summary = f"{st.state_name}, battery {st.battery}%, {st.temperature}°C, error: {st.error_name}"
                if summary != last:
                    log(f"Status: {summary}")
                    last = summary
            except Exception as exc:  # noqa: BLE001
                log(f"Keepalive failed: {exc}")

    async def handle(self, req: dict) -> dict:
        if not self.printer.is_connected:
            return {"ok": False, "error": "printer not connected (is it on?)"}
        op = req.get("op")
        if op == "cancel":  # deliberately bypasses the lock so it can interrupt a running print
            was_printing = self.printing
            await self.printer.cancel()
            if was_printing:
                async with self.lock:  # released once the print reports (early) completion
                    await self.printer.feed(TEAR_FEED_MM)
            return {"ok": True, "was_printing": was_printing}
        async with self.lock:
            if op == "status":
                st = await self.printer.status()
                return {
                    "ok": True,
                    "status": st.raw.hex(),
                    "version": await self.printer.version(),
                    "print_type": await self.printer.print_type(),
                }
            if op == "feed":
                await self.printer.feed(int(req["mm"]))
                return {"ok": True}
            if op == "retract":
                await self.printer.retract(int(req["mm"]))
                return {"ok": True}
            if op == "raw":  # exploration: arbitrary command, returns all replies seen
                replies = await self.printer.raw(
                    int(req["cmd"]), bytes.fromhex(req.get("payload", "00")), float(req.get("listen", 1.5))
                )
                return {"ok": True, "replies": [r.raw.hex() for r in replies]}
            if op == "print":
                rows = base64.b64decode(req["rows"])
                self.printer.extra_log.clear()
                self.printing = True
                try:
                    secs = await self.printer.print_rows(
                        rows, Mode(req["mode"]), int(req["intensity"]), float(req.get("chunk_delay", CHUNK_DELAY))
                    )
                finally:
                    self.printing = False
                extra = [f"{t:.3f} {name} {d.hex(' ')}" for t, name, d in self.printer.extra_log]
                return {"ok": True, "seconds": secs, "extra": extra, "transfer": self.printer.transfer_log}
        return {"ok": False, "error": f"unknown op {op!r}"}

    async def on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            try:
                resp = await self.handle(json.loads(line))
            except PrinterError as exc:
                resp = {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    async def run(self) -> None:
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SOCKET_PATH.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self.on_client, path=str(SOCKET_PATH), limit=16 * 1024 * 1024)
        log(f"Listening on {SOCKET_PATH}")
        try:
            async with server:
                await asyncio.gather(server.serve_forever(), self.connection_loop(), self.keepalive_loop())
        finally:
            SOCKET_PATH.unlink(missing_ok=True)
            await self.printer.disconnect()


class RemotePrinter:
    """Same interface the CLI uses on Printer, but routed through a running service."""

    def __init__(self, path: Path = SOCKET_PATH):
        self.path = path

    @staticmethod
    def available(path: Path = SOCKET_PATH) -> bool:
        return path.exists()

    async def _call(self, **req) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.path), limit=16 * 1024 * 1024)
        writer.write(json.dumps(req).encode() + b"\n")
        await writer.drain()
        resp = json.loads(await reader.readline())
        writer.close()
        if not resp.get("ok"):
            raise PrinterError(resp.get("error", "unknown error"))
        return resp

    async def info(self) -> tuple[Status, str, int]:
        resp = await self._call(op="status")
        return Status.from_payload(bytes.fromhex(resp["status"])), resp["version"], resp["print_type"]

    async def raw(self, cmd: int, payload: bytes = b"\x00", listen: float = 1.5) -> list[bytes]:
        resp = await self._call(op="raw", cmd=cmd, payload=payload.hex(), listen=listen)
        return [bytes.fromhex(r) for r in resp["replies"]]

    async def cancel(self) -> None:
        await self._call(op="cancel")

    async def feed(self, mm: int) -> None:
        await self._call(op="feed", mm=mm)

    async def retract(self, mm: int) -> None:
        await self._call(op="retract", mm=mm)

    async def print_rows(self, rows: bytes, mode: Mode, intensity: int, chunk_delay: float = CHUNK_DELAY) -> float:
        resp = await self._call(
            op="print", mode=int(mode), intensity=intensity, chunk_delay=chunk_delay,
            rows=base64.b64encode(rows).decode(),
        )
        self.last_extra = resp.get("extra", [])
        self.last_transfer = resp.get("transfer", [])
        return resp["seconds"]
