"""open-print: print to an MXW01 thermal printer over BLE."""
import argparse
import asyncio

from PIL import Image

from . import raster
from .printer import DEFAULT_INTENSITY, Printer, PrinterError
from .protocol import Mode
from .service import PrinterService, RemotePrinter

MODES = {"mono": Mode.MONO, "gray": Mode.GRAY4}
MAX_RETRACT_MM = 30  # the head-to-tear-bar distance is ~13.5 mm; more than this risks pulling paper out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="open-print", description=__doc__)
    ap.add_argument("--address", help="BLE address (default: auto-discover)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log raw packets")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show battery, temperature, firmware, errors")
    sub.add_parser("serve", help="hold the printer connection open (keeps it awake); other commands use it")

    sub.add_parser("cancel", help="stop the current print")
    feed = sub.add_parser("feed", help="advance paper")
    feed.add_argument("mm", type=int, nargs="?", default=10)
    retract = sub.add_parser("retract", help="pull paper back (max %d mm, to avoid unthreading)" % MAX_RETRACT_MM)
    retract.add_argument("mm", type=int, nargs="?", default=5)

    img = sub.add_parser("image", help="print an image file")
    img.add_argument("path")
    img.add_argument("--no-dither", action="store_true", help="threshold instead of dithering (mono only)")

    txt = sub.add_parser("text", help="print text")
    txt.add_argument("text")
    txt.add_argument("--size", type=int, default=raster.DEFAULT_TEXT_SIZE)
    txt.add_argument("--font", default=raster.DEFAULT_FONT)

    for sp in (img, txt):
        sp.add_argument("--mode", choices=MODES, default="mono", help="mono = 1-bit, gray = 4-bit grayscale")
        sp.add_argument("--intensity", type=lambda s: int(s, 0), default=DEFAULT_INTENSITY, help="0-255 heat")
        sp.add_argument("--preview", help="save the rendered bitmap here instead of printing")
        sp.add_argument(
            "--no-rotate",
            action="store_true",
            help="print top-first (reads correctly as paper feeds out, upside-down on an upright printer)",
        )
    return ap


async def run(args) -> None:
    bitmap = None
    if args.cmd in ("image", "text"):
        mode = MODES[args.mode]
        if args.cmd == "image":
            bitmap = raster.prepare(Image.open(args.path), mode, dither=not args.no_dither)
        else:
            bitmap = raster.render_text(args.text, size=args.size, font_path=args.font)
        if not args.no_rotate:
            # Rows feed out top-first, so rotate to read correctly with the printer sitting upright.
            bitmap = bitmap.rotate(180)
        if args.preview:
            bitmap.save(args.preview)
            print(f"Saved preview to {args.preview}")
            return

    if args.cmd == "serve":
        await PrinterService(args.address, verbose=args.verbose).run()
        return

    try:
        # Prefer a running `open-print serve`; it already holds the connection.
        await execute(args, RemotePrinter(), bitmap)
        return
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    async with Printer(args.address, verbose=args.verbose) as printer:
        await execute(args, printer, bitmap)


async def execute(args, printer, bitmap) -> None:
    if args.cmd == "status":
        st, version, print_type = await printer.info()
        print(f"firmware:    {version}")
        print(f"print type:  0x{print_type:02x}")
        print(f"state:       {st.state_name}")
        print(f"battery:     {st.battery}%")
        print(f"temperature: {st.temperature}°C")
        print(f"error:       {st.error_name}")
        print(f"sensor:      0x{st.sensor:04x}")
        print(f"raw:         {st.raw.hex(' ')}")
    elif args.cmd == "cancel":
        await printer.cancel()
    elif args.cmd == "feed":
        await printer.feed(args.mm)
    elif args.cmd == "retract":
        if args.mm > MAX_RETRACT_MM:
            raise PrinterError(f"refusing to retract more than {MAX_RETRACT_MM} mm")
        await printer.retract(args.mm)
    else:
        mode = MODES[args.mode]
        secs = await printer.print_rows(raster.to_rows(bitmap, mode), mode=mode, intensity=args.intensity)
        print(f"Printed {bitmap.height} lines in {secs:.1f}s.")


def main() -> None:
    try:
        asyncio.run(run(build_parser().parse_args()))
    except (PrinterError, RuntimeError, TimeoutError) as exc:
        raise SystemExit(f"error: {exc}") from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
