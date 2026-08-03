import json
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import Page

from .browser import (
    extract_incident_id,
    is_access_restricted,
    is_antibot_challenge_page,
    is_blocked_page,
    is_captcha_page,
    is_seller_page,
    page_has_usable_ozon_content,
    recover_access,
    safe_goto,
    wait_for_ozon_ready,
)
from .category_extract import (
    find_category_nodes,
    is_valid_category,
    merge_category_trees,
    parse_composer_response,
)
from .filters import (
    DOM_FILTER_SECTIONS_SCRIPT,
    FilterOptionNode,
    FilterSection,
    attach_category_context,
    extract_filter_sections_from_dom,
    merge_filter_sections,
    parse_filter_sections_from_composer,
)
from .utils import (
    extract_json_blocks,
    fast_delay,
    human_delay,
    normalize_seller_url,
    route_browser_url,
    to_browser_url,
)

from .config import (
    ALL_SELLERS_PATH,
    BrowserMode,
    CATALOG_LOAD_TIMEOUT_SEC,
    DESKTOP_BASE_URL,
    DESKTOP_MODE,
    GLOBAL_CATALOG_PATH,
    GLOBAL_COMPOSER_BATCH_CONCURRENCY,
    GLOBAL_COMPOSER_BATCH_PAUSE_MAX,
    GLOBAL_COMPOSER_BATCH_PAUSE_MIN,
    GLOBAL_COMPOSER_BATCH_SIZE,
    GLOBAL_COMPOSER_LONG_EVERY_N,
    GLOBAL_COMPOSER_LONG_PAUSE_MAX,
    GLOBAL_COMPOSER_LONG_PAUSE_MIN,
    GLOBAL_COMPOSER_ROOT_PAUSE_MAX,
    GLOBAL_COMPOSER_ROOT_PAUSE_MIN,
    MOBILE_BASE_URL,
    MOBILE_DESKTOP_HOST_SESSIONS,
    MOBILE_MODE,
)
from .seller_category_collector import (
    SellerCategoryCollector,
    collect_seller_categories,
    collected_to_filter_dict,
)

# Блок категорий в фильтре Ozon (CSS-modules класс)
CATEGORY_BLOCK_CLASS = "wb6_7"

_CATEGORY_BLOCK_JS = """
(args) => {
    const parentId = args.parentId ? String(args.parentId) : null;
    const rootIds = new Set((args.rootIds || []).map(String));
    const blockClass = args.blockClass || 'wb6_7';

    const findCategoryBlock = () => {
        const candidates = [];
        document.querySelectorAll('[class*="' + blockClass + '"]').forEach(el => candidates.push(el));
        document.querySelectorAll('[data-widget="filtersDesktop"], [data-widget="searchFilters"], [data-widget="filters"]').forEach(w => {
            w.querySelectorAll('[class*="' + blockClass + '"]').forEach(el => candidates.push(el));
        });
        let best = null;
        let maxLinks = 0;
        for (const b of candidates) {
            const count = b.querySelectorAll('a[href*="category"]').length;
            if (count > maxLinks) {
                maxLinks = count;
                best = b;
            }
        }
        if (best) return best;
        return document.querySelector('[data-widget="filtersDesktop"]')
            || document.querySelector('[data-widget="searchFilters"]')
            || document.querySelector('[data-widget="filters"]');
    };

    const block = findCategoryBlock();
    if (!block) return [];

    const blockLeft = block.getBoundingClientRect().left;
    const links = [];
    const seen = new Set();

    const extractCategoryId = (href) => {
        if (!href) return null;
        let m = href.match(/[?&]category=(\\d+)/i);
        if (m) return m[1];
        m = href.match(/\\/category\\/[^/?#]*-(\\d+)\\/?(?:[?#]|$)/i);
        if (m) return m[1];
        m = href.match(/\\/category\\/(\\d+)\\/?(?:[?#]|$)/i);
        return m ? m[1] : null;
    };

    block.querySelectorAll('a[href*="category"]').forEach(a => {
        const href = a.href || a.getAttribute('href') || '';
        const id = extractCategoryId(href);
        if (!id) return;
        if (seen.has(id)) return;

        let name = (a.textContent || a.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
        if (!name || name.length < 2) {
            const inner = a.querySelector('span, div');
            if (inner) name = (inner.textContent || '').trim().replace(/\\s+/g, ' ');
        }
        if (!name) return;

        seen.add(id);
        let domDepth = 0;
        let el = a.parentElement;
        while (el && el !== block) {
            domDepth += 1;
            el = el.parentElement;
        }
        const left = a.getBoundingClientRect().left;
        const xIndent = Math.round((left - blockLeft) / 8);
        links.push({ id, name, url: href, domDepth, xIndent });
    });

    if (!links.length) return [];

    const mapLink = (l) => ({ id: l.id, name: l.name, url: l.url });
    const pageMatch = window.location.href.match(/[?&]category=(\\d+)/i);
    const pageCategoryId = args.forcePageCategoryId
        ? String(args.forcePageCategoryId)
        : (pageMatch ? pageMatch[1] : null);

    const pickDirectChildren = (parentIndex) => {
        const p = links[parentIndex];
        const direct = [];
        for (let i = parentIndex + 1; i < links.length; i++) {
            const item = links[i];
            if (item.domDepth <= p.domDepth && item.xIndent <= p.xIndent) break;
            if (item.domDepth === p.domDepth + 1 || item.xIndent > p.xIndent) {
                if (item.domDepth <= p.domDepth + 1) direct.push(item);
            }
        }
        if (direct.length) return direct;

        const relaxed = [];
        for (let i = parentIndex + 1; i < links.length; i++) {
            const item = links[i];
            if (item.domDepth <= p.domDepth && item.xIndent <= p.xIndent) break;
            relaxed.push(item);
        }
        return relaxed;
    };

    if (!parentId) {
        const minDepth = Math.min(...links.map(l => l.domDepth));
        const roots = links.filter(l => l.domDepth === minDepth);
        const unique = [];
        const ids = new Set();
        for (const r of roots) {
            if (!ids.has(r.id)) {
                ids.add(r.id);
                unique.push(r);
            }
        }
        return unique.map(mapLink);
    }

    if (pageCategoryId === parentId) {
        const notParent = links.filter(l => l.id !== parentId);
        const nonRoots = notParent.filter(l => !rootIds.has(l.id));
        if (nonRoots.length) return nonRoots.map(mapLink);
        if (notParent.length) return notParent.map(mapLink);

        const minIndent = Math.min(...links.map(l => l.xIndent));
        const minLevel = links.filter(l => l.xIndent === minIndent && l.id !== parentId);
        if (minLevel.length) return minLevel.map(mapLink);
    }

    const parentIdx = links.findIndex(l => l.id === parentId);
    if (parentIdx >= 0) {
        const children = pickDirectChildren(parentIdx);
        if (children.length) return children.map(mapLink);
    }

    return [];
}
"""

