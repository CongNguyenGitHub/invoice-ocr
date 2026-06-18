"""Async HTTP client for downloading images from external CDNs.

Used by the worker to fetch invoice images from pre-existing CDN URLs
(e.g. img-campaign.gotit.vn) instead of MinIO/S3.
"""

from __future__ import annotations

import logging

import httpx

from src.config import settings
from src.domain.errors import PermanentPipelineError, StorageTransientError

logger = logging.getLogger(__name__)


class ImageTooLargeError(PermanentPipelineError):
    """Image exceeds the configured size limit."""

    def __init__(self, url: str, size: int) -> None:
        super().__init__(
            error_code="image_too_large",
            message=f"Image at {url} is {size} bytes, limit is {settings.IMAGE_DOWNLOAD_MAX_BYTES}",
        )


class ImageDownloadError(PermanentPipelineError):
    """HTTP error downloading image (4xx)."""

    def __init__(self, url: str, status_code: int) -> None:
        super().__init__(
            error_code="image_download_failed",
            message=f"HTTP {status_code} downloading {url}",
        )


async def download_image(url: str) -> bytes:
    """Download image from external URL with size limit and timeout.

    Raises:
        PermanentPipelineError: on 4xx or image too large.
        StorageTransientError: on network/5xx errors.
    """
    timeout = httpx.Timeout(settings.IMAGE_DOWNLOAD_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)

            if resp.status_code >= 500:
                raise StorageTransientError(f"CDN returned {resp.status_code} for {url}")

            if resp.status_code >= 400:
                raise ImageDownloadError(url, resp.status_code)

            resp.raise_for_status()

            content = resp.content
            if len(content) > settings.IMAGE_DOWNLOAD_MAX_BYTES:
                raise ImageTooLargeError(url, len(content))

            return content

    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
        raise StorageTransientError(f"CDN download failed for {url}: {e}") from e
    except httpx.HTTPError as e:
        # Catch-all for other httpx errors not already handled
        if isinstance(e, httpx.HTTPStatusError):
            raise  # Already handled above
        raise StorageTransientError(f"CDN download error for {url}: {e}") from e
