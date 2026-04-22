"""YOLO detector — single highest-confidence crop, strict no-fallback.

Given a preprocessed PIL image:
  1. Letterbox-resize to 640×640, BGR→RGB already, normalize 0..1, CHW, FP32.
  2. Call Triton via triton_client.infer_yolo (shape (1,3,640,640)).
  3. Decode (5, anchors) output: [cx,cy,w,h,conf]. Filter by
     YOLO_CONFIDENCE_THRESHOLD, pick argmax (invariant I10 — NO NMS, NO
     top-k fallback). If no box clears threshold → PermanentPipelineError.
  4. Reverse letterbox → image coords, expand by YOLO_CROP_PAD_PERCENT, clamp,
     return cropped PIL.
"""
from __future__ import annotations

import logging

import numpy as np
from PIL import Image

from src.config import settings
from src.domain.errors import PermanentPipelineError
from src.pipeline.triton_client import infer_yolo
from src.worker.metrics import triton_batch_size, yolo_rejection_total

logger = logging.getLogger(__name__)

_IMG_SIZE = 640


def _letterbox(img: Image.Image, size: int = _IMG_SIZE) -> tuple[np.ndarray, float, int, int]:
    """Resize preserving aspect ratio, pad to (size, size) with 114 gray.
    Returns (chw_float32, scale, pad_x, pad_y)."""
    w, h = img.size
    scale = min(size / w, size / h)
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    pad_x = (size - new_w) // 2
    pad_y = (size - new_h) // 2
    canvas.paste(resized, (pad_x, pad_y))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0  # HWC
    chw = np.transpose(arr, (2, 0, 1))  # CHW
    return chw, scale, pad_x, pad_y


async def detect_invoice(img: Image.Image) -> Image.Image:
    chw, scale, pad_x, pad_y = _letterbox(img)
    batch = chw[np.newaxis, ...].astype(np.float32)  # (1,3,640,640)
    triton_batch_size.observe(1)

    out = await infer_yolo(batch)  # (1, 5, anchors)
    if out.ndim != 3 or out.shape[1] != 5:
        raise PermanentPipelineError(
            "yolo_bad_output_shape", f"unexpected YOLO output shape: {out.shape}"
        )
    preds = out[0]  # (5, anchors)
    conf = preds[4]
    best = int(np.argmax(conf))
    if conf[best] < settings.YOLO_CONFIDENCE_THRESHOLD:
        yolo_rejection_total.inc()
        raise PermanentPipelineError(
            "no_invoice_detected",
            f"best conf {conf[best]:.3f} < {settings.YOLO_CONFIDENCE_THRESHOLD}",
        )

    cx, cy, bw, bh = (float(preds[i, best]) for i in range(4))
    # Reverse letterbox
    x1_lb, y1_lb = cx - bw / 2, cy - bh / 2
    x2_lb, y2_lb = cx + bw / 2, cy + bh / 2
    x1 = (x1_lb - pad_x) / scale
    y1 = (y1_lb - pad_y) / scale
    x2 = (x2_lb - pad_x) / scale
    y2 = (y2_lb - pad_y) / scale

    w, h = img.size
    pad_w = (x2 - x1) * settings.YOLO_CROP_PAD_PERCENT
    pad_h = (y2 - y1) * settings.YOLO_CROP_PAD_PERCENT
    x1 = max(0, int(round(x1 - pad_w)))
    y1 = max(0, int(round(y1 - pad_h)))
    x2 = min(w, int(round(x2 + pad_w)))
    y2 = min(h, int(round(y2 + pad_h)))
    if x2 <= x1 or y2 <= y1:
        raise PermanentPipelineError("yolo_degenerate_box", f"box=({x1},{y1},{x2},{y2})")
    return img.crop((x1, y1, x2, y2))
