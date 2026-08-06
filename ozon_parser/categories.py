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
    CATEGORY_PAGE_SETTLE_SEC,
    CATEGORY_VIEW_ALL_PAUSE_SEC,
    DESKTOP_BASE_URL,
    DESKTOP_MODE,
    GLOBAL_CATALOG_PATH,
    GLOBAL_CATEGORY_PAGE_MAX_DEPTH,
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
    SELLER_CATEGORY_PAGE_MAX_DEPTH,
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
# Ozon truncates the category filter; these controls reveal the full list.
# Keep «ещё» for generic expand helpers, but category view-all prefers exact phrases.
CATEGORY_SHOW_MORE_TEXTS = (
    "посмотреть все",
    "показать все",
    "смотреть все",
    "ещё",
    "еще",
    "развернуть",
    "показать ещё",
    "показать еще",
)
CATEGORY_VIEW_ALL_TEXTS = (
    "посмотреть все",
    "показать все",
    "смотреть все",
    "все категории",
)
# Filter sections that also have «Посмотреть все» — never click those for categories.
CATEGORY_VIEW_ALL_EXCLUDE_SECTIONS = (
    "бренд",
    "цвет",
    "тип",
    "размер",
    "материал",
    "продавец",
    "серия",
    "распродажа",
    "доставка",
    "цена",
    "скидка",
    "отзыв",
)

# Shared browser JS: resolve Ozon category IDs from hrefs (incl. seller path URLs).
_CATEGORY_LINK_UTILS_JS = """
const extractCategoryId = (href) => {
    if (!href) return null;
    let m = String(href).match(/[?&]category=(\\d+)/i);
    if (m) return m[1];
    m = String(href).match(/\\/category\\/[^/?#]*-(\\d+)\\/?(?:[?#]|$)/i);
    if (m) return m[1];
    m = String(href).match(/\\/category\\/(\\d+)\\/?(?:[?#]|$)/i);
    if (m) return m[1];
    // Shop pages use /seller/{shop}/{slug}-{id}/ without the word "category".
    m = String(href).match(/\\/seller\\/[^/?#]+\\/[^/?#]*-(\\d+)\\/?(?:[?#]|$)/i);
    if (m) return m[1];
    return null;
};
const countCategoryAnchors = (root) => {
    let n = 0;
    if (!root) return 0;
    root.querySelectorAll('a[href]').forEach((a) => {
        if (extractCategoryId(a.getAttribute('href') || a.href || '')) n += 1;
    });
    return n;
};
const pageCategoryIdFromLocation = () => {
    const href = window.location.href || '';
    const path = window.location.pathname || '';
    let m = href.match(/[?&]category=(\\d+)/i);
    if (m) return m[1];
    m = path.match(/\\/seller\\/[^/]+\\/[^/]*-(\\d+)\\/?$/i);
    if (m) return m[1];
    m = path.match(/\\/category\\/[^/]*-(\\d+)\\/?$/i);
    if (m) return m[1];
    m = path.match(/\\/category\\/(\\d+)\\/?$/i);
    return m ? m[1] : null;
};
"""

# Shared browser JS: locate only the Категория filter container (not Brand/Color/…).
_CATEGORY_SECTION_FINDER_JS = """
const normalize = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const firstLine = (t) => normalize(t).split('\\n')[0].trim();
const isViewAllText = (text, viewAll) => {
    const t = normalize(text);
    if (!t || t.length > 40) return false;
    return viewAll.some((s) => t === s || t.startsWith(s));
};
const isExcludedTitle = (title, excludeSections) =>
    excludeSections.some((s) => title === s || title.startsWith(s + ' '));
const findCategorySections = (blockClass, excludeSections) => {
    const out = [];
    const seen = new Set();
    const push = (el) => {
        if (!el || seen.has(el)) return;
        seen.add(el);
        out.push(el);
    };
    // Primary: dedicated category CSS-module block.
    document.querySelectorAll('[class*="' + blockClass + '"]').forEach(push);
    // Secondary: smallest filter chunk whose title line is exactly Категория/Категории.
    const roots = document.querySelectorAll(
        '[data-widget="filtersDesktop"], [data-widget="searchFilters"], [data-widget="filters"]'
    );
    roots.forEach((root) => {
        root.querySelectorAll('div, section, fieldset, li, aside').forEach((el) => {
            const title = firstLine(el.innerText || '');
            if (title !== 'категория' && title !== 'категории') return;
            // Must contain category links and preferably a view-all control.
            const links = countCategoryAnchors(el);
            if (links < 1) return;
            push(el);
        });
    });
    return out.filter((el) => {
        const title = firstLine(el.innerText || '');
        // Reject containers that are clearly another filter (brand/color/…).
        if (isExcludedTitle(title, excludeSections)) return false;
        // Accept wb6 blocks and title=Категория chunks only.
        const cls = String(el.className || '');
        if (cls.includes(blockClass)) return true;
        return title === 'категория' || title === 'категории';
    });
};
const findViewAllInCategorySections = (viewAll, excludeSections, blockClass) => {
    const sections = findCategorySections(blockClass, excludeSections);
    for (const scope of sections) {
        const candidates = Array.from(scope.querySelectorAll('button, a, [role="button"]'));
        for (const el of candidates) {
            const text = el.innerText || el.textContent || '';
            if (!isViewAllText(text, viewAll)) continue;
            // Ensure the button itself is not under a nested excluded subsection.
            let p = el;
            let nestedOk = true;
            for (let i = 0; i < 8 && p && p !== scope; i++) {
                const title = firstLine(p.innerText || '');
                if (isExcludedTitle(title, excludeSections)) {
                    nestedOk = false;
                    break;
                }
                p = p.parentElement;
            }
            if (!nestedOk) continue;
            try {
                const style = window.getComputedStyle(el);
                const visible = el.offsetParent !== null
                    || style.position === 'fixed'
                    || style.position === 'sticky';
                if (!visible) continue;
            } catch (e) { continue; }
            return { el, scope, text: normalize(text) };
        }
    }
    return null;
};
"""

# Prepend link utils so section finder can count seller-path category anchors.
_CATEGORY_SECTION_FINDER_JS = _CATEGORY_LINK_UTILS_JS + _CATEGORY_SECTION_FINDER_JS


_CATEGORY_BLOCK_JS = """
(args) => {
""" + _CATEGORY_LINK_UTILS_JS + """
    const parentId = args.parentId ? String(args.parentId) : null;
    const rootIds = new Set((args.rootIds || []).map(String));
    const blockClass = args.blockClass || 'wb6_7';

    const findCategoryBlock = () => {
        const candidates = [];
        const push = (el) => { if (el) candidates.push(el); };
        // Prefer expanded modal/drawer after «Посмотреть все» / «Все категории».
        document.querySelectorAll(
            '[role="dialog"], [aria-modal="true"], [data-widget*="modal" i], ' +
            '[data-widget*="Modal" i], [data-widget*="drawer" i], [data-widget*="Sheet" i], ' +
            '[class*="modal" i], [class*="drawer" i], [class*="popup" i]'
        ).forEach(push);
        document.querySelectorAll('[class*="' + blockClass + '"]').forEach(push);
        document.querySelectorAll('[data-widget="filtersDesktop"], [data-widget="searchFilters"], [data-widget="filters"]').forEach(w => {
            push(w);
            w.querySelectorAll('[class*="' + blockClass + '"]').forEach(push);
        });
        let best = null;
        let maxLinks = 0;
        for (const b of candidates) {
            const count = countCategoryAnchors(b);
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

    block.querySelectorAll('a[href]').forEach(a => {
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
    const pageCategoryId = args.forcePageCategoryId
        ? String(args.forcePageCategoryId)
        : pageCategoryIdFromLocation();

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
        // Prefer indent (visible hierarchy in modal); fall back to DOM depth.
        const minIndent = Math.min(...links.map(l => l.xIndent));
        let roots = links.filter(l => l.xIndent === minIndent);
        if (roots.length < 2) {
            const minDepth = Math.min(...links.map(l => l.domDepth));
            roots = links.filter(l => l.domDepth === minDepth);
        }
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
        // Never promote sibling shop roots to children of the current category.
        const nonRoots = notParent.filter(l => !rootIds.has(l.id));
        if (nonRoots.length) return nonRoots.map(mapLink);
        return [];
    }

    const parentIdx = links.findIndex(l => l.id === parentId);
    if (parentIdx >= 0) {
        const children = pickDirectChildren(parentIdx).filter(c => !rootIds.has(c.id) || c.id === parentId);
        if (children.length) return children.map(mapLink);
    }

    return [];
}
"""

