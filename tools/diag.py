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
    return ImageFont.truetype(raster.DEFAULT_FONT, size)


def canvas(h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("L", (W, h), 255)
    return img, ImageDraw.Draw(img)


def intensity_card(level: int) -> Image.Image:
    img, d = canvas(60)
    d.text((0, 2), f"heat 0x{level:02X} ({level})", font=font(18), fill=0)
    d.rectangle((0, 26, 119, 57), fill=0)  # solid
    for y in range(26, 58):  # 50% checkerboard
        for x in range(128, 248):
            if (x + y) % 2 == 0:
                img.putpixel((x, y), 0)
    for x in range(256, W, 4):  # 1px lines, 3px gaps
        d.line((x, 26, x, 57), fill=0)
    return img


def gray_card(level: int) -> Image.Image:
    img, d = canvas(170)
    d.text((0, 0), f"4-bit ramp, heat 0x{level:02X}", font=font(20), fill=0)
    step = W // 16
    for i in range(16):  # 16 discrete levels, 0 (white) .. 15 (black)
        shade = 255 - i * 17
        d.rectangle((i * step, 28, (i + 1) * step - 1, 88), fill=shade)
        d.text((i * step + 7, 90), f"{i:X}", font=font(14), fill=0)
    for x in range(W):  # continuous gradient
        d.line((x, 110, x, 135), fill=255 - round(x * 255 / (W - 1)))
    d.text((0, 140), "Small text 12px: The quick brown fox 0123", font=font(12), fill=0)
    d.text((0, 154), "Small gray text", font=font(12), fill=128)
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
        d.line((x, y, x + 2, y), fill=0)
    for tx in range(0, rows, 20):
        y = rows - 1 - tx  # undo the later 180° rotation
        d.line((0, y, 60, y), fill=0)
        d.text((W - 150, y - 16), f"tx {tx} d={delay}", font=font(14), fill=0)
    for i in range(16):
        d.rectangle((70 + i * 8, 0, 77 + i * 8, rows - 1), fill=255 - i * 17)
    return img


def ruler(mm: int = 60) -> Image.Image:
    """Labels = mm back from the LAST printed row (the edge at the head when printing ends).
    Built in upright-view coordinates: top of image = last printed (after the 180° rotation)."""
    img, d = canvas(mm * 8 + 1)
    for n in range(mm + 1):
        y = n * 8  # 8 dots/mm
        length = 90 if n % 10 == 0 else 50 if n % 5 == 0 else 22
        d.line((0, y, length, y), fill=0)
        d.line((W - length, y, W - 1, y), fill=0)
        if n % 5 == 0 and n:
            d.text((100, y - 9), f"{n} mm", font=font(16), fill=0)
    d.line((0, 0, W - 1, 0), fill=0)
    return img


def marker(label: str) -> Image.Image:
    """Thick line on the FIRST printed rows (bottom in upright view), label beside it."""
    img, d = canvas(20)
    d.rectangle((0, 17, W - 1, 19), fill=0)
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


def short_card() -> Image.Image:
    img, d = canvas(30)
    d.text((0, 4), "30 lines, no padding", font=font(20), fill=0)
    return img


async def print_job(p: RemotePrinter | None, img: Image.Image, mode: Mode, heat: int, out: Path | None,
                    name: str, min_lines: int | None = None, delay: float = 0.025) -> None:
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
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: FAILED {exc}")


async def main(test: str, out: Path | None, delay: float) -> None:
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
    elif test == "sync":
        await print_job(p, sync_card(delay), Mode.GRAY4, 0x5D, out, f"sync_{delay}", delay=delay)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("test")
    ap.add_argument("--dry-run", type=Path)
    ap.add_argument("--delay", type=float, default=0.025, help="seconds between data chunks")
    a = ap.parse_args()
    asyncio.run(main(a.test, a.dry_run, a.delay))
