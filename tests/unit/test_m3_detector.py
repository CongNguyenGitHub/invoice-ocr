"""M3 — detector unit tests (Triton mocked)."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image


@pytest.mark.asyncio
async def test_detector_crops_highest_confidence_box(monkeypatch) -> None:
    from src.pipeline import detector

    async def fake_infer(batch: np.ndarray) -> np.ndarray:
        # 3 anchors, only middle one above threshold
        out = np.zeros((1, 5, 3), dtype=np.float32)
        out[0, :, 1] = [320, 320, 200, 200, 0.95]
        out[0, :, 0] = [10, 10, 20, 20, 0.05]
        out[0, :, 2] = [500, 500, 50, 50, 0.10]
        return out

    monkeypatch.setattr(detector, "infer_yolo", fake_infer)

    img = Image.new("RGB", (1000, 1000), (255, 255, 255))
    crop = await detector.detect_invoice(img)
    # Letterbox scale 640/1000 = 0.64 → 200px box in net-space → ~312px image-space + 2% pad
    w, h = crop.size
    assert 290 < w < 340
    assert 290 < h < 340


@pytest.mark.asyncio
async def test_detector_rejects_low_confidence(monkeypatch) -> None:
    from src.domain.errors import PermanentPipelineError
    from src.pipeline import detector

    async def fake_infer(batch: np.ndarray) -> np.ndarray:
        out = np.zeros((1, 5, 1), dtype=np.float32)
        out[0, :, 0] = [320, 320, 100, 100, 0.10]  # below 0.35
        return out

    monkeypatch.setattr(detector, "infer_yolo", fake_infer)

    img = Image.new("RGB", (640, 640), (255, 255, 255))
    with pytest.raises(PermanentPipelineError) as exc:
        await detector.detect_invoice(img)
    assert exc.value.error_code == "no_invoice_detected"
