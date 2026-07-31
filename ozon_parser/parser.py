import re
import threading
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlencode, urlparse, urlunparse

from playwright.sync_api import Page, sync_playwright

from .browser import (
    close_session_context,
    is_blocked_page,
    is_seller_page,
    open_session_context,
    recover_access,
    safe_goto,
)
from .categories import CategoryLoader, CategoryTarget
from .parse_stats import ParseStats, ParseStatus, ParseTimer, SectionTiming, format_duration
from .config import (
    BrowserMode,
    DESKTOP_MODE,
    MAX_CATEGORY_RETRIES,
    MOBILE_MODE,
)
from .export import ProductRow
from .session import resolve_storage_state
from .utils import (
    has_bonus_text,
    human_category_delay,
    human_click_delay,
    human_delay,
    human_scroll_delay,
    normalize_seller_url,
    parse_bonus_points,
    parse_price,
    to_desktop_product_url,
    to_browser_url,
    with_price_sort_asc,
)


@dataclass
class ParseSettings:
    seller_url: str
    categories: list[CategoryTarget] | None
    min_price: float | None
    max_price: float | None
    max_products: int
    use_auth: bool
    import_browser_session: bool
    use_cdp: bool = True
    browser_mode: BrowserMode = DESKTOP_MODE


@dataclass
class _ParseState:
    detail_fetches: int = 0
    seen_urls: set[str] = field(default_factory=set)


