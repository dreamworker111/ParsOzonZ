import sys
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ozon_parser.categories import CategoryTarget
from ozon_parser.filters import FilterOptionNode
from ozon_parser.chrome_launcher import launch_chrome_for_ozon
from ozon_parser.config import (
    BrowserMode,
    DESKTOP_MODE,
    FONTS_DIR,
    MOBILE_MODE,
    OUTPUT_DIR,
)
from ozon_parser.auth import has_mobile_saved_session
from ozon_parser.export import ExportMeta, export_products
from ozon_parser.login import login_mobile_via_browser
from ozon_parser.parse_stats import ParseStatus
from ozon_parser.parser import OzonParser, ParseSettings


def load_roboto_light() -> str:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    font_path = FONTS_DIR / "Roboto-Light.ttf"
    if font_path.exists():
        fid = QFontDatabase.addApplicationFont(str(font_path))
        if fid >= 0:
            families = QFontDatabase.applicationFontFamilies(fid)
            if families:
                return families[0]
    return "Roboto"


def scaled_font_size(screen_height: int) -> int:
    base = 27
    if screen_height < 800:
        return max(16, int(base * screen_height / 1080))
    return base


def setup_action_button(button: QPushButton, min_height: int = 56) -> None:
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    button.setMinimumHeight(min_height)
    button.setMaximumWidth(16777215)



