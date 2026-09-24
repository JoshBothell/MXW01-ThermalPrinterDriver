"""Diagnostic / exploratory tests for the MXW01. Requires `open-print serve` to be running.

Usage: python tools/diag.py <test> [--dry-run DIR]
Tests:
  probe      query commands (no paper): A1 A7 AB B0 B1 + unknowns AE B2 B3
  intensity  one short mono job per heat level (~8cm paper)
  heatfine   same, finer steps 0x30..0x64 (0x64 = 100)
  gray       4-bit 16-step ramp + fine text at two heat levels
  photo      same photo in mono-dithered vs 4-bit gray
  short      a 30-line job with no padding (tests MIN_LINES)
  sync       4-bit transfer-integrity card (labels = transmitted row index); use --delay
  units      measure feed/retract units: ruler, retract 40 + marker, marker, feed 80 + marker
  cancel     480-row job; send AC (cancel) ~2.5s into physical printing
  long       600-row 4-bit sync card (115 KB, ~7.5 cm paper): 4-bit streaming test
  stream     one mono job of --rows rows (default 1400 = 67 KB, 17.5 cm) + transfer-rate profile
All images are rotated 180° like the CLI default, so they read upright on the printer.
--dry-run saves the bitmaps to DIR instead of printing.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from open_print import raster  # noqa: E402
from open_print.protocol import PRINTER_WIDTH, Mode  # noqa: E402
from open_print.service import RemotePrinter  # noqa: E402

ASSETS = Path(__file__).parent / "assets"
W = PRINTER_WIDTH


def font(size: int) -> ImageFont.FreeTypeFont:
    """Bold, and 1.5x the nominal size: the original sizes were hard to read on paper."""
    return raster.load_font(round(size * 1.5))


def canvas(h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("L", (W, h), 255)
    return img, ImageDraw.Draw(img)


def intensity_card(level: int) -> Image.Image:
    img, d = canvas(72)
    d.text((0, 0), f"heat 0x{level:02X} ({level})", font=font(18), fill=0)
    d.rectangle((0, 38, 119, 69), fill=0)  # solid
    for y in range(38, 70):  # 50% checkerboard (fine detail on purpose)
        for x in range(128, 248):
            if (x + y) % 2 == 0:
                img.putpixel((x, y), 0)
    for x in range(256, W, 4):  # 1px lines, 3px gaps (fine detail on purpose)
        d.line((x, 38, x, 69), fill=0)
    return img


def gray_card(level: int) -> Image.Image:
    img, d = canvas(200)
    d.text((0, 0), f"4-bit, heat 0x{level:02X}", font=font(18), fill=0)
    step = W // 16
    for i in range(16):  # 16 discrete levels, 0 (white) .. 15 (black)
        shade = 255 - i * 17
        d.rectangle((i * step, 36, (i + 1) * step - 1, 96), fill=shade)
        d.text((i * step + 5, 98), f"{i:X}", font=font(13), fill=0)
    for x in range(W):  # continuous gradient
        d.line((x, 126, x, 151), fill=255 - round(x * 255 / (W - 1)))
    d.text((0, 156), "Black text 0123", font=font(13), fill=0)
    d.text((0, 178), "Gray text 0123", font=font(13), fill=128)
    return img


def owl_path() -> Path:
    """Apple's stock owl picture (not redistributed); converted once from the macOS system copy."""
    path = ASSETS / "owl.png"
    if not path.exists():
        import subprocess

        ASSETS.mkdir(exist_ok=True)
        subprocess.run(["sips", "-s", "format", "png", "/Library/User Pictures/Animals/Owl.heic", "--out", str(path)],
                       check=True, capture_output=True)
    return path


def photo(mode: Mode) -> Image.Image:
    src = raster.to_gray(Image.open(owl_path()))
    src = src.crop((0, 60, W, 300))
    label = "mono Floyd-Steinberg" if mode == Mode.MONO else "4-bit gray"
    img, d = canvas(src.height + 26)
    d.text((0, 0), label, font=font(20), fill=0)
    img.paste(src, (0, 26))
    return raster.prepare(img, mode) if mode == Mode.MONO else img


