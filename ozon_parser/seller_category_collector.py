"""Рекурсивный сбор дерева категорий магазина через Composer API и JSON страницы."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import Page

from .browser import recover_access, safe_goto
from .category_extract import (
    find_category_node,
    is_valid_category,
    parse_composer_response,
)
from .config import (
    BrowserMode,
    CATALOG_LOAD_TIMEOUT_SEC,
    DESKTOP_MODE,
    MOBILE_MODE,
)
from .utils import fast_delay, normalize_seller_url, to_browser_url

@dataclass
class CollectedCategory:
    id: str
    name: str
    url: str
    level: int = 0
    parent_id: str | None = None
    path: str = ""
    children: list[CollectedCategory] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "level": self.level,
            "parent_id": self.parent_id,
            "path": self.path,
            "children": [child.to_dict() for child in self.children],
        }


class SellerCategoryCollector:
    """Рекурсивный сбор категорий без зависимости от HTML-блока фильтров."""

    def __init__(
        self,
        page: Page,
        seller_url: str,
        log: Callable[[str], None] | None = None,
        on_manual_bypass=None,
        deadline: float | None = None,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> None:
        self.page = page
        self.browser_mode = browser_mode
        self.seller_url = normalize_seller_url(seller_url, browser_mode)
        self.log = log or (lambda _m: None)
        self.on_manual_bypass = on_manual_bypass
        self.deadline = deadline
        self._root_ids: set[str] = set()
        self._root_names: set[str] = set()
        self._visited: set[str] = set()
        self._known_ids: set[str] = set()

    def collect(
        self,
        *,
        on_roots: Callable[[list[CollectedCategory]], None] | None = None,
        on_branch: Callable[[CollectedCategory], None] | None = None,
    ) -> list[CollectedCategory]:
        self.log("Открываем каталог Ozon...")
        if not self._open_page(self.seller_url):
            raise ConnectionError("Не удалось открыть каталог Ozon.")

        raw_roots = self._collect_direct_children(parent_id=None, page_url=self.seller_url)
        if not raw_roots:
            raise ConnectionError(
                "Категории не найдены в данных страницы каталога Ozon."
            )

        roots: list[CollectedCategory] = []
        for item in raw_roots:
            cid = str(item.get("id", ""))
            if not cid or cid in self._known_ids:
                continue
            node = self._make_node(item, parent=None)
            roots.append(node)
            self._root_ids.add(node.id)
            self._root_names.add(self._normalize_name(node.name))
            self._known_ids.add(node.id)

        self.log(f"Категории 1-го уровня: {len(roots)}")
        if on_roots:
            on_roots(roots)

        for idx, root in enumerate(roots, start=1):
            if self._timed_out():
                self.log(f"Лимит времени: обработано {idx - 1}/{len(roots)} веток")
                break
            self.log(f"Ветка {idx}/{len(roots)}: {root.name}")
            self._collect_recursive(root)
            if on_branch:
                on_branch(root)

        total = sum(1 + self._count_descendants(r) for r in roots)
        self.log(f"Готово: {len(roots)} корней, {total} категорий")
        return roots

    def _collect_recursive(self, node: CollectedCategory) -> None:
        if self._timed_out() or node.id in self._visited:
            return
        self._visited.add(node.id)

        prefix = "  " * node.level
        category_url = node.url or self._build_category_url(node.id)
        self.log(f"{prefix}→ {node.name}")

        if not self._open_page(category_url):
            self.log(f"{prefix}  не удалось открыть")
            return

        raw_children = self._collect_direct_children(
            parent_id=node.id,
            page_url=category_url,
        )
        if not raw_children:
            self.log(f"{prefix}  лист (подкатегорий нет)")
            return

        children: list[CollectedCategory] = []
        ancestor_names = {
            self._normalize_name(part)
            for part in node.path.split(" > ")
            if part.strip()
        }
        for item in raw_children:
            cid = str(item.get("id", ""))
            name_key = self._normalize_name(str(item.get("name", "")))
            if (
                not cid
                or cid == node.id
                or cid in self._known_ids
                or cid in self._root_ids
                or not name_key
                or name_key in ancestor_names
                or name_key in self._root_names
            ):
                continue
            child = self._make_node(item, parent=node)
            children.append(child)
            # ID резервируется сразу: одна категория не попадёт в две ветки.
            self._known_ids.add(cid)

        node.children = children
        self.log(f"{prefix}  {len(children)} подкатегорий")

        for child in children:
            if self._timed_out():
                return
            self._collect_recursive(child)

    def _collect_direct_children(
        self,
        parent_id: str | None,
        page_url: str,
    ) -> list[dict]:
        """Прямые потомки из categoryFilter официального Composer API."""
        composer = self._fetch_composer(page_url)
        if not composer:
            return []
        raw = self._extract_category_filter_children(composer, parent_id)
        if parent_id is not None and not raw:
            # Some layouts expose a nested category tree outside categoryFilter.
            # Use it only when a direct parent-child relation can be established.
            tree = parse_composer_response(composer)
            parent = find_category_node(tree, str(parent_id))
            if parent:
                raw = [
                    {
                        "id": str(child.get("id", "")),
                        "name": str(child.get("name", "")),
                        "url": child.get("url"),
                    }
                    for child in parent.get("children") or []
                ]
        return self._filter_fake(raw)

    def _extract_category_filter_children(
        self,
        composer: dict,
        parent_id: str | None,
    ) -> list[dict]:
        """
        Разобрать официальный categoryFilter продавца из Composer.

        Ozon сам отдаёт плоский список с полем level:
        родитель (level=N), затем его прямые дети (level=N+1).
        """
        category_lists: list[list[dict]] = []
        widget_states = composer.get("widgetStates") or {}

        widget_prefixes = (
            ("filtersdesktop",)
            if self.browser_mode == DESKTOP_MODE
            else ("filtersmobile", "mobilefilters", "searchfilters", "filters")
        )
        for key, raw_state in widget_states.items():
            if not str(key).lower().startswith(widget_prefixes):
                continue
            try:
                state = json.loads(raw_state) if isinstance(raw_state, str) else raw_state
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(state, dict):
                continue

            for section in state.get("sections") or []:
                for filter_item in section.get("filters") or []:
                    if (
                        filter_item.get("type") != "categoryFilter"
                        and filter_item.get("key") != "category"
                    ):
                        continue
                    category_filter = filter_item.get("categoryFilter") or {}
                    raw_list = category_filter.get("categories") or []
                    items = self._normalize_filter_sequence(raw_list)
                    if items:
                        category_lists.append(items)

        if not category_lists:
            return []

        if parent_id is None:
            roots: list[dict] = []
            seen_roots: set[str] = set()
            for items in category_lists:
                root_level = min(item["level"] for item in items)
                for item in items:
                    if item["level"] != root_level or item["id"] in seen_roots:
                        continue
                    seen_roots.add(item["id"])
                    roots.append(item)
            return roots

        parent_id = str(parent_id)
        children: list[dict] = []
        seen_children: set[str] = set()
        for items in category_lists:
            parent_index = next(
                (index for index, item in enumerate(items) if item["id"] == parent_id),
                -1,
            )
            if parent_index < 0:
                continue
            parent_level = items[parent_index]["level"]
            for item in items[parent_index + 1 :]:
                if item["level"] <= parent_level:
                    break
                if (
                    item["level"] == parent_level + 1
                    and item["id"] not in seen_children
                ):
                    seen_children.add(item["id"])
                    children.append(item)
        # Never infer children from unrelated minimum-level categories. That
        # was the source of duplicated roots under every processed branch.
        return children

    def _normalize_filter_sequence(self, raw_categories: list[dict]) -> list[dict]:
        items: list[dict] = []
        seen: set[str] = set()
        for raw in raw_categories:
            name = str(raw.get("title") or "").strip()
            href = str(raw.get("urlValue") or raw.get("url") or "")
            category_id = self._category_id_from_filter_item(raw, href)
            if not category_id or category_id in seen:
                continue
            if not is_valid_category(name, category_id):
                continue
            seen.add(category_id)
            try:
                level = int(raw.get("level", 0))
            except (TypeError, ValueError):
                level = 0
            items.append(
                {
                    "id": category_id,
                    "name": name,
                    "url": href or None,
                    "level": level,
                }
            )
        return items

    @staticmethod
    def _normalize_name(name: str) -> str:
        return re.sub(r"\s+", " ", name.strip().lower().replace("ё", "е"))

    @staticmethod
    def _category_id_from_filter_item(raw: dict, href: str) -> str | None:
        test_info = raw.get("testInfo") or {}
        automation_id = str(test_info.get("automatizationId") or "")
        match = re.search(r"filter-category-item-(\d+)", automation_id)
        if match:
            return match.group(1)

        match = re.search(r"[?&]category=(\d+)", href, re.I)
        if match:
            return match.group(1)

        match = re.search(r"/[^/?#]*-(\d+)/?(?:[?#]|$)", href)
        return match.group(1) if match else None

    def _filter_fake(self, items: list[dict]) -> list[dict]:
        """Отсечь фейковые категории (myProfile.profile, widget.* и пр.)."""
        seen: set[str] = set()
        out: list[dict] = []
        for item in items:
            cid = str(item.get("id", ""))
            name = str(item.get("name", "")).strip()
            if not cid or cid in seen:
                continue
            if not is_valid_category(name, cid):
                continue
            seen.add(cid)
            out.append({"id": cid, "name": name, "url": item.get("url")})
        return out

    def _fetch_composer(self, page_url: str) -> dict | None:
        rel_path, full_url = self._composer_paths(page_url)
        client_name = "mweb_client" if self.browser_mode == MOBILE_MODE else "dweb_client"
        script = """
        async ({ path, fullUrl, clientName }) => {
            const urls = [
                '/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(path),
                '/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(fullUrl),
            ];
            for (const apiUrl of urls) {
                try {
                    const resp = await fetch(apiUrl, {
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json',
                            'x-o3-app-name': clientName,
                        },
                    });
                    if (!resp.ok) continue;
                    return await resp.json();
                } catch (e) {}
            }
            return null;
        }
        """
        try:
            data = self.page.evaluate(
                script,
                {
                    "path": rel_path,
                    "fullUrl": full_url,
                    "clientName": client_name,
                },
            )
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _composer_paths(self, page_url: str) -> tuple[str, str]:
        full = to_browser_url(page_url, self.browser_mode)
        parsed = urlparse(full)
        path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
        rel = f"{path}?{parsed.query}" if parsed.query else path
        return rel, full

    def _open_page(self, url: str) -> bool:
        if not safe_goto(self.page, url, self.log, on_manual_bypass=self.on_manual_bypass):
            if not recover_access(self.page, self.log, self.on_manual_bypass, target_url=url):
                return False
            if not safe_goto(self.page, url, self.log, on_manual_bypass=self.on_manual_bypass):
                return False
        fast_delay(0.35, 0.7)
        return True

    def _make_node(self, data: dict, parent: CollectedCategory | None) -> CollectedCategory:
        cid = str(data["id"])
        name = str(data["name"]).strip()
        level = parent.level + 1 if parent else 0
        path = f"{parent.path} > {name}" if parent else name
        href = data.get("url")
        url = self._resolve_url(href, cid)
        return CollectedCategory(
            id=cid,
            name=name,
            url=url,
            level=level,
            parent_id=parent.id if parent else None,
            path=path,
            children=[],
        )

    def _build_category_url(self, category_id: str) -> str:
        parsed = urlparse(self.seller_url)
        query = urlencode({"category": category_id})
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))

    def _resolve_url(self, href: str | None, category_id: str) -> str:
        if href:
            absolute = to_browser_url(urljoin(self.seller_url, href), self.browser_mode)
            lower = absolute.lower()
            if "category" in lower and "ozon.ru" in lower:
                return absolute
        return self._build_category_url(category_id)

    def _timed_out(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    @staticmethod
    def _count_descendants(node: CollectedCategory) -> int:
        return sum(1 + SellerCategoryCollector._count_descendants(c) for c in node.children)


def collected_to_filter_dict(node: CollectedCategory) -> dict:
    return {
        "id": node.id,
        "name": node.name,
        "url": node.url,
        "level": node.level,
        "parent_id": node.parent_id,
        "path": node.path,
        "children": [collected_to_filter_dict(c) for c in node.children],
    }


def collect_seller_categories(
    page: Page,
    seller_url: str,
    log: Callable[[str], None] | None = None,
    on_manual_bypass=None,
    timeout_sec: int | None = CATALOG_LOAD_TIMEOUT_SEC,
    on_roots: Callable[[list[CollectedCategory]], None] | None = None,
    on_branch: Callable[[CollectedCategory], None] | None = None,
    browser_mode: BrowserMode = DESKTOP_MODE,
) -> list[CollectedCategory]:
    deadline = (
        time.monotonic() + max(60, timeout_sec)
        if timeout_sec is not None
        else None
    )
    collector = SellerCategoryCollector(
        page,
        seller_url,
        log=log,
        on_manual_bypass=on_manual_bypass,
        deadline=deadline,
        browser_mode=browser_mode,
    )
    return collector.collect(on_roots=on_roots, on_branch=on_branch)
