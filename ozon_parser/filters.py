import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from .category_extract import is_valid_category, is_valid_category_name
from .utils import to_desktop_url

SKIP_SECTIONS = {"фильтры", "filters", "сортировка", "sort", "цена", "price"}
SKIP_OPTION_NAMES = {
    "ещё",
    "еще",
    "все",
    "all",
    "показать все",
    "посмотреть все",
    "смотреть все",
    "показать ещё",
    "показать еще",
    "свернуть",
    "развернуть",
    "ещё...",
}
FILTER_PARAM_KEYS = (
    "category",
    "type",
    "brand",
    "color",
    "material",
    "country",
    "seller",
    "delivery",
    "rating",
    "discount",
)


@dataclass
class FilterOptionNode:
    id: str
    name: str
    url: str | None = None
    param_key: str = ""
    param_value: str = ""
    category_id: str = ""
    category_name: str = ""
    parent_name: str = ""
    children: list["FilterOptionNode"] = field(default_factory=list)


@dataclass
class FilterSection:
    title: str
    options: list[FilterOptionNode] = field(default_factory=list)


def is_valid_filter_option_name(name: str) -> bool:
    name = str(name).strip()
    if not name or len(name) < 2 or len(name) > 80:
        return False
    if name.lower() in SKIP_OPTION_NAMES:
        return False
    return is_valid_category_name(name) or bool(re.search(r"[а-яА-ЯёЁa-zA-Z0-9]", name))


def is_valid_section_title(title: str) -> bool:
    title = str(title).strip()
    if not title or len(title) > 40:
        return False
    return title.lower() not in SKIP_SECTIONS


def _params_from_href(href: str) -> tuple[str, str]:
    if not href:
        return "", ""
    query = urlparse(href).query
    if not query:
        return "", ""
    parsed = parse_qs(query)
    for key in FILTER_PARAM_KEYS:
        values = parsed.get(key)
        if values and values[0]:
            return key, str(values[0])
    for key, values in parsed.items():
        if values and values[0] and key not in ("layout", "sort", "page", "opened"):
            return key, str(values[0])
    return "", ""


def _option_id(section: str, param_key: str, param_value: str, name: str, category_id: str = "") -> str:
    if category_id and param_key and param_value:
        return f"{category_id}|{section}|{param_key}:{param_value}"
    if param_key and param_value:
        return f"{section}|{param_key}:{param_value}"
    safe_name = re.sub(r"\s+", "_", name.strip().lower())
    return f"{section}|{safe_name}"


def _normalize_option(
    section: str,
    name: str,
    href: str | None,
    seller_base: str,
    category_id: str = "",
    category_name: str = "",
) -> FilterOptionNode | None:
    name = re.sub(r"\s+", " ", str(name).strip())
    if not is_valid_filter_option_name(name):
        return None
    url = to_desktop_url(urljoin(seller_base, href)) if href else None
    param_key, param_value = _params_from_href(url or href or "")
    if param_key == "category" and param_value and not is_valid_category(name, param_value):
        return None
    option_id = _option_id(section, param_key, param_value, name, category_id)
    return FilterOptionNode(
        id=option_id,
        name=name,
        url=url,
        param_key=param_key,
        param_value=param_value,
        category_id=category_id,
        category_name=category_name,
    )


def _normalize_section_title(title: str) -> str:
    title = str(title).strip()
    if title.lower() in ("категории",):
        return "Категория"
    return title


def merge_filter_sections(sections_list: list[list[FilterSection]]) -> list[FilterSection]:
    merged: dict[str, FilterSection] = {}

    def merge_options(existing: list[FilterOptionNode], incoming: list[FilterOptionNode]) -> None:
        by_id = {opt.id: opt for opt in existing}
        for opt in incoming:
            if opt.id in by_id:
                current = by_id[opt.id]
                if opt.url and not current.url:
                    current.url = opt.url
                if opt.param_key and not current.param_key:
                    current.param_key = opt.param_key
                    current.param_value = opt.param_value
                if opt.children:
                    merge_options(current.children, opt.children)
            else:
                existing.append(opt)
                by_id[opt.id] = opt

    for sections in sections_list:
        for section in sections:
            title = _normalize_section_title(section.title)
            if not is_valid_section_title(title):
                continue
            if title not in merged:
                merged[title] = FilterSection(title=title, options=[])
            merge_options(merged[title].options, section.options)

    order = ["Категория", "Категории", "Тип", "Бренд"]
    result = sorted(
        merged.values(),
        key=lambda s: (order.index(s.title) if s.title in order else 99, s.title.lower()),
    )
    return [s for s in result if s.options]


