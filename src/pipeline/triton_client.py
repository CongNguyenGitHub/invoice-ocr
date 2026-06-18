"""Triton gRPC client — per-process singleton with thread-safe lazy init.

Triton owns YOLO concurrency (invariant I14): we never serialize calls here;
many concurrent `infer()` callers feed the dynamic batcher on the server side.
"""
from __future__ import annotations

import logging
import threading

import numpy as np
import tritonclient.grpc.aio as triton_grpc  # type: ignore[import-untyped]

from src.config import settings
from src.domain.errors import TritonUnavailableError

logger = logging.getLogger(__name__)

_client: triton_grpc.InferenceServerClient | None = None
_lock = threading.Lock()


def get_triton() -> triton_grpc.InferenceServerClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = triton_grpc.InferenceServerClient(url=settings.TRITON_HOST)
    return _client


async def infer_yolo(batch: np.ndarray) -> np.ndarray:
    """Run YOLO inference on a batched (N, 3, 640, 640) FP32 tensor."""
    client = get_triton()
    inputs = [triton_grpc.InferInput("images", list(batch.shape), "FP32")]
    inputs[0].set_data_from_numpy(batch)
    outputs = [triton_grpc.InferRequestedOutput("output0")]
    try:
        resp = await client.infer(
            model_name=settings.YOLO_MODEL_NAME,
            inputs=inputs,
            outputs=outputs,
        )
    except Exception as e:  # noqa: BLE001 — gRPC raises broad InferenceServerException
        raise TritonUnavailableError(f"triton_infer_failed: {e}") from e
    return resp.as_numpy("output0")


async def is_ready() -> bool:
    try:
        client = get_triton()
        return bool(await client.is_model_ready(settings.YOLO_MODEL_NAME))
    except Exception:  # noqa: BLE001
        return False
