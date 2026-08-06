import json
import re
import random
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable
from urllib.parse import urlencode, urlparse, urlunparse

from playwright.sync_api import Page, sync_playwright

from .browser import (
    close_session_context,
    ensure_ozon_session_ready,
    extract_incident_id,
    is_access_restricted,
    is_blocked_page,
    is_captcha_page,
    is_empty_catalog_filter_page,
    is_seller_page,
    open_session_context,
    recover_access,
    safe_goto,
    try_reset_catalog_filters,
    wait_for_ozon_ready,
)
from .categories import CategoryLoader, CategoryTarget
from .seller_discovery import discover_marketplace_sellers
from .composer_products import extract_product_cards_from_composer
from .parse_stats import ParseStats, ParseStatus, ParseTimer, SectionTiming, format_duration
from .config import (
    ALL_SELLERS_PATH,
    BLOCK_AUTO_WAIT_MAX_ATTEMPTS,
    BLOCK_AUTO_WAIT_POLL_SEC,
    BLOCK_AUTO_WAIT_SEC,
    BLOCK_POST_CLEAR_COOLDOWN_MAX,
    BLOCK_POST_CLEAR_COOLDOWN_MIN,
    BrowserMode,
    CATEGORY_REQUIRED_PARSE_MODES,
    CHECKPOINT_SAVE_EVERY_PRODUCTS,
    DESKTOP_MODE,
    GLOBAL_CATALOG_PATH,
    GLOBAL_FIRST_CATEGORY_PAUSE_MAX,
    GLOBAL_FIRST_CATEGORY_PAUSE_MIN,
    GLOBAL_LARGE_SELECTION_THRESHOLD,
    GLOBAL_PARSE_MODES,
    GLOBAL_SESSION_MAX_CATEGORIES,
    GLOBAL_SESSION_MAX_PRODUCTS,
    GLOBAL_WAVE_COOLDOWN_MAX,
    GLOBAL_WAVE_COOLDOWN_MIN,
    MAX_CATEGORY_RETRIES,
    MAX_MARKETPLACE_SELLERS,
    MAX_PRODUCT_DETAIL_FETCHES,
    MOBILE_MODE,
    PARSE_MODE_ALL_SELLERS_CATEGORIES,
    PARSE_MODE_ALL_SELLERS_FULL,
    PARSE_MODE_GLOBAL_CATEGORIES,
    PARSE_MODE_SELLER_CATEGORIES,
    PARSE_MODE_SELLER_FULL,
    ParseMode,
    PRODUCT_BATCH_PAUSE_MAX,
    PRODUCT_BATCH_PAUSE_MIN,
    PRODUCT_BATCH_SIZE,
    PRODUCT_MEGA_BATCH_SIZE,
    PRODUCT_MEGA_PAUSE_MAX,
    PRODUCT_MEGA_PAUSE_MIN,
    SAFE_CATEGORY_BATCH_SIZE,
    SAFE_CATEGORY_BREAK_MAX,
    SAFE_CATEGORY_BREAK_MIN,
    SAFE_GOTO_MAX_RETRIES,
    SAFE_SCROLL_BATCH_SIZE,
    SAFE_SCROLL_BREAK_MAX,
    SAFE_SCROLL_BREAK_MIN,
    SESSION_MAX_CATEGORIES,
    SESSION_MAX_PRODUCTS,
    WAVE_COOLDOWN_MAX,
    WAVE_COOLDOWN_MIN,
)
from .export import ProductRow
from .parse_checkpoint import (
    clear_checkpoint,
    load_checkpoint,
    save_checkpoint,
    target_key,
)
from .session import resolve_storage_state
from .utils import (
    extract_ozon_category_id,
    extract_ozon_product_id,
    has_bonus_text,
    human_category_delay,
    human_click_delay,
    human_delay,
    human_scroll_delay,
    normalize_seller_url,
    parse_bonus_points,
    parse_price,
    pick_product_name,
    to_desktop_product_url,
    to_browser_url,
    route_browser_url,
    sanitize_catalog_url,
    with_price_sort_asc,
    name_from_product_url,
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
    parse_mode: ParseMode = PARSE_MODE_SELLER_CATEGORIES
    bonus_only: bool = True

    @property
    def specific_seller(self) -> bool:
        return self.parse_mode in (PARSE_MODE_SELLER_FULL, PARSE_MODE_SELLER_CATEGORIES)

    @property
    def uses_global_catalog(self) -> bool:
        return self.parse_mode in GLOBAL_PARSE_MODES

    @property
    def requires_category_selection(self) -> bool:
        return self.parse_mode in CATEGORY_REQUIRED_PARSE_MODES


@dataclass
class _ParseState:
    detail_fetches: int = 0
    seen_urls: set[str] = field(default_factory=set)
    seen_product_ids: set[str] = field(default_factory=set)


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
        self._run_products_found = 0
        self._since_batch_pause = 0
        self._since_mega_pause = 0
        self._block_auto_waits_used = 0
        self._global_nav_fallbacks_used = 0
        self._composer_categories_done = 0
        self._global_bulk_mode = False

    def _route_url(self, url: str, browser_mode: BrowserMode) -> str:
        return route_browser_url(url, browser_mode, self._session_mode)

    def stop(self) -> None:
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def _protective_pause(self, minimum: float, maximum: float, message: str) -> bool:
        seconds = random.uniform(minimum, maximum)
        self.on_progress(f"{message}: {int(seconds)} сек.")
        return not self._stop_event.wait(seconds)

    def _wave_budgets(self, settings: ParseSettings) -> tuple[int, int]:
        """Soft wave sizes: auto-cooldown then continue in the same run."""
        category_count = len(settings.categories or [])
        if settings.uses_global_catalog:
            cats = GLOBAL_SESSION_MAX_CATEGORIES
            products = GLOBAL_SESSION_MAX_PRODUCTS
            if category_count >= GLOBAL_LARGE_SELECTION_THRESHOLD:
                cats = 1
                products = min(products, 50)
            return cats, min(products, settings.max_products)
        if settings.max_products >= 5000:
            return min(SESSION_MAX_CATEGORIES, 3), min(SESSION_MAX_PRODUCTS, 200)
        if settings.max_products >= 1000:
            return SESSION_MAX_CATEGORIES, SESSION_MAX_PRODUCTS
        return SESSION_MAX_CATEGORIES, min(SESSION_MAX_PRODUCTS, settings.max_products)

    # Back-compat alias for older tests/callers.
    def _session_budgets(self, settings: ParseSettings) -> tuple[int, int]:
        return self._wave_budgets(settings)

    def _wave_cooldown_range(self, settings: ParseSettings) -> tuple[float, float]:
        if settings.uses_global_catalog:
            return GLOBAL_WAVE_COOLDOWN_MIN, GLOBAL_WAVE_COOLDOWN_MAX
        return WAVE_COOLDOWN_MIN, WAVE_COOLDOWN_MAX

    def _pause_after_product_batch(self) -> bool:
        """Auto-pause every N products so long 10k runs stay under antibot radar."""
        self._run_products_found += 1
        self._since_batch_pause += 1
        self._since_mega_pause += 1
        total_found = self._run_products_found

        if self._since_mega_pause >= PRODUCT_MEGA_BATCH_SIZE:
            ok = self._protective_pause(
                PRODUCT_MEGA_PAUSE_MIN,
                PRODUCT_MEGA_PAUSE_MAX,
                (
                    f"Длинная автопауза после {PRODUCT_MEGA_BATCH_SIZE} товаров "
                    f"(всего собрано {total_found})"
                ),
            )
            self._since_mega_pause = 0
            self._since_batch_pause = 0
            return ok

        if self._since_batch_pause >= PRODUCT_BATCH_SIZE:
            ok = self._protective_pause(
                PRODUCT_BATCH_PAUSE_MIN,
                PRODUCT_BATCH_PAUSE_MAX,
                (
                    f"Автопауза после {PRODUCT_BATCH_SIZE} товаров "
                    f"(всего собрано {total_found})"
                ),
            )
            self._since_batch_pause = 0
            return ok
        return True

    def _park_blocked_tab(self) -> None:
        """Stop Ozon SPA polling on a fab_ page (no F5 — that prolongs the block)."""
        if not self._page:
            return
        try:
            current = str(getattr(self._page, "url", "") or "")
        except Exception:
            current = ""
        if not current or current.startswith("about:"):
            return
        self.on_progress("Останавливаем активность вкладки (about:blank), без F5...")
        try:
            self._page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            self.on_progress(f"Не удалось увести вкладку: {exc}")

    def _wait_out_access_block(
        self,
        settings: ParseSettings,
        *,
        reopen_url: str | None = None,
    ) -> bool:
        """Park tab, wait passively, then probe Ozon — never reload a fab_ page."""
        if self._block_auto_waits_used >= BLOCK_AUTO_WAIT_MAX_ATTEMPTS:
            self.on_progress(
                "Лимит автоожиданий блокировки исчерпан. "
                "Прогресс сохранён — продолжите позже."
            )
            return False
        self._block_auto_waits_used += 1
        wait_sec = int(BLOCK_AUTO_WAIT_SEC)
        incident = None
        if self._page:
            try:
                incident = extract_incident_id(self._page)
            except Exception:
                incident = None
        self.on_progress(
            "Ozon ограничил доступ (fab_/«нет соединения») — "
            f"автоожидание до {wait_sec} сек без F5 "
            f"(попытка {self._block_auto_waits_used}/{BLOCK_AUTO_WAIT_MAX_ATTEMPTS})"
            + (f", инцидент {incident}" if incident else "")
            + "..."
        )
        # about:blank stops Composer/SPA retries that keep the incident alive.
        self._park_blocked_tab()

        deadline = time.time() + BLOCK_AUTO_WAIT_SEC
        while time.time() < deadline and not self.is_stopped():
            # Do not treat about:blank as «доступ восстановлен».
            remaining = int(max(0, deadline - time.time()))
            self.on_progress(f"Ждём снятия блокировки… осталось ~{remaining} сек")
            if self._stop_event.wait(BLOCK_AUTO_WAIT_POLL_SEC):
                return False
        if self.is_stopped():
            return False

        probe = reopen_url or "https://www.ozon.ru/"
        self.on_progress("Проверяем доступ после ожидания...")
        if not self._page:
            return False
        opened = safe_goto(
            self._page,
            probe,
            self.on_progress,
            max_retries=1,
            on_manual_bypass=self.on_manual_bypass,
        )
        if not opened or is_access_restricted(self._page):
            self.on_progress("Блокировка не снялась за отведённое время")
            return False
        human_delay(BLOCK_POST_CLEAR_COOLDOWN_MIN, BLOCK_POST_CLEAR_COOLDOWN_MAX)
        self.on_progress("Доступ восстановлен — продолжаем сбор")
        return True

    def _maybe_start_next_wave(
        self,
        settings: ParseSettings,
        *,
        wave_cats: int,
        wave_products_gained: int,
        wave_cats_budget: int,
        wave_products_budget: int,
        total_products: int,
        goal: int,
    ) -> tuple[bool, int, int]:
        """If a soft wave budget is hit, cool down and reset counters (same run)."""
        need_wave = (
            wave_cats >= wave_cats_budget
            or wave_products_gained >= wave_products_budget
        )
        if not need_wave or total_products >= goal:
            return True, wave_cats, wave_products_gained
        cool_min, cool_max = self._wave_cooldown_range(settings)
        ok = self._protective_pause(
            cool_min,
            cool_max,
            (
                f"Автопауза волны: +{wave_products_gained} товаров / "
                f"{wave_cats} категорий (прогресс {total_products}/{goal})"
            ),
        )
        if not ok:
            return False, wave_cats, wave_products_gained
        if self._page and is_access_restricted(self._page):
            if not self._wait_out_access_block(settings):
                return False, wave_cats, wave_products_gained
        return True, 0, 0

    def _category_batch_settings(self, settings: ParseSettings) -> tuple[int, float, float]:
        # Same batch pacing for seller and global DOM leaf collection.
        return SAFE_CATEGORY_BATCH_SIZE, SAFE_CATEGORY_BREAK_MIN, SAFE_CATEGORY_BREAK_MAX

    def _pause_before_category(
        self,
        settings: ParseSettings,
        session_started: int,
    ) -> bool:
        """Protective pause before opening the next category — always sequential.

        Global mode uses the same human pacing as a specific seller: open one
        leaf listing, scroll DOM, then pause before the next leaf.
        """
        batch_size, break_min, break_max = self._category_batch_settings(settings)
        if session_started == 0 and not settings.specific_seller:
            return self._protective_pause(
                GLOBAL_FIRST_CATEGORY_PAUSE_MIN,
                GLOBAL_FIRST_CATEGORY_PAUSE_MAX,
                "Пауза перед первой категорией общего каталога",
            )
        if session_started > 0 and session_started % batch_size == 0:
            return self._protective_pause(
                break_min,
                break_max,
                "Защитная пауза после группы категорий",
            )
        if session_started > 0:
            self.on_progress("Пауза перед следующим фильтром...")
            human_category_delay()
        return True

    def _persist_progress(
        self,
        settings: ParseSettings,
        completed_targets: set[str],
        products: list[ProductRow],
    ) -> list[ProductRow]:
        products = self._dedupe_products(products)
        save_checkpoint(settings, completed_targets, products)
        return products

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
        elif mode == "mobile_guest_cdp":
            self.on_progress("Мобильный режим через Chrome для Ozon")
        self._session_mode = mode
        return browser, context, page, mode

    def run(self, settings: ParseSettings) -> tuple[list[ProductRow], ParseStats]:
        self._stop_event.clear()
        products: list[ProductRow] = []
        state = _ParseState()
        completed_targets: set[str] = set()
        stats = ParseStats()
        timer = ParseTimer()
        wave_cats_budget, wave_products_budget = self._wave_budgets(settings)
        wave_cats = 0
        wave_products_gained = 0
        categories_opened = 0
        self._run_products_found = 0
        self._since_batch_pause = 0
        self._since_mega_pause = 0
        self._block_auto_waits_used = 0
        self._global_nav_fallbacks_used = 0
        self._composer_categories_done = 0
        # Global product collection: open each selected /category/{id}/ listing
        # and scroll DOM cards. Composer-first product fetch caused fab_.
        self._global_bulk_mode = False

        checkpoint = load_checkpoint(settings)
        if checkpoint:
            products = self._dedupe_products(checkpoint.products)
            completed_targets = set(checkpoint.completed_targets)
            state.seen_urls = {
                self._canonical_product_url(product.url) for product in products
            }
            state.seen_product_ids = {
                pid
                for product in products
                if (pid := extract_ozon_product_id(product.url))
            }
            self._run_products_found = len(products)
            self._since_mega_pause = len(products) % PRODUCT_MEGA_BATCH_SIZE
            self.on_progress(
                f"Продолжение сохранённого сбора: {len(products)} товаров, "
                f"{len(completed_targets)} категорий завершено"
            )

        products_at_run_start = len(products)
        category_count = len(settings.categories or [])
        mode_hint = self._parse_mode_label(settings.parse_mode)
        self.on_progress(
            f"Непрерывный безопасный режим ({mode_hint}): карточки каталога, "
            f"без открытия страниц товаров. До {settings.max_products} товаров на категорию, "
            f"категорий: {category_count}. "
            f"{'Только с баллами за отзыв. ' if settings.bonus_only else 'Все товары (включая без баллов). '}"
            f"Автопаузы каждые {PRODUCT_BATCH_SIZE} товаров и после волны "
            f"({wave_cats_budget} кат. / +{wave_products_budget} тов.)."
        )
        if settings.parse_mode == PARSE_MODE_GLOBAL_CATEGORIES:
            self.on_progress(
                "Общий каталог: каждая отмеченная категория открывается как "
                "/category/{id}/ (только выбранный раздел, не витрина /seller/)."
            )
        if settings.max_products >= 1000:
            self.on_progress(
                "Большой объём: парсер сам делает длинные паузы и может ждать "
                "разбан без перезагрузки. Не жмите F5 в Chrome."
            )
        if (
            settings.parse_mode == PARSE_MODE_GLOBAL_CATEGORIES
            and category_count >= GLOBAL_LARGE_SELECTION_THRESHOLD
        ):
            self.on_progress(
                "Большой выбор категорий: волны укорочены, автопаузы чаще — "
                "один запуск может идти много часов до цели."
            )
        if products_at_run_start:
            self.on_progress(
                f"Уже в checkpoint: {products_at_run_start} товаров — продолжаем до цели."
            )

        with sync_playwright() as playwright:
            self._browser, self._context, self._page, self._session_mode = self._open_browser(
                playwright,
                use_auth=settings.use_auth,
                use_cdp=settings.use_cdp,
                import_browser=settings.import_browser_session,
                browser_mode=settings.browser_mode,
            )

            try:
                seller_url = (
                    normalize_seller_url(
                        settings.seller_url,
                        settings.browser_mode,
                        self._session_mode,
                    )
                    if settings.specific_seller
                    else self._route_url(
                        "https://www.ozon.ru" + ALL_SELLERS_PATH,
                        settings.browser_mode,
                    )
                )
                categories = settings.categories or []
                if settings.requires_category_selection and not categories:
                    self.on_progress("Категории не выбраны — парсинг остановлен")
                    return products, stats

                if not ensure_ozon_session_ready(
                    self._page,
                    self.on_progress,
                    warmup_url=seller_url,
                    on_manual_bypass=self.on_manual_bypass,
                ):
                    self.on_progress(
                        "Ozon не готов к парсингу. Откройте Chrome, дождитесь загрузки "
                        "сайта без «Похоже, нет соединения» / fab_ и повторите."
                    )
                    self._persist_progress(settings, completed_targets, products)
                    stats.total_duration_sec = timer.total_elapsed
                    return products, stats

                # After a heavy catalogue crawl the next hop is the most ban-prone.
                # Stay on the current tab and cool down before the first category URL.
                if settings.parse_mode == PARSE_MODE_GLOBAL_CATEGORIES:
                    self.on_progress(
                        "Пауза перед парсингом общего каталога "
                        f"({int(GLOBAL_FIRST_CATEGORY_PAUSE_MIN)}–"
                        f"{int(GLOBAL_FIRST_CATEGORY_PAUSE_MAX)} сек)..."
                    )
                    human_delay(
                        GLOBAL_FIRST_CATEGORY_PAUSE_MIN,
                        GLOBAL_FIRST_CATEGORY_PAUSE_MAX,
                    )
                    if is_access_restricted(self._page):
                        self.on_progress(
                            "После паузы доступ всё ещё ограничен — ждём разбан..."
                        )
                        if not self._wait_out_access_block(
                            settings, reopen_url=seller_url
                        ):
                            self._persist_progress(settings, completed_targets, products)
                            stats.total_duration_sec = timer.total_elapsed
                            return products, stats

                if is_access_restricted(self._page):
                    self.on_progress(
                        "Ozon проверяет браузер или ограничил доступ (fab_). "
                        "Подтвердите в Chrome и нажмите «Продолжить»."
                    )
                    self._persist_progress(settings, completed_targets, products)
                    if not recover_access(
                        self._page,
                        self.on_progress,
                        self.on_manual_bypass,
                        seller_url,
                    ):
                        stats.total_duration_sec = timer.total_elapsed
                        return products, stats
                    if not ensure_ozon_session_ready(
                        self._page,
                        self.on_progress,
                        warmup_url=seller_url,
                        on_manual_bypass=self.on_manual_bypass,
                    ):
                        stats.total_duration_sec = timer.total_elapsed
                        return products, stats

                # Avoid a bare /seller/ hop before the first category URL.
                # First pending target already opens /seller/?category=… once.
                if settings.specific_seller and not is_seller_page(self._page):
                    if not safe_goto(
                        self._page,
                        seller_url,
                        self.on_progress,
                        max_retries=SAFE_GOTO_MAX_RETRIES,
                        on_manual_bypass=self.on_manual_bypass,
                    ):
                        self.on_progress(
                            "Не удалось открыть страницу магазина без блокировки. "
                            "Прогресс сохранён — подождите 15–30 минут и продолжите."
                        )
                        self._persist_progress(settings, completed_targets, products)
                        stats.total_duration_sec = timer.total_elapsed
                        return products, stats

                parse_jobs = self._build_parse_jobs(settings, categories, self._page)
                if not parse_jobs:
                    self.on_progress("Нет задач для парсинга — проверьте режим и выбор категорий")
                    return products, stats

                total = sum(len(job_cats) for _job_seller, job_cats in parse_jobs)
                timer.reset()
                interrupted = False
                access_stopped = False
                idx = 0

                for job_seller_url, job_categories in parse_jobs:
                    if access_stopped or self.is_stopped():
                        break
                    active_seller = job_seller_url or seller_url
                    if job_seller_url:
                        self.on_progress(f"Магазин: {job_seller_url}")
                        if not safe_goto(
                            self._page,
                            job_seller_url,
                            self.on_progress,
                            max_retries=SAFE_GOTO_MAX_RETRIES,
                            on_manual_bypass=self.on_manual_bypass,
                        ):
                            self.on_progress(
                                f"Пропуск магазина (не открылся): {job_seller_url}"
                            )
                            continue

                    for target in job_categories:
                        idx += 1
                        if access_stopped:
                            break
                        if self.is_stopped():
                            interrupted = True
                            break

                        key = target_key(target, job_seller_url)
                        if key in completed_targets:
                            self.on_progress(
                                f"Пропуск завершённой категории {idx}/{total}: "
                                f"{target.name or target.id}"
                            )
                            continue

                        cont, wave_cats, wave_products_gained = self._maybe_start_next_wave(
                            settings,
                            wave_cats=wave_cats,
                            wave_products_gained=wave_products_gained,
                            wave_cats_budget=wave_cats_budget,
                            wave_products_budget=wave_products_budget,
                            total_products=len(products),
                            goal=settings.max_products,
                        )
                        if not cont:
                            interrupted = True
                            break

                        if not self._pause_before_category(settings, categories_opened):
                            interrupted = True
                            break

                        if is_access_restricted(self._page):
                            self.on_progress(
                                "Обнаружен fab_/блокировка перед открытием категории."
                            )
                            self._persist_progress(settings, completed_targets, products)
                            if self._wait_out_access_block(
                                settings,
                                reopen_url=active_seller or "https://www.ozon.ru/",
                            ):
                                if is_access_restricted(self._page):
                                    access_stopped = True
                                    interrupted = True
                                    break
                            else:
                                access_stopped = True
                                interrupted = True
                                break

                        section, category_label, item_label = self._target_labels(target)
                        timer.restart_section()
                        self._parse_timer_ref = timer
                        self._parse_index = idx
                        self._parse_total = total
                        self.on_progress(
                            f"Категория {idx}/{total} по очереди: "
                            f"{category_label or item_label or target.name or target.id}"
                        )
                        self._emit_status(timer, idx, total, section, category_label, item_label)

                        if settings.parse_mode == PARSE_MODE_GLOBAL_CATEGORIES:
                            try:
                                catalog_url = self._build_global_catalog_url(
                                    target,
                                    settings.browser_mode,
                                )
                            except ValueError as exc:
                                self.on_progress(str(exc))
                                continue
                            cat_id = self._resolve_global_category_id(target)
                            self.on_progress(
                                "Открываем выбранную категорию "
                                f"«{target.name or target.category_name or cat_id}» "
                                f"(id={cat_id}): {catalog_url}"
                            )
                        else:
                            catalog_url = self._catalog_url_for_target(
                                active_seller,
                                target,
                                settings,
                            )

                        def on_batch_progress(batch_products: list[ProductRow]) -> None:
                            nonlocal products
                            combined = self._dedupe_products(products + batch_products)
                            self._persist_progress(settings, completed_targets, combined)

                        per_category_cap = settings.max_products

                        before_count = len(products)
                        batch, category_completed = self._parse_catalog_with_retry(
                            catalog_url,
                            settings,
                            state,
                            target,
                            product_cap=per_category_cap,
                            on_progress_save=on_batch_progress,
                            specific_seller=bool(job_seller_url) or settings.specific_seller,
                        )
                        products.extend(batch)
                        products = self._persist_progress(
                            settings,
                            completed_targets,
                            products,
                        )
                        gained = max(0, len(products) - before_count)
                        wave_products_gained += gained
                        wave_cats += 1
                        categories_opened += 1

                        if category_completed:
                            completed_targets.add(key)
                            self._persist_progress(settings, completed_targets, products)
                        elif self._page and is_access_restricted(self._page):
                            self._persist_progress(settings, completed_targets, products)
                            if self._wait_out_access_block(
                                settings, reopen_url=catalog_url
                            ):
                                if not self._pause_before_category(settings, categories_opened):
                                    interrupted = True
                                    break
                                retry_batch, category_completed = self._parse_catalog_with_retry(
                                    catalog_url,
                                    settings,
                                    state,
                                    target,
                                    product_cap=settings.max_products,
                                    on_progress_save=on_batch_progress,
                                    specific_seller=bool(job_seller_url) or settings.specific_seller,
                                )
                                if retry_batch:
                                    products.extend(retry_batch)
                                    products = self._persist_progress(
                                        settings, completed_targets, products
                                    )
                                    gained = len(retry_batch)
                                    wave_products_gained += gained
                                    batch = list(batch) + list(retry_batch)
                                if category_completed:
                                    completed_targets.add(key)
                                    self._persist_progress(
                                        settings, completed_targets, products
                                    )
                                elif self._page and is_access_restricted(self._page):
                                    interrupted = True
                                    access_stopped = True
                                else:
                                    interrupted = not category_completed
                            else:
                                interrupted = True
                                access_stopped = True
                        else:
                            interrupted = not category_completed

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
                            f"всего собрано {len(products)}"
                        )
                        self._emit_status(
                            timer, idx, total, section, category_label, item_label,
                            message=f"Завершено: {len(batch)} товаров",
                        )
                        if access_stopped:
                            self.on_progress(
                                "Сбор приостановлен из‑за блокировки. Прогресс сохранён; "
                                "после разбана запустите снова с теми же категориями."
                            )
                            break
                        if interrupted and not category_completed:
                            if self.is_stopped():
                                self.on_progress("Остановлено пользователем. Прогресс сохранён.")
                                break
                            self.on_progress(
                                f"Категория не завершена ({item_label}). "
                                "Переходим к следующей; при повторном запуске попробуем снова."
                            )
                            interrupted = True
                            continue

                all_target_keys = {
                    target_key(target, job_seller)
                    for job_seller, job_cats in parse_jobs
                    for target in job_cats
                }
                if not interrupted and all_target_keys.issubset(completed_targets):
                    clear_checkpoint()
                    self.on_progress("Контрольная точка очищена — сбор завершён")
                else:
                    self._persist_progress(settings, completed_targets, products)
                    self.on_progress(
                        f"Прогресс сохранён: {len(products)} товаров. "
                        "При незавершённом сборе запустите парсер снова с теми же категориями."
                    )
            finally:
                if self._context:
                    close_session_context(self._browser, self._context, self._session_mode)

        stats.total_duration_sec = timer.total_elapsed
        products = self._dedupe_products(products)
        self.on_progress(f"Сессия завершена за {stats.total_duration_fmt}")
        return products, stats

    def _dedupe_products(self, products: list[ProductRow]) -> list[ProductRow]:
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        unique: list[ProductRow] = []
        for product in products:
            canonical_url = self._canonical_product_url(product.url)
            product_id = extract_ozon_product_id(canonical_url)
            if canonical_url and canonical_url in seen_urls:
                continue
            if product_id and product_id in seen_ids:
                continue
            if canonical_url:
                seen_urls.add(canonical_url)
            if product_id:
                seen_ids.add(product_id)
            product.url = canonical_url or product.url
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
        specific_seller: bool = True,
        prefer_cache: bool = True,
        roots_only: bool = False,
    ) -> list:
        # Full-tree cache only when the caller wants the complete catalogue.
        if not specific_seller and prefer_cache and not roots_only:
            from .catalog_cache import cache_age_hours, load_global_catalog, save_global_catalog

            cached = load_global_catalog()
            if cached:
                age = cache_age_hours()
                age_txt = f"{age:.1f} ч" if age is not None else "?"
                self.on_progress(
                    f"Каталог из кэша ({age_txt}) — без запросов к Ozon, "
                    "чтобы не провоцировать fab_."
                )
                if on_roots:
                    on_roots(cached)
                if on_subcategories_begin:
                    on_subcategories_begin(len(cached))
                if on_branch:
                    for node in cached:
                        on_branch(node)
                return cached

        with sync_playwright() as playwright:
            browser, context, page, mode = self._open_browser(
                playwright,
                use_auth=use_auth,
                use_cdp=use_cdp,
                browser_mode=browser_mode,
            )
            try:
                if not ensure_ozon_session_ready(
                    page,
                    self.on_progress,
                    on_manual_bypass=self.on_manual_bypass,
                ):
                    raise ConnectionError(
                        "Ozon не готов к загрузке категорий. "
                        "Откройте Chrome, дождитесь нормальной загрузки сайта "
                        "без Antibot Challenge и повторите."
                    )
                loader = CategoryLoader(page, browser_mode, session_mode=mode)
                if specific_seller:
                    if roots_only:
                        roots = loader.load_root_categories(
                            seller_url,
                            self.on_progress,
                            self.on_manual_bypass,
                        )
                        if on_roots and roots:
                            on_roots(roots)
                        return roots
                    return loader.load_category_tree(
                        seller_url,
                        self.on_progress,
                        self.on_manual_bypass,
                        on_roots=on_roots,
                        on_subcategories_begin=on_subcategories_begin,
                        on_branch=on_branch,
                    )
                if roots_only:
                    self.on_progress(
                        "Быстрая загрузка главных категорий — без обхода всех веток."
                    )
                    return loader.load_global_root_tree(
                        self.on_progress,
                        self.on_manual_bypass,
                        on_roots=on_roots,
                    )
                self.on_progress(
                    "Медленная загрузка полного каталога (по одной ветке, с паузами) — "
                    "это снижает риск «Похоже, нет соединения» / fab_."
                )
                categories = loader.load_global_category_tree(
                    self.on_progress,
                    self.on_manual_bypass,
                    on_roots=on_roots,
                    on_subcategories_begin=on_subcategories_begin,
                    on_branch=on_branch,
                )
                if categories:
                    try:
                        from .catalog_cache import save_global_catalog

                        path = save_global_catalog(categories)
                        self.on_progress(
                            f"Каталог сохранён в кэш: {path} "
                            "(следующая загрузка без Chrome, пока кэш свежий)."
                        )
                    except Exception as exc:
                        self.on_progress(f"Не удалось сохранить кэш каталога: {exc}")
                return categories
            finally:
                close_session_context(browser, context, mode)

    def expand_selected_category_subtrees(
        self,
        categories: list[CategoryTarget],
        seller_url: str = "",
        use_auth: bool = False,
        use_cdp: bool = True,
        browser_mode: BrowserMode = DESKTOP_MODE,
        specific_seller: bool = False,
        on_subcategories_begin: Callable[[int], None] | None = None,
        on_branch: Callable[[object], None] | None = None,
    ) -> list:
        """Load subcategory trees only for the selected categories."""
        if not categories:
            raise ValueError("Не выбраны категории для загрузки подкатегорий.")

        with sync_playwright() as playwright:
            browser, context, page, mode = self._open_browser(
                playwright,
                use_auth=use_auth,
                use_cdp=use_cdp,
                browser_mode=browser_mode,
            )
            try:
                if not ensure_ozon_session_ready(
                    page,
                    self.on_progress,
                    on_manual_bypass=self.on_manual_bypass,
                ):
                    raise ConnectionError(
                        "Ozon не готов к загрузке подкатегорий. "
                        "Дождитесь нормальной загрузки сайта без fab_ и повторите."
                    )
                loader = CategoryLoader(page, browser_mode, session_mode=mode)
                if specific_seller:
                    if on_subcategories_begin:
                        on_subcategories_begin(len(categories))
                    mapping = loader.load_subcategories_for_categories(
                        seller_url,
                        categories,
                        self.on_progress,
                        self.on_manual_bypass,
                        on_branch=on_branch,
                    )
                    branches: list = []
                    for target in categories:
                        cid = str(target.param_value or target.category_id or "")
                        children = mapping.get(cid) or []
                        from .filters import FilterOptionNode

                        node = FilterOptionNode(
                            id=str(target.id or cid),
                            name=str(target.name or cid),
                            url=target.url or None,
                            param_key=str(target.param_key or "category"),
                            param_value=cid,
                            category_id=cid,
                            category_name=str(target.category_name or target.name or cid),
                            parent_name=str(target.parent_name or ""),
                            children=children,
                        )
                        branches.append(node)
                        if on_branch:
                            on_branch(node)
                    return branches

                root_ids: list[str] = []
                root_meta: dict[str, dict] = {}
                for target in categories:
                    cid = str(target.param_value or target.category_id or "").strip()
                    if not cid or cid in root_meta:
                        continue
                    root_ids.append(cid)
                    root_meta[cid] = {
                        "name": target.name or cid,
                        "url": target.url,
                    }
                return loader.expand_global_category_subtrees(
                    root_ids,
                    self.on_progress,
                    self.on_manual_bypass,
                    on_subcategories_begin=on_subcategories_begin,
                    on_branch=on_branch,
                    root_meta=root_meta,
                    timeout_sec=None,
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
                loader = CategoryLoader(page, browser_mode, session_mode=mode)
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
                loader = CategoryLoader(page, browser_mode, session_mode=mode)
                return loader.load_filters_for_categories(
                    seller_url, categories, self.on_progress, self.on_manual_bypass,
                )
            finally:
                close_session_context(browser, context, mode)

    def _parse_mode_label(self, mode: ParseMode) -> str:
        labels = {
            PARSE_MODE_GLOBAL_CATEGORIES: "выбранные категории (весь Ozon)",
            PARSE_MODE_ALL_SELLERS_CATEGORIES: "все магазины × выбранные категории",
            PARSE_MODE_ALL_SELLERS_FULL: "все магазины × полный каталог",
            PARSE_MODE_SELLER_FULL: "конкретный магазин × все товары",
            PARSE_MODE_SELLER_CATEGORIES: "конкретный магазин × выбранные категории",
        }
        return labels.get(mode, str(mode))

    def _whole_store_target(
        self,
        seller_url: str,
        label: str = "Весь магазин",
    ) -> CategoryTarget:
        return CategoryTarget(
            id=f"seller|full|{seller_url}",
            name=label,
            url=seller_url,
            section="Категория",
            param_key="",
            param_value="",
            seller_scope=seller_url,
        )

    def _build_parse_jobs(
        self,
        settings: ParseSettings,
        categories: list[CategoryTarget],
        page: Page | None,
    ) -> list[tuple[str, list[CategoryTarget]]]:
        mode = settings.parse_mode
        if mode == PARSE_MODE_GLOBAL_CATEGORIES:
            return [("", list(categories))]
        if mode == PARSE_MODE_SELLER_CATEGORIES:
            seller = normalize_seller_url(
                settings.seller_url,
                settings.browser_mode,
                self._session_mode,
            )
            return [(seller, list(categories))]
        if mode == PARSE_MODE_SELLER_FULL:
            seller = normalize_seller_url(
                settings.seller_url,
                settings.browser_mode,
                self._session_mode,
            )
            return [(seller, [self._whole_store_target(seller)])]
        if mode in (PARSE_MODE_ALL_SELLERS_CATEGORIES, PARSE_MODE_ALL_SELLERS_FULL):
            if page is None:
                return []
            sellers = discover_marketplace_sellers(
                page,
                progress=self.on_progress,
                on_manual_bypass=self.on_manual_bypass,
                max_sellers=MAX_MARKETPLACE_SELLERS,
            )
            if not sellers:
                self.on_progress("Список магазинов пуст — нечего обходить")
                return []
            jobs: list[tuple[str, list[CategoryTarget]]] = []
            for seller in sellers:
                if self.is_stopped():
                    break
                if mode == PARSE_MODE_ALL_SELLERS_FULL:
                    jobs.append((seller, [self._whole_store_target(seller)]))
                else:
                    jobs.append(
                        (
                            seller,
                            [replace(target, seller_scope=seller) for target in categories],
                        )
                    )
            return jobs
        return []

    def _build_catalog_url(
        self,
        seller_url: str,
        param_key: str | None = None,
        param_value: str | None = None,
        browser_mode: BrowserMode = DESKTOP_MODE,
        category_id: str = "",
    ) -> str:
        base = seller_url.rstrip("/") + "/"
        return sanitize_catalog_url(
            base,
            param_key=str(param_key or ""),
            param_value=str(param_value or ""),
            category_id=str(category_id or ""),
            browser_mode=browser_mode,
            session_mode=self._session_mode,
            keep_sorting=True,
        )

    def _catalog_url_for_target(
        self,
        seller_url: str,
        target: CategoryTarget,
        settings: ParseSettings,
    ) -> str:
        """Clean seller catalog URL — never use raw target.url (stale Ozon filters)."""
        return self._build_catalog_url(
            seller_url,
            target.param_key,
            target.param_value,
            settings.browser_mode,
            category_id=target.category_id or "",
        )

    def _global_category_parse_url(
        self,
        category_id: str,
        browser_mode: BrowserMode = DESKTOP_MODE,
        *,
        source_url: str = "",
    ) -> str:
        """Product listing for a global category across sellers.

        Prefer the real /category/… listing (keeps the selected section).
        `/seller/0/?category=` often ignores deep ids and shows a mixed mall feed.
        """
        category_id = str(category_id or "").strip()
        source = str(source_url or "").strip()
        if source and "/category/" in source.lower():
            source_id = extract_ozon_category_id(source)
            if category_id and source_id == category_id:
                routed = source if source.startswith("http") else f"https://www.ozon.ru{source}"
                return sanitize_catalog_url(
                    routed,
                    browser_mode=browser_mode,
                    session_mode=self._session_mode,
                    keep_sorting=True,
                )
        return self._build_global_category_direct_url(category_id, browser_mode)

    def _resolve_global_category_id(self, target: CategoryTarget) -> str:
        return extract_ozon_category_id(
            target.category_id,
            target.param_value,
            target.id,
            target.url,
        )

    def _build_global_catalog_url(
        self,
        target: CategoryTarget,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> str:
        category_id = self._resolve_global_category_id(target)
        if not category_id:
            raise ValueError(
                "Не удалось определить ID категории для "
                f"«{target.name or target.id}». Отметьте категорию заново."
            )
        # Always open the exact selected id. Never reuse a possibly parent-level
        # source URL even when the slug contains the same digits.
        return self._build_global_category_direct_url(category_id, browser_mode)

    def _build_global_category_direct_url(
        self,
        category_id: str,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> str:
        base = self._route_url(
            f"https://www.ozon.ru{GLOBAL_CATALOG_PATH.rstrip('/')}/{category_id}/",
            browser_mode,
        )
        return sanitize_catalog_url(
            base,
            browser_mode=browser_mode,
            session_mode=self._session_mode,
            keep_sorting=True,
        )

    def _recover_empty_catalog_page(
        self,
        page: Page,
        clean_url: str,
        settings: ParseSettings,
        target: CategoryTarget | None = None,
    ) -> bool:
        if not is_empty_catalog_filter_page(page):
            return True

        label = (target.name if target else "") or "категория"
        self.on_progress(
            f"Пустая выдача у «{label}» (лишние фильтры Ozon) — сбрасываем..."
        )

        # Reset button often drops category= — always reopen the clean URL after.
        if try_reset_catalog_filters(page):
            human_delay(1.0, 2.0)
            self.on_progress("Нажато «Сбросить фильтры» — возвращаем чистый URL категории")

        routed = self._route_url(clean_url, settings.browser_mode)
        if safe_goto(
            page,
            routed,
            self.on_progress,
            max_retries=SAFE_GOTO_MAX_RETRIES,
            on_manual_bypass=self.on_manual_bypass,
        ):
            human_delay(1.0, 2.0)
            if not is_empty_catalog_filter_page(page):
                self.on_progress("Открыта чистая ссылка каталога без лишних фильтров")
                return True

        if settings.parse_mode == PARSE_MODE_GLOBAL_CATEGORIES and target:
            category_id = self._resolve_global_category_id(target)
            if category_id:
                direct_url = self._build_global_category_direct_url(
                    category_id,
                    settings.browser_mode,
                )
                if safe_goto(
                    page,
                    direct_url,
                    self.on_progress,
                    max_retries=SAFE_GOTO_MAX_RETRIES,
                    on_manual_bypass=self.on_manual_bypass,
                ):
                    human_delay(1.0, 2.0)
                    if not is_empty_catalog_filter_page(page):
                        self.on_progress(
                            f"Категория открыта напрямую: /category/{category_id}/"
                        )
                        return True

        if is_empty_catalog_filter_page(page):
            self.on_progress(
                "Не удалось убрать пустую выдачу — категория будет пропущена до следующего запуска"
            )
            return False
        return True

    def _ensure_price_sort_asc(self, page: Page) -> None:
        try:
            current = (page.url or "").lower()
        except Exception:
            current = ""
        if "sorting=price" in current or "sorting%3dprice" in current:
            return
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
        product_cap: int | None = None,
        on_progress_save: Callable[[list[ProductRow]], None] | None = None,
        specific_seller: bool | None = None,
    ) -> tuple[list[ProductRow], bool]:
        collected: list[ProductRow] = []
        for attempt in range(1, MAX_CATEGORY_RETRIES + 1):
            if self.is_stopped():
                return self._dedupe_products(collected), False

            results, loaded = self._parse_catalog(
                url,
                settings,
                state,
                category,
                product_cap=product_cap,
                on_progress_save=on_progress_save,
                specific_seller=specific_seller,
            )
            collected.extend(results)
            collected = self._dedupe_products(collected)
            if loaded and not self.is_stopped():
                return collected, True
            if self._page and is_access_restricted(self._page):
                return collected, False
            if attempt < MAX_CATEGORY_RETRIES:
                if not self._page:
                    self.on_progress("Страница браузера недоступна — парсинг остановлен")
                    return collected, False
                self.on_progress(f"Страница не загрузилась, повтор {attempt + 1}...")
                if is_access_restricted(self._page):
                    return collected, False
                if not recover_access(
                    self._page,
                    self.on_progress,
                    self.on_manual_bypass,
                    self._route_url(url, settings.browser_mode),
                ):
                    return collected, False
        return collected, False

    def _soft_goto_seller_category(
        self,
        page: Page,
        routed_url: str,
    ) -> bool:
        """Switch seller category with a single navigation and no auto-retries."""
        return safe_goto(
            page,
            routed_url,
            self.on_progress,
            max_retries=SAFE_GOTO_MAX_RETRIES,
            on_manual_bypass=self.on_manual_bypass,
        )

    def _navigate_to_catalog(
        self,
        page: Page,
        target: CategoryTarget | None,
        catalog_url: str,
        browser_mode: BrowserMode = DESKTOP_MODE,
        *,
        specific_seller: bool = True,
    ) -> bool:
        routed_url = self._route_url(catalog_url, browser_mode)
        if target:
            self.on_progress(
                f"Открываем выбранную категорию: {target.name or target.id}"
            )
            self.on_progress(f"URL каталога: {routed_url}")

        # Always open a sanitized URL directly. Clicking filter links on the seller
        # page often carries stale query flags and lands on «Сбросить фильтры».
        return safe_goto(
            page,
            routed_url,
            self.on_progress,
            max_retries=SAFE_GOTO_MAX_RETRIES,
            on_manual_bypass=self.on_manual_bypass,
        )

    def _parse_catalog(
        self,
        url: str,
        settings: ParseSettings,
        state: _ParseState,
        category: CategoryTarget | None = None,
        product_cap: int | None = None,
        on_progress_save: Callable[[list[ProductRow]], None] | None = None,
        specific_seller: bool | None = None,
    ) -> tuple[list[ProductRow], bool]:
        assert self._page is not None
        page = self._page
        results: list[ProductRow] = []
        routed_url = self._route_url(url, settings.browser_mode)
        target = category
        hard_cap = product_cap if product_cap is not None else settings.max_products
        last_saved_count = 0
        seller_mode = (
            settings.specific_seller if specific_seller is None else specific_seller
        )

        # Global and seller: same flow — open listing URL, sort by price, scroll DOM.
        # (Composer-first product fetch for global caused fab_ before useful results.)
        if not self._navigate_to_catalog(
            page,
            target,
            routed_url,
            settings.browser_mode,
            specific_seller=seller_mode,
        ):
            if self._page and is_access_restricted(self._page):
                self.on_progress(
                    "Ozon показал fab_/«Похоже, нет соединения» — временная блокировка, "
                    "не ошибка интернета. Ждём без F5, затем проверим доступ."
                )
                if not self._wait_out_access_block(
                    settings, reopen_url=routed_url
                ):
                    self.on_progress(
                        "Прогресс сохранён; подождите 15–30 минут "
                        "и продолжите без повторных обновлений страницы."
                    )
                    return results, False
                if is_access_restricted(self._page):
                    return results, False
            else:
                # Generic navigation failure: stop without a second blind goto.
                self.on_progress(
                    "Не удалось открыть категорию. Прогресс сохранён — повторите позже."
                )
                return results, False

        if not self._recover_empty_catalog_page(
            page, routed_url, settings, target
        ):
            return results, False

        self._ensure_price_sort_asc(page)

        if not self._recover_empty_catalog_page(
            page, routed_url, settings, target
        ):
            return results, False

        # First paint of tile grid is often delayed after sort/filters.
        self._ensure_catalog_page_fully_loaded(page)

        empty_rounds = 0
        scroll_rounds = 0
        processed = set()
        exhausted = False
        capped = False
        composer_fallback_used = False
        cards_seen = 0
        skipped_no_bonus = 0
        finalize_attempts = 0
        # Same deep scroll budget as specific seller (not the old 1-scroll bulk cap).
        max_scrolls = 10_000
        # Virtualized grids often need several empty passes before the next batch paints.
        max_empty_rounds = 8

        while (
            not self.is_stopped()
            and len(results) < hard_cap
            and empty_rounds < max_empty_rounds
        ):
            if is_access_restricted(page):
                self.on_progress(
                    "Во время сбора категории появилась блокировка Ozon (fab_)."
                )
                if self._wait_out_access_block(settings, reopen_url=routed_url):
                    self.on_progress("Блокировка снята — продолжаем прокрутку категории")
                    self._ensure_price_sort_asc(page)
                    self._ensure_catalog_page_fully_loaded(page)
                    empty_rounds = 0
                    continue
                self.on_progress("Останавливаем категорию без повторных запросов.")
                break

            cards = self._extract_product_cards(page, settings.browser_mode)
            if not composer_fallback_used:
                if is_access_restricted(page):
                    composer_fallback_used = True
                else:
                    composer_cards = self._try_composer_product_cards(
                        page, routed_url, settings.browser_mode
                    )
                    composer_fallback_used = True
                    if composer_cards:
                        before_dom = len(cards)
                        cards = self._merge_product_cards(cards, composer_cards)
                        self.on_progress(
                            f"Карточки: DOM {before_dom} + Composer {len(composer_cards)} "
                            f"→ {len(cards)} уникальных"
                        )
            new_cards = []
            for card in cards:
                href = str(card.get("href") or "")
                key = self._product_dedupe_key(href)
                if not key or key in processed:
                    continue
                processed.add(key)
                new_cards.append(card)
            cards_seen += len(new_cards)

            if not new_cards:
                empty_rounds += 1
                # Give lazy tiles a moment before the next scroll/empty count.
                if empty_rounds < max_empty_rounds:
                    self._wait_catalog_batch_settle(page, timeout_sec=4.0)
            else:
                empty_rounds = 0

            for card in new_cards:
                if self.is_stopped() or len(results) >= hard_cap:
                    break
                product = self._process_card(card, settings, state)
                if product:
                    results.append(product)
                    self.on_progress(
                        f"Найдено: {len(results)}/{hard_cap} в категории — "
                        f"{product.name[:50]}"
                    )
                    if not self._pause_after_product_batch():
                        break
                    if (
                        on_progress_save
                        and len(results) - last_saved_count >= CHECKPOINT_SAVE_EVERY_PRODUCTS
                    ):
                        on_progress_save(results)
                        last_saved_count = len(results)
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
                else:
                    skipped_no_bonus += 1
                human_click_delay()

            if skipped_no_bonus and not results and cards_seen >= 5 and scroll_rounds == 0:
                if settings.bonus_only:
                    self.on_progress(
                        f"На странице {cards_seen} карточек, но без баллов за отзыв "
                        f"({skipped_no_bonus} пропущено)"
                    )
                else:
                    self.on_progress(
                        f"На странице {cards_seen} карточек, но ни одна не прошла фильтры "
                        f"({skipped_no_bonus} пропущено)"
                    )

            if len(results) >= hard_cap:
                capped = True
                break
            if empty_rounds >= max_empty_rounds:
                if finalize_attempts < 2 and self._finalize_catalog_page_load(page):
                    finalize_attempts += 1
                    empty_rounds = max(0, max_empty_rounds - 3)
                    continue
                exhausted = True
                break
            if scroll_rounds >= max_scrolls:
                exhausted = True
                break
            if not self._scroll_for_more(page):
                if finalize_attempts < 2 and self._finalize_catalog_page_load(page):
                    finalize_attempts += 1
                    empty_rounds = 0
                    continue
                exhausted = True
                break
            scroll_rounds += 1
            if scroll_rounds % SAFE_SCROLL_BATCH_SIZE == 0:
                if not self._protective_pause(
                    SAFE_SCROLL_BREAK_MIN,
                    SAFE_SCROLL_BREAK_MAX,
                    "Защитная пауза при прокрутке каталога",
                ):
                    break
            human_scroll_delay()

        access_blocked = is_blocked_page(page) or is_captcha_page(page)
        if capped:
            category_completed = True
        else:
            category_completed = (
                exhausted
                and not access_blocked
                and not self.is_stopped()
                and len(processed) > 0
            )
        if exhausted and not category_completed and not access_blocked:
            self.on_progress(
                "Категория не завершена: карточки товаров не найдены на странице."
            )
        return results, category_completed

    def _fetch_composer_json(self, page: Page, page_url: str, browser_mode: BrowserMode) -> dict | None:
        path = urlparse(page_url).path
        if not path.endswith("/"):
            path += "/"
        full_url = self._route_url(page_url, browser_mode)
        client_name = "mweb_client" if browser_mode == MOBILE_MODE else "dweb_client"
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
            raw = page.evaluate(
                script,
                {"path": path, "fullUrl": full_url, "clientName": client_name},
            )
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _try_composer_product_cards(
        self,
        page: Page,
        page_url: str,
        browser_mode: BrowserMode,
    ) -> list[dict]:
        data = self._fetch_composer_json(page, page.url or page_url, browser_mode)
        if not data:
            return []
        parsed = urlparse(page.url or page_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "https://www.ozon.ru"
        return extract_product_cards_from_composer(data, base_url=base_url)

    @staticmethod
    def _merge_product_cards(dom_cards: list[dict], composer_cards: list[dict]) -> list[dict]:
        """Prefer richer single-tile text when the same product appears in both sources."""
        merged: dict[str, dict] = {}

        def tile_score(card: dict) -> tuple[int, int, int]:
            text = str(card.get("text") or "")
            name = str(card.get("name") or "")
            # Prefer a single-tile blob (few prices) over a whole-grid dump.
            price_marks = text.count("₽")
            return (-price_marks, len(name), -abs(len(text) - 350))

        def absorb(card: dict) -> None:
            href = str(card.get("href") or "").split("#")[0].split("?")[0]
            if not href or "/product/" not in href:
                return
            key = extract_ozon_product_id(href) or href.rstrip("/")
            incoming = {
                "href": href,
                "name": str(card.get("name") or "").strip(),
                "text": str(card.get("text") or ""),
                "html": str(card.get("html") or ""),
            }
            prev = merged.get(key)
            if not prev or tile_score(incoming) > tile_score(prev):
                best_name = pick_product_name(
                    (prev or {}).get("name"),
                    incoming.get("name"),
                )
                if best_name:
                    incoming["name"] = best_name
                merged[key] = incoming

        for source in (dom_cards or [], composer_cards or []):
            for card in source:
                absorb(card)
        return list(merged.values())

    def _extract_product_cards(
        self,
        page: Page,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> list[dict]:
        script = """
        () => {
            const cards = new Map();
            const normalize = (text) => (text || '').trim().replace(/\\s+/g, ' ');
            const productId = (href) => {
                const path = String(href || '').split('#')[0].split('?')[0];
                let m = path.match(/\\/product\\/(?:[^\\/]*?-)?(\\d{6,})\\/?$/i);
                if (m) return m[1];
                m = path.match(/\\/product\\/[^\\/?#]*?(\\d{6,})/i);
                return m ? m[1] : '';
            };
            const canonicalHref = (href) => String(href || '').split('#')[0].split('?')[0];
            const noise = /балл|бонус|отзыв|распродаж|суперцен|ценопад|новинка|^хит$|^\\d+\\s*шт\\.?$|осталось\\s+\\d+|в корзину|в избранное|бесплатная доставка|^акция$|^скидка$|бренд\\s*проверен|проверенн?ый\\s*бренд|бренд\\s*подтвержд|официальный\\s*бренд|^оригинал$/i;
            const isNoise = (value) => {
                if (!value || value.length < 3 || /₽/.test(value)) return true;
                if (noise.test(value)) return true;
                if (/^[-−+]?\\d+\\s*%$/.test(value)) return true;
                if (/^\\d([.,]\\d)?$/.test(value)) return true;
                return false;
            };
            const uniqueProductHrefs = (root) => {
                const hrefs = new Set();
                root.querySelectorAll('a[href*="/product/"]').forEach((a) => {
                    const href = canonicalHref(a.href || a.getAttribute('href') || '');
                    if (href.includes('/product/')) hrefs.add(href);
                });
                return hrefs;
            };
            // Climb until just before a multi-product grid; never use the whole catalog.
            const findContainer = (link) => {
                const own = canonicalHref(link.href || link.getAttribute('href') || '');
                let node = link.parentElement;
                let best = link;
                for (let depth = 0; depth < 14 && node; depth++) {
                    const hrefs = uniqueProductHrefs(node);
                    if (hrefs.size > 1) {
                        return best;
                    }
                    best = node;
                    if (
                        node.matches &&
                        node.matches('[data-index], article, [class*="tile-root"], [class*="tile_"]')
                    ) {
                        return node;
                    }
                    // Prefer a compact single-product node.
                    if (hrefs.size === 1 && hrefs.has(own) && (node.innerText || '').length > 40) {
                        const prices = ((node.innerText || '').match(/₽/g) || []).length;
                        if (prices > 0 && prices <= 4) {
                            best = node;
                        }
                    }
                    node = node.parentElement;
                }
                return best;
            };
            const pickName = (link, container) => {
                const candidates = [];
                const ownTitle = normalize(link.getAttribute('title') || '');
                const ownText = normalize(link.textContent || '');
                if (!isNoise(ownTitle)) candidates.push(ownTitle);
                if (!isNoise(ownText) && ownText.length >= 8) candidates.push(ownText);
                container.querySelectorAll('a[href*="/product/"], [title]').forEach((el) => {
                    const elHref = canonicalHref(el.href || el.getAttribute('href') || '');
                    const linkHref = canonicalHref(link.href || link.getAttribute('href') || '');
                    if (elHref && linkHref && elHref !== linkHref && elHref.includes('/product/')) {
                        return;
                    }
                    const titleAttr = normalize(el.getAttribute('title') || '');
                    const value = normalize(el.textContent || '');
                    if (!isNoise(titleAttr)) candidates.push(titleAttr);
                    if (!isNoise(value)) candidates.push(value);
                });
                const unique = Array.from(new Set(candidates.filter(Boolean)));
                return unique.sort((a, b) => {
                    const score = (s) => ((s.match(/[A-Za-zА-Яа-яЁё]/g) || []).length) * 10 + s.length;
                    return score(b) - score(a);
                })[0] || '';
            };

            document.querySelectorAll('a[href*="/product/"]').forEach((link) => {
                const href = link.href || link.getAttribute('href') || '';
                if (!href.includes('/product/')) return;
                const canonical = canonicalHref(href);
                const id = productId(canonical);
                const key = id || canonical;
                if (!key) return;
                const container = findContainer(link);
                // Guard: skip if container still looks like a whole grid.
                if (uniqueProductHrefs(container).size > 1) return;
                const text = container.innerText || '';
                if ((text.match(/₽/g) || []).length > 6) return;
                const name = pickName(link, container);
                const card = {
                    href: canonical,
                    name,
                    text,
                    html: container.innerHTML || ''
                };
                const previous = cards.get(key);
                const better =
                    !previous ||
                    ((text.match(/₽/g) || []).length < ((previous.text || '').match(/₽/g) || []).length) ||
                    ((text.match(/₽/g) || []).length === ((previous.text || '').match(/₽/g) || []).length &&
                        text.length < previous.text.length && text.length > 40) ||
                    (name && name.length > (previous.name || '').length);
                if (better) {
                    cards.set(key, card);
                }
            });
            return Array.from(cards.values());
        }
        """
        try:
            return page.evaluate(script) or []
        except Exception:
            return []

    def _card_has_bonus(self, card: dict) -> bool:
        text = card.get("text", "")
        html = card.get("html", "")
        if has_bonus_text(text) or has_bonus_text(html):
            return True
        if parse_bonus_points(text) or parse_bonus_points(html):
            return True
        html_lower = html.lower()
        if "отзыв" in html_lower and ("балл" in html_lower or "bonus" in html_lower):
            return True
        if "review" in html_lower and "bonus" in html_lower:
            return True
        if "отзыв" in html_lower and ("₽" in html or re.search(r"\d", html)):
            return True
        return False

    def _process_card(self, card: dict, settings: ParseSettings, state: _ParseState) -> ProductRow | None:
        href = self._canonical_product_url(card.get("href", ""))
        text = card.get("text", "")
        html = card.get("html", "") or ""
        product_id = extract_ozon_product_id(href)
        if not href:
            return None
        if href in state.seen_urls:
            return None
        if product_id and product_id in state.seen_product_ids:
            return None

        detail = self._parse_card_text(text)
        # Listing tiles often put price/bonus only in HTML attributes / nested spans.
        if html and (
            not detail.get("name")
            or detail.get("price_discounted") is None
            or detail.get("bonus_points") is None
        ):
            html_detail = self._parse_card_text(re.sub(r"<[^>]+>", "\n", html))
            if not detail.get("name") and html_detail.get("name"):
                detail["name"] = html_detail["name"]
            if detail.get("price_discounted") is None and html_detail.get("price_discounted") is not None:
                detail["price_discounted"] = html_detail["price_discounted"]
                detail["price_original"] = html_detail.get("price_original")
            if detail.get("bonus_points") is None and html_detail.get("bonus_points") is not None:
                detail["bonus_points"] = html_detail["bonus_points"]
        detail["url"] = href

        name = pick_product_name(
            card.get("name"),
            detail.get("name"),
            text,
            re.sub(r"<[^>]+>", "\n", html) if html else "",
        ) or name_from_product_url(href)
        price_disc = detail.get("price_discounted")
        price_orig = detail.get("price_original")
        bonus = detail.get("bonus_points")
        if not isinstance(bonus, int) or bonus <= 0:
            bonus = parse_bonus_points(text) or parse_bonus_points(html)
        card_has_bonus = self._card_has_bonus(card)

        # Keep only products that advertise review bonus when bonus_only is on.
        if settings.bonus_only:
            if not card_has_bonus and not (isinstance(bonus, int) and bonus > 0):
                return None

        # Bulk-safe mode: never open product pages. Missing fields are skipped.
        if (
            (not name or price_disc is None or (settings.bonus_only and (bonus is None or bonus <= 0)))
            and MAX_PRODUCT_DETAIL_FETCHES > 0
            and state.detail_fetches < MAX_PRODUCT_DETAIL_FETCHES
        ):
            full = self._fetch_product_detail(
                href,
                state,
                settings.browser_mode,
            )
            if full:
                name = name or full.get("name")
                price_disc = price_disc if price_disc is not None else full.get("price_discounted")
                price_orig = price_orig if price_orig is not None else full.get("price_original")
                bonus = bonus if bonus is not None and bonus > 0 else full.get("bonus_points")

        if not name or price_disc is None:
            return None
        if settings.bonus_only and (not isinstance(bonus, int) or bonus <= 0):
            return None
        if not isinstance(bonus, int) or bonus < 0:
            bonus = 0
        if price_orig is None or price_orig < price_disc:
            price_orig = price_disc
        if settings.min_price is not None and price_disc < settings.min_price:
            return None
        if settings.max_price is not None and price_disc > settings.max_price:
            return None

        state.seen_urls.add(href)
        if product_id:
            state.seen_product_ids.add(product_id)
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
        bonus = None
        for ln in lines:
            if has_bonus_text(ln):
                bonus = parse_bonus_points(ln)
                if bonus:
                    break
        if bonus is None:
            bonus = parse_bonus_points(text)
        name = pick_product_name(text, *lines)
        price_disc = min(prices) if prices else None
        price_orig = max(prices) if len(prices) > 1 else price_disc
        return {"name": name, "price_discounted": price_disc, "price_original": price_orig, "bonus_points": bonus}

    def _extract_name_from_text(self, text: str) -> str:
        return pick_product_name(text)

    def _fetch_product_detail(
        self,
        href: str,
        state: _ParseState,
        browser_mode: BrowserMode = DESKTOP_MODE,
    ) -> dict | None:
        assert self._page is not None
        detail_page = None
        url = self._route_url(href, browser_mode)
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

    def _wait_page_load_state(self, page: Page, *, timeout_ms: int = 12000) -> None:
        """Best-effort wait for document + short network quiet period."""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            # Ozon keeps long-polling; networkidle often never completes — keep short.
            page.wait_for_load_state("networkidle", timeout=min(4500, timeout_ms))
        except Exception:
            pass

    def _wait_catalog_listing_ready(
        self,
        page: Page,
        *,
        timeout_sec: float = 20.0,
        min_products: int = 1,
    ) -> bool:
        """Wait until product tiles appear on the catalog page."""
        if is_access_restricted(page):
            return False
        deadline = time.time() + timeout_sec
        last_count = -1
        while time.time() < deadline and not self.is_stopped():
            if is_access_restricted(page):
                return False
            metrics = self._catalog_scroll_metrics(page)
            count = int(metrics.get("products") or 0)
            if count != last_count and count > 0:
                self.on_progress(f"Ожидаем полную отрисовку каталога… карточек: {count}")
                last_count = count
            if count >= min_products:
                return True
            human_delay(0.6, 1.0)
        return int(self._catalog_scroll_metrics(page).get("products") or 0) >= min_products

    def _wait_catalog_batch_settle(
        self,
        page: Page,
        *,
        timeout_sec: float = 8.0,
        stable_checks: int = 2,
    ) -> dict:
        """Wait until product count / page height stop changing after a load/scroll."""
        if is_access_restricted(page):
            return self._catalog_scroll_metrics(page)
        deadline = time.time() + timeout_sec
        prev = self._catalog_scroll_metrics(page)
        stable = 0
        while time.time() < deadline and not self.is_stopped():
            if self._stop_event.wait(0.7):
                break
            cur = self._catalog_scroll_metrics(page)
            same = (
                cur["products"] == prev["products"]
                and abs(cur["h"] - prev["h"]) <= 4
            )
            if same:
                stable += 1
                if stable >= stable_checks:
                    return cur
            else:
                stable = 0
            prev = cur
        return self._catalog_scroll_metrics(page)

    def _ensure_catalog_page_fully_loaded(self, page: Page) -> None:
        """Open-category gate: document ready + tiles painted + batch settled."""
        if is_access_restricted(page):
            return
        self.on_progress("Дожидаемся полной загрузки страницы каталога...")
        self._wait_page_load_state(page, timeout_ms=15000)
        wait_for_ozon_ready(page, self.on_progress, timeout_sec=20)
        if is_access_restricted(page):
            return
        ready = self._wait_catalog_listing_ready(page, timeout_sec=18.0, min_products=1)
        self._wait_catalog_batch_settle(page, timeout_sec=7.0, stable_checks=2)
        # Warm first viewport: small scroll down and back loads lazy tiles.
        try:
            page.evaluate(
                """() => {
                    window.scrollBy(0, Math.max(300, Math.floor(window.innerHeight * 0.4)));
                }"""
            )
            human_delay(1.0, 1.6)
            self._wait_catalog_batch_settle(page, timeout_sec=5.0, stable_checks=2)
            page.evaluate("() => window.scrollTo(0, 0)")
            human_delay(0.6, 1.0)
            self._wait_catalog_batch_settle(page, timeout_sec=4.0, stable_checks=2)
        except Exception:
            pass
        metrics = self._catalog_scroll_metrics(page)
        if ready or metrics["products"] > 0:
            self.on_progress(
                f"Страница каталога загружена: {int(metrics['products'])} карточек на экране"
            )
        else:
            self.on_progress("Страница открыта, но карточки товаров ещё не появились")

    def _finalize_catalog_page_load(self, page: Page) -> bool:
        """One more full-load pass near the bottom before closing the category."""
        if is_access_restricted(page):
            return False
        before = self._catalog_scroll_metrics(page)
        self.on_progress("Финальная догрузка страницы каталога...")
        clicked = self._click_catalog_load_more(page)
        try:
            page.evaluate(
                """() => {
                    const h = Math.max(
                        document.body ? document.body.scrollHeight : 0,
                        document.documentElement ? document.documentElement.scrollHeight : 0
                    );
                    window.scrollTo(0, h);
                }"""
            )
        except Exception:
            pass
        self._wait_page_load_state(page, timeout_ms=8000)
        after = self._wait_catalog_batch_settle(page, timeout_sec=8.0, stable_checks=2)
        clicked_again = False
        if not clicked:
            clicked_again = self._click_catalog_load_more(page)
            if clicked_again:
                after = self._wait_catalog_batch_settle(page, timeout_sec=6.0, stable_checks=2)
        grew = (
            after["products"] > before["products"]
            or after["h"] > before["h"] + 8
            or clicked
            or clicked_again
        )
        return bool(grew)

    def _catalog_scroll_metrics(self, page: Page) -> dict:
        try:
            data = page.evaluate(
                """() => {
                    const y = window.scrollY || document.documentElement.scrollTop || 0;
                    const h = Math.max(
                        document.body ? document.body.scrollHeight : 0,
                        document.documentElement ? document.documentElement.scrollHeight : 0
                    );
                    const ih = window.innerHeight || 0;
                    const products = document.querySelectorAll('a[href*="/product/"]').length;
                    return { y, h, ih, products };
                }"""
            )
            if isinstance(data, dict):
                return {
                    "y": float(data.get("y") or 0),
                    "h": float(data.get("h") or 0),
                    "ih": float(data.get("ih") or 0),
                    "products": int(data.get("products") or 0),
                }
        except Exception:
            pass
        return {"y": 0.0, "h": 0.0, "ih": 0.0, "products": 0}

    def _click_catalog_load_more(self, page: Page) -> bool:
        """Click «Показать ещё» / similar controls when infinite scroll stalls."""
        selectors = (
            'button:has-text("Показать ещё")',
            'button:has-text("Показать еще")',
            'div[role="button"]:has-text("Показать ещё")',
            'div[role="button"]:has-text("Показать еще")',
            'a:has-text("Показать ещё")',
            'a:has-text("Показать еще")',
            'button:has-text("Показать больше")',
            '[data-widget*="megaPaginator"] button',
            '[data-widget*="paginator"] button:has-text("дальше")',
            '[data-widget*="paginator"] a:has-text("дальше")',
        )
        for selector in selectors:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click(timeout=3000)
                    self.on_progress("Нажато «Показать ещё» для догрузки товаров")
                    return True
            except Exception:
                continue
        return False

    def _scroll_for_more(self, page: Page) -> bool:
        """Scroll the product grid; True if more items may still load.

        Ozon often uses a virtualized feed where document.scrollHeight stops growing.
        Do not treat a flat height as end-of-list — keep scrolling until we cannot
        move further and no load-more control is present. After each move, wait for
        the next product batch to fully paint.
        """
        if is_blocked_page(page):
            return False
        try:
            before = self._catalog_scroll_metrics(page)
            if self._click_catalog_load_more(page):
                self._wait_page_load_state(page, timeout_ms=8000)
                after_click = self._wait_catalog_batch_settle(
                    page, timeout_sec=8.0, stable_checks=2
                )
                if (
                    after_click["h"] > before["h"]
                    or after_click["products"] > before["products"]
                    or after_click["y"] > before["y"] + 5
                ):
                    return True
                # Button may still be loading — allow another round.
                return True

            page.evaluate(
                """() => {
                    const step = Math.max(480, Math.floor(window.innerHeight * 0.92));
                    window.scrollBy(0, step);
                }"""
            )
            self._wait_page_load_state(page, timeout_ms=6000)
            after = self._wait_catalog_batch_settle(page, timeout_sec=7.0, stable_checks=2)
            grew = after["h"] > before["h"] + 2
            more_products = after["products"] > before["products"]
            moved = after["y"] > before["y"] + 20
            room_left = after["y"] + after["ih"] < after["h"] - 24
            if grew or more_products or moved or room_left:
                return True

            # Last nudge: hard scroll to bottom in case a sticky bar blocked the step.
            page.evaluate(
                """() => {
                    const h = Math.max(
                        document.body ? document.body.scrollHeight : 0,
                        document.documentElement ? document.documentElement.scrollHeight : 0
                    );
                    window.scrollTo(0, h);
                }"""
            )
            self._wait_page_load_state(page, timeout_ms=6000)
            final = self._wait_catalog_batch_settle(page, timeout_sec=6.0, stable_checks=2)
            if (
                final["h"] > after["h"] + 2
                or final["products"] > after["products"]
                or final["y"] > after["y"] + 20
            ):
                return True
            if self._click_catalog_load_more(page):
                self._wait_catalog_batch_settle(page, timeout_sec=6.0, stable_checks=2)
                return True
            return False
        except Exception:
            return False

    @staticmethod
    def _canonical_product_url(url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(to_desktop_product_url(url))
        path = parsed.path or ""
        if path and not path.endswith("/"):
            path += "/"
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    @classmethod
    def _product_dedupe_key(cls, url: str) -> str:
        """Stable key: numeric product id when possible, else canonical URL."""
        canonical = cls._canonical_product_url(url)
        return extract_ozon_product_id(canonical) or canonical
