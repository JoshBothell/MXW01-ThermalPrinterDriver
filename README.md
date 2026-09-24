# open_print

Print directly to a cheap Bluetooth "Fun Print" thermal printer from your computer, without the vendor app.

Right now this is a Python library, CLI and background service. It's the groundwork for a full app.

## The printer

| | |
|---|---|
| Model (BLE name) | **MXW01**, part of the "cat printer" family |
| Sold as | generic 58mm mini thermal printer, item ID `85M2K522V3`, white rounded case |
| Vendor app | **Fun Print** (iOS/Android), manual kept locally in `.reference/` (not redistributed) |
| Firmware | `1.9.3.1.2` (print type `0x01`) |
| Device ID | `5e e3 4c 36 dd 5e` (from command A7) |
| Radio | Bluetooth Low Energy only, 2402–2480 MHz, max 4.6 dBm. Does **not** appear in OS Bluetooth settings |
| BLE services | advertises `0xAF30`; main service `0xAE30` (plus `0xAE3A`, probably firmware update) |
| Print head | 384 dots wide, 8 dots/mm (~203 dpi), **48 mm printable** on 57/58 mm paper |
| Modes | 1-bit black/white and 4-bit (16-level) grayscale |
| Power | built-in battery, USB-C for **charging only** (no data) |
| LED | green flashing = advertising/waiting · green solid = connected · red = charging (no paper-out signal) |
| Paper | 57/58 mm thermal roll; ~13.5 mm head-to-tear-bar; feeds and retracts a few mm on power-up |
| Quirks | turns itself off after a few idle minutes unless connected; one connection at a time |

Full protocol notes: [docs/protocol.md](docs/protocol.md).

## Setup

Needs Python ≥ 3.10 (the macOS system Python 3.9 is too old for the Bluetooth library).

```sh
brew install uv            # if needed
uv venv --python 3.12 .venv
uv pip install --python .venv -e .
source .venv/bin/activate  # now `open-print` is on your PATH
```

The first time, macOS asks whether your terminal app may use Bluetooth. Allow it.

## Usage

Turn the printer on (green light flashing) and close the Fun Print app on your phone.

```sh
open-print serve &          # optional: holds the connection so the printer stays on (solid green)
open-print status           # firmware, battery, temperature, errors
open-print text "Hello"     # --size 40, --font /path/to.ttf
open-print image photo.jpg  # dithered black/white (best for photos)
open-print image photo.jpg --mode gray    # 4-bit grayscale
open-print image logo.png --no-dither     # hard threshold, for line art
open-print feed 20          # advance paper 20 mm
open-print retract 5        # pull paper back 5 mm (capped at 30 mm so it doesn't unthread)
open-print cancel           # stop the current print
open-print text "x" --preview out.png     # render to a file instead of printing
```

Common options for `text`/`image`: `--intensity 0..0x5D` (heat; higher values are no darker),
`--no-rotate` (by default output is rotated to read correctly with the printer upright), `--mode mono|gray`.

When `open-print serve` is running, other commands send their jobs through it. When it isn't, each command
connects on its own, which is slower (it has to scan first) and doesn't keep the printer awake.

## What we've learned (practical)

- **Dithered mono (Floyd-Steinberg) looks best** for photos and is the default. 4-bit gray (`--mode gray`)
  works once the data is paced, but it's slower, only levels 5–F of 0–F show up, and it looks worse side by side.
- **Heat maxes out at 0x5D (93)**, the default. Anything higher is no darker. At 0x5D the finest
  checkerboards and 1px lines stay crisp. 0x30–0x5D gives lighter options.
- **Send data slowly.** The printer silently drops data that arrives too fast, which garbles the rest of the
  print. The driver waits 25 ms between chunks (15 ms breaks, 20 ms works).
- **Every print ends with ~13.5 mm of automatic feed** to reach the tear bar, so combine content into one job.
- **Long prints work as a single job.** The printer streams (verified with 17.5 cm of mono). 4-bit gray is
  currently only reliable up to ~31 mm per print.
- **`open-print cancel`** stops a running print within about a second, then feeds the partial print out to the tear bar.
- Generated text defaults to **Menlo Bold 32**. Thin strokes are hard to read on thermal paper.
- **Feed/retract move ~1 mm per unit**, and commands sent while the printer is busy are silently ignored.
  The driver waits for standby.
- **Paper out** gives no beep or light; it only shows in `status` (error "no paper"), and prints are refused.
  Lid open and empty-with-lid-closed report the same thing (there's only a paper sensor, no lid switch).
- No minimum job length is needed (docs claim 90 rows; 30 rows printed fine).
- The print head warms up noticeably over consecutive jobs (20 → 44 °C across ~15 test prints).

## Layout

```
open_print/protocol.py   packet framing, commands, status parsing
open_print/printer.py    BLE transport + print job sequence (bleak)
open_print/raster.py     image/text → printer rows (1-bit and 4-bit)
open_print/service.py    `serve`: persistent connection + Unix-socket job API
open_print/cli.py        `open-print` command
tools/scan.py            BLE scan (find the printer)
tools/explore.py         dump GATT table
tools/probe.py           early raw command probe
tools/diag.py            diagnostic prints (heat ladder, gray ramp, sync/pacing, photo)
docs/protocol.md         protocol reference
```

## Credits

Protocol groundwork by [jeremy46231/MXW01-catprinter](https://github.com/jeremy46231/MXW01-catprinter),
[MaikelChan/CatPrinterBLE](https://github.com/MaikelChan/CatPrinterBLE) and
[kamome-run/jarful](https://github.com/kamome-run/jarful).
