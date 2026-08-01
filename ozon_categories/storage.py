"""Persist category tree to JSON, SQLite and CSV."""

from __future__ import annotations

import csv
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import orjson

from .cache import flatten_tree
from .config import DEFAULT_OUTPUT_DIR
from .models import CategoryNode

logger = logging.getLogger(__name__)


class CategoryStorage:
    """Export category trees to multiple formats."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self.output_dir = output_dir or DEFAULT_OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save_json(self, roots: list[CategoryNode], filename: str = "categories.json") -> Path:
        path = self.output_dir / filename
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "categories_total": len(flatten_tree(roots)),
            "roots": [root.to_dict() for root in roots],
        }
        path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        logger.info("Saved JSON: %s (%s nodes)", path, payload["categories_total"])
        return path

    def save_sqlite(self, roots: list[CategoryNode], filename: str = "categories.sqlite") -> Path:
        path = self.output_dir / filename
        if path.exists():
            path.unlink()

        conn = sqlite3.connect(path)
        rows: list[tuple] = []
        try:
            conn.execute(
                """
                CREATE TABLE categories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT,
                    parent_id TEXT,
                    level INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX idx_categories_parent ON categories(parent_id)")
            conn.execute("CREATE INDEX idx_categories_level ON categories(level)")

            updated_at = datetime.now(timezone.utc).isoformat()
            rows = [
                (n.id, n.name, n.url, n.parent_id, n.level, n.path, updated_at)
                for n in flatten_tree(roots)
            ]
            conn.executemany(
                "INSERT INTO categories (id, name, url, parent_id, level, path, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

        logger.info("Saved SQLite: %s (%s rows)", path, len(rows))
        return path

    def save_csv(self, roots: list[CategoryNode], filename: str = "categories.csv") -> Path:
        path = self.output_dir / filename
        flat = flatten_tree(roots)
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["id", "name", "url", "parent_id", "level", "path"],
            )
            writer.writeheader()
            for node in flat:
                writer.writerow(
                    {
                        "id": node.id,
                        "name": node.name,
                        "url": node.url or "",
                        "parent_id": node.parent_id or "",
                        "level": node.level,
                        "path": node.path,
                    }
                )
        logger.info("Saved CSV: %s (%s rows)", path, len(flat))
        return path

    @staticmethod
    def load_json(path: Path) -> list[CategoryNode]:
        raw = orjson.loads(path.read_bytes())
        return [CategoryNode.from_dict(item) for item in raw.get("roots", raw)]