def sync_card(delay: float, rows: int = 240) -> Image.Image:
    """Labels show *transmitted* row index (card is pre-rotated so tx row 0 prints first)."""
    img, d = canvas(rows)
    for y in range(rows):  # 3px diagonal; any byte shift breaks it visibly
        x = (y * 3) % W
        d.line((x, y, x + 6, y), fill=0)
    for tx in range(0, rows, 20):
        y = rows - 1 - tx  # undo the later 180° rotation
        d.rectangle((0, y - 2, 60, y), fill=0)
        d.text((W - 205, y - 22), f"tx {tx} d={delay}", font=font(12), fill=0)
    for i in range(16):
        d.rectangle((70 + i * 6, 0, 75 + i * 6, rows - 1), fill=255 - i * 17)
    return img


def ruler(mm: int = 60) -> Image.Image:
    """Labels = mm back from the LAST printed row (the edge at the head when printing ends).
    Built in upright-view coordinates: top of image = last printed (after the 180° rotation)."""
    img, d = canvas(mm * 8 + 1)
    for n in range(mm + 1):
        y = n * 8  # 8 dots/mm
        length = 90 if n % 10 == 0 else 50 if n % 5 == 0 else 22
        width = 3 if n % 5 == 0 else 1
        d.line((0, y, length, y), fill=0, width=width)
        d.line((W - length, y, W - 1, y), fill=0, width=width)
        if n % 5 == 0 and n:
            d.text((100, y - 14), f"{n} mm", font=font(16), fill=0)
    d.line((0, 0, W - 1, 0), fill=0)
    return img


def marker(label: str) -> Image.Image:
    """Thick line on the FIRST printed rows (bottom in upright view), label beside it."""
    img, d = canvas(28)
    d.rectangle((0, 24, W - 1, 27), fill=0)
    d.text((W - 110, 0), label, font=font(14), fill=0)
    return img


async def wait_idle(p: RemotePrinter, timeout: float = 20.0) -> None:
    t = time.monotonic()
    await asyncio.sleep(0.5)
    while time.monotonic() - t < timeout:
        replies = await p.raw(0xA1, b"\x00", listen=0.3)
        if any(r[2] == 0xA1 and r[6] == 0 for r in replies):
            return
    print("  (timed out waiting for standby)")


