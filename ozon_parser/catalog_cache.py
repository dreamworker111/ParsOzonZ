"""Disk cache for the full Ozon global category tree.

Avoids re-hammering Composer after a successful load (main fab_ trigger).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import GLOBAL_CATALOG_CACHE_TTL_SEC, SESSION_DIR
from .filters import FilterOptionNode

CACHE_PATH = SESSION_DIR / "global_catalog_cache.json"
CACHE_VERSION = 1


def _node_to_dict(node: FilterOptionNode) -> dict:
    return {
        "id": node.id,
        "name": node.name,
        "url": node.url,
        "param_key": node.param_key,
        "param_value": node.param_value,
        "category_id": node.category_id,
        "category_name": node.category_name,
        "parent_name": node.parent_name,
        "children": [_node_to_dict(child) for child in node.children],
    }


def _node_from_dict(data: dict) -> FilterOptionNode:
    return FilterOptionNode(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        url=data.get("url"),
        param_key=str(data.get("param_key") or ""),
        param_value=str(data.get("param_value") or ""),
        category_id=str(data.get("category_id") or ""),
        category_name=str(data.get("category_name") or ""),
        parent_name=str(data.get("parent_name") or ""),
        children=[
            _node_from_dict(child)
            for child in (data.get("children") or [])
            if isinstance(child, dict)
        ],
    )


def save_global_catalog(nodes: list[FilterOptionNode], path: Path | None = None) -> Path:
    target = path or CACHE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "saved_at": time.time(),
        "roots": [_node_to_dict(node) for node in nodes],
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def load_global_catalog(
    path: Path | None = None,
    *,
    max_age_sec: float | None = None,
) -> list[FilterOptionNode] | None:
    target = path or CACHE_PATH
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(payload.get("version") or 0) != CACHE_VERSION:
        return None
    saved_at = float(payload.get("saved_at") or 0)
    ttl = GLOBAL_CATALOG_CACHE_TTL_SEC if max_age_sec is None else max_age_sec
    if saved_at <= 0 or (time.time() - saved_at) > ttl:
        return None
    roots = payload.get("roots") or []
    if not isinstance(roots, list) or not roots:
        return None
    nodes = [_node_from_dict(item) for item in roots if isinstance(item, dict)]
    return nodes or None


def cache_age_hours(path: Path | None = None) -> float | None:
    target = path or CACHE_PATH
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        saved_at = float(payload.get("saved_at") or 0)
    except Exception:
        return None
    if saved_at <= 0:
        return None
    return max(0.0, (time.time() - saved_at) / 3600.0)
