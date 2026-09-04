"""Gera o icone multi-resolucao usado pelo executavel e instalador."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = PROJECT_ROOT / "assets" / "gsus-auditoria.ico"


def main() -> None:
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((10, 10, 246, 246), radius=56, fill="#ffffff")
    block = 76
    gap = 20
    start = 42
    radius = 18
    positions = (
        (start, start, "#111315"),
        (start + block + gap, start, "#111315"),
        (start, start + block + gap, "#111315"),
        (start + block + gap, start + block + gap, "#ff4f0a"),
    )
    for x, y, color in positions:
        draw.rounded_rectangle((x, y, x + block, y + block), radius=radius, fill=color)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(OUTPUT)


if __name__ == "__main__":
    main()
