"""Safe image ingestion: validate on the header first, decode second."""

from __future__ import annotations

import io
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, UnidentifiedImageError

from eye_blink.config import ALLOWED_IMAGE_FORMATS
from eye_blink.errors import InvalidImageError, UnsupportedMediaTypeError


def probe_image(data: bytes, *, max_pixels: int) -> tuple[str, int, int]:
    """Validate an image from its header only. Returns ``(format, width, height)``.

    The pixel budget is enforced here, *before* any decoding, so a tiny "decompression bomb"
    cannot exhaust memory.
    """
    if not data:
        raise InvalidImageError("empty request body")
    try:
        with Image.open(io.BytesIO(data)) as probe:  # header-only, lazy
            fmt = probe.format
            width, height = probe.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("could not identify an image in the request body") from exc
    except Image.DecompressionBombError as exc:  # pragma: no cover - Pillow's own guard
        raise InvalidImageError("image dimensions exceed the safety limit") from exc

    if fmt not in ALLOWED_IMAGE_FORMATS:
        raise UnsupportedMediaTypeError(f"format {fmt!r} is not supported; allowed: {', '.join(ALLOWED_IMAGE_FORMATS)}")
    if width < 1 or height < 1 or width * height > max_pixels:
        raise InvalidImageError(f"image is {width}x{height}; the limit is {max_pixels} pixels")
    return fmt, width, height


def validate_and_decode(data: bytes, *, max_pixels: int) -> NDArray[np.uint8]:
    """Return an HxWx3 uint8 BGR image, or raise a typed error.

    EXIF orientation is applied by OpenCV, so all returned coordinates are relative to the
    upright image.
    """
    probe_image(data, max_pixels=max_pixels)
    decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None or decoded.ndim != 3 or decoded.shape[2] != 3:
        raise InvalidImageError("image data is corrupt or could not be decoded")
    return cast("NDArray[np.uint8]", decoded)
