# -*- coding: utf-8 -*-
"""
icon_utils.py
-------------
Loads the system tray icon.

IMPORTANT: this is NOT the same as PyInstaller's --icon=icon.ico flag!
--icon=icon.ico embeds the icon into the .exe file itself (what you see in
Windows Explorer / on the taskbar). This function instead loads a raster
image AT RUNTIME, which piktray displays in the system tray - a separate,
completely independent mechanism.

Search order:
  1. icon.ico next to the .exe/main.py - can be swapped without rebuilding.
  2. icon.ico bundled into the onefile build via `--add-data "icon.ico;."`
     (available in the temporary folder sys._MEIPASS while the .exe runs).
  3. A fallback icon generated on the fly with Pillow -
     the app is GUARANTEED not to crash because of a missing file.
"""
import logging
import os
import sys

from PIL import Image, ImageDraw

from paths import get_base_dir

logger = logging.getLogger(__name__)

ICON_FILENAME = "icon.ico"


def load_icon_image() -> Image.Image:
    candidates = [os.path.join(get_base_dir(), ICON_FILENAME)]

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, ICON_FILENAME))  # type: ignore[attr-defined]

    for path in candidates:
        try:
            return Image.open(path).convert("RGBA")
        except (FileNotFoundError, OSError):
            continue

    logger.info("File %s not found next to the app - using the built-in fallback icon.",
                ICON_FILENAME)
    return _generate_fallback_icon()


def _generate_fallback_icon(size: int = 64) -> Image.Image:
    """Draws a simple "play button in a circle" icon with no dependency on external files."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(64, 118, 245, 255))
    draw.polygon(
        [
            (size * 0.38, size * 0.28),
            (size * 0.38, size * 0.72),
            (size * 0.75, size * 0.50),
        ],
        fill=(255, 255, 255, 255),
    )
    return img