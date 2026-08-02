"""
Ozon category collector.

Architecture choice (stable source)
----------------------------------
Primary source: **Ozon Composer API** (`/api/composer-api.bx/page/json/v2`).
This is the same JSON endpoint used by the official Ozon web client (header
`x-o3-app-name: dweb_client`). It returns `widgetStates` with category filter
widgets — structured, includes IDs/names/URLs and nested children when present.

Why not HTML-only?
- CSS class names (e.g. wb6_7) are hashed and change between deployments.
- Composer JSON is stable at the schema level and survives UI reskins.

Fallback chain per page:
1. Composer API JSON
2. HTML block wb6_7 (BeautifulSoup + lxml)
3. Embedded `<script type="application/json">` blocks

Recursive strategy:
- Load seller/catalog entry page → root categories
- For each category, request its catalog URL (`?category=<id>`) via Composer
- Extract direct children, enqueue, repeat (unlimited depth, cycle-safe)

Integration:
- Works standalone via httpx (+ optional cookies)
- Optional Playwright `Page` for CDP/browser session (recommended for anti-bot)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .cache import CategoryCache, compute_fingerprint, detect_structure_changes, flatten_tree
from .config import (
    COMPOSER_API_PATH,
    COMPOSER_APP_NAME,
    DEFAULT_CACHE_DIR,
    DEFAULT_OUTPUT_DIR,
    DESKTOP_BASE_URL,
    MAX_RETRIES,
    REQUEST_TIMEOUT_SEC,
)
from .models import CategoryNode, LoadStats
from .parser import extract_direct_children, extract_json_blocks, parse_composer_response, parse_html_category_block, parse_page_all_sources
from .storage import CategoryStorage
from .utils import (
    RateLimiter,
    UserAgentRotator,
    absolute_ozon_url,
    build_category_path,
    build_category_url,
    is_valid_category,
    normalize_source_url,
    retry_with_backoff,
    seller_base_path,
)

logger = logging.getLogger(__name__)


class PageFetcher(Protocol):
    """Fetch Composer JSON and optional HTML for a catalog path."""

    def fetch(self, path: str) -> tuple[dict[str, Any] | None, str | None]: ...


class HttpxPageFetcher:
    """HTTP fetcher with User-Agent rotation, rate limit and retries."""

    def __init__(
        self,
        *,
        cookies: list[dict[str, Any]] | None = None,
        rate_limiter: RateLimiter | None = None,
        ua_rotator: UserAgentRotator | None = None,
        stats: LoadStats | None = None,
    ) -> None:
        self._cookies = cookies or []
        self._rate = rate_limiter or RateLimiter()
        self._ua = ua_rotator or UserAgentRotator()
        self._stats = stats
        cookie_jar = {
            c["name"]: c["value"]
            for c in self._cookies
            if isinstance(c, dict) and "name" in c and "value" in c
        }
        self._client = httpx.Client(
            base_url=DESKTOP_BASE_URL,
            timeout=REQUEST_TIMEOUT_SEC,
            follow_redirects=True,
            headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            cookies=cookie_jar,
        )

    def close(self) -> None:
        self._client.close()

    @retry_with_backoff(max_retries=MAX_RETRIES)
    def fetch(self, path: str) -> tuple[dict[str, Any] | None, str | None]:
        self._rate.wait()
        if self._stats:
            self._stats.requests_count += 1

        headers = {
            "Accept": "application/json, text/html",
            "x-o3-app-name": COMPOSER_APP_NAME,
            "User-Agent": self._ua.next(),
        }
        composer_url = f"{COMPOSER_API_PATH}?url={quote(path, safe='')}"
        composer_data: dict[str, Any] | None = None
        html: str | None = None

        try:
            resp = self._client.get(composer_url, headers=headers)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("application/json"):
                composer_data = resp.json()
            else:
                logger.debug("Composer non-JSON status=%s path=%s", resp.status_code, path)
        except Exception as exc:
            logger.warning("Composer request failed for %s: %s", path, exc)
            if self._stats:
                self._stats.errors_count += 1

        if not composer_data or not parse_composer_response(composer_data):
            self._rate.wait()
            if self._stats:
                self._stats.requests_count += 1
            try:
                resp = self._client.get(
                    f"{DESKTOP_BASE_URL}{path}" if path.startswith("/") else path,
                    headers={**headers, "Accept": "text/html"},
                )
                if resp.status_code == 200:
                    html = resp.text
            except Exception as exc:
                logger.warning("HTML fallback failed for %s: %s", path, exc)
                if self._stats:
                    self._stats.errors_count += 1

        return composer_data, html


class PlaywrightPageFetcher:
    """Use live browser session (CDP) — best reliability against Ozon anti-bot."""

    def __init__(self, page: Any, stats: LoadStats | None = None) -> None:
        self._page = page
        self._stats = stats

    def fetch(self, path: str) -> tuple[dict[str, Any] | None, str | None]:
        if self._stats:
            self._stats.requests_count += 1
        script = """
        async (path) => {
            const candidates = [
                '/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(path),
            ];
            for (const apiUrl of candidates) {
                try {
                    const resp = await fetch(apiUrl, {
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json',
                            'x-o3-app-name': 'dweb_client',
                        },
                    });
                    if (!resp.ok) continue;
                    return JSON.stringify(await resp.json());
                } catch (e) {}
            }
            return null;
        }
        """
        composer_data: dict[str, Any] | None = None
        try:
            raw = self._page.evaluate(script, path)
            composer_data = json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("Playwright composer failed for %s: %s", path, exc)
            if self._stats:
                self._stats.errors_count += 1

        html: str | None = None
        if not composer_data or not parse_composer_response(composer_data):
            try:
                page_path = path if path.startswith("/") else f"/{path}"
                self._page.goto(
                    f"{DESKTOP_BASE_URL}{page_path}",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                html = self._page.content()
            except Exception as exc:
                logger.warning("Playwright HTML fallback failed: %s", exc)
                if self._stats:
                    self._stats.errors_count += 1

        return composer_data, html


class OzonCategoryCollector:
    """
    Collects full Ozon category tree for a seller/catalog URL.

    Example:
        collector = OzonCategoryCollector("https://www.ozon.ru/seller/example/")
        tree = collector.load_categories()
        collector.save_json()
    """

    def __init__(
        self,
        source_url: str,
        *,
        cache_dir: Path | str | None = None,
        output_dir: Path | str | None = None,
        cookies: list[dict[str, Any]] | None = None,
        playwright_page: Any | None = None,
        fetcher: PageFetcher | None = None,
    ) -> None:
        self.source_url = normalize_source_url(source_url)
        self._cache = CategoryCache(Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR)
        self._storage = CategoryStorage(Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR)
        self._stats = LoadStats(source_url=self.source_url)
        self._roots: list[CategoryNode] = []
        self._index: dict[str, CategoryNode] = {}
        self._root_ids: set[str] = set()
        self._root_names: set[str] = set()
        self._visited: set[str] = set()

        if fetcher is not None:
            self._fetcher = fetcher
            self._owns_fetcher = False
        elif playwright_page is not None:
            self._fetcher = PlaywrightPageFetcher(playwright_page, self._stats)
            self._owns_fetcher = False
        else:
            self._fetcher = HttpxPageFetcher(cookies=cookies, stats=self._stats)
            self._owns_fetcher = True

    def close(self) -> None:
        if self._owns_fetcher and isinstance(self._fetcher, HttpxPageFetcher):
            self._fetcher.close()

    def __enter__(self) -> OzonCategoryCollector:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def stats(self) -> LoadStats:
        return self._stats

    @property
    def roots(self) -> list[CategoryNode]:
        return list(self._roots)

    def load_categories(self, *, force: bool = False) -> list[CategoryNode]:
        """Load from cache if structure unchanged, otherwise fetch fresh tree."""
        if not force:
            cached = self._cache.load(self.source_url)
            if cached:
                self._roots = cached
                self._rebuild_index()
                self._stats.from_cache = True
                self._stats.roots_count = len(self._roots)
                self._stats.categories_total = len(flatten_tree(self._roots))
                self._stats.max_depth = max((n.level for n in flatten_tree(self._roots)), default=0)
                self._stats.finish()
                logger.info(
                    "Loaded %s categories from cache (fingerprint %s)",
                    self._stats.categories_total,
                    (self._cache.read_fingerprint() or "")[:12],
                )
                return self._roots
        return self.update_categories()

    def update_categories(self) -> list[CategoryNode]:
        """Force refresh, detect structure changes, update cache."""
        previous = self._cache.load(self.source_url) or []
        self._visited.clear()
        self._stats = LoadStats(source_url=self.source_url)

        logger.info("Collecting categories for %s", self.source_url)
        self._roots = self._collect_full_tree()
        self._rebuild_index()

        fingerprint = compute_fingerprint(self._roots)
        old_fp = self._cache.read_fingerprint()
        if previous:
            self._stats.changes = detect_structure_changes(previous, self._roots)
            self._stats.structure_changed = bool(self._stats.changes) or fingerprint != old_fp
        else:
            self._stats.structure_changed = True

        self._cache.save(self.source_url, self._roots)
        self._stats.roots_count = len(self._roots)
        self._stats.categories_total = len(flatten_tree(self._roots))
        self._stats.max_depth = max((n.level for n in flatten_tree(self._roots)), default=0)
        self._stats.finish()

        logger.info(
            "Collection done: roots=%s total=%s depth=%s errors=%s retries=%s duration=%.1fs changed=%s",
            self._stats.roots_count,
            self._stats.categories_total,
            self._stats.max_depth,
            self._stats.errors_count,
            self._stats.retries_count,
            self._stats.duration_sec,
            self._stats.structure_changed,
        )
        for change in self._stats.changes[:20]:
            logger.info("Structure change [%s] %s: %s", change.kind, change.category_id, change.message)
        if len(self._stats.changes) > 20:
            logger.info("... and %s more changes", len(self._stats.changes) - 20)

        return self._roots

    def save_json(self, filename: str = "categories.json") -> Path:
        return self._storage.save_json(self._roots, filename)

    def save_sqlite(self, filename: str = "categories.sqlite") -> Path:
        return self._storage.save_sqlite(self._roots, filename)

    def save_csv(self, filename: str = "categories.csv") -> Path:
        return self._storage.save_csv(self._roots, filename)

    def get_category_by_id(self, category_id: str) -> CategoryNode | None:
        return self._index.get(str(category_id))

    def get_category_by_name(self, name: str, *, exact: bool = True) -> CategoryNode | None:
        name_l = name.strip().lower()
        for node in self._index.values():
            if exact and node.name.lower() == name_l:
                return node
            if not exact and name_l in node.name.lower():
                return node
        return None

    def get_children(self, category_id: str) -> list[CategoryNode]:
        node = self.get_category_by_id(category_id)
        return list(node.children) if node else []

    def get_parent(self, category_id: str) -> CategoryNode | None:
        node = self.get_category_by_id(category_id)
        if not node or not node.parent_id:
            return None
        return self.get_category_by_id(node.parent_id)

    def find_path(self, category_id: str) -> list[CategoryNode]:
        node = self.get_category_by_id(category_id)
        if not node:
            return []
        parts: list[CategoryNode] = []
        current: CategoryNode | None = node
        while current is not None:
            parts.append(current)
            current = self.get_parent(current.id)
        return list(reversed(parts))

    def _collect_full_tree(self) -> list[CategoryNode]:
        base_path = seller_base_path(self.source_url)
        composer, html = self._safe_fetch(base_path)
        raw_roots = parse_page_all_sources(composer, html, parent_id=None)
        if not raw_roots and html:
            for block in extract_json_blocks(html):
                raw_roots.extend(parse_composer_response(block) if isinstance(block, dict) else [])

        roots: list[CategoryNode] = []
        for item in raw_roots:
            if not is_valid_category(str(item.get("name", "")), str(item.get("id", ""))):
                continue
            node = self._make_node(item, parent=None)
            roots.append(node)
            self._root_ids.add(node.id)
            self._root_names.add(self._normalize_name(node.name))

        queue: list[CategoryNode] = list(roots)
        self._visited.clear()

        while queue:
            parent = queue.pop(0)
            if parent.id in self._visited:
                continue

            if parent.children:
                self._visited.add(parent.id)
                for child in parent.children:
                    if child.id not in self._visited:
                        queue.append(child)
                continue

            self._visited.add(parent.id)
            try:
                children = self._fetch_children(parent.id)
            except Exception as exc:
                logger.error("Failed children for %s (%s): %s", parent.name, parent.id, exc)
                self._stats.errors_count += 1
                continue

            parent.children = children
            for child in children:
                if child.id not in self._visited:
                    queue.append(child)

        return roots

    def _fetch_children(self, parent_id: str) -> list[CategoryNode]:
        path = build_category_path(self.source_url, parent_id)
        composer, html = self._safe_fetch(path)

        tree = parse_composer_response(composer) if composer else []
        raw_children = extract_direct_children(
            tree,
            parent_id,
            root_ids=self._root_ids,
            page_category_id=parent_id,
        )
        if not raw_children:
            raw_children = parse_page_all_sources(composer, html, parent_id)
        if not raw_children and html:
            raw_children = parse_html_category_block(html, parent_id)

        parent = self.get_category_by_id(parent_id) or self._find_node_in_roots(parent_id)
        nodes: list[CategoryNode] = []
        for item in raw_children:
            cid = str(item.get("id", ""))
            name_key = self._normalize_name(str(item.get("name", "")))
            existing = self._index.get(cid)
            if (
                cid == parent_id
                or cid in self._root_ids
                or name_key in self._root_names
                or cid in {c.id for c in (parent.children if parent else [])}
                or (existing is not None and existing.parent_id != parent_id)
            ):
                continue
            if not is_valid_category(str(item.get("name", "")), cid):
                continue
            nodes.append(self._make_node(item, parent=parent))
        return nodes

    @staticmethod
    def _normalize_name(name: str) -> str:
        return re.sub(r"\s+", " ", name.strip().lower().replace("ё", "е"))

    def _safe_fetch(self, path: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            return self._fetcher.fetch(path)
        except Exception as exc:
            self._stats.errors_count += 1
            logger.error("Fetch failed for %s: %s", path, exc)
            return None, None

    def _make_node(self, data: dict[str, Any], parent: CategoryNode | None) -> CategoryNode:
        cid = str(data["id"])
        name = str(data["name"]).strip()
        base = urlparse_scheme_netloc(self.source_url)
        url = absolute_ozon_url(base, data.get("url")) or build_category_url(self.source_url, cid)
        level = parent.level + 1 if parent else 0
        path = f"{parent.path} > {name}" if parent else name

        node = CategoryNode(
            id=cid,
            name=name,
            url=url,
            parent_id=parent.id if parent else None,
            level=level,
            path=path,
            children=[],
        )
        self._index[cid] = node

        for raw_child in data.get("children") or []:
            if is_valid_category(str(raw_child.get("name", "")), str(raw_child.get("id", ""))):
                node.children.append(self._make_node(raw_child, parent=node))

        return node

    def _find_node_in_roots(self, category_id: str) -> CategoryNode | None:
        if category_id in self._index:
            return self._index[category_id]

        def walk(node: CategoryNode) -> CategoryNode | None:
            if node.id == category_id:
                return node
            for child in node.children:
                found = walk(child)
                if found:
                    return found
            return None

        for root in self._roots:
            found = walk(root)
            if found:
                return found
        return None

    def _rebuild_index(self) -> None:
        self._index.clear()
        self._root_ids = {r.id for r in self._roots}
        self._root_names = {self._normalize_name(r.name) for r in self._roots}

        def walk(node: CategoryNode) -> None:
            self._index[node.id] = node
            for child in node.children:
                walk(child)

        for root in self._roots:
            walk(root)


def urlparse_scheme_netloc(source_url: str) -> str:
    from urllib.parse import urlparse

    p = urlparse(normalize_source_url(source_url))
    return f"{p.scheme}://{p.netloc}"