class FilterTreeWidget(QTreeWidget):
    ROLE_ID = Qt.ItemDataRole.UserRole
    ROLE_URL = Qt.ItemDataRole.UserRole + 1
    ROLE_SECTION = Qt.ItemDataRole.UserRole + 2
    ROLE_PARAM_KEY = Qt.ItemDataRole.UserRole + 3
    ROLE_PARAM_VALUE = Qt.ItemDataRole.UserRole + 4
    ROLE_CATEGORY_ID = Qt.ItemDataRole.UserRole + 5
    ROLE_CATEGORY_NAME = Qt.ItemDataRole.UserRole + 6
    ROLE_PARENT_NAME = Qt.ItemDataRole.UserRole + 7
    ROLE_RAW_NAME = Qt.ItemDataRole.UserRole + 8
    ROLE_IS_PLACEHOLDER = Qt.ItemDataRole.UserRole + 9
    ROLE_EMPTY_SUBCATEGORIES = Qt.ItemDataRole.UserRole + 10

    PLACEHOLDER_COLOR = QColor("#6b7280")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setAnimated(True)
        self.setIndentation(22)
        self.itemChanged.connect(self._on_item_changed)
        self._block_signals = False

    def populate_categories(self, roots: list[FilterOptionNode]) -> None:
        self.clear()
        self._block_signals = True
        for root in roots:
            self.addTopLevelItem(self._create_option_item("Категория", root, depth=0))
        self.expandToDepth(3)
        self._block_signals = False

    def _style_placeholder_item(self, item: QTreeWidgetItem) -> None:
        item.setForeground(0, QBrush(self.PLACEHOLDER_COLOR))
        font = item.font(0)
        font.setItalic(True)
        item.setFont(0, font)

    def _create_loading_placeholder(self) -> QTreeWidgetItem:
        placeholder = QTreeWidgetItem(["Загрузка подкатегорий..."])
        placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        placeholder.setData(0, self.ROLE_IS_PLACEHOLDER, True)
        self._style_placeholder_item(placeholder)
        return placeholder

    def _create_empty_subcategories_item(self) -> QTreeWidgetItem:
        placeholder = QTreeWidgetItem(["Подкатегории не найдены"])
        placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        placeholder.setData(0, self.ROLE_IS_PLACEHOLDER, True)
        placeholder.setData(0, self.ROLE_EMPTY_SUBCATEGORIES, True)
        self._style_placeholder_item(placeholder)
        return placeholder

    def _remove_loading_placeholders(self, item: QTreeWidgetItem) -> None:
        for j in range(item.childCount() - 1, -1, -1):
            child = item.child(j)
            if child.data(0, self.ROLE_IS_PLACEHOLDER) and not child.data(
                0, self.ROLE_EMPTY_SUBCATEGORIES
            ):
                item.removeChild(child)

    def select_all_items(self) -> None:
        self._block_signals = True

        def walk(item: QTreeWidgetItem) -> None:
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(0, Qt.CheckState.Checked)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        self._block_signals = False

    def reset_selection(self) -> None:
        self._block_signals = True

        def walk(item: QTreeWidgetItem) -> None:
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        self._block_signals = False

    def begin_incremental_load(self) -> None:
        self.clear()

    def set_initial_roots(self, roots: list[FilterOptionNode]) -> None:
        self._block_signals = True
        for root in roots:
            item = self._create_option_item("Категория", root, depth=0)
            item.addChild(self._create_loading_placeholder())
            self.addTopLevelItem(item)
        self.expandToDepth(1)
        self._block_signals = False

    def _expand_item_branch(self, item: QTreeWidgetItem, max_depth: int = 6, depth: int = 0) -> None:
        if depth >= max_depth:
            return
        if item.data(0, self.ROLE_IS_PLACEHOLDER):
            return
        item.setExpanded(True)
        for i in range(item.childCount()):
            self._expand_item_branch(item.child(i), max_depth, depth + 1)

    def update_category_branch(self, node: FilterOptionNode) -> None:
        self._block_signals = True
        item = self._find_item_by_param_value(node.param_value)
        new_item = self._create_option_item("Категория", node, depth=0)
        if item is None:
            self.addTopLevelItem(new_item)
        else:
            idx = self.indexOfTopLevelItem(item)
            if idx >= 0:
                self.takeTopLevelItem(idx)
                self.insertTopLevelItem(idx, new_item)
        self._expand_item_branch(new_item)
        self._block_signals = False
        self.scrollToItem(new_item)

    def _finalize_subcategory_placeholders(self, item: QTreeWidgetItem) -> None:
        has_checkable_child = False
        for i in range(item.childCount()):
            child = item.child(i)
            if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                has_checkable_child = True
                self._finalize_subcategory_placeholders(child)

        if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            for i in range(item.childCount()):
                self._finalize_subcategory_placeholders(item.child(i))
            return

        self._remove_loading_placeholders(item)
        if has_checkable_child:
            return

        for j in range(item.childCount() - 1, -1, -1):
            child = item.child(j)
            if child.data(0, self.ROLE_IS_PLACEHOLDER):
                item.removeChild(child)
        item.addChild(self._create_empty_subcategories_item())

    def finalize_catalog_load(self, categories: list[FilterOptionNode]) -> None:
        self._block_signals = True
        for i in range(self.topLevelItemCount()):
            self._finalize_subcategory_placeholders(self.topLevelItem(i))
            self._expand_item_branch(self.topLevelItem(i))
        self._block_signals = False

    def merge_subcategories(self, mapping: dict[str, list[FilterOptionNode]]) -> int:
        added = 0
        self._block_signals = True
        for parent_id, children in mapping.items():
            item = self._find_item_by_param_value(parent_id)
            if not item or not children:
                continue
            saved_state = item.checkState(0)
            while item.childCount():
                item.removeChild(item.child(0))
            for child in children:
                item.addChild(self._create_option_item("Категория", child, depth=self._item_depth(item) + 1))
                added += 1
            item.setCheckState(0, saved_state)
            item.setExpanded(True)
        self._block_signals = False
        return added

    def _find_item_by_param_value(self, param_value: str) -> QTreeWidgetItem | None:
        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if str(item.data(0, self.ROLE_PARAM_VALUE) or "") == param_value:
                return item
            for i in range(item.childCount()):
                if found := walk(item.child(i)):
                    return found
            return None

        for i in range(self.topLevelItemCount()):
            if found := walk(self.topLevelItem(i)):
                return found
        return None

    def _item_depth(self, item: QTreeWidgetItem) -> int:
        depth = 0
        parent = item.parent()
        while parent is not None:
            depth += 1
            parent = parent.parent()
        return depth

    def _create_option_item(
        self,
        section: str,
        node: FilterOptionNode,
        depth: int = -1,
    ) -> QTreeWidgetItem:
        display = node.name
        item = QTreeWidgetItem([display])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        item.setData(0, self.ROLE_ID, node.id)
        item.setData(0, self.ROLE_URL, node.url)
        item.setData(0, self.ROLE_SECTION, section)
        item.setData(0, self.ROLE_PARAM_KEY, node.param_key)
        item.setData(0, self.ROLE_PARAM_VALUE, node.param_value)
        item.setData(0, self.ROLE_CATEGORY_ID, node.category_id)
        item.setData(0, self.ROLE_CATEGORY_NAME, node.category_name)
        item.setData(0, self.ROLE_PARENT_NAME, node.parent_name)
        item.setData(0, self.ROLE_RAW_NAME, node.name)
        child_depth = depth + 1 if depth >= 0 else -1
        for child in node.children:
            item.addChild(self._create_option_item(section, child, depth=child_depth))
        return item

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._block_signals:
            return
        if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        self._block_signals = True
        state = item.checkState(0)
        # Галочка на родителе отмечает все подкатегории; снятие — снимает у всех детей.
        if state in (Qt.CheckState.Checked, Qt.CheckState.Unchecked):
            self._set_children_state(item, state)
        self._update_parent_state(item.parent())
        self._block_signals = False

    def _set_children_state(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                child.setCheckState(0, state)
            self._set_children_state(child, state)

    def _update_parent_state(self, parent: QTreeWidgetItem | None) -> None:
        if parent is None or not (parent.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            if parent is not None:
                self._update_parent_state(parent.parent())
            return
        checked = 0
        partial = 0
        checkable_children = 0
        for i in range(parent.childCount()):
            child = parent.child(i)
            if not (child.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            checkable_children += 1
            st = child.checkState(0)
            if st == Qt.CheckState.Checked:
                checked += 1
            elif st == Qt.CheckState.PartiallyChecked:
                partial += 1
        if checkable_children == 0:
            self._update_parent_state(parent.parent())
            return
        if checked == checkable_children:
            parent.setCheckState(0, Qt.CheckState.Checked)
        elif checked == 0 and partial == 0:
            parent.setCheckState(0, Qt.CheckState.Unchecked)
        else:
            parent.setCheckState(0, Qt.CheckState.PartiallyChecked)
        self._update_parent_state(parent.parent())

    def _append_target(self, item: QTreeWidgetItem, targets: list[CategoryTarget]) -> None:
        option_id = item.data(0, self.ROLE_ID)
        if not option_id:
            return
        parent_name = str(item.data(0, self.ROLE_PARENT_NAME) or "")
        cat_id = str(item.data(0, self.ROLE_CATEGORY_ID) or item.data(0, self.ROLE_PARAM_VALUE) or "")
        section = str(item.data(0, self.ROLE_SECTION) or "")
        display = str(item.data(0, self.ROLE_RAW_NAME) or item.text(0))
        if parent_name and section == "Категория":
            display = f"{parent_name} → {display}"
        targets.append(
            CategoryTarget(
                id=str(option_id),
                name=display,
                url=item.data(0, self.ROLE_URL),
                section=section,
                param_key=str(item.data(0, self.ROLE_PARAM_KEY) or ""),
                param_value=str(item.data(0, self.ROLE_PARAM_VALUE) or ""),
                category_id=cat_id,
                category_name=str(item.data(0, self.ROLE_CATEGORY_NAME) or display),
                parent_name=parent_name,
            )
        )

    def _has_checked_descendant(self, item: QTreeWidgetItem) -> bool:
        for i in range(item.childCount()):
            child = item.child(i)
            if not (child.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                if self._has_checked_descendant(child):
                    return True
                continue
            if child.checkState(0) != Qt.CheckState.Unchecked:
                return True
        return False

    def selected_targets(self) -> list[CategoryTarget]:
        targets: list[CategoryTarget] = []

        def walk(item: QTreeWidgetItem) -> None:
            if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                for i in range(item.childCount()):
                    walk(item.child(i))
                return

            state = item.checkState(0)
            if state == Qt.CheckState.Unchecked:
                return

            if state == Qt.CheckState.PartiallyChecked:
                for i in range(item.childCount()):
                    walk(item.child(i))
                return

            # Checked: если отмечены подкатегории — парсим только их, не родителя
            if self._has_checked_descendant(item):
                for i in range(item.childCount()):
                    walk(item.child(i))
                return

            self._append_target(item, targets)

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        return targets

    def selected_checked_categories(self) -> list[CategoryTarget]:
        """Явно отмеченные категории (без каскада на детей)."""
        targets: list[CategoryTarget] = []

        def walk(item: QTreeWidgetItem) -> None:
            if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                for i in range(item.childCount()):
                    walk(item.child(i))
                return
            if item.checkState(0) == Qt.CheckState.Checked:
                self._append_target(item, targets)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        return targets

    def selected_leaf_categories(self) -> list[CategoryTarget]:
        return [
            target for target in self.selected_targets()
            if target.section == "Категория" or target.param_key == "category"
        ]

    def selected_categories(self) -> list[CategoryTarget]:
        return self.selected_leaf_categories()


class LoadCategoriesWorker(QThread):
    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)
    roots_loaded = pyqtSignal(list)
    subcategories_begin = pyqtSignal(int)
    branch_loaded = pyqtSignal(object)
    manual_bypass_needed = pyqtSignal(str)

    def __init__(
        self,
        seller_url: str,
        browser_mode: BrowserMode = DESKTOP_MODE,
        use_auth: bool = False,
    ):
        super().__init__()
        self.seller_url = seller_url
        self.browser_mode = browser_mode
        self.use_auth = use_auth
        self._bypass_event: threading.Event | None = None

    def resume_manual_bypass(self) -> None:
        if self._bypass_event:
            self._bypass_event.set()

    def run(self) -> None:
        import threading

        self._bypass_event = threading.Event()

        def on_manual_bypass(incident: str | None) -> None:
            self.manual_bypass_needed.emit(incident or "")
            self._bypass_event.clear()
            self._bypass_event.wait(timeout=600)

        try:
            parser = OzonParser(
                on_progress=lambda msg: self.progress.emit(msg),
                on_manual_bypass=on_manual_bypass,
            )
            categories = parser.load_categories(
                self.seller_url,
                use_auth=self.use_auth,
                use_cdp=self.browser_mode == DESKTOP_MODE,
                browser_mode=self.browser_mode,
                on_roots=lambda roots: self.roots_loaded.emit(roots),
                on_subcategories_begin=lambda total: self.subcategories_begin.emit(total),
                on_branch=lambda node: self.branch_loaded.emit(node),
            )
            self.finished_ok.emit(categories)
        except Exception as exc:
            self.failed.emit(str(exc))


class ParseWorker(QThread):
    finished_ok = pyqtSignal(list, str, object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)
    status_update = pyqtSignal(object)
    captcha_needed = pyqtSignal()
    manual_bypass_needed = pyqtSignal(str)

    def __init__(self, settings: ParseSettings, parser: OzonParser, export_meta: ExportMeta):
        super().__init__()
        self.settings = settings
        self.parser = parser
        self.export_meta = export_meta
        self._captcha_event = None
        self._bypass_event = None

    def resume_after_captcha(self) -> None:
        if self._captcha_event:
            self._captcha_event.set()

    def resume_manual_bypass(self) -> None:
        if self._bypass_event:
            self._bypass_event.set()

    def run(self) -> None:
        import threading

        self._captcha_event = threading.Event()
        self._bypass_event = threading.Event()

        def on_captcha() -> bool:
            self.captcha_needed.emit()
            self._captcha_event.wait(timeout=600)
            return True

        def on_manual_bypass(incident: str | None) -> None:
            self.manual_bypass_needed.emit(incident or "")
            self._bypass_event.clear()
            self._bypass_event.wait(timeout=600)

        self.parser.on_progress = lambda msg: self.progress.emit(msg)
        self.parser.on_status = lambda status: self.status_update.emit(status)
        self.parser.on_captcha = on_captcha
        self.parser.on_manual_bypass = on_manual_bypass

        try:
            products, stats = self.parser.run(self.settings)
            self.export_meta.parse_duration = stats.total_duration_fmt
            self.export_meta.section_timings = stats.summary_text()
            if not products:
                self.finished_ok.emit([], "", stats)
                return
            filepath = export_products(products, self.export_meta)
            self.finished_ok.emit(products, str(filepath.resolve()), stats)
        except Exception as exc:
            self.failed.emit(str(exc))


class MobileLoginWorker(QThread):
    completed = pyqtSignal(bool)
    progress = pyqtSignal(str)

    def run(self) -> None:
        ok = login_mobile_via_browser(
            on_progress=lambda message: self.progress.emit(message)
        )
        self.completed.emit(ok)


class MainWindow(QMainWindow):
    STATUS_IDLE = "Готов к работе"
    STATUS_PARSING = "Парсинг выполняется..."

    def __init__(self):
        super().__init__()
        screen = QApplication.primaryScreen().availableGeometry()
        font_family = load_roboto_light()
        font_size = scaled_font_size(screen.height())
        self.app_font = QFont(font_family, font_size, QFont.Weight.Light)
        QApplication.instance().setFont(self.app_font)

        self.parser: OzonParser | None = None
        self.parse_worker: ParseWorker | None = None
        self.category_worker: LoadCategoriesWorker | None = None
        self.mobile_login_worker: MobileLoginWorker | None = None
        self._parsed_products: list = []
        self._parsed_filepath: str = ""
        self._last_export_meta: ExportMeta | None = None
        self._catalog_subcat_total = 0
        self._loaded_category_mode: BrowserMode | None = None
        self._loaded_category_auth: bool | None = None

        self.setWindowTitle("Ozon Parser — Баллы за отзыв")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        pad = max(14, min(28, int(min(screen.width(), screen.height()) * 0.018)))
        root.setContentsMargins(pad, pad, pad, pad)
        root.setSpacing(pad)

        # --- Шапка: заголовок + статус справа ---
        header = QHBoxLayout()
        title = QLabel("Ozon Parser")
        title.setObjectName("AppTitle")
        header.addWidget(title)
        header.addStretch()
        self.app_status_label = QLabel(self.STATUS_IDLE)
        self.app_status_label.setObjectName("AppStatus")
        self.app_status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.app_status_label)
        root.addLayout(header)

        # --- Основная область: три колонки на всю ширину ---
        main_row = QHBoxLayout()
        main_row.setSpacing(pad)

        col_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Левая колонка — параметры парсинга
        params_group = QGroupBox("Параметры парсинга")
        params_group.setObjectName("ParamsGroup")
        params_group.setSizePolicy(col_policy)
        params_group.setMinimumWidth(260)
        params_layout = QVBoxLayout(params_group)
        params_layout.setSpacing(14)

        params_layout.addWidget(QLabel("Ссылка на продавца (опционально)"))
        self.seller_input = QLineEdit()
        self.seller_input.setText("https://www.ozon.ru/seller/puladuo-4720087/")
        self.seller_input.setPlaceholderText("https://www.ozon.ru/seller/...")
        params_layout.addWidget(self.seller_input)

        mode_row = QGridLayout()
        mode_row.addWidget(QLabel("Режим браузера"), 0, 0)
        self.browser_mode_combo = QComboBox()
        self.browser_mode_combo.addItem("Desktop", DESKTOP_MODE)
        self.browser_mode_combo.addItem("Мобильный", MOBILE_MODE)
        mode_row.addWidget(self.browser_mode_combo, 1, 0)
        mode_row.addWidget(QLabel("Авторизация"), 0, 1)
        self.auth_mode_combo = QComboBox()
        self.auth_mode_combo.addItem("Без авторизации", False)
        self.auth_mode_combo.addItem("С авторизацией", True)
        mode_row.addWidget(self.auth_mode_combo, 1, 1)
        params_layout.addLayout(mode_row)

        price_row = QHBoxLayout()
        price_col_l = QVBoxLayout()
        price_col_l.addWidget(QLabel("Цена от"))
        self.min_price = QDoubleSpinBox()
        self.min_price.setRange(0, 9999999)
        self.min_price.setSuffix(" ₽")
        price_col_l.addWidget(self.min_price)
        price_col_r = QVBoxLayout()
        price_col_r.addWidget(QLabel("Цена до"))
        self.max_price = QDoubleSpinBox()
        self.max_price.setRange(0, 9999999)
        self.max_price.setValue(999999)
        self.max_price.setSuffix(" ₽")
        price_col_r.addWidget(self.max_price)
        price_row.addLayout(price_col_l)
        price_row.addLayout(price_col_r)
        params_layout.addLayout(price_row)

        params_layout.addWidget(QLabel("Количество товаров (на категорию)"))
        self.max_products = QSpinBox()
        self.max_products.setRange(1, 10000)
        self.max_products.setValue(100)
        params_layout.addWidget(self.max_products)

        self.chrome_btn = QPushButton("Открыть Chrome")
        self.chrome_btn.clicked.connect(self.launch_chrome)
        setup_action_button(self.chrome_btn, min_height=44)
        params_layout.addWidget(self.chrome_btn)

        self.mobile_login_btn = QPushButton("Войти в мобильный Ozon")
        self.mobile_login_btn.clicked.connect(self.launch_mobile_login)
        setup_action_button(self.mobile_login_btn, min_height=44)
        params_layout.addWidget(self.mobile_login_btn)
        self.browser_mode_combo.currentIndexChanged.connect(self._on_session_mode_changed)
        self.auth_mode_combo.currentIndexChanged.connect(self._on_session_mode_changed)
        self._update_session_controls()

        self.detail_status_label = QLabel("Откройте Chrome и загрузите категории")
        self.detail_status_label.setWordWrap(True)
        self.detail_status_label.setObjectName("DetailStatus")
        params_layout.addWidget(self.detail_status_label)

        params_layout.addStretch()

        self.start_btn = QPushButton("Запустить парсер")
        self.start_btn.setObjectName("PrimaryButton")
        setup_action_button(self.start_btn, min_height=52)
        self.start_btn.clicked.connect(self.toggle_parsing)
        params_layout.addWidget(self.start_btn)

        self.download_btn = QPushButton("Скачать XLSX (0)")
        self.download_btn.setObjectName("SecondaryButton")
        self.download_btn.setEnabled(False)
        setup_action_button(self.download_btn, min_height=52)
        self.download_btn.clicked.connect(self.download_xlsx)
        params_layout.addWidget(self.download_btn)

        main_row.addWidget(params_group, stretch=22)

        # Центральный блок — каталог
        catalog_wrap = QWidget()
        catalog_wrap.setSizePolicy(col_policy)
        catalog_layout = QVBoxLayout(catalog_wrap)
        catalog_layout.setContentsMargins(0, 0, 0, 0)
        catalog_layout.setSpacing(pad)

        catalog_group = QGroupBox("Категории магазина")
        catalog_group.setObjectName("CatalogGroup")
        catalog_group.setSizePolicy(col_policy)
        cat_inner = QVBoxLayout(catalog_group)
        cat_inner.setSpacing(pad)

        cat_header = QHBoxLayout()
        cat_hint = QLabel("Загружается полная иерархия категорий магазина Ozon. Отметьте нужные и запустите парсер.")
        cat_hint.setObjectName("CatalogHint")
        cat_hint.setWordWrap(True)
        cat_header.addWidget(cat_hint, stretch=1)

        self.select_all_cat_btn = QPushButton("Выбрать все")
        self.select_all_cat_btn.setObjectName("LinkButton")
        self.select_all_cat_btn.setFlat(True)
        self.select_all_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_cat_btn.clicked.connect(self._select_all_categories)
        cat_header.addWidget(self.select_all_cat_btn)

        self.reset_cat_btn = QPushButton("Сбросить")
        self.reset_cat_btn.setObjectName("LinkButton")
        self.reset_cat_btn.setFlat(True)
        self.reset_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_cat_btn.clicked.connect(self._reset_categories)
        cat_header.addWidget(self.reset_cat_btn)
        cat_inner.addLayout(cat_header)

        self.category_tree = FilterTreeWidget()
        self.category_tree.setObjectName("CategoryTree")
        self.category_tree.setSizePolicy(col_policy)
        self.category_tree.setMinimumHeight(200)
        cat_inner.addWidget(self.category_tree, stretch=1)

        self.load_cat_btn = QPushButton("Загрузить категории")
        self.load_cat_btn.clicked.connect(self.load_categories)
        setup_action_button(self.load_cat_btn, min_height=44)
        cat_inner.addWidget(self.load_cat_btn)

        catalog_layout.addWidget(catalog_group, stretch=1)

        main_row.addWidget(catalog_wrap, stretch=46)

        # Правая колонка — прогресс и лог
        progress_group = QGroupBox("Прогресс и лог")
        progress_group.setObjectName("ProgressGroup")
        progress_group.setSizePolicy(col_policy)
        progress_group.setMinimumWidth(240)
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setSpacing(pad)

        self.progress_value_label = QLabel("—")
        self.progress_value_label.setObjectName("ProgressValue")
        self.progress_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_layout.addWidget(self.progress_value_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        progress_layout.addWidget(self.progress_bar)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("LogView")
        self.log_view.setPlaceholderText("Здесь будут сообщения о ходе работы...")
        self.log_view.setSizePolicy(col_policy)
        self.log_view.setMinimumHeight(100)
        progress_layout.addWidget(self.log_view, stretch=1)

        main_row.addWidget(progress_group, stretch=32)

        root.addLayout(main_row, stretch=1)

        footer = QLabel(f"Файлы сохраняются в: {OUTPUT_DIR}")
        footer.setObjectName("Footer")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(footer)

        self.status_label = self.detail_status_label
        self._apply_styles()
        self._set_app_status(self.STATUS_IDLE, parsing=False)
        self._update_download_button()

    def _set_app_status(self, text: str, *, parsing: bool = False) -> None:
        self.app_status_label.setText(text)
        self.app_status_label.setProperty("parsing", parsing)
        self.app_status_label.style().unpolish(self.app_status_label)
        self.app_status_label.style().polish(self.app_status_label)

    def _selected_browser_mode(self) -> BrowserMode:
        return self.browser_mode_combo.currentData()

    def _selected_use_auth(self) -> bool:
        return bool(self.auth_mode_combo.currentData())

    def _update_session_controls(self) -> None:
        mobile = self._selected_browser_mode() == MOBILE_MODE
        use_auth = self._selected_use_auth()
        self.chrome_btn.setVisible(not mobile)
        self.chrome_btn.setEnabled(not mobile)
        self.mobile_login_btn.setVisible(mobile and use_auth)
        self.mobile_login_btn.setEnabled(
            mobile
            and use_auth
            and not (
                self.mobile_login_worker
                and self.mobile_login_worker.isRunning()
            )
        )

    def _on_session_mode_changed(self) -> None:
        self._update_session_controls()
        if (
            self._loaded_category_mode is not None
            and (
                self._loaded_category_mode != self._selected_browser_mode()
                or self._loaded_category_auth != self._selected_use_auth()
            )
        ):
            self.category_tree.clear()
            self._loaded_category_mode = None
            self._loaded_category_auth = None
            message = "Режим изменён — загрузите категории заново"
            self.status_label.setText(message)
            self._append_log(message)

    def _validate_mobile_auth(self) -> bool:
        if (
            self._selected_browser_mode() == MOBILE_MODE
            and self._selected_use_auth()
            and not has_mobile_saved_session()
        ):
            QMessageBox.warning(
                self,
                "Требуется мобильный вход",
                "Сначала нажмите «Войти в мобильный Ozon» и завершите вход.",
            )
            return False
        return True

    def _update_download_button(self) -> None:
        count = len(self._parsed_products)
        self.download_btn.setText(f"Скачать XLSX ({count})")
        self.download_btn.setEnabled(count > 0)

    def download_xlsx(self) -> None:
        if not self._parsed_products:
            QMessageBox.information(self, "Нет данных", "Сначала выполните парсинг.")
            return
        path = self._parsed_filepath
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        if self._last_export_meta:
            path = str(export_products(self._parsed_products, self._last_export_meta).resolve())
            self._parsed_filepath = path
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _apply_styles(self) -> None:
        fs = self.app_font.pointSize()
        btn_fs = max(14, min(18, fs - 8))
        cb_fs = max(13, min(17, fs - 8))
        btn_h = max(44, btn_fs * 2 + 8)
        input_fs = max(14, fs - 6)
        tree_fs = max(14, fs - 4)
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{
                background: #12151c;
                color: #e8eaed;
            }}
            QLabel#AppTitle {{
                font-size: {fs + 4}px;
                font-weight: 300;
                color: #ffffff;
            }}
            QLabel#AppStatus {{
                font-size: {max(16, fs - 2)}px;
                font-weight: 300;
                color: #9aa0a6;
                padding: 6px 12px;
                border-radius: 6px;
                background: #1e222b;
            }}
            QLabel#AppStatus[parsing="true"] {{
                color: #7dd3fc;
                background: #1a2836;
            }}
            QLabel#DetailStatus {{
                color: #9aa0a6;
                font-size: {max(13, fs - 8)}px;
            }}
            QLabel#Footer {{
                color: #6b7280;
                font-size: {max(12, fs - 10)}px;
            }}
            QGroupBox {{
                font-size: {fs - 2}px;
                font-weight: 300;
                border: 1px solid #2d3340;
                border-radius: 10px;
                margin-top: 14px;
                padding-top: 18px;
                background: #1a1e27;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 8px;
                color: #f3f4f6;
            }}
            QPushButton {{
                font-size: {btn_fs}px;
                font-weight: 300;
                background: #2563eb;
                color: #ffffff;
                border: none;
                border-radius: 8px;
                padding: 10px 14px;
                min-height: {btn_h}px;
            }}
            QPushButton#SecondaryButton {{
                background: #2d3748;
                border: 1px solid #4b5563;
            }}
            QPushButton#SecondaryButton:hover {{
                background: #374151;
            }}
            QPushButton:hover {{
                background: #1d4ed8;
            }}
            QPushButton:disabled {{
                background: #2a3140;
                color: #6b7280;
                border: 1px solid #2d3340;
            }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                font-size: {input_fs}px;
                font-weight: 300;
                padding: 8px 12px;
                border: 1px solid #3d4450;
                border-radius: 6px;
                background: #0f1319;
                color: #e8eaed;
                min-height: 36px;
            }}
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
                border-color: #2563eb;
            }}
            QTreeWidget {{
                font-size: {tree_fs}px;
                font-weight: 300;
                border: 1px solid #3d4450;
                border-radius: 8px;
                background: #0f1319;
                color: #e8eaed;
                alternate-background-color: #151922;
                outline: 0;
            }}
            QTreeWidget#CategoryTree {{
                border: 1px solid #2d3340;
                background: #12151c;
                padding: 4px 2px;
            }}
            QTreeWidget::item {{
                padding: 5px 2px;
                min-height: 28px;
                border-radius: 4px;
            }}
            QTreeWidget::item:hover {{
                background: #1f2937;
            }}
            QTreeWidget::item:selected {{
                background: #1e3a5f;
                color: #ffffff;
            }}
            QTreeWidget::branch {{
                background: transparent;
            }}
            QTreeWidget::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 2px solid #ffffff;
                background: transparent;
            }}
            QTreeWidget::indicator:hover {{
                border-color: #93c5fd;
            }}
            QTreeWidget::indicator:checked {{
                background: #2563eb;
                border-color: #2563eb;
                image: none;
            }}
            QTreeWidget::indicator:indeterminate {{
                background: #2563eb;
                border-color: #2563eb;
            }}
            QLabel#CatalogHint {{
                color: #9aa0a6;
                font-size: {max(13, fs - 8)}px;
                font-weight: 300;
            }}
            QPushButton#LinkButton {{
                background: transparent;
                color: #60a5fa;
                border: none;
                font-size: {max(13, fs - 8)}px;
                font-weight: 300;
                padding: 4px 10px;
                min-height: 0;
            }}
            QPushButton#LinkButton:hover {{
                color: #93c5fd;
                background: transparent;
                text-decoration: underline;
            }}
            QPushButton#LinkButton:pressed {{
                color: #3b82f6;
            }}
            QCheckBox {{
                font-size: {cb_fs}px;
                font-weight: 300;
                spacing: 8px;
                color: #d1d5db;
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 2px solid #ffffff;
                background: transparent;
            }}
            QCheckBox::indicator:checked {{
                background: #2563eb;
                border-color: #2563eb;
            }}
            QCheckBox::indicator:disabled {{
                border-color: #6b7280;
                background: #4b5563;
            }}
            QLabel {{
                font-size: {tree_fs}px;
                font-weight: 300;
                color: #d1d5db;
            }}
            QScrollBar:vertical {{
                background: #1a1e27;
                width: 10px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: #4b5563;
                border-radius: 5px;
                min-height: 24px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QLabel#ProgressValue {{
                font-size: {fs + 8}px;
                font-weight: 300;
                color: #7dd3fc;
                padding: 8px 0;
            }}
            QPlainTextEdit#LogView {{
                font-size: {max(13, fs - 8)}px;
                font-weight: 300;
                font-family: Consolas, "Courier New", monospace;
                background: #0f1319;
                color: #cbd5e1;
                border: 1px solid #3d4450;
                border-radius: 8px;
                padding: 10px;
            }}
            QProgressBar {{
                background: #0f1319;
                border: 1px solid #3d4450;
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: #2563eb;
                border-radius: 3px;
            }}
            """
        )

    def _append_log(self, message: str) -> None:
        text = message.strip()
        if not text:
            return
        self.log_view.appendPlainText(text)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_progress_fraction(self, current: int, total: int) -> None:
        if total > 0:
            self.progress_value_label.setText(f"{current}/{total}")
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(min(current, total))
        else:
            self.progress_value_label.setText("—")
            self.progress_bar.setValue(0)

    def _reset_progress_panel(self) -> None:
        self.progress_value_label.setText("—")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    def _on_worker_progress(self, message: str) -> None:
        self.status_label.setText(message)
        self._append_log(message)

    def _reset_parse_display(self) -> None:
        self._reset_progress_panel()

    def _on_parse_status(self, status: ParseStatus) -> None:
        if status.total_count:
            self._set_progress_fraction(status.current_index, status.total_count)

    def launch_chrome(self) -> None:
        if launch_chrome_for_ozon():
            msg = "Chrome открыт. Дождитесь загрузки Ozon, затем нажмите «Загрузить категории»"
            self.status_label.setText(msg)
            self._append_log(msg)
        else:
            QMessageBox.warning(
                self,
                "Chrome не найден",
                "Установите Google Chrome или запустите chrome_for_ozon.bat вручную.",
            )

    def launch_mobile_login(self) -> None:
        if self.mobile_login_worker and self.mobile_login_worker.isRunning():
            return
        self.mobile_login_btn.setEnabled(False)
        message = "Открываем отдельное окно мобильной авторизации..."
        self.status_label.setText(message)
        self._append_log(message)
        self.mobile_login_worker = MobileLoginWorker()
        self.mobile_login_worker.progress.connect(self._on_worker_progress)
        self.mobile_login_worker.completed.connect(self._on_mobile_login_completed)
        self.mobile_login_worker.start()

    def _on_mobile_login_completed(self, ok: bool) -> None:
        self._update_session_controls()
        if ok:
            message = "Мобильный профиль сохранён и готов для авторизованного режима"
            self.status_label.setText(message)
            self._append_log(message)
            return
        QMessageBox.warning(
            self,
            "Мобильный вход не сохранён",
            "Не удалось открыть или сохранить отдельный мобильный профиль.",
        )

    def _show_manual_bypass_dialog(self, incident: str) -> None:
        msg = (
            "Ozon заблокировал доступ (это не проблема интернета).\n\n"
        )
        if incident:
            msg += f"Инцидент: {incident}\n\n"
        msg += (
            "В окне Chrome:\n"
            "1. Нажмите F5 (обновить страницу)\n"
            "2. Пройдите проверку, если появится\n"
            "3. Дождитесь нормальной загрузки Ozon\n"
            "4. Нажмите OK здесь"
        )
        QMessageBox.information(self, "Требуется ваше действие", msg)

    def _on_category_manual_bypass(self, incident: str) -> None:
        self._show_manual_bypass_dialog(incident)
        if self.category_worker:
            self.category_worker.resume_manual_bypass()

    def _on_parse_manual_bypass(self, incident: str) -> None:
        self._show_manual_bypass_dialog(incident)
        if self.parse_worker:
            self.parse_worker.resume_manual_bypass()

    def _select_all_categories(self) -> None:
        self.category_tree.select_all_items()

    def _reset_categories(self) -> None:
        self.category_tree.reset_selection()

    def load_categories(self) -> None:
        url = self.seller_input.text().strip()
        if not url:
            QMessageBox.warning(
                self,
                "Ссылка не указана",
                "Для загрузки категорий укажите ссылку на продавца или откройте Chrome с нужным магазином.",
            )
            return
        if not self._validate_mobile_auth():
            return
        browser_mode = self._selected_browser_mode()
        use_auth = self._selected_use_auth()
        self.load_cat_btn.setEnabled(False)
        self.status_label.setText("Загрузка категорий...")
        self._append_log("Загрузка категорий верхнего уровня...")
        self.category_tree.begin_incremental_load()
        self.category_worker = LoadCategoriesWorker(
            url,
            browser_mode=browser_mode,
            use_auth=use_auth,
        )
        self._loaded_category_mode = browser_mode
        self._loaded_category_auth = use_auth
        self.category_worker.progress.connect(self._on_worker_progress)
        self.category_worker.roots_loaded.connect(self._on_category_roots_loaded)
        self.category_worker.subcategories_begin.connect(self._on_subcategories_begin)
        self.category_worker.branch_loaded.connect(self._on_category_branch_loaded)
        self.category_worker.manual_bypass_needed.connect(self._on_category_manual_bypass)
        self.category_worker.finished_ok.connect(self._on_categories_loaded)
        self.category_worker.failed.connect(self._on_categories_failed)
        self.category_worker.start()

    def _count_subcategories(self, nodes: list) -> int:
        def count_descendants(node) -> int:
            total = 0
            for child in node.children:
                total += 1 + count_descendants(child)
            return total

        return sum(count_descendants(node) for node in nodes)

    def _on_category_roots_loaded(self, roots: list) -> None:
        self.category_tree.set_initial_roots(roots)
        self.status_label.setText(
            f"Категории отображены ({len(roots)}). Подкатегории загружаются..."
        )
        self._append_log(f"Категории отображены: {len(roots)}")

    def _on_subcategories_begin(self, total: int) -> None:
        self._catalog_subcat_total = total
        self._catalog_subcat_done = 0
        self._append_log(f"Загрузка подкатегорий для {total} категорий (до 15 мин)...")

    def _on_category_branch_loaded(self, node) -> None:
        self.category_tree.update_category_branch(node)

    def _on_categories_loaded(self, categories: list) -> None:
        self.category_tree.finalize_catalog_load(categories)
        self.category_tree.expandToDepth(4)
        self.load_cat_btn.setEnabled(True)
        sub_count = self._count_subcategories(categories)
        total = self._catalog_subcat_total or len(categories)
        with_subcats = sum(1 for c in categories if c.children)
        if total and with_subcats < total:
            self.status_label.setText(
                f"Категории частично загружены: подкатегории для {with_subcats}/{total} разделов."
            )
            self._append_log(
                f"Лимит 15 мин: подкатегории собраны для {with_subcats}/{total} категорий"
            )
        else:
            self.status_label.setText(
                f"Категории загружены: {len(categories)} разделов, {sub_count} подкатегорий. "
                "Отметьте нужные и запустите парсер."
            )
        self._append_log(f"Итого: {len(categories)} категорий, {sub_count} подкатегорий")

    def _on_categories_failed(self, error: str) -> None:
        self.load_cat_btn.setEnabled(True)
        self._loaded_category_mode = None
        self._loaded_category_auth = None
        self.status_label.setText(f"Ошибка загрузки категорий: {error}")
        hint = (
            "Ozon заблокировал доступ.\n\n"
            "1. Нажмите «Открыть Chrome для Ozon»\n"
            "2. Дождитесь загрузки сайта в Chrome\n"
            "3. Если видите «нет соединения» — обновите страницу (F5)\n"
            "4. Повторите «Загрузить категории»"
        )
        if "нет соединения" in error.lower() or "заблокировал" in error.lower():
            QMessageBox.warning(self, "Блокировка Ozon", hint)
        else:
            QMessageBox.warning(self, "Ошибка", error)

    def toggle_parsing(self) -> None:
        if self.parse_worker and self.parse_worker.isRunning():
            if self.parser:
                self.parser.stop()
            self.start_btn.setEnabled(False)
            self.status_label.setText("Остановка парсера...")
            self._append_log("Остановка парсера...")
            return
        self.start_parsing()

    def start_parsing(self) -> None:
        url = self.seller_input.text().strip()
        if not url:
            QMessageBox.warning(
                self,
                "Ссылка не указана",
                "Укажите ссылку на продавца для запуска парсера.",
            )
            return
        if not self._validate_mobile_auth():
            return
        browser_mode = self._selected_browser_mode()
        use_auth = self._selected_use_auth()
        if (
            self._loaded_category_mode != browser_mode
            or self._loaded_category_auth != use_auth
        ):
            QMessageBox.warning(
                self,
                "Категории устарели",
                "Загрузите категории заново для выбранного режима браузера и авторизации.",
            )
            return

        categories = self.category_tree.selected_leaf_categories()
        if not categories:
            QMessageBox.warning(
                self,
                "Ничего не выбрано",
                "Отметьте категории или подкатегории для парсинга.",
            )
            return

        min_p = self.min_price.value() if self.min_price.value() > 0 else None
        max_p = self.max_price.value() if self.max_price.value() < 999999 else None

        settings = ParseSettings(
            seller_url=url,
            categories=categories,
            min_price=min_p,
            max_price=max_p,
            max_products=self.max_products.value(),
            use_auth=use_auth,
            import_browser_session=browser_mode == DESKTOP_MODE,
            use_cdp=browser_mode == DESKTOP_MODE,
            browser_mode=browser_mode,
        )

        export_meta = ExportMeta(
            seller_url=url,
            min_price=min_p,
            max_price=max_p,
            max_products=self.max_products.value(),
            categories=self._selected_targets_label(categories),
        )

        self._reset_parse_display()
        self.log_view.clear()
        self._append_log("Запуск парсера...")
        self._set_app_status(self.STATUS_PARSING, parsing=True)
        self.parser = OzonParser()
        self.parse_worker = ParseWorker(settings, self.parser, export_meta)
        self._last_export_meta = export_meta
        self.parse_worker.progress.connect(self._on_worker_progress)
        self.parse_worker.status_update.connect(self._on_parse_status)
        self.parse_worker.captcha_needed.connect(self._on_captcha)
        self.parse_worker.manual_bypass_needed.connect(self._on_parse_manual_bypass)
        self.parse_worker.finished_ok.connect(self._on_parse_finished)
        self.parse_worker.failed.connect(self._on_parse_failed)
        self.parse_worker.start()

        self.start_btn.setText("Остановить парсер")
        self.start_btn.setEnabled(True)
        self.load_cat_btn.setEnabled(False)

    def _selected_targets_label(self, categories: list[CategoryTarget]) -> str:
        if not categories:
            return "—"
        labels: list[str] = []
        for target in categories:
            prefix = f"{target.parent_name} → " if target.parent_name else ""
            labels.append(f"{prefix}{target.name}")
        return ", ".join(labels)

    def _on_captcha(self) -> None:
        QMessageBox.information(
            self,
            "Капча",
            "Обнаружена капча. Решите её в открытом окне браузера, затем нажмите OK.",
        )
        if self.parse_worker:
            self.parse_worker.resume_after_captcha()

    def _on_parse_finished(self, products: list, filepath: str, stats) -> None:
        self._set_app_status(self.STATUS_IDLE, parsing=False)
        self.start_btn.setText("Запустить парсер")
        self.start_btn.setEnabled(True)
        self.load_cat_btn.setEnabled(True)
        duration_text = stats.total_duration_fmt if stats else "—"
        self._parsed_products = products
        self._parsed_filepath = filepath
        self._update_download_button()
        if not products:
            self.status_label.setText(f"Товары не найдены. Время парсинга: {duration_text}")
            self._append_log(f"Товары не найдены. Время: {duration_text}")
            if stats and stats.section_timings:
                for timing in stats.section_timings:
                    self._append_log(timing.summary_line())
            QMessageBox.information(self, "Готово", f"Товары не найдены\n\nВремя: {duration_text}")
            return
        abs_path = filepath
        self.status_label.setText(
            f"Готово! {len(products)} товаров за {duration_text}. Нажмите «Скачать XLSX»."
        )
        self._append_log(f"Готово: {len(products)} товаров за {duration_text}")
        if stats and stats.section_timings:
            for timing in stats.section_timings:
                self._append_log(timing.summary_line())
        QMessageBox.information(
            self,
            "Готово",
            f"Сохранено {len(products)} товаров\nВремя парсинга: {duration_text}\n\n{abs_path}",
        )

    def _on_parse_failed(self, error: str) -> None:
        self._set_app_status(self.STATUS_IDLE, parsing=False)
        self.start_btn.setText("Запустить парсер")
        self.start_btn.setEnabled(True)
        self.load_cat_btn.setEnabled(True)
        self.status_label.setText(f"Ошибка: {error}")
        self._append_log(f"Ошибка: {error}")
        QMessageBox.critical(self, "Ошибка", error)


def run_app() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    run_app()
