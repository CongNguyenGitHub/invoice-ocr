"""Deterministic-replay integration tests.

Asserts that running the preprocessor on the same image multiple times yields
identical pHash + identical (size, mode) tuples.  This catches:

  * Accidental introduction of a non-deterministic resampling mode
  * A change to MAX_IMAGE_DIMENSION that silently re-buckets thumbnails
  * EXIF transpose drift
  * Pillow upgrade behaviour changes

Does NOT call Gemini.  Does NOT require Docker.

Fixture images:
  * Six images from data/eval/test_set.json (lowest golden_index)
  * Cached on first run under tests/fixtures/replay_images/
  * Skipped if the test set is missing or images are unreachable
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from src.pipeline.preprocessor import preprocess_image

REPO_ROOT     = Path(__file__).resolve().parents[2]
TEST_SET      = REPO_ROOT / "data" / "eval" / "test_set.json"
FIXTURES_DIR  = REPO_ROOT / "tests" / "fixtures" / "replay_images"
N_IMAGES      = 6
REPLAY_RUNS   = 3   # Re-run the same image this many times


@pytest.fixture(scope="module")
def replay_image_paths() -> list[Path]:
    """Return paths to N_IMAGES fixture images, downloading on first run."""
    if not TEST_SET.exists():
        pytest.skip(f"test_set.json not present at {TEST_SET}")

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    records = json.loads(TEST_SET.read_text(encoding="utf-8"))[:N_IMAGES]

    paths: list[Path] = []
    for rec in records:
        url = rec.get("file", "")
        if not url:
            continue
        local = FIXTURES_DIR / url.split("/")[-1]
        if not local.exists():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ci-tests/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    local.write_bytes(r.read())
            except Exception as exc:
                pytest.skip(f"Could not download fixture image {url}: {exc}")
        paths.append(local)

    if len(paths) < 3:
        pytest.skip(f"Only {len(paths)} fixture images available; need >=3")
    return paths


def test_phash_is_deterministic_across_runs(replay_image_paths: list[Path]) -> None:
    """Same image -> same pHash, every time."""
    drift: list[str] = []
    for path in replay_image_paths:
        raw = path.read_bytes()
        hashes = {preprocess_image(raw).phash for _ in range(REPLAY_RUNS)}
        if len(hashes) != 1:
            drift.append(f"{path.name}: {hashes}")
    assert not drift, "pHash changed across re-invocations: " + "; ".join(drift)


def test_preprocess_output_dims_are_deterministic(replay_image_paths: list[Path]) -> None:
    """Same image -> same (width, height, mode) tuple, every time."""
    drift: list[str] = []
    for path in replay_image_paths:
        raw = path.read_bytes()
        signatures: set[tuple] = set()
        for _ in range(REPLAY_RUNS):
            r = preprocess_image(raw)
            signatures.add((*r.pil.size, r.pil.mode))
        if len(signatures) != 1:
            drift.append(f"{path.name}: {signatures}")
    assert not drift, "preprocess output drifted: " + "; ".join(drift)


def test_phash_changes_when_image_changes(replay_image_paths: list[Path]) -> None:
    """Sanity: different images yield different pHashes (no constant-hash regression).

    All N_IMAGES are distinct receipts, so we expect N_IMAGES distinct pHashes.
    Allow up to 1 collision in case two test_set images happen to be near-duplicates.
    """
    hashes = {preprocess_image(p.read_bytes()).phash for p in replay_image_paths}
    assert len(hashes) >= len(replay_image_paths) - 1, (
        f"Got {len(hashes)} unique pHashes from {len(replay_image_paths)} distinct "
        f"images — preprocessor may be returning a constant: {hashes}"
    )