_CATEGORY_FULL_SUBTREE_JS = """
(args) => {
    const parentId = args.parentId ? String(args.parentId) : null;
    const rootIds = new Set((args.rootIds || []).map(String));
    const blockClass = args.blockClass || 'wb6_7';
    const forcePageCategoryId = args.forcePageCategoryId ? String(args.forcePageCategoryId) : null;

    const extractCategoryId = (href) => {
        if (!href) return null;
        let m = href.match(/[?&]category=(\\d+)/i);
        if (m) return m[1];
        m = href.match(/\\/category\\/[^/?#]*-(\\d+)\\/?(?:[?#]|$)/i);
        if (m) return m[1];
        m = href.match(/\\/category\\/(\\d+)\\/?(?:[?#]|$)/i);
        return m ? m[1] : null;
    };

    const findCategoryBlock = () => {
        const candidates = [];
        document.querySelectorAll('[class*="' + blockClass + '"]').forEach(el => candidates.push(el));
        document.querySelectorAll('[data-widget="filtersDesktop"], [data-widget="searchFilters"], [data-widget="filters"]').forEach(w => {
            w.querySelectorAll('[class*="' + blockClass + '"]').forEach(el => candidates.push(el));
        });
        let best = null;
        let maxLinks = 0;
        for (const b of candidates) {
            const count = b.querySelectorAll('a[href*="category"]').length;
            if (count > maxLinks) {
                maxLinks = count;
                best = b;
            }
        }
        return best
            || document.querySelector('[data-widget="filtersDesktop"]')
            || document.querySelector('[data-widget="searchFilters"]')
            || document.querySelector('[data-widget="filters"]');
    };

    const block = findCategoryBlock();
    if (!block || !parentId) return [];

    const blockLeft = block.getBoundingClientRect().left;
    const links = [];
    const seen = new Set();

    block.querySelectorAll('a[href*="category"]').forEach(a => {
        const href = a.href || a.getAttribute('href') || '';
        const id = extractCategoryId(href);
        if (!id || seen.has(id)) return;

        let name = (a.textContent || a.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
        if (!name || name.length < 2) {
            const inner = a.querySelector('span, div');
            if (inner) name = (inner.textContent || '').trim().replace(/\\s+/g, ' ');
        }
        if (!name) return;

        seen.add(id);
        let domDepth = 0;
        let el = a.parentElement;
        while (el && el !== block) {
            domDepth += 1;
            el = el.parentElement;
        }
        const left = a.getBoundingClientRect().left;
        const xIndent = Math.round((left - blockLeft) / 8);
        links.push({ id, name, url: href, domDepth, xIndent });
    });

    if (!links.length) return [];

    const pageMatch = window.location.href.match(/[?&]category=(\\d+)/i);
    const pageCategoryId = forcePageCategoryId || (pageMatch ? pageMatch[1] : null);

    let slice = [];
    const parentIdx = links.findIndex(l => l.id === parentId);
    if (parentIdx >= 0) {
        const pDepth = links[parentIdx].domDepth;
        const pIndent = links[parentIdx].xIndent;
        for (let i = parentIdx + 1; i < links.length; i++) {
            const item = links[i];
            if (item.domDepth <= pDepth && item.xIndent <= pIndent) break;
            slice.push(item);
        }
    } else if (pageCategoryId === parentId) {
        slice = links.filter(l => l.id !== parentId && !rootIds.has(l.id));
        if (!slice.length) slice = links.filter(l => l.id !== parentId);
    }

    if (!slice.length) return [];

    const minDepth = Math.min(...slice.map(l => l.domDepth));
    const minIndent = Math.min(...slice.map(l => l.xIndent));
    const normalized = slice.map(l => ({
        id: l.id,
        name: l.name,
        url: l.url,
        depth: l.domDepth - minDepth,
        indent: l.xIndent - minIndent,
    }));

    const roots = [];
    const stack = [];

    const isChildOf = (parent, child) => {
        if (child.depth > parent.depth) return true;
        if (child.depth === parent.depth && child.indent > parent.indent) return true;
        return false;
    };

    for (const n of normalized) {
        while (stack.length && !isChildOf(stack[stack.length - 1], n)) {
            stack.pop();
        }
        const node = { id: n.id, name: n.name, url: n.url, children: [] };
        if (!stack.length) {
            roots.push(node);
        } else {
            stack[stack.length - 1].children.push(node);
        }
        stack.push({ depth: n.depth, indent: n.indent, node: node });
    }

    return roots;
}
"""


@dataclass
class CategoryTarget:
    id: str
    name: str = ""
    url: str | None = None
    section: str = ""
    param_key: str = ""
    param_value: str = ""
    category_id: str = ""
    category_name: str = ""
    parent_name: str = ""


@dataclass
class CategoryNode:
    id: str
    name: str
    url: str | None = None
    children: list["CategoryNode"] = field(default_factory=list)


