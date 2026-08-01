"""Cache with structure fingerprint and change detection."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

from .config import CACHE_FILENAME, DEFAULT_CACHE_DIR, FINGERPRINT_FILENAME
from .models import CachePayload, CategoryNode, StructureChange

logger = logging.getLogger(__name__)


def flatten_tree(roots: list[CategoryNode]) -> list[CategoryNode]:
    flat: list[CategoryNode] = []

    def walk(node: CategoryNode) -> None:
        flat.append(node)
        for child in node.children:
            walk(child)

    for root in roots:
        walk(root)
    return flat


def compute_fingerprint(roots: list[CategoryNode]) -> str:
    """Stable hash of tree structure (id, parent, name, url, level)."""
    flat = flatten_tree(roots)
    payload = sorted(
        (n.id, n.parent_id or "", n.name, n.url or "", n.level) for n in flat
    )
    digest = hashlib.sha256(orjson.dumps(payload)).hexdigest()
    return digest


def detect_structure_changes(
    old_roots: list[CategoryNode],
    new_roots: list[CategoryNode],
) -> list[StructureChange]:
    old_map = {n.id: n for n in flatten_tree(old_roots)}
    new_map = {n.id: n for n in flatten_tree(new_roots)}
    changes: list[StructureChange] = []

    for cid, node in new_map.items():
        if cid not in old_map:
            changes.append(
                StructureChange("added", cid, f"Новая категория: {node.path or node.name}")
            )
            continue
        prev = old_map[cid]
        if prev.name != node.name:
            changes.append(
                StructureChange("renamed", cid, f"Переименована: {prev.name!r} → {node.name!r}")
            )
        if (prev.url or "") != (node.url or ""):
            changes.append(
                StructureChange("url_changed", cid, f"URL: {prev.url!r} → {node.url!r}")
            )
        if (prev.parent_id or "") != (node.parent_id or ""):
            changes.append(
                StructureChange(
                    "parent_changed",
                    cid,
                    f"Родитель: {prev.parent_id!r} → {node.parent_id!r}",
                )
            )
        if prev.level != node.level:
            changes.append(
                StructureChange("level_changed", cid, f"Уровень: {prev.level} → {node.level}")
            )

    for cid, node in old_map.items():
        if cid not in new_map:
            changes.append(
                StructureChange("removed", cid, f"Удалена категория: {node.path or node.name}")
            )

    return changes


class CategoryCache:
    """File-based cache for category trees."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / CACHE_FILENAME
        self.fingerprint_file = self.cache_dir / FINGERPRINT_FILENAME

    def load(self, source_url: str) -> list[CategoryNode] | None:
        if not self.cache_file.exists():
            return None
        try:
            raw = orjson.loads(self.cache_file.read_bytes())
            payload = CachePayload.from_dict(raw)
            if payload.source_url != source_url:
                logger.info("Cache source_url mismatch — ignore cache")
                return None
            return [CategoryNode.from_dict(item) for item in payload.roots]
        except Exception as exc:
            logger.warning("Failed to read cache: %s", exc)
            return None

    def save(self, source_url: str, roots: list[CategoryNode]) -> str:
        fingerprint = compute_fingerprint(roots)
        payload = CachePayload(
            fingerprint=fingerprint,
            updated_at=datetime.now(timezone.utc).isoformat(),
            source_url=source_url,
            roots=[root.to_dict() for root in roots],
        )
        self.cache_file.write_bytes(orjson.dumps(payload.to_dict(), option=orjson.OPT_INDENT_2))
        self.fingerprint_file.write_text(fingerprint, encoding="utf-8")
        logger.info("Cache saved: %s categories, fingerprint=%s", len(flatten_tree(roots)), fingerprint[:12])
        return fingerprint

    def read_fingerprint(self) -> str | None:
        if self.fingerprint_file.exists():
            return self.fingerprint_file.read_text(encoding="utf-8").strip()
        if self.cache_file.exists():
            try:
                raw = orjson.loads(self.cache_file.read_bytes())
                return str(raw.get("fingerprint", "")) or None
            except Exception:
                return None
        return None

    def is_valid_for(self, source_url: str, roots: list[CategoryNode]) -> bool:
        cached = self.load(source_url)
        if not cached:
            return False
        return compute_fingerprint(cached) == compute_fingerprint(roots)