_CATEGORY_FULL_SUBTREE_JS = """
(args) => {
""" + _CATEGORY_LINK_UTILS_JS + """
    const parentId = args.parentId ? String(args.parentId) : null;
    const rootIds = new Set((args.rootIds || []).map(String));
    const blockClass = args.blockClass || 'wb6_7';
    const forcePageCategoryId = args.forcePageCategoryId ? String(args.forcePageCategoryId) : null;

    const findCategoryBlock = () => {
        const candidates = [];
        const push = (el) => { if (el) candidates.push(el); };
        // Prefer expanded modal/drawer after «Посмотреть все» — it has the full list.
        document.querySelectorAll(
            '[role="dialog"], [aria-modal="true"], [data-widget*="modal" i], ' +
            '[data-widget*="Modal" i], [data-widget*="drawer" i], [data-widget*="Sheet" i], ' +
            '[class*="modal" i], [class*="drawer" i], [class*="popup" i]'
        ).forEach(push);
        document.querySelectorAll('[class*="' + blockClass + '"]').forEach(push);
        document.querySelectorAll('[data-widget="filtersDesktop"], [data-widget="searchFilters"], [data-widget="filters"]').forEach(w => {
            push(w);
            w.querySelectorAll('[class*="' + blockClass + '"]').forEach(push);
        });
        let best = null;
        let maxLinks = 0;
        for (const b of candidates) {
            const count = countCategoryAnchors(b);
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

    const blockLeft = block.getBoundingClientRect().left || 0;
    const links = [];
    const seen = new Set();

    block.querySelectorAll('a[href]').forEach(a => {
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

    const pageCategoryId = forcePageCategoryId || pageCategoryIdFromLocation();

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
    }
    // Modal after «Посмотреть все» often lists only children (parent link absent).
    // Never fall back to "all non-parent links" — that promotes sibling shop roots
    // (e.g. «Красота и здоровье» under «Электроника»).
    if (!slice.length && (pageCategoryId === parentId || forcePageCategoryId === parentId)) {
        slice = links.filter(l => l.id !== parentId && !rootIds.has(l.id));
    }
    if (!slice.length && parentIdx < 0) {
        slice = links.filter(l => l.id !== parentId && !rootIds.has(l.id));
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

    // Indent-based stack: only nest when visibly indented under the previous row.
    // Depth-only nesting was promoting siblings/grandchildren incorrectly.
    for (const n of normalized) {
        while (stack.length && n.indent <= stack[stack.length - 1].indent) {
            stack.pop();
        }
        const node = { id: n.id, name: n.name, url: n.url, children: [] };
        if (!stack.length) {
            roots.push(node);
        } else {
            stack[stack.length - 1].node.children.push(node);
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
    seller_scope: str = ""


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
        # 0 = fastest safe pace; rises after fab_/blocks, cools on success.
        self._pace_heat = 0
        self._last_fetch_api_children: list[dict] = []

    def _note_tree_ok(self) -> None:
        if self._pace_heat > 0:
            self._pace_heat -= 1

    def _note_tree_pressure(self, amount: int = 2) -> None:
        self._pace_heat = min(6, self._pace_heat + max(1, amount))

    def _scaled_pause(self, lo: float, hi: float) -> None:
        """Human-like pause; stretches when Ozon pressure (heat) is high."""
        factor = 1.0 + 0.4 * self._pace_heat
        human_delay(lo * factor, hi * factor)

    def _pause_sec(self, seconds: float) -> None:
        ms = max(0, int(float(seconds) * 1000))
        if ms <= 0:
            return
        try:
            self.page.wait_for_timeout(ms)
        except Exception:
            time.sleep(seconds)

    def _pause_after_page_open(self) -> None:
        self._pause_sec(CATEGORY_PAGE_SETTLE_SEC)

    def _pause_after_view_all_click(self) -> None:
        self._pause_sec(CATEGORY_VIEW_ALL_PAUSE_SEC)

    def _batch_tree_pause(self) -> None:
        self._scaled_pause(
            GLOBAL_COMPOSER_BATCH_PAUSE_MIN,
            GLOBAL_COMPOSER_BATCH_PAUSE_MAX,
        )

    def _long_tree_pause(self) -> None:
        self._scaled_pause(
            GLOBAL_COMPOSER_LONG_PAUSE_MIN,
            GLOBAL_COMPOSER_LONG_PAUSE_MAX,
        )

    def _root_tree_pause(self) -> None:
        self._scaled_pause(
            GLOBAL_COMPOSER_ROOT_PAUSE_MIN,
            GLOBAL_COMPOSER_ROOT_PAUSE_MAX,
        )

    def _page_blocked(self) -> bool:
        return is_access_restricted(self.page) or is_blocked_page(self.page)

    def _catalog_base_url(self) -> str:
        if (
            self.browser_mode == MOBILE_MODE
            and self.session_mode not in MOBILE_DESKTOP_HOST_SESSIONS
        ):
            return MOBILE_BASE_URL
        return DESKTOP_BASE_URL

    def _is_seller_shop_base(self, seller_base: str) -> bool:
        """True only for a concrete shop URL (/seller/{slug}/), not /seller or /seller/0."""
        return self._seller_shop_key(seller_base) is not None

    @staticmethod
    def _seller_shop_key(url: str) -> str | None:
        parts = [p for p in (urlparse(url).path or "").lower().split("/") if p]
        if len(parts) < 2 or parts[0] != "seller":
            return None
        shop = parts[1]
        if not shop or shop == "0":
            return None
        return shop

    def _ensure_seller_shop_open(self, seller_url: str, log, on_manual_bypass) -> None:
        url = self._route_url(seller_url)
        target = self._seller_shop_key(url)
        current = self._seller_shop_key(self.page.url or "")
        if (
            target
            and target == current
            and not is_access_restricted(self.page)
            and not is_blocked_page(self.page)
        ):
            log("Используем открытую страницу магазина")
            return
        log("Открываем страницу магазина...")
        if not safe_goto(self.page, url, log, on_manual_bypass=on_manual_bypass):
            if not recover_access(self.page, log, on_manual_bypass, target_url=url):
                raise ConnectionError(
                    "Ozon заблокировал доступ. Подождите 15–30 минут, "
                    "откройте Chrome один раз и повторите."
                )
            if not safe_goto(self.page, url, log, on_manual_bypass=on_manual_bypass):
                raise ConnectionError("Не удалось открыть страницу магазина.")
        self._pause_after_page_open()

    def _seller_filter_collector(self, seller_base: str) -> SellerCategoryCollector:
        base = seller_base
        if not self._is_seller_shop_base(base):
            base = self._route_url(self.page.url).split("?")[0]
        collector = SellerCategoryCollector(
            self.page,
            base,
            browser_mode=self.browser_mode,
        )
        collector._root_ids = set(getattr(self, "_seller_root_ids", set()))
        return collector

    def _extract_seller_filter_children(
        self,
        page_url: str,
        parent_id: str | None,
        seller_base: str,
    ) -> list[dict]:
        """Only categories from the shop's categoryFilter — never the global Ozon tree."""
        data = self._fetch_composer_json(page_url)
        if not data:
            return []
        collector = self._seller_filter_collector(seller_base)
        page_cat = SellerCategoryCollector._category_id_from_page_url(page_url)
        if parent_id is not None and not page_cat:
            page_cat = str(parent_id)
        raw = collector._extract_category_filter_children(
            data,
            parent_id,
            page_category_id=page_cat,
        )
        if parent_id is not None:
            deeper = collector._extract_category_filter_deeper_ids(data, parent_id)
            if deeper:
                store = getattr(self, "_composer_deeper_ids", None)
                if store is None:
                    store = {}
                    self._composer_deeper_ids = store
                store[str(parent_id)] = set(deeper)
        if not raw:
            return []
        return self._cleanup_categories(
            self._clone_category_branch(raw, str(parent_id or ""), seller_base)
        )

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
        self._ensure_seller_shop_open(url, log, on_manual_bypass)

        self._wait_for_filters(log)
        self._open_category_filter(log)
        log("Открываем полный список категорий магазина...")
        self._ensure_shop_all_categories_opened(log)
        self._ensure_category_view_all_opened(log, on_manual_bypass=on_manual_bypass)
        self._expand_filter_sections(log)

        roots = self._collect_root_categories(log)
        tree = [node for root in roots if (node := self._category_dict_to_option(root, parent_name=""))]

        if not tree:
            log("Категории не найдены")
        else:
            log(f"Загружено главных категорий магазина: {len(tree)}")
            self._remember_shop_root_ids(
                [
                    {
                        "id": str(node.param_value or node.category_id),
                        "name": node.name,
                    }
                    for node in tree
                ]
            )

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

    def load_global_root_tree(
        self,
        progress: Callable[[str], None] | None = None,
        on_manual_bypass=None,
        on_roots: Callable[[list[FilterOptionNode]], None] | None = None,
    ) -> list[FilterOptionNode]:
        """Load only top-level Ozon categories (no subcategory crawl)."""
        log = progress or (lambda _m: None)
        log("Сбор главных категорий Ozon (без подкатегорий)...")
        raw_roots = self._load_global_root_categories(log, on_manual_bypass)
        if not raw_roots:
            raise self._global_catalog_access_error(
                "Общий каталог не вернул корневые категории."
            )
        self._seller_root_ids = {str(root["id"]) for root in raw_roots}
        result = [
            node
            for raw in raw_roots
            if (
                node := self._dict_to_option_tree(
                    {**raw, "children": []},
                    parent_name="",
                )
            )
        ]
        if not result:
            raise ConnectionError("Главные категории Ozon не распознаны.")
        if on_roots:
            on_roots(result)
        if not self._page_access_blocked():
            self._park_catalog_tab(log)
        log(f"Готово: главных категорий — {len(result)}. Отметьте нужные для подкатегорий.")
        return result

    def expand_global_category_subtrees(
        self,
        root_ids: list[str],
        progress: Callable[[str], None] | None = None,
        on_manual_bypass=None,
        on_subcategories_begin: Callable[[int], None] | None = None,
        on_branch: Callable[[FilterOptionNode], None] | None = None,
        timeout_sec: int | None = None,
        root_meta: dict[str, dict] | None = None,
    ) -> list[FilterOptionNode]:
        """Deep-load subcategories only for the selected root category IDs."""
        log = progress or (lambda _m: None)
        ids = [str(cid) for cid in root_ids if str(cid).strip()]
        if not ids:
            raise ValueError("Не выбраны категории для загрузки подкатегорий.")

        base = self._catalog_base_url()
        category_base = base.rstrip("/") + GLOBAL_CATALOG_PATH
        meta = root_meta or {}
        raw_roots: list[dict] = []
        for cid in ids:
            info = meta.get(cid) or {}
            raw_roots.append(
                {
                    "id": cid,
                    "name": str(info.get("name") or cid),
                    "url": info.get("url") or self._build_category_url(category_base, cid),
                    "children": [],
                }
            )

        if not getattr(self, "_seller_root_ids", None):
            self._seller_root_ids = set(ids)

        if on_subcategories_begin:
            on_subcategories_begin(len(raw_roots))

        if is_access_restricted(self.page) or is_blocked_page(self.page):
            raise self._global_catalog_access_error(
                "Доступ ограничен до загрузки подкатегорий."
            )

        log(
            f"Загрузка подкатегорий только для {len(raw_roots)} выбранных разделов "
            "(«Посмотреть все» на каждом уровне)..."
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
                log(f"Лимит времени: обработано {index - 1}/{len(raw_roots)} веток")
                break
            if index > 1:
                log(
                    "Пауза перед следующей выбранной веткой "
                    f"({int(GLOBAL_COMPOSER_ROOT_PAUSE_MIN)}–"
                    f"{int(GLOBAL_COMPOSER_ROOT_PAUSE_MAX)} сек)..."
                )
                self._root_tree_pause()
                if is_access_restricted(self.page) or is_blocked_page(self.page):
                    self._note_tree_pressure()
                    log(
                        f"Остановка: блокировка Ozon перед веткой "
                        f"{index}/{len(raw_roots)}"
                    )
                    break
            root_id = str(root["id"])
            log(f"Ветка {index}/{len(raw_roots)}: {root['name']}")

            def _emit_branch() -> None:
                if not on_branch:
                    return
                if node := self._dict_to_option_tree(root, parent_name=""):
                    on_branch(node)

            # Every depth: page → «Посмотреть все» → collect; UI updates incrementally.
            self._collect_category_branch_whole(
                category_base,
                root,
                log=log,
                on_manual_bypass=on_manual_bypass,
                deadline=deadline,
                on_update=_emit_branch,
            )
            if not root.get("children"):
                batch = self._fetch_categories_batch(category_base, [root_id], log) or {}
                api_children = batch.get(root_id) or []
                seeds = self._resolve_direct_child_seeds(
                    api_children,
                    parent_id=root_id,
                    api_children=api_children,
                )
                root["children"] = seeds
                if seeds:
                    log(
                        f"  Composer fallback: {len(seeds)} подкатегорий — "
                        "обход детей с «Посмотреть все»..."
                    )
                    _emit_branch()
                    seen = {root_id}
                    visits = [0]
                    for child in seeds:
                        if deadline is not None and self._past_deadline(deadline):
                            break
                        if is_access_restricted(self.page) or is_blocked_page(self.page):
                            break
                        self._collect_category_branch_whole(
                            category_base,
                            child,
                            log=log,
                            on_manual_bypass=on_manual_bypass,
                            deadline=deadline,
                            on_update=_emit_branch,
                            depth=1,
                            visited=seen,
                            page_visits=visits,
                        )
            # Final UI sync once per root (avoid duplicate rebuild of deep leaves).
            _emit_branch()


        result = [
            node
            for raw in raw_roots
            if (node := self._dict_to_option_tree(raw, parent_name=""))
        ]
        if not self._page_access_blocked():
            self._park_catalog_tab(log)
        subtotal = sum(self._count_option_descendants(node) for node in result)
        log(
            f"Подкатегории готовы: {len(result)} разделов, узлов внутри: {subtotal}"
        )
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
                    self._batch_tree_pause()
            deadline = (
                time.monotonic() + max(60, timeout_sec)
                if timeout_sec is not None
                else None
            )
            for index, root in enumerate(raw_roots, start=1):
                if is_access_restricted(self.page) or is_blocked_page(self.page):
                    self._note_tree_pressure()
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
                    self._root_tree_pause()
                    if is_access_restricted(self.page) or is_blocked_page(self.page):
                        self._note_tree_pressure()
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
        depth: int = 0,
        visited: set[str] | None = None,
        page_visits: list[int] | None = None,
        page_max_depth: int | None = None,
        ancestors: set[str] | None = None,
    ) -> None:
        """Page+view-all for shallow levels; Composer fills the rest (fast)."""
        if deadline is not None and self._past_deadline(deadline):
            return
        if depth > self.MAX_SUBCATEGORY_DEPTH:
            return
        if is_access_restricted(self.page) or is_blocked_page(self.page):
            self._note_tree_pressure()
            log("  остановка: Ozon ограничил доступ (fab_)")
            return

        seen = visited if visited is not None else set()
        visits = page_visits if page_visits is not None else [0]
        walk_limit = (
            int(GLOBAL_CATEGORY_PAGE_MAX_DEPTH)
            if page_max_depth is None
            else int(page_max_depth)
        )
        cid = str(node.get("id") or "")
        if not cid or cid in seen:
            return
        seen.add(cid)

        name = str(node.get("name", "") or cid)
        indent = "  " * depth
        log(f"{indent}→ [{depth}] {name} — страница и «Посмотреть все»...")

        self._last_fetch_api_children = []
        raw = self._fetch_category_subtree(
            seller_base, node, log, on_manual_bypass,
        )
        visits[0] += 1
        if self._page_blocked():
            self._note_tree_pressure()
            log(f"{indent}  остановка: fab_ после открытия «{name}»")
            return
        self._note_tree_ok()

        # Prefer Composer children captured on the same page (avoid a second round-trip).
        api_children = list(self._last_fetch_api_children or [])
        if not api_children:
            batch = self._fetch_categories_batch(seller_base, [cid], log) or {}
            api_children = batch.get(cid) or []
        deeper_ids = set(
            (getattr(self, "_composer_deeper_ids", {}) or {}).get(cid) or set()
        )
        # For concrete shops, Composer "deeper ids" can be incomplete/noisy and may
        # accidentally include real direct children on deep levels. Do not exclude
        # by this hint in seller mode, otherwise recursion can stop around L3.
        if self._is_seller_shop_base(seller_base):
            deeper_ids = set()
        elif not api_children:
            deeper_ids = set()
        else:
            dom_children_preview = self._unwrap_parent_children(raw, cid)
            nested_dom = any((n.get("children") or []) for n in dom_children_preview)
            if nested_dom:
                # DOM already exposes hierarchy — Composer "deeper" hints often lie.
                deeper_ids = set()
        ancestor_ids = set(ancestors or ())
        ancestor_ids.add(cid)
        # Never treat parent/ancestor sections as "children" of a deeper node.
        # Ozon menus often re-list «Женские аксессуары» under «Аксессуары для волос».
        exclude_ids = set(deeper_ids) | ancestor_ids
        children = self._resolve_direct_child_seeds(
            raw,
            parent_id=cid,
            api_children=api_children,
            exclude_ids=exclude_ids,
            ancestor_ids=ancestor_ids,
        )
        if not children and api_children:
            children = self._resolve_direct_child_seeds(
                api_children,
                parent_id=cid,
                api_children=api_children,
                exclude_ids=exclude_ids,
                ancestor_ids=ancestor_ids,
            )

        node["children"] = self._dedupe_category_dict_children(children)
        if not node["children"]:
            log(f"{indent}  листьев/подкатегорий нет")
            if on_update and depth <= 1:
                on_update()
            return

        log(
            f"{indent}  прямых подкатегорий: {len(node['children'])}"
            + (
                f" (DOM/view-all + Composer {len(api_children)})"
                if api_children
                else " (DOM/view-all)"
            )
        )
        if on_update and depth <= 1:
            on_update()

        # Last page level: fill deeper nodes via Composer (no more navigations).
        # Concrete shops skip this — Composer global tree is not shop assortment.
        if depth >= walk_limit:
            if self._is_seller_shop_base(seller_base):
                log(
                    f"{indent}  дальше только через страницы магазина "
                    f"(глобальный Composer отключён для магазина)..."
                )
                # Keep walking pages for remaining depth instead of global API fill.
                if depth < self.MAX_SUBCATEGORY_DEPTH:
                    for index, child in enumerate(node["children"], start=1):
                        if deadline is not None and self._past_deadline(deadline):
                            break
                        if is_access_restricted(self.page) or is_blocked_page(self.page):
                            break
                        child_id = str(child.get("id") or "")
                        if not child_id or child_id in seen:
                            continue
                        if index > 1 or depth > 0:
                            self._batch_tree_pause()
                        self._collect_category_branch_whole(
                            seller_base,
                            child,
                            log=log,
                            on_manual_bypass=on_manual_bypass,
                            deadline=deadline,
                            on_update=on_update,
                            depth=depth + 1,
                            visited=seen,
                            page_visits=visits,
                            page_max_depth=self.MAX_SUBCATEGORY_DEPTH,
                            ancestors=ancestor_ids,
                        )
                if on_update and depth <= 1:
                    on_update()
                return
            log(
                f"{indent}  дальше глубина через Composer "
                f"(без открытия страниц, page-depth={walk_limit})..."
            )
            self._complete_category_subtree(
                seller_base,
                node["children"],
                log,
                deadline=deadline,
                max_depth=max(1, self.MAX_SUBCATEGORY_DEPTH - depth),
                on_manual_bypass=on_manual_bypass,
            )
            for child in node["children"]:
                cid_child = str(child.get("id") or "")
                if cid_child:
                    seen.add(cid_child)
            if on_update and depth <= 1:
                on_update()
            if depth == 0:
                total = self._count_dict_descendants(node)
                log(
                    f"  ветка «{name}»: {len(node['children'])} верхнего уровня, "
                    f"всего узлов: {total}, страниц: {visits[0]}"
                )
            return

        for index, child in enumerate(node["children"], start=1):
            if deadline is not None and self._past_deadline(deadline):
                log(f"{indent}  лимит времени: глубина собрана частично")
                break
            if is_access_restricted(self.page) or is_blocked_page(self.page):
                log(f"{indent}  остановка углубления: fab_")
                break

            child_id = str(child.get("id") or "")
            if not child_id or child_id in seen:
                continue

            if index > 1 or depth > 0:
                self._batch_tree_pause()
            if (
                GLOBAL_COMPOSER_LONG_EVERY_N > 0
                and visits[0] > 0
                and visits[0] % GLOBAL_COMPOSER_LONG_EVERY_N == 0
            ):
                log(
                    f"{indent}  длинная пауза "
                    f"(каждые {GLOBAL_COMPOSER_LONG_EVERY_N} страниц)..."
                )
                self._long_tree_pause()

            self._collect_category_branch_whole(
                seller_base,
                child,
                log=log,
                on_manual_bypass=on_manual_bypass,
                deadline=deadline,
                on_update=on_update,
                depth=depth + 1,
                visited=seen,
                page_visits=visits,
                page_max_depth=walk_limit,
                ancestors=ancestor_ids,
            )

        if depth == 0:
            total = self._count_dict_descendants(node)
            log(
                f"  ветка «{name}»: {len(node.get('children') or [])} верхнего уровня, "
                f"всего узлов: {total}, страниц: {visits[0]}"
            )

        if on_update:
            on_update()

    def _tree_quality_score(self, nodes: list[dict]) -> float:
        """Prefer structured trees over flat mega-lists of mixed depths."""
        if not nodes:
            return 0.0
        total = self._count_dict_descendants({"children": nodes})
        top = len(nodes)
        max_depth = 0

        def walk(items: list[dict], depth: int) -> None:
            nonlocal max_depth
            max_depth = max(max_depth, depth)
            for item in items:
                walk(item.get("children") or [], depth + 1)

        walk(nodes, 1)
        score = float(total) + max_depth * 12.0
        # Flat list of many "siblings" is usually descendants dumped together.
        if max_depth <= 1 and top >= 12:
            score *= 0.35
        return score

    def _drop_sibling_shop_roots(
        self,
        nodes: list[dict],
        parent_id: str,
    ) -> list[dict]:
        """Remove other top-level shop categories from a subtree payload."""
        parent_id = str(parent_id or "")
        root_ids = {str(x) for x in (getattr(self, "_seller_root_ids", set()) or set())}
        if not root_ids:
            return nodes or []
        cleaned: list[dict] = []
        for node in nodes or []:
            cid = str(node.get("id") or "")
            if not cid or cid == parent_id:
                continue
            if cid in root_ids:
                continue
            child = dict(node)
            child["children"] = self._drop_sibling_shop_roots(
                node.get("children") or [],
                parent_id,
            )
            cleaned.append(child)
        return cleaned

    def _remember_shop_root_ids(self, nodes: list[dict] | None = None) -> set[str]:
        """Union-expand known shop root IDs; never shrink to a truncated Composer set."""
        existing = {str(x) for x in (getattr(self, "_seller_root_ids", set()) or set()) if str(x)}
        extra = {
            str(node.get("id"))
            for node in (nodes or [])
            if node.get("id")
        }
        merged = existing | extra
        if merged:
            self._seller_root_ids = merged
        return set(getattr(self, "_seller_root_ids", set()) or set())

    def _seed_shop_root_ids(self, seller_base: str, log) -> None:
        """Load the full shop root ID set from DOM (+ optional filter), never shrink.

        Prefer Composer/filter roots when enough are already known — re-opening
        «Посмотреть все» mid-session is a common fab_ trigger before product parse.
        """
        existing = getattr(self, "_seller_root_ids", set()) or set()
        if len(existing) >= 8:
            log(
                f"  корни магазина уже известны: {len(existing)} "
                "— без повторного «Посмотреть все»"
            )
            return

        filter_roots: list[dict] = []
        try:
            filter_roots = self._extract_seller_filter_children(
                seller_base, parent_id=None, seller_base=seller_base,
            )
        except Exception:
            filter_roots = []
        roots = self._remember_shop_root_ids(list(filter_roots or []))
        if len(roots) >= 8:
            log(f"  корни магазина (фильтр Composer): {len(roots)}")
            return

        if self._page_access_blocked():
            log("  пропуск «Посмотреть все»: уже fab_/блокировка")
            return

        try:
            self._open_category_filter(log, fast=True)
            self._ensure_shop_all_categories_opened(log)
            self._ensure_category_view_all_opened(log)
        except Exception:
            pass
        if self._page_access_blocked():
            log("  остановка seed корней: fab_ после открытия фильтра")
            return
        dom_roots = self._extract_categories_from_wb6_block(parent_id=None)
        before = len(getattr(self, "_seller_root_ids", set()) or set())
        roots = self._remember_shop_root_ids(list(dom_roots or []) + list(filter_roots or []))
        if len(roots) > before:
            log(f"  корни магазина для фильтрации подкатегорий: {len(roots)}")

    def _unwrap_parent_children(
        self,
        nodes: list[dict],
        parent_id: str,
    ) -> list[dict]:
        """Return the child list that belongs under parent_id."""
        parent_id = str(parent_id or "")
        if not nodes:
            return []
        parent_node = self._find_category_node(nodes, parent_id)
        if parent_node and parent_node.get("children"):
            return self._drop_sibling_shop_roots(
                list(parent_node.get("children") or []),
                parent_id,
            )
        return self._drop_sibling_shop_roots(list(nodes), parent_id)

    def _resolve_direct_child_seeds(
        self,
        nodes: list[dict],
        *,
        parent_id: str,
        api_children: list[dict] | None = None,
        exclude_ids: set[str] | None = None,
        ancestor_ids: set[str] | None = None,
    ) -> list[dict]:
        """Build direct-child seeds for depth walk.

        Prefer DOM after «Посмотреть все» (complete visible list). Composer is
        often truncated to the pre-modal filter, so it must not drop DOM extras.
        Composer/DOM nesting only excludes known grandchildren.
        Sibling shop roots (Одежда / Красота / …) are never children of another root.
        Ancestor ids (parent chain) are never accepted as children — Ozon often
        re-lists «Женские аксессуары» under deeper hair-accessory pages.
        """
        parent_id = str(parent_id or "")
        root_ids = {str(x) for x in (getattr(self, "_seller_root_ids", set()) or set())}
        ancestors = {str(x) for x in (ancestor_ids or set()) if str(x)}
        if parent_id:
            ancestors.add(parent_id)
        dom_children = self._unwrap_parent_children(nodes or [], parent_id)
        api_list = [
            item
            for item in list(api_children or [])
            if str(item.get("id") or "") not in root_ids
            or str(item.get("id") or "") == parent_id
        ]
        excluded = {str(x) for x in (exclude_ids or set()) if str(x)}
        excluded.update(cid for cid in root_ids if cid != parent_id)
        excluded.update(ancestors)

        def flatten_index(items: list[dict]) -> dict[str, dict]:
            store: dict[str, dict] = {}

            def walk(entries: list[dict]) -> None:
                for entry in entries or []:
                    cid = str(entry.get("id") or "")
                    if cid and cid not in store:
                        store[cid] = entry
                    walk(entry.get("children") or [])

            walk(items)
            return store

        # Nested nodes under a top-level DOM row are grandchildren, not siblings.
        for top in dom_children:
            for nested_id in flatten_index(top.get("children") or []):
                excluded.add(nested_id)

        dom_by_id = {str(n.get("id") or ""): n for n in dom_children if n.get("id")}
        api_by_id = flatten_index(api_list)

        def seed_from(source: dict, fallback_name: str = "") -> dict | None:
            cid = str(source.get("id") or "")
            if not cid or cid == parent_id or cid in excluded or cid in ancestors:
                return None
            if cid in root_ids and cid != parent_id:
                return None
            name = str(source.get("name") or fallback_name or cid).strip()
            if not is_valid_category(name, cid):
                return None
            return {
                "id": cid,
                "name": name,
                "url": source.get("url"),
                "children": [],
            }

        seeds: list[dict] = []
        seen: set[str] = set()

        # 1) DOM top-level first — includes categories revealed by «Посмотреть все».
        for node in dom_children:
            cid = str(node.get("id") or "")
            if not cid or cid in seen:
                continue
            seed = seed_from(node)
            if not seed:
                continue
            seen.add(cid)
            seeds.append(seed)

        # 2) Add Composer-only direct children the DOM missed.
        for api_node in api_list:
            cid = str(api_node.get("id") or "")
            if not cid or cid in seen:
                continue
            dom_node = dom_by_id.get(cid) or {}
            merged = {
                "id": cid,
                "name": str(dom_node.get("name") or api_node.get("name") or cid),
                "url": dom_node.get("url") or api_node.get("url"),
            }
            seed = seed_from(merged)
            if not seed:
                continue
            seen.add(cid)
            seeds.append(seed)

        return seeds

    def _dedupe_category_dict_children(self, children: list[dict]) -> list[dict]:
        """Keep first occurrence of each category id (avoids L5 double-insert)."""
        seen: set[str] = set()
        result: list[dict] = []
        for child in children or []:
            cid = str(child.get("id") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            result.append(child)
        return result

    def _as_depth_seed_nodes(self, nodes: list[dict]) -> list[dict]:
        """Top-level IDs with empty children for Composer/page depth fill."""
        return self._resolve_direct_child_seeds(nodes, parent_id="", api_children=None)

    def _try_page_child_fill(
        self,
        seller_base: str,
        node: dict,
        log,
        on_manual_bypass,
    ) -> bool:
        """Open category page when Composer returned no children."""
        if node.get("children"):
            return False
        cid = str(node.get("id") or "")
        if not cid:
            return False
        if is_access_restricted(self.page) or is_blocked_page(self.page):
            return False
        parent = {
            "id": cid,
            "name": str(node.get("name") or cid),
            "url": node.get("url") or self._build_category_url(seller_base, cid),
            "children": [],
        }
        raw = self._fetch_category_subtree(seller_base, parent, log, on_manual_bypass)
        if not raw:
            return False
        api_children = list(self._last_fetch_api_children or [])
        seeds = self._resolve_direct_child_seeds(
            raw,
            parent_id=cid,
            api_children=api_children,
        )
        if not seeds and api_children:
            seeds = self._resolve_direct_child_seeds(
                api_children,
                parent_id=cid,
                api_children=api_children,
            )
        if not seeds:
            return False
        node["children"] = seeds
        log(
            f"  страница «Посмотреть все»: +{len(seeds)} подкатегорий "
            f"для «{node.get('name', cid)}»"
        )
        return True

    def _complete_category_subtree(
        self,
        seller_base: str,
        nodes: list[dict],
        log,
        deadline: float | None = None,
        max_depth: int | None = None,
        on_manual_bypass=None,
    ) -> None:
        """Дозагрузить полную иерархию через Composer API (без переходов по страницам).

        Empty leaves are fetched. Nodes that already have children are still
        Composer-checked once and merged, so a shallow DOM pass cannot block
        deeper levels.
        """
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
                if children and node.get("_composer_done"):
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
                enriched = False
                for node, depth in batch:
                    cid = str(node["id"])
                    node["_composer_done"] = True
                    api_children = [
                        child
                        for child in (results.get(cid) or [])
                        if str(child.get("id") or "") not in {"", cid}
                    ]
                    existing = node.get("children") or []
                    if not existing:
                        if api_children:
                            node["children"] = self._dedupe_category_dict_children(api_children)
                            fetched += 1
                            enriched = True
                    elif api_children:
                        merged = self._merge_category_dict_lists(existing, api_children)
                        merged = [
                            child
                            for child in merged
                            if str(child.get("id") or "") not in {"", cid}
                        ]
                        if self._count_dict_descendants(
                            {"children": merged}
                        ) > self._count_dict_descendants({"children": existing}):
                            node["children"] = self._dedupe_category_dict_children(merged)
                            fetched += 1
                            enriched = True
                    elif not existing and not api_children:
                        if self._try_page_child_fill(
                            seller_base, node, log, on_manual_bypass,
                        ):
                            fetched += 1
                            enriched = True
                    for child in node.get("children") or []:
                        next_queue.append((child, depth + 1))
                if fetched and fetched % 40 == 0:
                    log(f"  дозагружено веток: {fetched}...")
                if enriched:
                    self._batch_tree_pause()
                    if (
                        fetched
                        and GLOBAL_COMPOSER_LONG_EVERY_N > 0
                        and fetched % GLOBAL_COMPOSER_LONG_EVERY_N == 0
                    ):
                        log(
                            "  длинная пауза Composer "
                            f"(каждые {GLOBAL_COMPOSER_LONG_EVERY_N} пакетов)..."
                        )
                        self._long_tree_pause()
                    if is_access_restricted(self.page) or is_blocked_page(self.page):
                        self._note_tree_pressure()
                        log("  остановка дозагрузки: fab_ после паузы")
                        return
                    self._note_tree_ok()

            queue.extend(next_queue)
            if not batch and next_queue:
                continue
            if not batch and not next_queue:
                break

        def _strip_flags(items: list[dict]) -> None:
            for item in items:
                item.pop("_composer_done", None)
                _strip_flags(item.get("children") or [])

        _strip_flags(nodes)

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
            filter_parser = self._seller_filter_collector(seller_base)
            deeper_by_parent: dict[str, set[str]] = getattr(
                self, "_composer_deeper_ids", None
            ) or {}
            self._composer_deeper_ids = deeper_by_parent
            seller_only = self._is_seller_shop_base(seller_base)
            for cid, (path, _full) in zip(category_ids, url_pairs):
                data = payload.get(path)
                if not data:
                    continue
                deeper_by_parent[str(cid)] = filter_parser._extract_category_filter_deeper_ids(
                    data, cid
                )
                direct = filter_parser._extract_category_filter_children(
                    data,
                    cid,
                    page_category_id=cid,
                )
                if direct:
                    result[cid] = self._clone_category_branch(
                        direct,
                        cid,
                        seller_base,
                    )
                    continue
                # Shop pages: never fall back to global Ozon category tree.
                if seller_only:
                    result[cid] = []
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
        parent_id = str(parent_id or "")
        root_ids = {str(x) for x in (getattr(self, "_seller_root_ids", set()) or set())}
        children: list[dict] = []
        seen: set[str] = set()
        for item in items:
            cid = str(item.get("id", ""))
            if not cid or cid == parent_id or cid in seen:
                continue
            if cid in root_ids and cid != parent_id:
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
        fast_delay(0.25, 0.45)

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
            f'[class*="{CATEGORY_BLOCK_CLASS}"] a[href*="/seller/"]',
            f'[class*="{CATEGORY_BLOCK_CLASS}"] a[href*="category="]',
            '[data-widget="filtersDesktop"] a[href*="/seller/"]',
            '[data-widget="filtersDesktop"] a[href*="category="]',
            '[data-widget="searchFilters"] a[href*="category="]',
        ):
            try:
                self.page.wait_for_selector(sel, timeout=timeout)
                return
            except Exception:
                continue
        fast_delay(0.3, 0.5)

    def _count_category_filter_links(self) -> int:
        """Count category links inside the Категория block only (not Brand/Color)."""
        try:
            script = (
                """({ blockClass, excludeSections }) => {
                    """
                + _CATEGORY_SECTION_FINDER_JS
                + """
                    const sections = findCategorySections(blockClass, excludeSections);
                    let maxLinks = 0;
                    for (const block of sections) {
                        const n = countCategoryAnchors(block);
                        if (n > maxLinks) maxLinks = n;
                    }
                    return maxLinks;
                }"""
            )
            return int(
                self.page.evaluate(
                    script,
                    {
                        "blockClass": CATEGORY_BLOCK_CLASS,
                        "excludeSections": list(CATEGORY_VIEW_ALL_EXCLUDE_SECTIONS),
                    },
                )
                or 0
            )
        except Exception:
            return 0

    def _discover_category_show_more(self) -> dict:
        """Find «Посмотреть все» only inside the Категория filter section."""
        script = (
            """
        ({ viewAll, excludeSections, blockClass }) => {
            """
            + _CATEGORY_SECTION_FINDER_JS
            + """
            const hit = findViewAllInCategorySections(viewAll, excludeSections, blockClass);
            if (!hit) return { present: false, href: '', text: '' };
            const el = hit.el;
            return {
                present: true,
                href: el.href || el.getAttribute('href') || '',
                text: hit.text.slice(0, 64),
                context: firstLine(hit.scope.innerText || '').slice(0, 80),
            };
        }
        """
        )
        try:
            result = self.page.evaluate(
                script,
                {
                    "viewAll": list(CATEGORY_VIEW_ALL_TEXTS),
                    "excludeSections": list(CATEGORY_VIEW_ALL_EXCLUDE_SECTIONS),
                    "blockClass": CATEGORY_BLOCK_CLASS,
                },
            ) or {}
        except Exception:
            return {"present": False, "href": "", "text": ""}
        return {
            "present": bool(result.get("present")),
            "href": str(result.get("href") or "").strip(),
            "text": str(result.get("text") or "").strip(),
        }

    def _ensure_shop_all_categories_opened(self, log) -> bool:
        """Open seller «Все категории» control (tag strip) before reading roots."""
        script = """
        () => {
            const normalize = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const nodes = Array.from(
                document.querySelectorAll('button, a, [role="button"], span, div')
            );
            for (const el of nodes) {
                const text = normalize(el.innerText || el.textContent || '');
                if (!text || text.length > 40) continue;
                if (
                    text !== 'все категории'
                    && !text.startsWith('все категории')
                    && text !== 'смотреть все категории'
                ) {
                    continue;
                }
                try {
                    const style = window.getComputedStyle(el);
                    const visible = el.offsetParent !== null
                        || style.position === 'fixed'
                        || style.position === 'sticky';
                    if (!visible) continue;
                    el.scrollIntoView({ block: 'center', behavior: 'instant' });
                    el.click();
                    return { clicked: true, text };
                } catch (e) {}
            }
            return { clicked: false, text: '' };
        }
        """
        try:
            before = self._count_visible_category_links()
            result = self.page.evaluate(script) or {}
            if not result.get("clicked"):
                return False
            self._pause_after_view_all_click()
            after = self._count_visible_category_links()
            log(
                f"  «Все категории»: открыто, ссылок {before} → {after}"
            )
            return True
        except Exception:
            return False

    def _prefer_larger_root_list(self, *lists: list[dict]) -> list[dict]:
        """Pick the fullest top-level shop category list (ignore truncated strips)."""
        best: list[dict] = []
        for items in lists:
            cleaned = self._cleanup_categories(items or [])
            # Count only top-level nodes — nested children are for subcat load later.
            tops = [
                {
                    "id": str(item.get("id")),
                    "name": str(item.get("name") or ""),
                    "url": item.get("url"),
                    "children": [],
                }
                for item in cleaned
                if item.get("id")
            ]
            if len(tops) > len(best):
                best = tops
        return best

    def _ensure_category_view_all_opened(
        self,
        log,
        on_manual_bypass=None,
    ) -> dict:
        """Open «Посмотреть все» only in Категория (never Brand/Color/other filters)."""
        info = self._discover_category_show_more()
        if not info.get("present"):
            return {"present": False, "opened": False, "via": "", "clicks": 0}

        before = self._count_category_filter_links()
        clicks = self._click_category_show_more(rounds=6)
        after = self._count_category_filter_links()
        opened = clicks > 0 and after >= before
        grew = after > before

        extra = 0
        for _ in range(4):
            still = self._discover_category_show_more()
            if not still.get("present"):
                opened = True
                break
            n = self._click_category_show_more(rounds=2)
            if not n:
                break
            extra += n
            now = self._count_category_filter_links()
            if now > after:
                after = now
                grew = True
                opened = True
            else:
                break

        clicks += extra
        if opened or grew:
            log(
                f"  «Посмотреть все» (только Категория): кликов {clicks}, "
                f"ссылок {before} → {after}"
            )
            return {
                "present": True,
                "opened": True,
                "via": "category_filter",
                "clicks": clicks,
                "before": before,
                "after": after,
            }

        log(
            "  «Посмотреть все» в Категории найдено, но список не вырос — "
            "собираем видимые пункты"
        )
        return {
            "present": True,
            "opened": False,
            "via": "category_filter",
            "clicks": clicks,
            "before": before,
            "after": after,
        }

    def _click_category_show_more(self, rounds: int = 8) -> int:
        """Click «Посмотреть все» only inside Категория — never Brand/Color/etc."""
        script = (
            """
        ({ viewAll, excludeSections, blockClass }) => {
            """
            + _CATEGORY_SECTION_FINDER_JS
            + """
            const countInCategory = () => {
                const sections = findCategorySections(blockClass, excludeSections);
                let maxLinks = 0;
                for (const block of sections) {
                    const n = countCategoryAnchors(block);
                    if (n > maxLinks) maxLinks = n;
                }
                return maxLinks;
            };
            const before = countInCategory();
            const hit = findViewAllInCategorySections(viewAll, excludeSections, blockClass);
            let clicked = 0;
            if (hit && hit.el) {
                try {
                    hit.el.scrollIntoView({ block: 'center', behavior: 'instant' });
                    hit.el.click();
                    clicked = 1;
                } catch (e) {}
            }
            return {
                clicked,
                before,
                after: countInCategory(),
                href: '',
                modalOpen: false,
                section: hit ? firstLine(hit.scope.innerText || '') : '',
            };
        }
        """
        )
        total_clicks = 0
        last_links = -1
        for _ in range(max(1, rounds)):
            try:
                result = self.page.evaluate(
                    script,
                    {
                        "viewAll": list(CATEGORY_VIEW_ALL_TEXTS),
                        "excludeSections": list(CATEGORY_VIEW_ALL_EXCLUDE_SECTIONS),
                        "blockClass": CATEGORY_BLOCK_CLASS,
                    },
                ) or {}
            except Exception:
                break
            clicked = int(result.get("clicked") or 0)
            after = int(result.get("after") or 0)
            total_clicks += clicked
            if clicked:
                self._pause_after_view_all_click()
                try:
                    self.page.wait_for_selector(
                        f'[class*="{CATEGORY_BLOCK_CLASS}"] a[href*="/category/"]',
                        timeout=3000,
                    )
                except Exception:
                    pass
            if clicked <= 0:
                break
            if after <= last_links and last_links >= 0:
                break
            last_links = after
            if not self._discover_category_show_more().get("present"):
                break
        return total_clicks

    def _count_visible_category_links(self) -> int:
        try:
            script = "() => {\n" + _CATEGORY_LINK_UTILS_JS + "\nreturn countCategoryAnchors(document);\n}"
            return int(self.page.evaluate(script) or 0)
        except Exception:
            return 0

    def _merge_category_dict_lists(
        self,
        primary: list[dict],
        secondary: list[dict],
    ) -> list[dict]:
        """Merge two category trees by id without promoting nested nodes to top-level."""
        if not primary:
            return secondary or []
        if not secondary:
            return primary

        # Prefer structured trees over flat descendant dumps.
        if self._tree_quality_score(secondary) > self._tree_quality_score(primary):
            primary, secondary = secondary, primary

        def index_nodes(
            nodes: list[dict],
            store: dict[str, dict] | None = None,
        ) -> dict[str, dict]:
            out = store if store is not None else {}
            for node in nodes or []:
                cid = str(node.get("id") or "")
                if cid and cid not in out:
                    out[cid] = node
                index_nodes(node.get("children") or [], out)
            return out

        def merge_lists(left: list[dict], right: list[dict]) -> list[dict]:
            by_id: dict[str, dict] = {}
            order: list[str] = []
            for source in (left, right):
                for node in source or []:
                    cid = str(node.get("id") or "")
                    if not cid:
                        continue
                    if cid not in by_id:
                        by_id[cid] = {
                            "id": cid,
                            "name": str(node.get("name") or cid).strip(),
                            "url": node.get("url"),
                            "children": list(node.get("children") or []),
                        }
                        order.append(cid)
                        continue
                    existing = by_id[cid]
                    if not existing.get("name") and node.get("name"):
                        existing["name"] = str(node.get("name")).strip()
                    if not existing.get("url") and node.get("url"):
                        existing["url"] = node.get("url")
                    existing["children"] = merge_lists(
                        existing.get("children") or [],
                        node.get("children") or [],
                    )
            return [by_id[cid] for cid in order if cid in by_id]

        merged = merge_lists(primary, [])
        primary_ids = index_nodes(merged)
        # Attach only truly new top-level nodes; nested matches merge in place.
        extras: list[dict] = []
        for node in secondary or []:
            cid = str(node.get("id") or "")
            if not cid:
                continue
            if cid in primary_ids:
                existing = primary_ids[cid]
                existing["children"] = merge_lists(
                    existing.get("children") or [],
                    node.get("children") or [],
                )
                if not existing.get("name") and node.get("name"):
                    existing["name"] = str(node.get("name")).strip()
                if not existing.get("url") and node.get("url"):
                    existing["url"] = node.get("url")
            else:
                extras.append(
                    {
                        "id": node.get("id"),
                        "name": node.get("name"),
                        "url": node.get("url"),
                        "children": list(node.get("children") or []),
                    }
                )
        if extras:
            merged = merge_lists(merged, extras)
        return merged

    def _expand_category_subtree_in_filter(self, parent_id: str) -> None:
        """Раскрыть поддерево категории в фильтре (стрелки, «Посмотреть все»)."""
        script = """
        (parentId) => {
        """ + _CATEGORY_LINK_UTILS_JS + """
            const pid = String(parentId);
            const block = document.querySelector('[class*="wb6_7"]')
                || document.querySelector('[data-widget="filtersDesktop"]')
                || document.querySelector('[data-widget="searchFilters"]');
            if (!block) return;

            block.querySelectorAll('a[href]').forEach(a => {
                const href = a.href || a.getAttribute('href') || '';
                if (extractCategoryId(href) !== pid) return;
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
        }
        """
        try:
            self.page.evaluate(script, str(parent_id))
            fast_delay(0.35, 0.65)
        except Exception:
            pass
        self._click_category_show_more(rounds=6)

    def _expand_all_categories_in_filter(self) -> None:
        """Раскрыть всё дерево категорий в фильтре перед сбором иерархии."""
        script = f"""
        () => {{
            const block = document.querySelector('[class*="{CATEGORY_BLOCK_CLASS}"]')
                || document.querySelector('[data-widget="filtersDesktop"]')
                || document.querySelector('[data-widget="searchFilters"]');
            if (!block) return;
            for (let round = 0; round < 4; round++) {{
                let clicked = false;
                block.querySelectorAll('button, [role="button"]').forEach(el => {{
                    try {{
                        if (el.offsetParent !== null) {{ el.click(); clicked = true; }}
                    }} catch (e) {{}}
                }});
                if (!clicked) break;
            }}
        }}
        """
        try:
            self.page.evaluate(script)
            fast_delay(0.35, 0.65)
        except Exception:
            pass
        self._click_category_show_more(rounds=10)

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
        """Родительская категория → фильтр → «Посмотреть все» → сбор категорий."""
        parent_id = str(parent["id"])
        catalog_url = self._build_category_url(seller_base, parent_id)
        self._last_fetch_api_children = []
        try:
            if not safe_goto(self.page, catalog_url, log, on_manual_bypass=on_manual_bypass):
                log(f"  не удалось открыть категорию {parent_id}")
                return []
            self._pause_after_page_open()
            self._prepare_category_filter_panel(log, fast=True)

            # Check filter/subcategory panel for «Посмотреть все» and open it first.
            view_all = self._ensure_category_view_all_opened(
                log,
                on_manual_bypass=on_manual_bypass,
            )
            if view_all.get("present") and not view_all.get("opened"):
                self._open_category_filter(log, fast=True)
                view_all = self._ensure_category_view_all_opened(
                    log,
                    on_manual_bypass=on_manual_bypass,
                )
            if not view_all.get("present"):
                log("  «Посмотреть все» в панели нет — собираем видимый список")

            # Concrete shop: DOM + shop categoryFilter only (never global /category/ tree).
            if self._is_seller_shop_base(seller_base):
                dom_tree = self._extract_full_subtree_dom(parent_id)
                filter_tree = self._extract_seller_filter_children(
                    self.page.url or catalog_url,
                    parent_id,
                    seller_base,
                )
                if filter_tree:
                    self._last_fetch_api_children = self._drop_sibling_shop_roots(
                        filter_tree, parent_id,
                    )
                merged = self._merge_category_dict_lists(dom_tree, filter_tree)
                if merged:
                    cleaned = self._drop_sibling_shop_roots(
                        self._cleanup_categories(merged),
                        parent_id,
                    )
                    if cleaned:
                        log(
                            f"  категории магазина: "
                            f"{self._count_dict_descendants({'children': cleaned})} узлов "
                            f"(DOM {self._count_dict_descendants({'children': dom_tree})}, "
                            f"фильтр {len(filter_tree)})"
                        )
                        return cleaned

                # Soft shop-only retry: expand filter / view-all once more.
                self._expand_category_subtree_in_filter(parent_id)
                if view_all.get("present"):
                    self._ensure_category_view_all_opened(
                        log,
                        on_manual_bypass=on_manual_bypass,
                    )
                fast_delay(0.25, 0.45)
                dom_tree = self._extract_full_subtree_dom(parent_id)
                filter_tree = self._extract_seller_filter_children(
                    catalog_url,
                    parent_id,
                    seller_base,
                )
                if filter_tree:
                    self._last_fetch_api_children = self._drop_sibling_shop_roots(
                        filter_tree, parent_id,
                    )
                merged = self._merge_category_dict_lists(dom_tree, filter_tree)
                if merged:
                    cleaned = self._drop_sibling_shop_roots(
                        self._cleanup_categories(merged),
                        parent_id,
                    )
                    if cleaned:
                        return cleaned

                flat = self._extract_categories_from_wb6_block(
                    parent_id,
                    force_page_category=True,
                )
                if flat:
                    cleaned = self._drop_sibling_shop_roots(
                        self._cleanup_categories(flat),
                        parent_id,
                    )
                    if cleaned:
                        log(f"  плоский сбор категорий магазина: {len(cleaned)}")
                        return cleaned

                api_children = self._fetch_subcategories_via_api(
                    seller_base, parent_id, log,
                )
                if api_children:
                    cleaned = self._drop_sibling_shop_roots(
                        self._cleanup_categories(api_children),
                        parent_id,
                    )
                    if cleaned:
                        self._last_fetch_api_children = cleaned
                        log(
                            f"  подкатегории магазина из фильтра Composer: {len(cleaned)}"
                        )
                        return cleaned


                log("  в магазине подкатегорий нет")
                return []

            dom_tree = self._extract_full_subtree_dom(parent_id)
            if dom_tree:
                log(
                    f"  DOM после раскрытия: "
                    f"{self._count_dict_descendants({'children': dom_tree})} узлов"
                )

            composer_tree = self._extract_full_subtree_composer(
                self.page.url or catalog_url,
                parent_id,
                seller_base,
                log,
            )
            try:
                collector = self._seller_filter_collector(seller_base)
                live = self._fetch_composer_json(self.page.url or catalog_url)
                if live:
                    deeper = collector._extract_category_filter_deeper_ids(live, parent_id)
                    if deeper:
                        store = getattr(self, "_composer_deeper_ids", None)
                        if store is None:
                            store = {}
                            self._composer_deeper_ids = store
                        store[str(parent_id)] = set(deeper)
                    direct = collector._extract_category_filter_children(live, parent_id)
                    if direct:
                        cloned = self._clone_category_branch(direct, parent_id, seller_base)
                        if cloned:
                            self._last_fetch_api_children = cloned
                            composer_tree = self._merge_category_dict_lists(
                                composer_tree,
                                cloned,
                            )
            except Exception:
                pass

            merged = self._merge_category_dict_lists(dom_tree, composer_tree)
            if merged:
                cleaned = self._cleanup_categories(merged)
                log(
                    f"  собрано подкатегорий/узлов: "
                    f"{self._count_dict_descendants({'children': cleaned})}"
                )
                return cleaned

            # Leaf / already-full filter: «Посмотреть все» did not grow the list.
            # Skip flat breadcrumb fallback (it invents fake children from ancestors).
            grew = int(view_all.get("after") or 0) > int(view_all.get("before") or 0)
            if view_all.get("present") and not grew:
                api_children = self._fetch_subcategories_via_api(
                    seller_base, parent_id, log,
                )
                if api_children:
                    self._last_fetch_api_children = api_children
                    log(f"  подкатегории из Composer API: {len(api_children)}")
                    return self._cleanup_categories(api_children)
                log("  лист: «Посмотреть все» не расширило список категорий")
                return []

            # One gentle retry only — empty leaves must not burn minutes on loops.
            self._expand_category_subtree_in_filter(parent_id)
            if view_all.get("present"):
                self._ensure_category_view_all_opened(
                    log,
                    on_manual_bypass=on_manual_bypass,
                )
            fast_delay(0.25, 0.45)
            subtree = self._extract_full_subtree_dom(parent_id)
            if subtree:
                return self._cleanup_categories(subtree)

            # Flat list from category filter block (ignores fragile indent tree).
            flat = self._extract_categories_from_wb6_block(
                parent_id,
                force_page_category=True,
            )
            if flat:
                log(f"  плоский сбор из фильтра категорий: {len(flat)}")
                return self._cleanup_categories(flat)

            from_page = self._extract_categories_from_page_json()
            if from_page:
                parent_node = self._find_category_node(from_page, parent_id)
                if parent_node and parent_node.get("children"):
                    cloned = self._clone_category_branch(
                        parent_node["children"], parent_id, seller_base,
                    )
                    if cloned:
                        return self._cleanup_categories(cloned)

            # Last resort: Composer API for this parent only.
            api_children = self._fetch_subcategories_via_api(seller_base, parent_id, log)
            if api_children:
                self._last_fetch_api_children = api_children
                log(f"  подкатегории из Composer API: {len(api_children)}")
                return self._cleanup_categories(api_children)

            link_count = self._count_visible_category_links()
            log(f"  подкатегории не найдены (ссылок на странице: {link_count})")
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
        on_branch: Callable[[FilterOptionNode], None] | None = None,
    ) -> dict[str, list[FilterOptionNode]]:
        log = progress or (lambda _m: None)
        seller_base = normalize_seller_url(seller_url, self.browser_mode)
        result: dict[str, list[FilterOptionNode]] = {}

        unique: dict[str, CategoryTarget] = {}
        for cat in categories:
            cid = cat.param_value or cat.category_id
            if cid:
                unique[cid] = cat

        if self._is_seller_shop_base(seller_base):
            try:
                self._ensure_seller_shop_open(seller_base, log, on_manual_bypass)
                self._seed_shop_root_ids(seller_base, log)
            except Exception as exc:
                log(f"Не удалось обновить список корней магазина: {exc}")

        if not getattr(self, "_seller_root_ids", None):
            self._seller_root_ids = set(unique.keys())
        else:
            # Keep selected parents in the set, but never replace full roots with only them.
            self._seller_root_ids = set(self._seller_root_ids) | set(unique.keys())

        total = len(unique)
        log(
            f"Загрузка подкатегорий магазина: {total} раздел(ов), "
            f"полная глубина с «Посмотреть все» на каждом уровне "
            f"(до {SELLER_CATEGORY_PAGE_MAX_DEPTH})..."
        )
        for idx, (cid, category) in enumerate(unique.items(), start=1):
            log(f"Подкатегории {idx}/{total}: {category.name}")
            parent = {
                "id": cid,
                "name": category.name,
                "url": category.url or self._build_category_url(seller_base, cid),
                "children": [],
            }

            def _emit() -> None:
                if not on_branch:
                    return
                node = self._dict_to_option_tree(parent, parent_name="")
                if node:
                    # Preserve display name from selection when available.
                    if category.name and not node.name:
                        node.name = str(category.name)
                    on_branch(node)

            self._collect_category_branch_whole(
                seller_base,
                parent,
                log=log,
                on_manual_bypass=on_manual_bypass,
                on_update=_emit,
                depth=0,
                page_max_depth=SELLER_CATEGORY_PAGE_MAX_DEPTH,
            )
            children = [
                node
                for item in parent.get("children", [])
                if (node := self._dict_to_option_tree(item, parent_name=str(category.name or "")))
            ]
            if children:
                result[cid] = children
                total_nodes = self._count_dict_descendants(parent)
                log(
                    f"  → {len(children)} прямых подкатегорий, "
                    f"всего узлов в ветке: {total_nodes}"
                )
            else:
                log("  → подкатегории не найдены")
            _emit()

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
            self._pause_after_page_open()

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
            try:
                from .browser import probe_chrome_network, OZON_NETWORK_BLOCK_HINT

                internet_ok, ozon_ok = probe_chrome_network(self.page)
                if internet_ok and not ozon_ok:
                    parts.append(OZON_NETWORK_BLOCK_HINT)
                else:
                    parts.append(
                        "Chrome откроется при «Загрузить категории». "
                        "Убедитесь, что сайт загружается без блокировки, затем повторите."
                    )
            except Exception:
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
            self._pause_after_page_open()
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
        page_url = self.page.url or ""
        seller_base = page_url.split("?")[0]
        # Concrete shop: only the store category filter — never global Ozon tree.
        if self._is_seller_shop_base(seller_base):
            # Composer categoryFilter is often truncated; DOM after
            # «Все категории» / «Посмотреть все» (modal) has the full root list.
            from_dom = self._extract_categories_from_dom()
            from_filter = self._extract_seller_filter_children(
                page_url, parent_id=None, seller_base=seller_base,
            )
            from_modal = self._extract_categories_from_wb6_block(parent_id=None)
            cleaned = self._prefer_larger_root_list(from_modal, from_dom, from_filter)
            if not cleaned:
                # One more attempt: reopen all-categories and re-read the largest block.
                self._ensure_shop_all_categories_opened(log)
                self._ensure_category_view_all_opened(log)
                from_modal = self._extract_categories_from_wb6_block(parent_id=None)
                cleaned = self._prefer_larger_root_list(
                    from_modal,
                    self._extract_categories_from_dom(),
                    from_filter,
                )
            log(
                f"Категории магазина (полные главные): {len(cleaned)} "
                f"(DOM/модалка {len(from_dom)}/{len(from_modal or [])}, "
                f"фильтр Composer {len(from_filter)})"
            )
            return cleaned

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
                page_max_depth=SELLER_CATEGORY_PAGE_MAX_DEPTH,
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
            page_max_depth=SELLER_CATEGORY_PAGE_MAX_DEPTH,
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
        # Only open the Категория section — never Brand/Color/other filters.
        for sel in (
            '[data-widget="filtersDesktop"] span:has-text("Категория")',
            '[data-widget="filtersDesktop"] span:has-text("Категории")',
            '[data-widget="searchFilters"] span:has-text("Категория")',
            '[data-widget="searchFilters"] span:has-text("Категории")',
        ):
            try:
                el = self.page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    human_delay(0.3, 0.6)
                    break
            except Exception:
                continue
        self._click_category_show_more(rounds=4)

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
        full_url = self._route_url(page_url)
        parsed = urlparse(full_url)
        path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
        # Keep ?category=… — without it Composer returns the shop root filter.
        rel_path = f"{path}?{parsed.query}" if parsed.query else path
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
                {"path": rel_path, "fullUrl": full_url, "clientName": client_name},
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
        """ + _CATEGORY_LINK_UTILS_JS + """
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
                    const id = extractCategoryId(href);
                    if (!id) return;
                    let depth = 0;
                    let node = el.parentElement;
                    while (node && node !== root) {
                        depth += 1;
                        node = node.parentElement;
                    }
                    add(name, id, href, depth);
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
        """ + _CATEGORY_LINK_UTILS_JS + """
            const pid = String(parentId);
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
                root.querySelectorAll('a[href]').forEach(el => {
                    const href = el.href || el.getAttribute('href') || '';
                    const id = extractCategoryId(href);
                    if (!id || seen.has(id)) return;
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

            const pIdx = items.findIndex(i => i.id === pid);
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

            const pageCat = pageCategoryIdFromLocation();
            if (pageCat && pageCat === pid) {
                return items.filter(i => i.id !== pid);
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
