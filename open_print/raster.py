"""Turn images and text into MXW01 bitmap rows."""
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .protocol import MIN_LINES, PRINTER_WIDTH, Mode, bytes_per_line

DEFAULT_FONT = "/System/Library/Fonts/Menlo.ttc"


def to_gray(img: Image.Image) -> Image.Image:
    """Flatten transparency onto white, convert to 8-bit gray, scale to printer width."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img)
    img = img.convert("L")
    if img.width != PRINTER_WIDTH:
        height = round(img.height * PRINTER_WIDTH / img.width)
        img = img.resize((PRINTER_WIDTH, height), Image.LANCZOS)
    return img


def prepare(img: Image.Image, mode: Mode = Mode.MONO, dither: bool = True) -> Image.Image:
    """Return a printer-ready image: mode '1' for MONO, mode 'L' for GRAY4."""
    gray = to_gray(img)
    if mode == Mode.GRAY4:
        return gray
    return gray.convert("1", dither=Image.FLOYDSTEINBERG if dither else Image.NONE)


def to_rows(img: Image.Image, mode: Mode = Mode.MONO, min_lines: int = MIN_LINES) -> bytes:
    """Pack a 384-wide image into printer rows, padded with blank rows to min_lines."""
    assert img.width == PRINTER_WIDTH, img.width
    out = bytearray()
    if mode == Mode.GRAY4:
        px = img.convert("L").load()
        for y in range(img.height):
            for x in range(0, PRINTER_WIDTH, 2):
                left = (255 - px[x, y]) >> 4
                right = (255 - px[x + 1, y]) >> 4
                out.append((left << 4) | right)
    else:
        if img.mode != "1":
            img = img.convert("1", dither=Image.NONE)  # plain threshold; dither in prepare() if wanted
        px = img.load()
        for y in range(img.height):
            row = bytearray(PRINTER_WIDTH // 8)
            for x in range(PRINTER_WIDTH):
                if px[x, y] == 0:  # black
                    row[x >> 3] |= 1 << (x & 7)
            out += row
    missing = min_lines - img.height
    if missing > 0:
        out += bytes(bytes_per_line(mode) * missing)
    return bytes(out)


def render_text(text: str, size: int = 28, font_path: str = DEFAULT_FONT) -> Image.Image:
    """Render black text on white (mode 'L'), left-aligned, word-wrapped to the paper width."""
    font = ImageFont.truetype(font_path, size)
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}" if line else word
            if measure.textlength(trial, font=font) <= PRINTER_WIDTH:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = word
        lines.append(line)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + 4
    img = Image.new("L", (PRINTER_WIDTH, line_h * len(lines) + 8), 255)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((0, 4 + i * line_h), line, font=font, fill=0)
    return img
