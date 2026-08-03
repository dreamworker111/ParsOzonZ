"""Unit tests for global catalog disk cache."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ozon_parser.catalog_cache import (
    cache_age_hours,
    load_global_catalog,
    save_global_catalog,
)
from ozon_parser.filters import FilterOptionNode


class CatalogCacheTests(unittest.TestCase):
    def test_roundtrip_and_ttl(self) -> None:
        tree = [
            FilterOptionNode(
                id="1",
                name="Электроника",
                url="https://www.ozon.ru/category/1/",
                param_key="category",
                param_value="1",
                category_id="1",
                category_name="Электроника",
                children=[
                    FilterOptionNode(
                        id="2",
                        name="Телефоны",
                        param_key="category",
                        param_value="2",
                        category_id="2",
                        category_name="Телефоны",
                        parent_name="Электроника",
                    )
                ],
            )
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            save_global_catalog(tree, path)
            loaded = load_global_catalog(path)
            assert loaded is not None
            self.assertEqual(loaded[0].name, "Электроника")
            self.assertEqual(loaded[0].children[0].name, "Телефоны")
            self.assertIsNotNone(cache_age_hours(path))

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["saved_at"] = time.time() - 10_000
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(load_global_catalog(path, max_age_sec=60))


if __name__ == "__main__":
    unittest.main()