class CategoryLoader:
    MAX_SUBCATEGORY_DEPTH = 15
    SUBTREE_API_BATCH_SIZE = GLOBAL_COMPOSER_BATCH_SIZE

    def __init__(
        self,
        page: Page,
        browser_mode: BrowserMode = DESKTOP_MODE,
        session_mode: str | None = None,
    ):
        self.page = page
        self.browser_mode = browser_mode
        self.session_mode = session_mode

    def _catalog_base_url(self) -> str:
        if (
            self.browser_mode == MOBILE_MODE
            and self.session_mode not in MOBILE_DESKTOP_HOST_SESSIONS
        ):
            return MOBILE_BASE_URL
        return DESKTOP_BASE_URL

    def _route_url(self, url: str) -> str:
        return route_browser_url(url, self.browser_mode, self.session_mode)

    def load_root_categories(
        self,
        seller_url: str,
        progress: Callable[[str], None] | None = None,
        on_manual_bypass=None,
    ) -> list[FilterOptionNode]:
        log = progress or (lambda _m: None)
        url = self._route_url(seller_url)

        if not is_seller_page(self.page):
            log("Открываем страницу магазина...")
            if not safe_goto(self.page, url, log, on_manual_bypass=on_manual_bypass):
                if not recover_access(self.page, log, on_manual_bypass, target_url=url):
                    raise ConnectionError(
                        "Ozon заблокировал доступ. Подождите 15–30 минут, "
                        "откройте Chrome один раз и повторите."
                    )
                if not safe_goto(self.page, url, log, on_manual_bypass=on_manual_bypass):
                    raise ConnectionError("Не удалось открыть страницу магазина.")
        else:
            log("Используем открытую страницу магазина")

        self._wait_for_filters(log)
        self._open_category_filter(log)

        roots = self._collect_root_categories(log)
        tree = [node for root in roots if (node := self._category_dict_to_option(root, parent_name=""))]

        if not tree:
            log("Категории не найдены")
        else:
            log(f"Загружено категорий: {len(tree)}")

        return tree

    def load_category_tree(
        self,
        seller_url: str,
        progress: Callable[[str], None] | None = None,
        on_manual_bypass=None,
        on_roots: Callable[[list[FilterOptionNode]], None] | None = None,
        on_subcategories_begin: Callable[[int], None] | None = None,
        on_branch: Callable[[FilterOptionNode], None] | None = None,
        timeout_sec: int = CATALOG_LOAD_TIMEOUT_SEC,
    ) -> list[FilterOptionNode]:
        """Сбор полного дерева категорий магазина (рекурсивный обход всех уровней)."""
        log = progress or (lambda _m: None)

        def emit_roots(roots) -> None:
            self._seller_root_ids = {r.id for r in roots}
            if not on_roots:
                return
            stubs: list[FilterOptionNode] = []
            for root in roots:
                data = collected_to_filter_dict(root)
                data["children"] = []
                if node := self._dict_to_option_tree(data, parent_name=""):
                    stubs.append(node)
            if stubs:
                on_roots(stubs)

        def emit_branch(root) -> None:
            if not on_branch:
                return
            if node := self._dict_to_option_tree(collected_to_filter_dict(root), parent_name=""):
                on_branch(node)

        def roots_loaded(roots) -> None:
            emit_roots(roots)
            if on_subcategories_begin and roots:
                on_subcategories_begin(len(roots))

        log("Сбор иерархии категорий магазина (рекурсивный обход)...")
        collected = collect_seller_categories(
            self.page,
            seller_url,
            log=log,
            on_manual_bypass=on_manual_bypass,
            timeout_sec=timeout_sec,
            on_roots=roots_loaded,
            on_branch=emit_branch,
            browser_mode=self.browser_mode,
        )

        self._seller_root_ids = {r.id for r in collected}
        result: list[FilterOptionNode] = []
        subtotal = 0
        for root in collected:
            data = collected_to_filter_dict(root)
            if node := self._dict_to_option_tree(data, parent_name=""):
                result.append(node)
                subtotal += self._count_option_descendants(node)

        if result:
            log(f"Готово: {len(result)} корневых категорий, всего узлов: {subtotal}")
        else:
            log("Категории не найдены")

        return result

    def load_global_category_tree(
        self,
        progress: Callable[[str], None] | None = None,
        on_manual_bypass=None,
        on_roots: Callable[[list[FilterOptionNode]], None] | None = None,
        on_subcategories_begin: Callable[[int], None] | None = None,
        on_branch: Callable[[FilterOptionNode], None] | None = None,
        timeout_sec: int | None = None,
    ) -> list[FilterOptionNode]:
        """Load the full Ozon catalogue tree for all sellers, with all branches."""
        log = progress or (lambda _m: None)
        base = self._catalog_base_url()
        catalog_base = base.rstrip("/") + ALL_SELLERS_PATH

        def emit_roots(roots) -> None:
            self._seller_root_ids = {r.id for r in roots}
            if not on_roots:
                return
            stubs: list[FilterOptionNode] = []
            for root in roots:
                data = collected_to_filter_dict(root)
                data["children"] = []
                if node := self._dict_to_option_tree(data, parent_name=""):
                    stubs.append(node)
            if stubs:
                on_roots(stubs)

        def emit_branch(root) -> None:
            if not on_branch:
                return
            if node := self._dict_to_option_tree(collected_to_filter_dict(root), parent_name=""):
                on_branch(node)

        def roots_loaded(roots) -> None:
            emit_roots(roots)
            if on_subcategories_begin and roots:
                on_subcategories_begin(len(roots))

        log("Сбор полного каталога Ozon (все продавцы, все ветки)...")
        try:
            raw_roots = self._load_global_root_categories(log, on_manual_bypass)
            if not raw_roots:
                raise self._global_catalog_access_error(
                    "Общий каталог не вернул корневые категории."
                )

            self._seller_root_ids = {str(root["id"]) for root in raw_roots}
            if on_roots:
                stubs = [
                    node
                    for raw in raw_roots
                    if (
                        node := self._dict_to_option_tree(
                            {**raw, "children": []},
                            parent_name="",
                        )
                    )
                ]
                if stubs:
                    on_roots(stubs)
            if on_subcategories_begin:
                on_subcategories_begin(len(raw_roots))

            # Composer tree API needs real /category/{id}/ pages to return children.
            # Browser navigation still prefers /seller/ (see _load_global_root_categories).
            category_base = base.rstrip("/") + GLOBAL_CATALOG_PATH
            root_ids = [str(root["id"]) for root in raw_roots]
            if is_access_restricted(self.page) or is_blocked_page(self.page):
                raise self._global_catalog_access_error(
                    "Доступ ограничен до загрузки веток каталога."
                )
            root_children: dict[str, list[dict]] = {}
            chunk = max(1, int(GLOBAL_COMPOSER_BATCH_SIZE))
            for offset in range(0, len(root_ids), chunk):
                if is_access_restricted(self.page) or is_blocked_page(self.page):
                    raise self._global_catalog_access_error(
                        "Доступ ограничен во время загрузки корневых веток."
                    )
                part_ids = root_ids[offset : offset + chunk]
                part = self._fetch_categories_batch(category_base, part_ids, log)
                if part is None:
                    raise self._global_catalog_access_error(
                        "Composer API вернул блокировку при загрузке корневых веток."
                    )
                root_children.update(part)
                if offset + chunk < len(root_ids):
                    human_delay(
                        GLOBAL_COMPOSER_BATCH_PAUSE_MIN,
                        GLOBAL_COMPOSER_BATCH_PAUSE_MAX,
                    )
            deadline = (
                time.monotonic() + max(60, timeout_sec)
                if timeout_sec is not None
                else None
            )
            for index, root in enumerate(raw_roots, start=1):
                if is_access_restricted(self.page) or is_blocked_page(self.page):
                    log(
                        f"Остановка: блокировка Ozon после {index - 1}/{len(raw_roots)} веток"
                    )
                    break
                if deadline is not None and self._past_deadline(deadline):
                    log(
                        f"Лимит времени: обработано {index - 1}/{len(raw_roots)} веток"
                    )
                    break
                if index > 1:
                    log(
                        "Пауза перед следующей корневой веткой "
                        f"({int(GLOBAL_COMPOSER_ROOT_PAUSE_MIN)}–"
                        f"{int(GLOBAL_COMPOSER_ROOT_PAUSE_MAX)} сек)..."
                    )
                    human_delay(
                        GLOBAL_COMPOSER_ROOT_PAUSE_MIN,
                        GLOBAL_COMPOSER_ROOT_PAUSE_MAX,
                    )
                    if is_access_restricted(self.page) or is_blocked_page(self.page):
                        log(
                            f"Остановка: блокировка Ozon перед веткой "
                            f"{index}/{len(raw_roots)}"
                        )
                        break
                root_id = str(root["id"])
                log(f"Ветка {index}/{len(raw_roots)}: {root['name']}")
                root["children"] = root_children.get(root_id) or []
                self._complete_category_subtree(
                    category_base,
                    root["children"],
                    log,
                    deadline,
                )
                if on_branch:
                    if node := self._dict_to_option_tree(root, parent_name=""):
                        on_branch(node)

            result = [
                node
                for raw in raw_roots
                if (node := self._dict_to_option_tree(raw, parent_name=""))
            ]
            if not result:
                raise ConnectionError("Полный каталог Ozon не распознан.")
            subtotal = sum(self._count_option_descendants(node) for node in result)
            blocked_after = self._page_access_blocked()
            if blocked_after:
                incident = extract_incident_id(self.page) or "fab_"
                log(
                    f"Каталог собран частично/полностью, но Chrome уже под fab_ "
                    f"({incident}). Не обновляйте страницу — подождите 15–30 мин."
                )
            else:
                self._park_catalog_tab(log)
            log(
                f"Готово: полный каталог Ozon — {len(result)} корневых разделов, "
                f"всего узлов: {subtotal}"
            )
            return result
        except Exception as primary_exc:
            if self._page_access_blocked():
                raise self._global_catalog_access_error(str(primary_exc)) from primary_exc
            log(f"Основной сбор каталога не удался: {primary_exc}")
            log("Пробуем резервную загрузку Composer API...")
            try:
                from ozon_categories.collector import OzonCategoryCollector

                with OzonCategoryCollector(
                    catalog_base,
                    playwright_page=self.page,
                ) as collector:
                    collected_dicts = [
                        node.to_dict() for node in collector.load_categories(force=True)
                    ]
            except Exception as fallback_exc:
                if self._page_access_blocked():
                    raise self._global_catalog_access_error(str(primary_exc)) from fallback_exc
                raise ConnectionError(
                    "Не удалось загрузить полный каталог Ozon. "
                    f"Основная ошибка: {primary_exc}. Резерв: {fallback_exc}"
                ) from fallback_exc

            if on_roots:
                stubs = [
                    node
                    for raw in collected_dicts
                    if (
                        node := self._dict_to_option_tree(
                            {**raw, "children": []},
                            parent_name="",
                        )
                    )
                ]
                if stubs:
                    on_roots(stubs)
                    if on_subcategories_begin:
                        on_subcategories_begin(len(stubs))

            result = [
                node
                for raw in collected_dicts
                if (node := self._dict_to_option_tree(raw, parent_name=""))
            ]
            if on_branch:
                for node in result:
                    on_branch(node)
            if not result:
                raise ConnectionError(str(primary_exc)) from primary_exc

            self._seller_root_ids = {node.id for node in result}
            subtotal = sum(self._count_option_descendants(node) for node in result)
            log(
                f"Общий каталог загружен через Composer API: "
                f"{len(result)} разделов, всего узлов: {subtotal}"
            )
            return result

    def _collect_category_branch_whole(
        self,
        seller_base: str,
        node: dict,
        *,
        log,
        on_manual_bypass,
        deadline: float | None = None,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        """Один заход в категорию — сбор всей вложенной иерархии из фильтра."""
        if deadline is not None and self._past_deadline(deadline):
            return

        name = str(node.get("name", "") or node.get("id", ""))
        log(f"→ {name} — сбор всей иерархии...")

        node["children"] = self._fetch_category_subtree(
            seller_base, node, log, on_manual_bypass,
        )
        if node["children"]:
            initial = self._count_dict_descendants(node)
            log(f"  первичный сбор: {initial} узлов, дозагрузка иерархии через API...")
            self._complete_category_subtree(
                seller_base, node["children"], log, deadline,
            )

        total = self._count_dict_descendants(node)
        if node["children"]:
            log(
                f"  {len(node['children'])} подкатегорий верхнего уровня, "
                f"всего узлов в ветке: {total}"
            )
        else:
            log("  подкатегории не найдены")

        if on_update:
            on_update()

    def _complete_category_subtree(
        self,
        seller_base: str,
        nodes: list[dict],
        log,
        deadline: float | None = None,
        max_depth: int | None = None,
    ) -> None:
        """Дозагрузить полную иерархию через Composer API (без переходов по страницам)."""
        if not nodes:
            return
        depth_limit = max_depth if max_depth is not None else self.MAX_SUBCATEGORY_DEPTH
        queue: list[tuple[dict, int]] = [(node, 1) for node in nodes]
        fetched = 0

        while queue:
            if deadline is not None and self._past_deadline(deadline):
                log("  лимит времени: иерархия собрана частично")
                return

            batch: list[tuple[dict, int]] = []
            next_queue: list[tuple[dict, int]] = []

            while queue and len(batch) < self.SUBTREE_API_BATCH_SIZE:
                node, depth = queue.pop(0)
                if depth > depth_limit:
                    continue
                children = node.get("children") or []
                if children:
                    for child in children:
                        next_queue.append((child, depth + 1))
                    continue
                batch.append((node, depth))

            if batch:
                if is_access_restricted(self.page) or is_blocked_page(self.page):
                    log("  остановка дозагрузки: Ozon ограничил доступ (fab_)")
                    return
                ids = [str(n["id"]) for n, _ in batch]
                results = self._fetch_categories_batch(seller_base, ids, log)
                if results is None:
                    log("  пакет категорий недоступен из‑за блокировки — стоп")
                    return
                for node, depth in batch:
                    cid = str(node["id"])
                    children = results.get(cid) or []
                    if children:
                        node["children"] = children
                        fetched += 1
                        for child in children:
                            next_queue.append((child, depth + 1))
                if fetched and fetched % 40 == 0:
                    log(f"  дозагружено веток: {fetched}...")
                if any(results.get(str(n["id"])) for n, _ in batch):
                    # Human-like gap after every Composer batch (anti fab_).
                    human_delay(
                        GLOBAL_COMPOSER_BATCH_PAUSE_MIN,
                        GLOBAL_COMPOSER_BATCH_PAUSE_MAX,
                    )
                    if (
                        fetched
                        and GLOBAL_COMPOSER_LONG_EVERY_N > 0
                        and fetched % GLOBAL_COMPOSER_LONG_EVERY_N == 0
                    ):
                        log(
                            "  длинная пауза Composer "
                            f"(каждые {GLOBAL_COMPOSER_LONG_EVERY_N} пакетов)..."
                        )
                        human_delay(
                            GLOBAL_COMPOSER_LONG_PAUSE_MIN,
                            GLOBAL_COMPOSER_LONG_PAUSE_MAX,
                        )
                    if is_access_restricted(self.page) or is_blocked_page(self.page):
                        log("  остановка дозагрузки: fab_ после паузы")
                        return

            queue.extend(next_queue)
            if not batch and next_queue:
                continue
            if not batch and not next_queue:
                break

    def _count_dict_descendants(self, node: dict) -> int:
        total = 0
        for child in node.get("children") or []:
            total += 1 + self._count_dict_descendants(child)
        return total

    def _clone_category_branch(
        self,
        items: list[dict],
        parent_id: str,
        seller_base: str,
    ) -> list[dict]:
        root_ids = getattr(self, "_seller_root_ids", set())
        result: list[dict] = []
        seen: set[str] = set()
        for item in items:
            cid = str(item.get("id", ""))
            if not cid or cid == parent_id or cid in seen:
                continue
            if cid in root_ids and str(parent_id) not in root_ids:
                continue
            if not is_valid_category(str(item.get("name", "")), cid):
                continue
            seen.add(cid)
            nested = self._clone_category_branch(item.get("children") or [], cid, seller_base)
            href = item.get("url")
            if href:
                url = self._route_url(urljoin(seller_base, href))
            else:
                url = self._build_category_url(seller_base, cid)
            result.append(
                {
                    "id": cid,
                    "name": str(item.get("name", "")).strip(),
                    "url": url,
                    "children": nested,
                }
            )
        return result

    def _past_deadline(self, deadline: float) -> bool:
        return time.monotonic() >= deadline

    def _park_catalog_tab(self, log) -> None:
        """Stop Ozon SPA activity after tree load so the tab stays quiet."""
        if self._page_access_blocked():
            return
        try:
            current = str(getattr(self.page, "url", "") or "")
        except Exception:
            current = ""
        if not current or current.startswith("about:"):
            return
        log("Успокаиваем вкладку Chrome (без F5 по Ozon) после сбора каталога...")
        try:
            # about:blank stops Composer/SPA polling that often flips the tab to fab_.
            self.page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            log(f"Не удалось увести вкладку с Ozon: {exc}")

    def _count_option_descendants(self, node: FilterOptionNode) -> int:
        total = 0
        for child in node.children:
            total += 1 + self._count_option_descendants(child)
        return total

    def _dict_to_option_tree(self, data: dict, parent_name: str = "") -> FilterOptionNode | None:
        option = self._category_dict_to_option(data, parent_name=parent_name)
        if not option:
            return None
        child_parent = option.name
        option.children = [
            child
            for item in data.get("children", [])
            if (child := self._dict_to_option_tree(item, parent_name=child_parent))
        ]
        return option

    def _composer_urls_for_category(self, seller_base: str, category_id: str) -> tuple[str, str]:
        catalog_url = self._build_category_url(seller_base, category_id)
        parsed = urlparse(catalog_url)
        path = parsed.path
        if not path.endswith("/"):
            path += "/"
        query = parsed.query
        rel_path = f"{path}?{query}" if query else path
        return rel_path, catalog_url

    def _fetch_categories_batch(
        self,
        seller_base: str,
        category_ids: list[str],
        log,
    ) -> dict[str, list[dict]] | None:
        if not category_ids:
            return {}

        if is_access_restricted(self.page) or is_blocked_page(self.page):
            log("Пакетный запрос категорий отменён: доступ ограничен")
            return None

        url_pairs = [self._composer_urls_for_category(seller_base, cid) for cid in category_ids]
        script = """
        async ({ items, clientName, concurrencyLimit }) => {
            const fetchOne = async ({ path, fullUrl }) => {
                const candidates = [
                    '/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(path),
                ];
                if (fullUrl) {
                    candidates.push('/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(fullUrl));
                }
                for (const apiUrl of candidates) {
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
            };
            const out = {};
            const queue = items.slice();
            // Sequential by default — parallel workers were triggering fab_ mid-tree.
            const concurrency = Math.max(
                1,
                Math.min(Number(concurrencyLimit) || 1, queue.length || 1)
            );
            const workers = Array.from({length: concurrency}, async () => {
                while (queue.length) {
                    const item = queue.shift();
                    if (!item) break;
                    out[item.path] = await fetchOne(item);
                }
            });
            await Promise.all(workers);
            return JSON.stringify(out);
        }
        """
        result: dict[str, list[dict]] = {cid: [] for cid in category_ids}
        if is_antibot_challenge_page(self.page):
            log("Ozon проверяет браузер перед пакетным запросом категорий...")
            if not wait_for_ozon_ready(self.page, log):
                return None
        try:
            payload_items = [{"path": p, "fullUrl": u} for p, u in url_pairs]
            client_name = "mweb_client" if self.browser_mode == MOBILE_MODE else "dweb_client"
            raw = self.page.evaluate(
                script,
                {
                    "items": payload_items,
                    "clientName": client_name,
                    "concurrencyLimit": int(GLOBAL_COMPOSER_BATCH_CONCURRENCY),
                },
            )
            if not raw:
                return result
            if "fab_chlg" in raw.lower() or "похоже, нет соединения" in raw.lower():
                log("Composer вернул страницу блокировки — останавливаем сбор категорий")
                return None
            payload = json.loads(raw)
            filter_parser = SellerCategoryCollector(
                self.page,
                self._catalog_base_url().rstrip("/") + ALL_SELLERS_PATH,
                browser_mode=self.browser_mode,
            )
            filter_parser._root_ids = set(getattr(self, "_seller_root_ids", set()))
            for cid, (path, _full) in zip(category_ids, url_pairs):
                data = payload.get(path)
                if not data:
                    continue
                direct = filter_parser._extract_category_filter_children(data, cid)
                if direct:
                    result[cid] = self._clone_category_branch(
                        direct,
                        cid,
                        seller_base,
                    )
                    continue
                subcats = self._cleanup_categories(parse_composer_response(data))
                parent_node = self._find_category_node(subcats, cid)
                if parent_node and parent_node.get("children"):
                    nested = self._clone_category_branch(
                        parent_node["children"], cid, seller_base,
                    )
                    if nested:
                        result[cid] = nested
                        continue
                result[cid] = self._extract_direct_children(
                    subcats, cid, page_category_id=cid,
                )
        except Exception as exc:
            if is_access_restricted(self.page) or is_blocked_page(self.page):
                log(f"Пакетный API категорий остановлен (блокировка): {exc}")
                return None
            log(f"Пакетный API категорий: {exc}")
        return result

    def _collect_flat_categories(self, nodes: list[dict]) -> list[dict]:
        flat: list[dict] = []

        def walk(items: list[dict]) -> None:
            for item in items:
                flat.append(item)
                walk(item.get("children") or [])

        walk(nodes)
        return flat

    def _find_category_node(self, nodes: list[dict], category_id: str) -> dict | None:
        category_id = str(category_id)
        for node in nodes:
            if str(node.get("id", "")) == category_id:
                return node
            if found := self._find_category_node(node.get("children") or [], category_id):
                return found
        return None

    def _sanitize_child_dicts(self, items: list[dict], parent_id: str) -> list[dict]:
        children: list[dict] = []
        seen: set[str] = set()
        for item in items:
            cid = str(item.get("id", ""))
            if not cid or cid == parent_id or cid in seen:
                continue
            if not is_valid_category(str(item.get("name", "")), cid):
                continue
            seen.add(cid)
            children.append(
                {
                    "id": cid,
                    "name": str(item.get("name", "")).strip(),
                    "url": item.get("url"),
                    "children": [],
                }
            )
        return children

    def _extract_direct_children(
        self,
        subcats: list[dict],
        parent_id: str,
        page_category_id: str | None = None,
    ) -> list[dict]:
        """Прямые потомки parent_id: из дерева или плоского списка страницы категории."""
        parent_id = str(parent_id)
        root_ids = getattr(self, "_seller_root_ids", set())

        parent_node = self._find_category_node(subcats, parent_id)
        if parent_node:
            nested = self._sanitize_child_dicts(parent_node.get("children") or [], parent_id)
            if nested:
                return nested

        # Parent absent => relationship is unproven. Never copy unrelated
        # categories into this branch.
        return []

    def _page_category_id(self) -> str | None:
        try:
            qs = parse_qs(urlparse(self.page.url).query)
            vals = qs.get("category") or qs.get("Category") or []
            return str(vals[0]) if vals else None
        except Exception:
            return None

    def _extract_categories_from_wb6_block(
        self,
        parent_id: str | None = None,
        *,
        force_page_category: bool = False,
    ) -> list[dict]:
        """Категории из блока wb6_7; для parent_id — подкатегории ниже родителя."""
        root_ids = list(getattr(self, "_seller_root_ids", set()))
        try:
            raw = self.page.evaluate(
                _CATEGORY_BLOCK_JS,
                {
                    "parentId": parent_id,
                    "blockClass": CATEGORY_BLOCK_CLASS,
                    "rootIds": root_ids,
                    "forcePageCategoryId": str(parent_id) if force_page_category and parent_id else None,
                },
            ) or []
            return self._clean_category_dicts(raw)
        except Exception:
            return []

    def _clean_category_dicts(self, raw: list[dict]) -> list[dict]:
        seller_base = self._route_url(self.page.url).split("?")[0]
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            cid = str(item.get("id", ""))
            name = str(item.get("name", ""))
            if not cid or cid in seen:
                continue
            if not is_valid_category(name, cid):
                continue
            seen.add(cid)
            href = item.get("url")
            url = self._route_url(urljoin(seller_base, href)) if href else None
            cleaned.append({"id": cid, "name": name, "url": url, "children": []})
        return cleaned

    def _prepare_category_filter_panel(self, log, fast: bool = True) -> None:
        self._wait_for_filters(log, fast=fast)
        self._open_category_filter(log, fast=fast)
        self._wait_for_category_links(fast=fast)
        try:
            self.page.evaluate(
                f"""
                () => {{
                    const block = document.querySelector('[class*="{CATEGORY_BLOCK_CLASS}"]');
                    if (block) block.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                }}
                """
            )
        except Exception:
            pass
        fast_delay(0.4, 0.75)

    def _extract_subcategories_from_category_menu(self, parent_id: str) -> list[dict]:
        return self._extract_categories_from_wb6_block(parent_id)

    def _extract_subcategories_from_dom(self, parent_id: str) -> list[dict]:
        return self._extract_subcategories_from_category_menu(parent_id)

    def _fetch_subcategories_via_api(
        self,
        seller_base: str,
        parent_id: str,
        log,
    ) -> list[dict]:
        batch = self._fetch_categories_batch(seller_base, [parent_id], log) or {}
        return batch.get(parent_id) or []

    def _wait_for_category_links(self, fast: bool = True) -> None:
        timeout = 5000 if fast else 10000
        for sel in (
            f'[class*="{CATEGORY_BLOCK_CLASS}"] a[href*="category="]',
            '[data-widget="filtersDesktop"] a[href*="category="]',
            '[data-widget="searchFilters"] a[href*="category="]',
        ):
            try:
                self.page.wait_for_selector(sel, timeout=timeout)
                return
            except Exception:
                continue
        fast_delay(0.3, 0.5)

    def _expand_category_subtree_in_filter(self, parent_id: str) -> None:
        """Раскрыть поддерево категории в фильтре (стрелки «ещё», «развернуть»)."""
        script = """
        (parentId) => {
            const pid = String(parentId);
            const block = document.querySelector('[class*="wb6_7"]')
                || document.querySelector('[data-widget="filtersDesktop"]')
                || document.querySelector('[data-widget="searchFilters"]');
            if (!block) return;

            const extractId = (href) => {
                if (!href) return null;
                let m = href.match(/[?&]category=(\\d+)/i);
                if (m) return m[1];
                m = href.match(/\\/category\\/[^/?#]*-(\\d+)\\/?(?:[?#]|$)/i);
                return m ? m[1] : null;
            };

            block.querySelectorAll('a[href*="category"]').forEach(a => {
                const href = a.href || a.getAttribute('href') || '';
                if (extractId(href) !== pid) return;
                let row = a.parentElement;
                for (let i = 0; i < 6 && row && row !== block; i++) {
                    const toggles = row.querySelectorAll('button, [role="button"], svg');
                    for (const t of toggles) {
                        if (t === a || t.contains(a)) continue;
                        try { t.click(); } catch (e) {}
                    }
                    row = row.parentElement;
                }
            });

            const showMore = ['ещё', 'еще', 'показать все', 'развернуть'];
            block.querySelectorAll('button, span, a').forEach(el => {
                const text = (el.textContent || '').trim().toLowerCase();
                if (showMore.some(s => text === s || text.startsWith(s))) {
                    try { if (el.offsetParent !== null) el.click(); } catch (e) {}
                }
            });
        }
        """
        try:
            self.page.evaluate(script, str(parent_id))
            fast_delay(0.35, 0.65)
        except Exception:
            pass

    def _expand_all_categories_in_filter(self) -> None:
        """Раскрыть всё дерево категорий в фильтре перед сбором иерархии."""
        script = f"""
        () => {{
            const block = document.querySelector('[class*="{CATEGORY_BLOCK_CLASS}"]')
                || document.querySelector('[data-widget="filtersDesktop"]')
                || document.querySelector('[data-widget="searchFilters"]');
            if (!block) return;
            const showMore = ['ещё', 'еще', 'показать все', 'развернуть'];
            for (let round = 0; round < 6; round++) {{
                let clicked = false;
                block.querySelectorAll('button, [role="button"]').forEach(el => {{
                    try {{
                        if (el.offsetParent !== null) {{ el.click(); clicked = true; }}
                    }} catch (e) {{}}
                }});
                block.querySelectorAll('span, a').forEach(el => {{
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (showMore.some(s => text === s || text.startsWith(s))) {{
                        try {{
                            if (el.offsetParent !== null) {{ el.click(); clicked = true; }}
                        }} catch (e) {{}}
                    }}
                }});
                if (!clicked) break;
            }}
        }}
        """
        try:
            self.page.evaluate(script)
            fast_delay(0.45, 0.8)
        except Exception:
            pass

    def _extract_full_subtree_composer(
        self,
        catalog_url: str,
        parent_id: str,
        seller_base: str,
        log,
    ) -> list[dict]:
        trees: list[list[dict]] = []
        for url in (catalog_url, self.page.url):
            data = self._fetch_composer_json(url)
            if not data:
                continue
            parsed = parse_composer_response(data)
            if parsed:
                trees.append(parsed)

        if not trees:
            return []

        merged = merge_category_trees(trees)
        parent_node = self._find_category_node(merged, parent_id)
        if parent_node and parent_node.get("children"):
            cloned = self._clone_category_branch(parent_node["children"], parent_id, seller_base)
            if cloned:
                return cloned

        # Without the parent node there is no proven hierarchy.
        return []

    def _clean_category_dicts_recursive(self, raw: list[dict]) -> list[dict]:
        seller_base = self._route_url(self.page.url).split("?")[0]
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            cid = str(item.get("id", ""))
            name = str(item.get("name", ""))
            if not cid or cid in seen:
                continue
            if not is_valid_category(name, cid):
                continue
            seen.add(cid)
            href = item.get("url")
            url = self._route_url(urljoin(seller_base, href)) if href else None
            cleaned.append(
                {
                    "id": cid,
                    "name": name,
                    "url": url,
                    "children": self._clean_category_dicts_recursive(item.get("children") or []),
                }
            )
        return cleaned

    def _extract_full_subtree_dom(self, parent_id: str) -> list[dict]:
        root_ids = list(getattr(self, "_seller_root_ids", set()))
        try:
            raw = self.page.evaluate(
                _CATEGORY_FULL_SUBTREE_JS,
                {
                    "parentId": parent_id,
                    "blockClass": CATEGORY_BLOCK_CLASS,
                    "rootIds": root_ids,
                    "forcePageCategoryId": str(parent_id),
                },
            ) or []
            return self._clean_category_dicts_recursive(raw)
        except Exception:
            return []

    def _fetch_category_subtree(
        self,
        seller_base: str,
        parent: dict,
        log,
        on_manual_bypass,
    ) -> list[dict]:
        """Один заход на страницу категории — полное дерево подкатегорий."""
        parent_id = str(parent["id"])
        catalog_url = self._build_category_url(seller_base, parent_id)
        try:
            if not safe_goto(self.page, catalog_url, log, on_manual_bypass=on_manual_bypass):
                log(f"  не удалось открыть категорию {parent_id}")
                return []
            fast_delay(0.5, 1.0)
            self._prepare_category_filter_panel(log, fast=True)
            self._expand_all_categories_in_filter()
            self._expand_category_subtree_in_filter(parent_id)

            subtree = self._extract_full_subtree_composer(
                catalog_url, parent_id, seller_base, log,
            )
            if subtree:
                return self._cleanup_categories(subtree)

            for attempt in range(3):
                self._expand_all_categories_in_filter()
                self._expand_category_subtree_in_filter(parent_id)
                subtree = self._extract_full_subtree_dom(parent_id)
                if subtree:
                    return self._cleanup_categories(subtree)
                fast_delay(0.4, 0.7)
                self._open_category_filter(log, fast=True)

            from_page = self._extract_categories_from_page_json()
            if from_page:
                parent_node = self._find_category_node(from_page, parent_id)
                if parent_node and parent_node.get("children"):
                    cloned = self._clone_category_branch(
                        parent_node["children"], parent_id, seller_base,
                    )
                    if cloned:
                        return self._cleanup_categories(cloned)

            link_count = self.page.evaluate(
                f"""() => {{
                    const b = document.querySelector('[class*="{CATEGORY_BLOCK_CLASS}"]');
                    return b ? b.querySelectorAll('a[href*="category"]').length : 0;
                }}"""
            )
            log(f"  подкатегории не найдены (ссылок в фильтре: {link_count})")
            return []
        except Exception as exc:
            log(f"  ошибка сбора иерархии: {exc}")
            return []

    def _fetch_subcategories_on_category_page(
        self,
        seller_base: str,
        parent: dict,
        log,
        on_manual_bypass,
    ) -> list[dict]:
        """Сбор только прямых подкатегорий (верхний уровень)."""
        subtree = self._fetch_category_subtree(seller_base, parent, log, on_manual_bypass)
        return self._sanitize_child_dicts(subtree, str(parent["id"]))

    def _fetch_subcategories_nav_fallback(
        self,
        seller_base: str,
        parent: dict,
        log,
        on_manual_bypass,
    ) -> list[dict]:
        return self._fetch_subcategories_on_category_page(
            seller_base, parent, log, on_manual_bypass,
        )

    def load_subcategories_for_categories(
        self,
        seller_url: str,
        categories: list[CategoryTarget],
        progress: Callable[[str], None] | None = None,
        on_manual_bypass=None,
    ) -> dict[str, list[FilterOptionNode]]:
        log = progress or (lambda _m: None)
        seller_base = normalize_seller_url(seller_url, self.browser_mode)
        result: dict[str, list[FilterOptionNode]] = {}

        unique: dict[str, CategoryTarget] = {}
        for cat in categories:
            cid = cat.param_value or cat.category_id
            if cid:
                unique[cid] = cat

        total = len(unique)
        for idx, (cid, category) in enumerate(unique.items(), start=1):
            log(f"Подкатегории {idx}/{total}: {category.name}")
            parent = {
                "id": cid,
                "name": category.name,
                "url": category.url,
            }
            children = self._load_subcategories(
                seller_base,
                parent,
                depth=1,
                log=log,
                on_manual_bypass=on_manual_bypass,
                recursive=True,
            )
            if children:
                result[cid] = children
                log(f"  → {len(children)} подкатегорий")
            else:
                log("  → подкатегории не найдены")

        return result

    def load_filters_for_categories(
        self,
        seller_url: str,
        categories: list[CategoryTarget],
        progress: Callable[[str], None] | None = None,
        on_manual_bypass=None,
    ) -> list[FilterSection]:
        log = progress or (lambda _m: None)
        seller_base = normalize_seller_url(seller_url, self.browser_mode)
        grouped: list[FilterSection] = []

        for idx, category in enumerate(categories, start=1):
            label = f"{category.parent_name} → {category.name}" if category.parent_name else category.name
            log(f"Фильтры {idx}/{len(categories)}: {label}")

            catalog_url = category.url or self._build_category_url(seller_base, category.param_value)
            if not safe_goto(self.page, catalog_url, log, on_manual_bypass=on_manual_bypass):
                if not recover_access(self.page, log, on_manual_bypass, catalog_url):
                    log(f"Не удалось открыть: {label}")
                    continue
                if not safe_goto(self.page, catalog_url, log, on_manual_bypass=on_manual_bypass):
                    continue

            self._wait_for_filters(log)
            self._expand_filter_sections(log)

            from_dom = self._extract_filter_sections_from_dom()
            from_api = self._extract_filter_sections_from_composer(catalog_url, seller_base.split("?")[0], log)
            sections = merge_filter_sections([from_dom, from_api])
            sections = attach_category_context(sections, category.param_value, label)

            if sections:
                grouped.append(FilterSection(title=f"— {label} —", options=[]))
                grouped.extend(sections)
            else:
                log(f"Фильтры не найдены для: {label}")

        return grouped

    def load_from_seller(
        self,
        seller_url: str,
        progress: Callable[[str], None] | None = None,
        on_manual_bypass=None,
    ) -> list[FilterSection]:
        tree = self.load_root_categories(seller_url, progress, on_manual_bypass)
        if not tree:
            return []
        return [FilterSection(title="Категория", options=tree)]

    def _build_category_url(self, seller_base: str, category_id: str) -> str:
        """Build URL for a category node.

        - Seller shops: /seller/...?category=ID (filter on shop page)
        - Global tree Composer API: /category/ID/ (returns subcategory hierarchy)
        """
        parsed = urlparse(seller_base)
        scheme = parsed.scheme or "https"
        host = parsed.netloc or urlparse(self._catalog_base_url()).netloc
        path_lower = (parsed.path or "").lower()
        if "/seller/" in path_lower:
            path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
            query = urlencode({"category": category_id})
            return urlunparse((scheme, host, path, "", query, ""))
        # Global catalogue Composer endpoint — required for subcategory trees.
        path = f"{GLOBAL_CATALOG_PATH.rstrip('/')}/{category_id}/"
        return urlunparse((scheme, host, path, "", "", ""))

    def _page_access_blocked(self) -> bool:
        try:
            return is_access_restricted(self.page)
        except Exception:
            return False

    def _ensure_page_ready_for_catalog(self, log, on_manual_bypass) -> bool:
        if page_has_usable_ozon_content(self.page) and not self._page_access_blocked():
            return True
        if wait_for_ozon_ready(self.page, log):
            return True
        if is_antibot_challenge_page(self.page):
            log("Ozon проверяет браузер перед загрузкой каталога...")
            return recover_access(
                self.page,
                log,
                on_manual_bypass,
            ) and wait_for_ozon_ready(self.page, log)
        return False

    def _global_catalog_access_error(self, detail: str = "") -> ConnectionError:
        incident = ""
        try:
            incident = extract_incident_id(self.page) or ""
        except Exception:
            incident = ""
        blocked = self._page_access_blocked()
        parts = []
        if blocked:
            parts.append(
                "Ozon временно ограничил доступ к каталогу "
                "(проверка браузера, блокировка или captcha)."
            )
            if incident:
                parts.append(f"Инцидент: {incident}")
            parts.append(
                "Подождите 15–30 минут, обновите страницу в Chrome один раз, "
                "пройдите проверку при необходимости, затем снова нажмите "
                "«Загрузить категории»."
            )
        else:
            parts.append(detail or "Общий каталог не вернул корневые категории.")
            parts.append(
                "Chrome откроется при «Загрузить категории». "
                "Убедитесь, что сайт загружается без блокировки, затем повторите."
            )
        return ConnectionError("\n".join(parts))

    def _load_global_root_categories(self, log, on_manual_bypass) -> list[dict]:
        """Try several catalog entry points until root categories appear."""
        base = self._catalog_base_url()
        # Prefer /seller/ first — /category/ triggers fab_ more often.
        candidates = [
            base.rstrip("/") + ALL_SELLERS_PATH,
            base.rstrip("/") + "/",
            base.rstrip("/") + GLOBAL_CATALOG_PATH,
        ]

        if self._page_access_blocked():
            log("Обнаружена блокировка Ozon перед загрузкой каталога...")
            if not recover_access(
                self.page,
                log,
                on_manual_bypass,
                candidates[0],
            ):
                return []
            if not self._ensure_page_ready_for_catalog(log, on_manual_bypass):
                return []

        # If Chrome already shows a usable Ozon page (often with leftover
        # ?__rr=1&abt_att=1), collect roots without another navigation.
        try:
            current = str(getattr(self.page, "url", "") or "")
        except Exception:
            current = ""
        if (
            "ozon.ru" in current.lower()
            and not self._page_access_blocked()
            and page_has_usable_ozon_content(self.page)
        ):
            log("Собираем корневые категории с уже открытой страницы...")
            roots = self._collect_root_categories(log)
            if len(roots) >= 2:
                log(f"Найдено корневых категорий: {len(roots)}")
                return roots

        for source_url in candidates:
            if self._page_access_blocked():
                log("Каталог недоступен из‑за блокировки Ozon — остановка без лишних переходов")
                return []

            log(f"Открываем каталог: {source_url}")
            if not safe_goto(
                self.page,
                source_url,
                log,
                on_manual_bypass=on_manual_bypass,
            ):
                if self._page_access_blocked():
                    return []
                continue
            if self._page_access_blocked():
                log("Страница каталога недоступна — Ozon проверяет браузер или заблокировал доступ")
                if not recover_access(
                    self.page,
                    log,
                    on_manual_bypass,
                    source_url,
                ):
                    return []
                if not wait_for_ozon_ready(self.page, log):
                    return []

            roots = self._collect_root_categories(log)
            if len(roots) < 2 and is_antibot_challenge_page(self.page):
                log("Корневые категории не найдены — ждём завершения проверки Ozon...")
                if wait_for_ozon_ready(self.page, log):
                    roots = self._collect_root_categories(log)
            if len(roots) >= 2:
                log(f"Найдено корневых категорий: {len(roots)}")
                return roots

            # Extra pass via category-filter collector used for seller trees.
            try:
                collector = SellerCategoryCollector(
                    self.page,
                    source_url,
                    log=log,
                    on_manual_bypass=on_manual_bypass,
                    browser_mode=self.browser_mode,
                )
                filter_roots = collector._collect_direct_children(None, source_url)
                cleaned = self._cleanup_categories(filter_roots)
                if len(cleaned) >= 2:
                    log(
                        f"Корневые категории получены из фильтра каталога: "
                        f"{len(cleaned)}"
                    )
                    return cleaned
            except Exception as exc:
                log(f"Фильтр каталога: {exc}")

        return []

    def _collect_root_categories(self, log) -> list[dict]:
        from_dom = self._extract_categories_from_dom()
        from_api = self._extract_categories_from_composer_api(self.page.url, log)
        categories = merge_category_trees([from_dom, from_api])

        if len(categories) < 2:
            from_page = self._extract_categories_from_page_json()
            categories = merge_category_trees([categories, from_page])

        return self._cleanup_categories(categories)

    def _load_subcategories(
        self,
        seller_base: str,
        parent: dict,
        depth: int,
        log,
        on_manual_bypass,
        recursive: bool = False,
    ) -> list[FilterOptionNode]:
        if depth > self.MAX_SUBCATEGORY_DEPTH:
            return []

        parent_name = str(parent.get("name", ""))
        parent["children"] = []

        if recursive:
            self._collect_category_branch_whole(
                seller_base,
                parent,
                log=log,
                on_manual_bypass=on_manual_bypass,
            )
        else:
            parent["children"] = self._fetch_subcategories_on_category_page(
                seller_base, parent, log, on_manual_bypass,
            )

        child_nodes: list[FilterOptionNode] = []
        for item in parent.get("children", []):
            if node := self._dict_to_option_tree(item, parent_name=parent_name):
                child_nodes.append(node)
        return child_nodes

    def _nested_dict_children(
        self,
        seller_base: str,
        parent: dict,
        depth: int,
        log,
        on_manual_bypass,
    ) -> list[dict]:
        if depth > self.MAX_SUBCATEGORY_DEPTH:
            return []
        self._collect_category_branch_whole(
            seller_base,
            parent,
            log=log,
            on_manual_bypass=on_manual_bypass,
        )
        return parent.get("children") or []

    def _wait_for_filters(self, log, fast: bool = False) -> None:
        log("Ожидание фильтров каталога...")
        timeout = 4000 if fast else 8000
        for sel in (
            '[data-widget="filtersDesktop"]',
            '[data-widget="searchFilters"]',
            '[data-widget="filters"]',
        ):
            try:
                self.page.wait_for_selector(sel, timeout=timeout)
                return
            except Exception:
                continue
        if fast:
            fast_delay(0.3, 0.6)
        else:
            human_delay(2.0, 3.0)

    def _expand_filter_sections(self, log) -> None:
        for sel in (
            '[data-widget="filtersDesktop"] span',
            '[data-widget="filtersDesktop"] button',
            '[data-widget="searchFilters"] span',
            '[data-widget="searchFilters"] button',
        ):
            try:
                for el in self.page.query_selector_all(sel)[:40]:
                    text = (el.inner_text() or "").strip().lower()
                    if text in ("категория", "категории", "тип", "бренд", "цвет", "размер", "материал"):
                        if el.is_visible():
                            el.click()
                            human_delay(0.3, 0.6)
            except Exception:
                continue

    def _open_category_filter(self, log, fast: bool = False) -> None:
        for sel in (
            '[data-widget="filtersDesktop"] span:has-text("Категория")',
            '[data-widget="filtersDesktop"] span:has-text("Категории")',
            '[data-widget="searchFilters"] span:has-text("Категория")',
            'button:has-text("Категория")',
        ):
            try:
                el = self.page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    if fast:
                        fast_delay(0.2, 0.4)
                    else:
                        human_delay(0.8, 1.5)
                    break
            except Exception:
                continue

    def _fetch_composer_json(self, page_url: str) -> dict | None:
        path = urlparse(page_url).path
        if not path.endswith("/"):
            path += "/"
        full_url = self._route_url(page_url)
        client_name = "mweb_client" if self.browser_mode == MOBILE_MODE else "dweb_client"
        script = """
        async ({ path, fullUrl, clientName }) => {
            const candidates = [
                '/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(path),
                '/api/composer-api.bx/page/json/v2?url=' + encodeURIComponent(fullUrl),
            ];
            for (const apiUrl of candidates) {
                try {
                    const resp = await fetch(apiUrl, {
                        credentials: 'include',
                        headers: { 'Accept': 'application/json', 'x-o3-app-name': clientName },
                    });
                    if (!resp.ok) continue;
                    return JSON.stringify(await resp.json());
                } catch (e) {}
            }
            return null;
        }
        """
        try:
            raw = self.page.evaluate(
                script,
                {"path": path, "fullUrl": full_url, "clientName": client_name},
            )
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _extract_filter_sections_from_composer(self, page_url: str, seller_base: str, log) -> list[FilterSection]:
        try:
            data = self._fetch_composer_json(page_url)
            if not data:
                return []
            return parse_filter_sections_from_composer(data, seller_base)
        except Exception as exc:
            log(f"Composer API фильтры: {exc}")
            return []

    def _extract_filter_sections_from_dom(self) -> list[FilterSection]:
        try:
            raw = self.page.evaluate(DOM_FILTER_SECTIONS_SCRIPT) or []
            return extract_filter_sections_from_dom(self.page.url, raw)
        except Exception:
            return []

    def _extract_categories_from_composer_api(self, page_url: str, log) -> list[dict]:
        try:
            data = self._fetch_composer_json(page_url)
            if not data:
                return []
            return parse_composer_response(data)
        except Exception as exc:
            log(f"Composer API категории: {exc}")
            return []

    def _extract_categories_from_page_json(self) -> list[dict]:
        html = self.page.content()
        blocks = extract_json_blocks(html)
        trees = [find_category_nodes(block) for block in blocks]
        return merge_category_trees(trees)

    def _extract_categories_from_dom(self) -> list[dict]:
        from_wb6 = self._extract_categories_from_wb6_block(parent_id=None)
        if from_wb6:
            return from_wb6
        script = """
        () => {
            const results = [];
            const seen = new Set();
            const containers = [
                '[data-widget="filtersDesktop"]',
                '[data-widget="searchFilters"]',
                '[data-widget="filters"]',
            ];
            const roots = [];
            containers.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => roots.push(el));
            });
            if (!roots.length) return results;

            const add = (name, id, href, depth) => {
                name = (name || '').trim().replace(/\\s+/g, ' ');
                if (!name || !id) return;
                if (!/^\\d+$/.test(String(id))) return;
                const key = id + '|' + name;
                if (seen.has(key)) return;
                seen.add(key);
                results.push({ id: String(id), name, url: href || null, depth: depth || 0, children: [] });
            };

            roots.forEach(root => {
                root.querySelectorAll('a[href]').forEach(el => {
                    const name = el.textContent || el.getAttribute('title') || '';
                    const href = el.href || el.getAttribute('href') || '';
                    const m = href.match(/[?&]category=(\\d+)/i);
                    if (!m) return;
                    let depth = 0;
                    let node = el.parentElement;
                    while (node && node !== root) {
                        depth += 1;
                        node = node.parentElement;
                    }
                    add(name, m[1], href, depth);
                });
            });

            const sorted = [...results].sort((a, b) => a.depth - b.depth);
            const tree = [];
            sorted.forEach(item => {
                const parent = [...sorted].reverse().find(
                    p => p.id !== item.id && p.depth < item.depth
                );
                if (parent && parent.depth === item.depth - 1) {
                    parent.children.push(item);
                } else if (item.depth <= 1) {
                    tree.push(item);
                } else if (!tree.some(t => t.id === item.id)) {
                    tree.push(item);
                }
            });
            return tree.length ? tree : results;
        }
        """
        try:
            raw = self.page.evaluate(script) or []
            seller_base = self._route_url(self.page.url).split("?")[0]
            cleaned = []
            for item in raw:
                if not is_valid_category(item["name"], item["id"]):
                    continue
                href = item.get("url")
                if href:
                    item["url"] = self._route_url(urljoin(seller_base, href))
                cleaned.append(item)
            return cleaned
        except Exception:
            return []

    def _extract_categories_from_dom_legacy(self, parent_id: str) -> list[dict]:
        """Запасной сбор из фильтра: пункты ниже родителя по DOM-глубине."""
        script = """
        (parentId) => {
            const parentId = String(parentId);
            const containers = [
                '[data-widget="filtersDesktop"]',
                '[data-widget="searchFilters"]',
                '[data-widget="filters"]',
            ];
            const roots = [];
            containers.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => roots.push(el));
            });
            if (!roots.length) return [];

            const items = [];
            const seen = new Set();
            roots.forEach(root => {
                root.querySelectorAll('a[href*="category="]').forEach(el => {
                    const href = el.href || el.getAttribute('href') || '';
                    const m = href.match(/[?&]category=(\\d+)/i);
                    if (!m) return;
                    const id = m[1];
                    if (seen.has(id)) return;
                    const name = (el.textContent || el.getAttribute('title') || '').trim();
                    if (!name) return;
                    seen.add(id);
                    let depth = 0;
                    let node = el.parentElement;
                    while (node && node !== root) {
                        depth += 1;
                        node = node.parentElement;
                    }
                    items.push({ id, name, url: href, depth });
                });
            });

            const pIdx = items.findIndex(i => i.id === parentId);
            if (pIdx >= 0) {
                const pDepth = items[pIdx].depth;
                const out = [];
                for (let i = pIdx + 1; i < items.length; i++) {
                    if (items[i].depth <= pDepth) break;
                    if (items[i].depth === pDepth + 1) out.push(items[i]);
                }
                if (out.length) return out;
                for (let i = pIdx + 1; i < items.length; i++) {
                    if (items[i].depth <= pDepth) break;
                    out.push(items[i]);
                }
                return out;
            }

            const urlMatch = window.location.href.match(/[?&]category=(\\d+)/i);
            if (urlMatch && urlMatch[1] === parentId) {
                return items.filter(i => i.id !== parentId);
            }
            return [];
        }
        """
        try:
            raw = self.page.evaluate(script, str(parent_id)) or []
            return self._clean_category_dicts(raw)
        except Exception:
            return []

    def _cleanup_categories(self, items: list[dict]) -> list[dict]:
        seen: set[str] = set()

        def walk(node: dict) -> dict | None:
            nid = str(node.get("id", "")).strip()
            name = str(node.get("name", "")).strip()
            if not is_valid_category(name, nid) or nid in seen:
                return None
            seen.add(nid)
            children = []
            for child in node.get("children", []):
                converted = walk(child)
                if converted:
                    children.append(converted)
            return {"id": nid, "name": name, "url": node.get("url"), "children": children}

        result: list[dict] = []
        for item in items:
            converted = walk(item)
            if converted:
                result.append(converted)
        return result

    def _category_dict_to_option(self, data: dict, parent_name: str = "") -> FilterOptionNode | None:
        nid = str(data.get("id", "")).strip()
        name = str(data.get("name", "")).strip()
        if not is_valid_category(name, nid):
            return None
        display = f"{parent_name} → {name}" if parent_name else name
        return FilterOptionNode(
            id=f"Категория|category:{nid}",
            name=name,
            url=data.get("url"),
            param_key="category",
            param_value=nid,
            category_id=nid,
            category_name=display,
            parent_name=parent_name,
        )
