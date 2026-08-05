"""Persistent progress for long, restartable parsing sessions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import OUTPUT_DIR
from .export import ProductRow


CHECKPOINT_PATH = OUTPUT_DIR / "parse_checkpoint.json"


@dataclass
class ParseCheckpoint:
    signature: str
    completed_targets: set[str]
    products: list[ProductRow]


def target_key(target, seller_scope: str = "") -> str:
    scope = str(
        seller_scope
        or getattr(target, "seller_scope", "")
        or ""
    ).strip()
    parts = (
        scope,
        str(getattr(target, "id", "") or ""),
        str(getattr(target, "category_id", "") or ""),
        str(getattr(target, "param_key", "") or ""),
        str(getattr(target, "param_value", "") or ""),
        str(getattr(target, "url", "") or ""),
    )
    return "|".join(parts)


def settings_signature(settings) -> str:
    # Sorted unique keys keep resume stable if the user re-selects the same
    # categories in a different order (common with ~1000-item trees).
    targets = sorted({target_key(target) for target in (settings.categories or [])})
    payload = {
        "seller_url": settings.seller_url,
        "parse_mode": settings.parse_mode,
        "specific_seller": settings.specific_seller,
        "browser_mode": settings.browser_mode,
        "min_price": settings.min_price,
        "max_price": settings.max_price,
        "max_products": settings.max_products,
        "bonus_only": bool(getattr(settings, "bonus_only", True)),
        "targets": targets,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def checkpoint_exists(path: Path | None = None) -> bool:
    return (path or CHECKPOINT_PATH).exists()


def describe_checkpoint(settings, path: Path | None = None) -> str | None:
    checkpoint = load_checkpoint(settings, path)
    if not checkpoint:
        return None
    return (
        f"{len(checkpoint.products)} товаров, "
        f"{len(checkpoint.completed_targets)} категорий завершено"
    )


def load_checkpoint(settings, path: Path | None = None) -> ParseCheckpoint | None:
    checkpoint_path = path or CHECKPOINT_PATH
    if not checkpoint_path.exists():
        return None
    try:
        raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        signature = settings_signature(settings)
        if raw.get("signature") != signature:
            return None
        products = [
            ProductRow(**item)
            for item in raw.get("products", [])
            if isinstance(item, dict)
        ]
        return ParseCheckpoint(
            signature=signature,
            completed_targets={
                str(item) for item in raw.get("completed_targets", []) if item
            },
            products=products,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_checkpoint(
    settings,
    completed_targets: set[str],
    products: list[ProductRow],
    path: Path | None = None,
) -> None:
    checkpoint_path = path or CHECKPOINT_PATH
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "signature": settings_signature(settings),
        "completed_targets": sorted(completed_targets),
        "products": [asdict(product) for product in products],
        "product_count": len(products),
    }
    temporary = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(checkpoint_path)


def clear_checkpoint(path: Path | None = None) -> None:
    checkpoint_path = path or CHECKPOINT_PATH
    try:
        checkpoint_path.unlink(missing_ok=True)
    except OSError:
        pass