def exclude_category_sections(sections: list[FilterSection]) -> list[FilterSection]:
    return [
        section for section in sections
        if _normalize_section_title(section.title).lower() != "категория"
    ]


def attach_category_context(
    sections: list[FilterSection],
    category_id: str,
    category_name: str,
) -> list[FilterSection]:
    result: list[FilterSection] = []

    def walk_options(options: list[FilterOptionNode], section_title: str) -> list[FilterOptionNode]:
        attached: list[FilterOptionNode] = []
        for opt in options:
            attached.append(
                FilterOptionNode(
                    id=_option_id(
                        section_title,
                        opt.param_key,
                        opt.param_value,
                        opt.name,
                        category_id,
                    ) if opt.param_key else f"{category_id}|{opt.id}",
                    name=opt.name,
                    url=opt.url,
                    param_key=opt.param_key,
                    param_value=opt.param_value,
                    category_id=category_id,
                    category_name=category_name,
                    children=walk_options(opt.children, section_title),
                )
            )
        return attached

    for section in exclude_category_sections(sections):
        titled = f"{section.title} ({category_name})"
        result.append(FilterSection(title=titled, options=walk_options(section.options, section.title)))
    return result


def parse_filter_sections_from_composer(data: dict, seller_base: str) -> list[FilterSection]:
    sections: list[FilterSection] = []
    widget_states = data.get("widgetStates") or {}
    for key, raw in widget_states.items():
        key_lower = key.lower()
        if not any(token in key_lower for token in ("filter", "catalog", "search")):
            continue
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        _find_filter_sections_in_obj(state, sections, seller_base)
    return sections


def _find_filter_sections_in_obj(obj: Any, sections: list[FilterSection], seller_base: str) -> None:
    if isinstance(obj, dict):
        title = obj.get("title") or obj.get("name") or obj.get("caption") or obj.get("label")
        if isinstance(title, dict):
            title = title.get("text") or title.get("title")
        items = (
            obj.get("filters")
            or obj.get("items")
            or obj.get("values")
            or obj.get("sections")
            or obj.get("categories")
            or obj.get("nodes")
        )
        if isinstance(title, str) and isinstance(items, list) and items:
            section_title = title.strip()
            if is_valid_section_title(section_title):
                options = _options_from_items(section_title, items, seller_base)
                if options:
                    sections.append(FilterSection(title=section_title, options=options))
        for value in obj.values():
            _find_filter_sections_in_obj(value, sections, seller_base)
    elif isinstance(obj, list):
        for item in obj:
            _find_filter_sections_in_obj(item, sections, seller_base)


def _options_from_items(section: str, items: list, seller_base: str) -> list[FilterOptionNode]:
    options: list[FilterOptionNode] = []
    seen: set[str] = set()

    def add_option(name: str, href: str | None, children: list | None = None) -> None:
        node = _normalize_option(section, name, href, seller_base)
        if not node or node.id in seen:
            return
        seen.add(node.id)
        if children:
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_name = (
                    child.get("title")
                    or child.get("name")
                    or child.get("text")
                    or child.get("caption")
                    or ""
                )
                if isinstance(child_name, dict):
                    child_name = child_name.get("text") or child_name.get("title") or ""
                child_link = child.get("link") or child.get("deeplink") or child.get("url") or ""
                child_children = child.get("children") or child.get("categories") or child.get("items")
                child_node = _normalize_option(section, str(child_name), str(child_link) or None, seller_base)
                if child_node and child_node.id not in seen:
                    seen.add(child_node.id)
                    if child_children:
                        nested = _options_from_items(section, child_children, seller_base)
                        child_node.children.extend(nested)
                    node.children.append(child_node)
        options.append(node)

    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("title") or item.get("name") or item.get("text") or item.get("caption") or ""
        if isinstance(name, dict):
            name = name.get("text") or name.get("title") or ""
        link = item.get("link") or item.get("deeplink") or item.get("url") or ""
        children = item.get("children") or item.get("categories") or item.get("items")
        add_option(str(name), str(link) or None, children if isinstance(children, list) else None)

    return options