def progress_card(rows: int = 480) -> Image.Image:
    """Labels = transmitted row index every 40 rows, so the stop point can be read off the paper."""
    img, d = canvas(rows)
    for tx in range(0, rows, 40):
        y = rows - 1 - tx
        d.rectangle((0, y - 4, W - 1, y), fill=0)
        d.text((10, y - 36), f"row {tx}", font=font(18), fill=0)
    d.rectangle((W // 2 - 2, 0, W // 2 + 2, rows - 1), fill=0)
    return img


def short_card() -> Image.Image:
    img, d = canvas(30)
    d.text((0, 4), "30 lines, no padding", font=font(20), fill=0)
    return img


async def print_job(p: RemotePrinter | None, img: Image.Image, mode: Mode, heat: int, out: Path | None,
                    name: str, min_lines: int | None = None, delay: float = 0.025,
                    profile: bool = False) -> None:
    img = img.rotate(180)
    if out:
        out.mkdir(parents=True, exist_ok=True)
        img.save(out / f"{name}.png")
        print(f"saved {name}.png")
        return
    kwargs = {} if min_lines is None else {"min_lines": min_lines}
    rows = raster.to_rows(img, mode, **kwargs)
    t = time.monotonic()
    try:
        secs = await p.print_rows(rows, mode, heat, chunk_delay=delay)
        print(f"{name}: {img.height} lines, mode {mode.name}, heat 0x{heat:02X}: "
              f"{secs:.2f}s printing, {time.monotonic() - t:.2f}s total, {len(rows)} bytes")
        for line in p.last_extra:
            print(f"    {line}")
        if profile:  # streaming experiments: show the transfer-rate profile
            prev_t, prev_b = 0.0, 0
            for t, b in p.last_transfer:
                rate = (b - prev_b) / max(t - prev_t, 1e-6) / 1024
                print(f"    t={t:5.1f}s sent={b / 1024:6.1f} KB  rate={rate:4.1f} KB/s")
                prev_t, prev_b = t, b
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: FAILED {exc}")


async def main(test: str, out: Path | None, delay: float, rows: int = 1400) -> None:
    p = None if out else RemotePrinter()
    if test == "probe":
        for cmd in (0xA1, 0xA7, 0xAB, 0xB0, 0xB1, 0xAE, 0xB2, 0xB3):
            replies = await p.raw(cmd, b"\x00", listen=1.5)
            shown = " | ".join(r.hex(" ") for r in replies) or "(no reply)"
            print(f"0x{cmd:02X}: {shown}")
    elif test == "intensity":
        for level in (0x10, 0x30, 0x5D, 0x80, 0xB0, 0xE0, 0xFF):
            await print_job(p, intensity_card(level), Mode.MONO, level, out, f"intensity_{level:02x}")
    elif test == "heatfine":
        for level in (0x30, 0x38, 0x40, 0x48, 0x50, 0x58, 0x5D, 0x64):
            await print_job(p, intensity_card(level), Mode.MONO, level, out, f"heatfine_{level:02x}")
    elif test == "gray":
        for level in (0x5D, 0xE0):
            await print_job(p, gray_card(level), Mode.GRAY4, level, out, f"gray_{level:02x}")
    elif test == "photo":
        await print_job(p, photo(Mode.MONO), Mode.MONO, 0x5D, out, "photo_mono")
        await print_job(p, photo(Mode.GRAY4), Mode.GRAY4, 0x5D, out, "photo_gray")
    elif test == "photogray":
        await print_job(p, photo(Mode.GRAY4), Mode.GRAY4, 0x5D, out, "photo_gray")
    elif test == "short":
        await print_job(p, short_card(), Mode.MONO, 0x5D, out, "short", min_lines=0)
    elif test == "units":
        await print_job(p, ruler(), Mode.MONO, 0x5D, out, "units_ruler")
        if not out:
            await wait_idle(p)
            await p.retract(40)
            await wait_idle(p)
        await print_job(p, marker("R40"), Mode.MONO, 0x5D, out, "units_r40")
        await print_job(p, marker("N0"), Mode.MONO, 0x5D, out, "units_n0")
        if not out:
            await wait_idle(p)
            await p.feed(80)
            await wait_idle(p)
        await print_job(p, marker("F80"), Mode.MONO, 0x5D, out, "units_f80")
    elif test == "cancel" and out:
        await print_job(p, progress_card(), Mode.MONO, 0x5D, out, "cancel")
    elif test == "cancel":
        # Transfer of 23 KB takes ~3.3s at 25 ms pacing, then physical printing starts at the flush.
        async def cancel_later():
            await asyncio.sleep(6.0)
            await RemotePrinter().cancel()
            print(f"  cancel sent at t+6.0s")
        await asyncio.gather(print_job(p, progress_card(), Mode.MONO, 0x5D, out, "cancel"), cancel_later())
        st, *_ = await p.info()
        print(f"  after: state={st.state_name} error={st.error_name} raw={st.raw.hex(' ')}")
    elif test == "long":
        await print_job(p, sync_card(delay, rows=600), Mode.GRAY4, 0x5D, out, "long", delay=delay)
    elif test == "stream":
        await print_job(p, progress_card(rows), Mode.MONO, 0x5D, out, f"stream_{rows}", delay=delay, profile=True)
    elif test == "sync":
        await print_job(p, sync_card(delay), Mode.GRAY4, 0x5D, out, f"sync_{delay}", delay=delay)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("test")
    ap.add_argument("--dry-run", type=Path)
    ap.add_argument("--delay", type=float, default=0.025, help="seconds between data chunks")
    ap.add_argument("--rows", type=int, default=1400, help="rows for the stream test")
    a = ap.parse_args()
    asyncio.run(main(a.test, a.dry_run, a.delay, a.rows))
