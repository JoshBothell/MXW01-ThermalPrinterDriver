# CLAUDE.md

## Project
open_print drives an **MXW01** 58mm BLE thermal printer ("Fun Print" app, item ID 85M2K522V3) directly from
macOS. This repo is the foundation for a **full app, planned as a webapp**. The CLI and service are the
initial interface, so keep the library layer (`protocol` / `printer` / `raster`) UI-agnostic. User-facing
choices (render mode, dithering, heat) are CLI flags for now and should become webapp controls later.

Read `docs/protocol.md` before touching the protocol. It separates verified facts from community claims.

## Environment
- Python venv at `.venv` (Python 3.12 via `uv`; system Python 3.9 can't build pyobjc). Deps: bleak, pillow.
- Install: `uv pip install --python .venv -e .`. Run with `.venv/bin/open-print …` or `source .venv/bin/activate`.
- macOS CoreBluetooth hides MAC addresses. The printer's address here is a per-host UUID
  (`AE20E845-D1C5-AA5D-4474-B4A3BC9337C6` on the dev Mac). Discover by name `MXW*` / service `0xAF30`.

## Working with the physical printer
- The user is the "flesh proxy". Ask them to power on the printer, check prints, or open the lid. Describe
  exactly what to look for on the paper.
- Printer must be on (green LED) with the phone app closed. It auto-sleeps within minutes unless something is
  connected. Run `open-print serve` in the background to hold the connection.
- **Restart `serve` after changing library code.** It's a long-running process.
- `tools/diag.py <test>` needs `serve` running. Use `--dry-run DIR` to render bitmaps and look at them before
  spending paper.
- Paper is finite: keep diagnostic prints short and batch them.

## Hard-won rules
- **Pacing:** 25 ms between 182-byte AE03 chunks (`CHUNK_DELAY`). 10 ms breaks after ~19 KB, 15 ms after ~23 KB;
  20 ms is clean. The printer silently drops data. There's no flow-control signal (AE04/AE05 are silent).
- **Busy = ignored:** commands sent while status state ≠ 0 are acked but ignored. Use `Printer.wait_idle()`.
- **Feed/retract units ≈ 1 mm.** CLI caps retract at 30 mm. Every job auto-feeds ~13.5 mm after printing.
- **Errors:** status errflag/errcode 1/1 = no paper (verified). A9 reply 02 = rejected, no paper.
- **Heat:** A2 saturates at 0x5D (confirmed on paper; 0x64+ no darker, fine detail still crisp). Default 0x5D.
- **Orientation:** the first row sent prints first. The CLI rotates 180° so output reads upright on the printer.
- **4-bit:** mode byte 0x02, 192 bytes/row, high nibble = left dot, 0xF = black. Levels 0–4 barely mark.
- **Mono:** 48 bytes/row, LSB = leftmost dot, 1 = black. Floyd-Steinberg dithered mono beats 4-bit for photos
  (user preference, confirmed side by side), so it's the default. Keep 4-bit as a toggle.
- No padding to 90 rows needed.
- Don't send unknown opcodes (A5, A6, A8, AF, B4+) or touch service AE3A (likely OTA) without asking the user.

## Architecture
- `protocol.py`: pure functions/dataclasses (framing, CRC, Status parsing). No I/O.
- `printer.py`: `Printer` async context manager over bleak. `print_rows(rows, mode, intensity, chunk_delay)`.
- `raster.py`: PIL → rows. `prepare()` scales to 384 wide; `to_rows()` packs for a mode.
- `service.py`: `PrinterService` (holds connection, 30s status keepalive, auto-reconnect, Unix socket
  `~/.open_print/printer.sock`, newline-JSON ops: status/feed/retract/print/raw) and `RemotePrinter` client.
- `cli.py`: tries `RemotePrinter` first and falls back to a direct `Printer`.

## Open questions / next experiments
- Error codes 9 (community "no paper"), 4 (overheat) and 8 (low battery) are unverified. Lid open and empty
  both give 1.
- Pacing could be smarter: burst the first ~16 KB, then throttle. Maximum job length/buffer size unknown.
- Mode 0x01, commands AC (cancel), AE/B2 meaning, status byte 8.
- Gray-mode tuning: remap levels so 0–4 aren't wasted (compress the tone curve into 5–F).