def extract_filter_sections_from_dom(page_url: str, raw_sections: list[dict]) -> list[FilterSection]:
    seller_base = to_desktop_url(page_url).split("?")[0]
    sections: list[FilterSection] = []
    for raw in raw_sections:
        title = str(raw.get("title", "")).strip()
        if not is_valid_section_title(title):
            continue
        options: list[FilterOptionNode] = []
        seen: set[str] = set()
        for item in raw.get("options", []):
            node = _normalize_option(title, item.get("name", ""), item.get("href"), seller_base)
            if node and node.id not in seen:
                seen.add(node.id)
                options.append(node)
        if options:
            sections.append(FilterSection(title=title, options=options))
    return sections


DOM_FILTER_SECTIONS_SCRIPT = """
() => {
    const sections = [];
    const seenSections = new Set();
    const skipTitles = new Set(['фильтры', 'filters', 'сортировка', 'sort']);
    const skipNames = new Set([
        'ещё', 'еще', 'все', 'all', 'показать все', 'посмотреть все',
        'смотреть все', 'показать ещё', 'показать еще', 'свернуть', 'развернуть'
    ]);

    const containers = [];
    document.querySelectorAll(
        '[data-widget="filtersDesktop"], [data-widget="searchFilters"], [data-widget="filters"]'
    ).forEach(el => containers.push(el));
    if (!containers.length) return sections;

    const normalize = (text) => (text || '').trim().replace(/\\s+/g, ' ');

    const findSectionBlock = (linkEl) => {
        let node = linkEl.closest('[data-filter-group]') || linkEl.parentElement;
        for (let depth = 0; depth < 10 && node; depth++) {
            if (!containers.some(c => c.contains(node))) break;
            const titleEl = node.querySelector(
                '[data-widget="filterTitle"], [data-widget="filterHeading"], ' +
                'span[class*="title"], div[class*="title"], button[class*="title"], h3, h4'
            );
            if (titleEl && !titleEl.querySelector('a[href*="/seller/"]')) {
                const title = normalize(titleEl.textContent);
                if (title && title.length <= 40 && !skipTitles.has(title.toLowerCase())) {
                    return title;
                }
            }
            const children = Array.from(node.children || []);
            for (const child of children) {
                if (child === linkEl || child.contains(linkEl)) continue;
                if (child.querySelector('a[href*="/seller/"]')) continue;
                const text = normalize(child.textContent);
                if (text && text.length <= 40 && !text.includes('\\n') && !skipTitles.has(text.toLowerCase())) {
                    return text;
                }
            }
            node = node.parentElement;
        }
        return null;
    };

    const sectionMap = new Map();

    containers.forEach(container => {
        container.querySelectorAll('a[href*="/seller/"]').forEach(link => {
            const href = link.href || link.getAttribute('href') || '';
            const name = normalize(link.textContent || link.getAttribute('title') || '');
            if (!name || name.length > 80 || skipNames.has(name.toLowerCase())) return;
            if (!/[?&](category|type|brand|color|material|country)=/i.test(href)) return;

            let sectionTitle = findSectionBlock(link);
            if (!sectionTitle) {
                if (/[?&]category=/i.test(href)) sectionTitle = 'Категория';
                else if (/[?&]type=/i.test(href)) sectionTitle = 'Тип';
                else if (/[?&]brand=/i.test(href)) sectionTitle = 'Бренд';
                else sectionTitle = 'Другое';
            }

            const key = sectionTitle.toLowerCase();
            if (!sectionMap.has(key)) {
                sectionMap.set(key, { title: sectionTitle, options: [], seen: new Set() });
            }
            const bucket = sectionMap.get(key);
            const optKey = href + '|' + name;
            if (bucket.seen.has(optKey)) return;
            bucket.seen.add(optKey);
            bucket.options.push({ name, href });
        });
    });

    sectionMap.forEach(bucket => {
        if (bucket.options.length) {
            sections.push({ title: bucket.title, options: bucket.options });
        }
    });
    return sections;
}
"""
