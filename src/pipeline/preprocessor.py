"""Image preprocessor — minimal 2-field PreprocessResult.

Steps (arch §5.1):
  1. Image.open + load (catches truncated)
  2. EXIF transpose (full 8-case)
  3. Convert RGB
  4. Resize if max dim > MAX_IMAGE_DIMENSION (LANCZOS thumbnail)
  5. phash = imagehash.phash(img)
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError

from src.config import settings
from src.domain.errors import PermanentPipelineError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreprocessResult:
    pil: Image.Image
    phash: str


def preprocess_image(raw: bytes) -> PreprocessResult:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise PermanentPipelineError("truncated_upload", str(e)) from e

    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    if max(img.size) > settings.MAX_IMAGE_DIMENSION:
        img.thumbnail(
            (settings.MAX_IMAGE_DIMENSION, settings.MAX_IMAGE_DIMENSION),
            Image.Resampling.LANCZOS,
        )

    phash = str(imagehash.phash(img))
    return PreprocessResult(pil=img, phash=phash)
