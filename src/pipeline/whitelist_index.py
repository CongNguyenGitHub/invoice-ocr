"""Whitelist index with rapidfuzz bucket prefilter + atomic hot reload.

Per-worker-process singleton. Two sub-indexes (`store`, `product`) sharing the
same internal layout. Bucket key is `(lower[:3], len(lower)//4)` with ±1
length-bucket drift tolerance (arch §9.3).
"""
from __future__ import annotations

import json
import logging
import threading
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz, process

from src.worker.metrics import whitelist_match_total

logger = logging.getLogger(__name__)

_KIND_FILE = {
    "store": "store_names_whitelist.json",
    "product": "product_names_whitelist.json",
}
_KIND_CUTOFFS = {
    # (primary, fallback). fallback=None means "no full-scan fallback".
    "store": (80, 60),
    "product": (70, None),
}


def _normalize(raw: str) -> str:
    return unicodedata.normalize("NFC", raw).strip()


class WhitelistIndex:
    def __init__(self) -> None:
        self._buckets: dict[str, dict[tuple[str, int], list[tuple[str, str]]]] = {}
        self._all_lower: dict[str, list[str]] = {}
        self._canonical_of: dict[str, dict[str, str]] = {}
        self.last_mtime: dict[str, float] = {}
        self.source_path: dict[str, Path] = {}
        self._lock = threading.Lock()

    @classmethod
    def build(cls, whitelist_dir: str) -> "WhitelistIndex":
        idx = cls()
        for kind, fname in _KIND_FILE.items():
            path = Path(whitelist_dir) / fname
            if path.exists():
                idx.reload(kind, path)
            else:
                logger.warning("whitelist_missing", extra={"kind": kind, "path": str(path)})
                idx._install_empty(kind, path)
        return idx

    def reload(self, kind: str, path: Path) -> None:
        names = self._load_one(path)
        new_buckets, new_all_lower, new_canonical = self._build_index(names)
        with self._lock:
            self._buckets[kind] = new_buckets
            self._all_lower[kind] = new_all_lower
            self._canonical_of[kind] = new_canonical
            self.last_mtime[kind] = path.stat().st_mtime if path.exists() else 0.0
            self.source_path[kind] = path

    def _install_empty(self, kind: str, path: Path) -> None:
        with self._lock:
            self._buckets[kind] = {}
            self._all_lower[kind] = []
            self._canonical_of[kind] = {}
            self.last_mtime[kind] = 0.0
            self.source_path[kind] = path

    @staticmethod
    def _load_one(path: Path) -> list[str]:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{path} must be a JSON array of strings")
        return [str(x) for x in data]

    @staticmethod
    def _build_index(names: list[str]):
        buckets: dict[tuple[str, int], list[tuple[str, str]]] = {}
        canonical_of: dict[str, str] = {}
        all_lower: list[str] = []
        for canonical in names:
            lower = canonical.lower()
            if not lower:
                continue
            key = (lower[:3], len(lower) // 4)
            buckets.setdefault(key, []).append((lower, canonical))
            canonical_of[lower] = canonical
            all_lower.append(lower)
        return buckets, all_lower, canonical_of

    # ---- public match API ----
    def match_store(self, raw: str) -> str:
        return self._match("store", raw)

    def match_product(self, raw: str) -> str:
        return self._match("product", raw)

    def _match(self, kind: str, raw: str) -> str:
        if not raw:
            return raw
        normalized = _normalize(raw)
        lower = normalized.lower()
        if not lower:
            return normalized

        primary, fallback = _KIND_CUTOFFS[kind]
        buckets = self._buckets.get(kind, {})
        canonical_of = self._canonical_of.get(kind, {})

        k0, k1 = lower[:3], len(lower) // 4
        candidates: list[tuple[str, str]] = []
        for delta in (-1, 0, 1):
            candidates.extend(buckets.get((k0, k1 + delta), []))

        if not candidates:
            if fallback is None:
                whitelist_match_total.labels(field=kind, tier="miss").inc()
                return normalized
            all_lower = self._all_lower.get(kind, [])
            if not all_lower:
                whitelist_match_total.labels(field=kind, tier="miss").inc()
                return normalized
            best = process.extractOne(lower, all_lower, scorer=fuzz.WRatio)
            if best and best[1] >= fallback:
                whitelist_match_total.labels(field=kind, tier="fuzzy_low").inc()
                return canonical_of[best[0]]
            whitelist_match_total.labels(field=kind, tier="miss").inc()
            return normalized

        best = process.extractOne(
            lower, [c[0] for c in candidates], scorer=fuzz.WRatio
        )
        if best and best[1] >= primary:
            tier = "exact" if best[1] == 100 else "fuzzy_high"
            whitelist_match_total.labels(field=kind, tier=tier).inc()
            return canonical_of[best[0]]
        whitelist_match_total.labels(field=kind, tier="miss").inc()
        return normalized