class OzonParser:
    def __init__(
        self,
        on_progress: Callable[[str], None] | None = None,
        on_product: Callable[[ProductRow], None] | None = None,
        on_captcha: Callable[[], bool] | None = None,
        on_manual_bypass: Callable[[str | None], None] | None = None,
        on_status: Callable[[ParseStatus], None] | None = None,
    ):
        self.on_progress = on_progress or (lambda _msg: None)
        self.on_product = on_product or (lambda _p: None)
        self.on_captcha = on_captcha or (lambda: True)
        self.on_manual_bypass = on_manual_bypass or (lambda _inc: None)
        self.on_status = on_status or (lambda _s: None)
        self._stop_event = threading.Event()
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._session_mode: str = "standard"
        self._parse_timer_ref: ParseTimer | None = None
        self._parse_index = 0
        self._parse_total = 0

    def stop(self) -> None:
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def _open_browser(
        self,
        playwright,
        *,
        use_auth: bool,
        use_cdp: bool,
        import_browser: bool = True,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ):
        storage_state = (
            resolve_storage_state(use_auth, import_browser=import_browser)
            if browser_mode == DESKTOP_MODE
            else None
        )
        browser, context, page, mode = open_session_context(
            playwright,
            headless=False,
            storage_state=storage_state,
            use_cdp=use_cdp,
            progress=self.on_progress,
            prefer_ozon_profile=not use_auth,
            browser_mode=browser_mode,
            use_auth=use_auth,
        )
        if mode == "cdp":
            self.on_progress("Подключено к Chrome")
        elif mode == "ozon_persistent":
            self.on_progress("Подключено через профиль Chrome для Ozon")
        return browser, context, page, mode

    def run(self, settings: ParseSettings) -> tuple[list[ProductRow], ParseStats]:
        self._stop_event.clear()
        products: list[ProductRow] = []
        state = _ParseState()
        stats = ParseStats()
        timer = ParseTimer()

        with sync_playwright() as playwright:
            self._browser, self._context, self._page, self._session_mode = self._open_browser(
                playwright,
                use_auth=settings.use_auth,
                use_cdp=settings.use_cdp,
                import_browser=settings.import_browser_session,
                browser_mode=settings.browser_mode,
            )

            try:
                seller_url = normalize_seller_url(
                    settings.seller_url,
                    settings.browser_mode,
                )
                categories = settings.categories or []
                if not categories:
                    self.on_progress("Фильтры не выбраны — парсинг остановлен")
                    return products, stats

                total = len(categories)
                timer.reset()
                for idx, target in enumerate(categories, start=1):
                    if self.is_stopped():
                        break

                    if idx > 1:
                        self.on_progress("Пауза перед следующим фильтром...")
                        human_category_delay()

                    section, category_label, item_label = self._target_labels(target)
                    timer.restart_section()
                    self._parse_timer_ref = timer
                    self._parse_index = idx
                    self._parse_total = total
                    self._emit_status(timer, idx, total, section, category_label, item_label)

                    catalog_url = target.url or self._build_catalog_url(
                        seller_url,
                        target.param_key,
                        target.param_value,
                        settings.browser_mode,
                    )
                    batch = self._parse_catalog_with_retry(
                        catalog_url, settings, state, target,
                    )
                    products.extend(batch)

                    duration = timer.section_elapsed
                    stats.section_timings.append(
                        SectionTiming(
                            section=section,
                            category=category_label,
                            name=item_label,
                            index=idx,
                            total=total,
                            duration_sec=duration,
                            products_found=len(batch),
                        )
                    )
                    self.on_progress(
                        f"Готово {idx}/{total}: {section} — {item_label} | "
                        f"{len(batch)} тов. за {format_duration(duration)} | "
                        f"всего {format_duration(timer.total_elapsed)}"
                    )
                    self._emit_status(
                        timer, idx, total, section, category_label, item_label,
                        message=f"Завершено: {len(batch)} товаров",
                    )
            finally:
                if self._context:
                    close_session_context(self._browser, self._context, self._session_mode)

        stats.total_duration_sec = timer.total_elapsed
        products = self._dedupe_products(products)
        self.on_progress(f"Парсинг завершён за {stats.total_duration_fmt}")
        return products, stats

    def _dedupe_products(self, products: list[ProductRow]) -> list[ProductRow]:
        seen: set[str] = set()
        unique: list[ProductRow] = []
        for product in products:
            canonical_url = self._canonical_product_url(product.url)
            if canonical_url in seen:
                continue
            seen.add(canonical_url)
            product.url = canonical_url
            unique.append(product)
        return sorted(unique, key=lambda product: product.price_discounted)

    def _target_labels(self, target: CategoryTarget) -> tuple[str, str, str]:
        section = target.section or "Фильтр"
        if target.param_key == "category" or section == "Категория":
            if target.parent_name:
                return section, target.parent_name, target.name
            return section, target.name, target.name
        category = target.category_name or target.parent_name or "—"
        return section, category, target.name or target.id

    def _emit_status(
        self,
        timer: ParseTimer,
        index: int,
        total: int,
        section: str,
        category: str,
        name: str,
        message: str = "",
    ) -> None:
        self.on_status(
            ParseStatus(
                active_section=section,
                active_category=category,
                active_name=name,
                current_index=index,
                total_count=total,
                total_elapsed_sec=timer.total_elapsed,
                section_elapsed_sec=timer.section_elapsed,
                message=message or f"Парсинг {index}/{total}",
            )
        )

    def load_categories(
        self,
        seller_url: str,
        use_auth: bool = False,
        use_cdp: bool = True,
        browser_mode: BrowserMode = DESKTOP_MODE,
        on_roots: Callable[[list], None] | None = None,
        on_subcategories_begin: Callable[[int], None] | None = None,
        on_branch: Callable[[object], None] | None = None,
    ) -> list:
        with sync_playwright() as playwright:
            browser, context, page, mode = self._open_browser(
                playwright,
                use_auth=use_auth,
                use_cdp=use_cdp,
                browser_mode=browser_mode,
            )
            try:
                loader = CategoryLoader(page, browser_mode)
                return loader.load_category_tree(
                    seller_url,
                    self.on_progress,
                    self.on_manual_bypass,
                    on_roots=on_roots,
                    on_subcategories_begin=on_subcategories_begin,
                    on_branch=on_branch,
                )
            finally:
                close_session_context(browser, context, mode)

    def load_subcategories(
        self,
        seller_url: str,
        categories: list[CategoryTarget],
        use_auth: bool = False,
        use_cdp: bool = True,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> dict[str, list]:
        with sync_playwright() as playwright:
            browser, context, page, mode = self._open_browser(
                playwright,
                use_auth=use_auth,
                use_cdp=use_cdp,
                browser_mode=browser_mode,
            )
            try:
                loader = CategoryLoader(page, browser_mode)
                return loader.load_subcategories_for_categories(
                    seller_url, categories, self.on_progress, self.on_manual_bypass,
                )
            finally:
                close_session_context(browser, context, mode)

    def load_filters(
        self,
        seller_url: str,
        categories: list[CategoryTarget],
        use_auth: bool = False,
        use_cdp: bool = True,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> list:
        with sync_playwright() as playwright:
            browser, context, page, mode = self._open_browser(
                playwright,
                use_auth=use_auth,
                use_cdp=use_cdp,
                browser_mode=browser_mode,
            )
            try:
                loader = CategoryLoader(page, browser_mode)
                return loader.load_filters_for_categories(
                    seller_url, categories, self.on_progress, self.on_manual_bypass,
                )
            finally:
                close_session_context(browser, context, mode)

    def _build_catalog_url(
        self,
        seller_url: str,
        param_key: str | None = None,
        param_value: str | None = None,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> str:
        base = seller_url.rstrip("/")
        if param_key and param_value:
            parsed = urlparse(base)
            query = urlencode({param_key: param_value, "sorting": "price"})
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))
        return with_price_sort_asc(base + "/", browser_mode)

    def _ensure_price_sort_asc(self, page: Page) -> None:
        selectors = (
            'button:has-text("По возрастанию цены")',
            'span:has-text("По возрастанию цены")',
            'div:has-text("По возрастанию цены")',
            'button:has-text("Дешевле")',
            'span:has-text("Дешевле")',
            '[role="button"]:has-text("Дешевле")',
            '[data-widget*="sort"] button',
        )
        for selector in selectors:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click()
                    human_delay(1.0, 2.0)
                    self.on_progress("Сортировка: по возрастанию цены")
                    return
            except Exception:
                continue

    def _parse_catalog_with_retry(
        self,
        url: str,
        settings: ParseSettings,
        state: _ParseState,
        category: CategoryTarget | None = None,
    ) -> list[ProductRow]:
        for attempt in range(1, MAX_CATEGORY_RETRIES + 1):
            if self.is_stopped():
                return []

            results, loaded = self._parse_catalog(url, settings, state, category)
            if loaded:
                return results
            if attempt < MAX_CATEGORY_RETRIES:
                self.on_progress(f"Страница не загрузилась, повтор {attempt + 1}...")
                assert self._page
                recover_access(
                    self._page,
                    self.on_progress,
                    self.on_manual_bypass,
                    to_browser_url(url, settings.browser_mode),
                )
        return []

    def _navigate_to_catalog(
        self,
        page: Page,
        target: CategoryTarget | None,
        catalog_url: str,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> bool:
        routed_url = to_browser_url(catalog_url, browser_mode)
        if target:
            self.on_progress(
                f"Открываем выбранную категорию: {target.name or target.id}"
            )
        if target and is_seller_page(page):
            selectors: list[str] = []
            query = urlparse(target.url).query if target.url else ""
            if query:
                for part in query.split("&"):
                    if part:
                        selectors.append(f'a[href*="{part}"]')
                        selectors.append(f'a[href*="{part.replace("=", "%3D")}"]')
            if target.param_key and target.param_value:
                selectors.append(f'a[href*="{target.param_key}={target.param_value}"]')
                selectors.append(
                    f'a[href*="{target.param_key}%3D{target.param_value}"]'
                )
            for selector in selectors:
                try:
                    link = page.query_selector(selector)
                    if link and link.is_visible():
                        section = target.section or "Фильтр"
                        self.on_progress(f"Переход: {section} — {target.name or target.id}")
                        link.click()
                        human_delay(2.0, 4.0)
                        if is_blocked_page(page):
                            return False
                        current = page.url.lower()
                        if target.param_key and target.param_value:
                            key = target.param_key.lower()
                            val = target.param_value.lower()
                            if f"{key}={val}" in current or f"{key}%3d{val}" in current:
                                return True
                        if query and all(part.lower() in current for part in query.split("&") if part):
                            return True
                except Exception:
                    continue
        return safe_goto(
            page,
            routed_url,
            self.on_progress,
            on_manual_bypass=self.on_manual_bypass,
        )

    def _parse_catalog(
        self,
        url: str,
        settings: ParseSettings,
        state: _ParseState,
        category: CategoryTarget | None = None,
    ) -> tuple[list[ProductRow], bool]:
        assert self._page is not None
        page = self._page
        results: list[ProductRow] = []
        routed_url = with_price_sort_asc(url, settings.browser_mode)
        target = category

        if not self._navigate_to_catalog(
            page,
            target,
            routed_url,
            settings.browser_mode,
        ):
            if not recover_access(page, self.on_progress, self.on_manual_bypass, routed_url):
                return results, False
            if not self._navigate_to_catalog(
                page,
                target,
                routed_url,
                settings.browser_mode,
            ):
                return results, False

        self._ensure_price_sort_asc(page)

        empty_rounds = 0
        processed: set[str] = set()

        while not self.is_stopped() and len(results) < settings.max_products and empty_rounds < 4:
            if is_blocked_page(page):
                if not recover_access(page, self.on_progress, self.on_manual_bypass, routed_url):
                    break
                self._navigate_to_catalog(
                    page,
                    target,
                    routed_url,
                    settings.browser_mode,
                )

            cards = self._extract_product_cards(page, settings.browser_mode)
            new_cards = [c for c in cards if c.get("href") not in processed]

            if not new_cards:
                empty_rounds += 1
            else:
                empty_rounds = 0

            for card in new_cards:
                if self.is_stopped() or len(results) >= settings.max_products:
                    break
                href = card.get("href", "")
                if href:
                    processed.add(href)
                product = self._process_card(card, settings, state)
                if product:
                    results.append(product)
                    self.on_progress(f"Найдено: {len(results)} — {product.name[:50]}")
                    if target and self._parse_timer_ref:
                        section, cat, name = self._target_labels(target)
                        self._emit_status(
                            self._parse_timer_ref,
                            self._parse_index,
                            self._parse_total,
                            section,
                            cat,
                            name,
                            message=f"Найдено товаров: {len(results)}",
                        )
                human_click_delay()

            if not self._scroll_for_more(page):
                break
            human_scroll_delay()

        page_loaded = bool(processed) or bool(results) or not is_blocked_page(page)
        return results, page_loaded

    def _extract_product_cards(
        self,
        page: Page,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> list[dict]:
        script = """
        ({ containerSelectors }) => {
            const cards = new Map();
            const normalize = (text) => (text || '').trim().replace(/\\s+/g, ' ');
            const findContainer = (link) => {
                for (const selector of containerSelectors) {
                    const container = link.closest(selector);
                    if (container) return container;
                }
                return link.parentElement || link;
            };
            document.querySelectorAll('a[href*="/product/"]').forEach(link => {
                const href = link.href || link.getAttribute('href') || '';
                if (!href.includes('/product/')) return;
                let canonical = href.split('#')[0].split('?')[0];
                const container = findContainer(link);
                const text = container.innerText || '';
                const candidates = [];
                container.querySelectorAll('a[href*="/product/"], [title]').forEach(el => {
                    const value = normalize(el.textContent || el.getAttribute('title') || '');
                    if (!value || value.length < 3 || /₽/.test(value)) return;
                    if (/балл|бонус|отзыв/i.test(value)) return;
                    candidates.push(value);
                });
                const name = candidates.sort((a, b) => b.length - a.length)[0] || '';
                const card = {
                    href: canonical,
                    name,
                    text,
                    html: container.innerHTML || ''
                };
                const previous = cards.get(canonical);
                if (!previous || card.text.length > previous.text.length) {
                    cards.set(canonical, card);
                }
            });
            return Array.from(cards.values());
        }
        """
        common = [
            '[data-index]',
            'article',
            '[data-widget*="searchResults"] article',
            '[data-widget*="tile"]',
        ]
        mode_specific = (
            [
                '[data-widget="searchResultsV2"] > div',
                '[data-widget="searchResultsV2"] article',
            ]
            if browser_mode == DESKTOP_MODE
            else [
                '[data-widget*="searchResults"] > div',
                '[data-widget*="catalog"] article',
                '[data-widget*="product"]',
            ]
        )
        try:
            return page.evaluate(
                script,
                {"containerSelectors": mode_specific + common},
            ) or []
        except Exception:
            return []

    def _card_has_bonus(self, card: dict) -> bool:
        text = card.get("text", "")
        html = card.get("html", "")
        if has_bonus_text(text) or has_bonus_text(html):
            return True
        html_lower = html.lower()
        if "отзыв" in html_lower and ("балл" in html_lower or "bonus" in html_lower):
            return True
        if "review" in html_lower and "bonus" in html_lower:
            return True
        return False

    def _process_card(self, card: dict, settings: ParseSettings, state: _ParseState) -> ProductRow | None:
        href = self._canonical_product_url(card.get("href", ""))
        text = card.get("text", "")
        if not href or href in state.seen_urls:
            return None

        detail = self._parse_card_text(text)
        detail["url"] = href

        name = card.get("name") or detail.get("name") or self._extract_name_from_text(text)
        price_disc = detail.get("price_discounted")
        price_orig = detail.get("price_original")
        bonus = detail.get("bonus_points")
        card_has_bonus = self._card_has_bonus(card)

        if not name or price_disc is None or (card_has_bonus and bonus is None):
            full = self._fetch_product_detail(
                href,
                state,
                settings.browser_mode,
            )
            if full:
                name = name or full.get("name")
                price_disc = price_disc if price_disc is not None else full.get("price_discounted")
                price_orig = price_orig if price_orig is not None else full.get("price_original")
                bonus = bonus if bonus is not None else full.get("bonus_points")

        if not name or price_disc is None:
            return None
        if price_orig is None or price_orig < price_disc:
            price_orig = price_disc
        if bonus is None:
            bonus = 0
        if settings.min_price is not None and price_disc < settings.min_price:
            return None
        if settings.max_price is not None and price_disc > settings.max_price:
            return None

        state.seen_urls.add(href)
        return ProductRow(
            name=name,
            price_discounted=price_disc,
            price_original=price_orig,
            url=to_desktop_product_url(href),
            bonus_points=bonus,
        )

    def _parse_card_text(self, text: str) -> dict:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        prices: list[float] = []
        for line in lines:
            if has_bonus_text(line):
                continue
            prices.extend(
                price
                for match in re.finditer(r"([\d\s\u2009]+)\s*₽", line)
                if (price := parse_price(match.group(0))) is not None
            )
        bonus = next((parse_bonus_points(ln) for ln in lines if has_bonus_text(ln)), None)
        name = next(
            (
                ln
                for ln in lines
                if ln
                and not re.search(r"\d[\d\s]*\s*₽", ln)
                and not has_bonus_text(ln)
                and not re.fullmatch(r"[-−+]?\d+\s*%", ln)
            ),
            lines[0] if lines else "",
        )
        price_disc = min(prices) if prices else None
        price_orig = max(prices) if len(prices) > 1 else price_disc
        return {"name": name, "price_discounted": price_disc, "price_original": price_orig, "bonus_points": bonus}

    def _extract_name_from_text(self, text: str) -> str:
        for line in text.split("\n"):
            line = line.strip()
            if line and not re.search(r"\d[\d\s]*\s*₽", line) and not has_bonus_text(line):
                return line
        return text.split("\n")[0].strip() if text else ""

    def _fetch_product_detail(
        self,
        href: str,
        state: _ParseState,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> dict | None:
        assert self._page is not None
        detail_page = None
        url = to_browser_url(href, browser_mode)
        try:
            detail_page = self._page.context.new_page()
            human_delay(1.0, 2.0)
            if not safe_goto(
                detail_page,
                url,
                self.on_progress,
                max_retries=2,
                on_manual_bypass=self.on_manual_bypass,
            ):
                return None
            state.detail_fetches += 1
            text = detail_page.evaluate("() => document.body.innerText") or ""
            prices = self._extract_prices(detail_page, text)
            return {
                "has_bonus": has_bonus_text(text),
                "name": self._extract_product_name(detail_page, browser_mode),
                "price_discounted": prices.get("discounted"),
                "price_original": prices.get("original"),
                "bonus_points": self._extract_bonus(detail_page, text) or 0,
                "url": href,
            }
        except Exception:
            return None
        finally:
            if detail_page:
                try:
                    detail_page.close()
                except Exception:
                    pass

    def _extract_product_name(
        self,
        page: Page,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> str | None:
        selectors = ["h1"]
        if browser_mode == MOBILE_MODE:
            selectors.extend(
                (
                    '[data-widget="webProductHeading"]',
                    '[data-widget="mobileProductHeading"]',
                    '[data-widget*="productTitle"]',
                    '[data-widget*="heading"]',
                )
            )
        else:
            selectors.extend(
                ('[data-widget="webProductHeading"]', '[data-widget="webTitle"]')
            )
        for sel in selectors:
            el = page.query_selector(sel)
            if el and (text := el.inner_text().strip()):
                return text
        return None

    def _extract_prices(self, page: Page, text: str) -> dict:
        prices: list[float] = []
        for sel in (
            '[data-widget="webPrice"]',
            '[data-widget="webSale"]',
            '[data-widget="mobilePrice"]',
            '[data-widget*="price"]',
            '[data-widget*="Price"]',
        ):
            el = page.query_selector(sel)
            if not el:
                continue
            widget_text = el.inner_text()
            prices.extend(
                p
                for match in re.finditer(r"([\d\s\u2009]+)\s*₽", widget_text)
                if (p := parse_price(match.group(0))) is not None
            )
        if not prices:
            prices = [
                p
                for match in re.finditer(r"([\d\s\u2009]+)\s*₽", text)
                if (p := parse_price(match.group(0))) is not None
            ]
        if not prices:
            return {"discounted": None, "original": None}
        return {"discounted": min(prices), "original": max(prices)}

    def _extract_bonus(self, page: Page, text: str) -> int | None:
        for line in text.split("\n"):
            if has_bonus_text(line) and (bonus := parse_bonus_points(line)):
                return bonus
        return parse_bonus_points(text)

    def _scroll_for_more(self, page: Page) -> bool:
        if is_blocked_page(page):
            return False
        try:
            prev = page.evaluate("() => document.body.scrollHeight")
            page.evaluate("window.scrollBy(0, window.innerHeight * 0.85)")
            human_delay(2.0, 3.5)
            return page.evaluate("() => document.body.scrollHeight") > prev
        except Exception:
            return False

    @staticmethod
    def _canonical_product_url(url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(to_desktop_product_url(url))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
