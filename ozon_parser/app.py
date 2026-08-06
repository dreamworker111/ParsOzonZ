import sys
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ozon_parser.categories import CategoryTarget
from ozon_parser.filters import FilterOptionNode
from ozon_parser.config import (
    BrowserMode,
    DESKTOP_MODE,
    FONTS_DIR,
    MOBILE_MODE,
    OUTPUT_DIR,
    PARSE_MODE_ALL_SELLERS_CATEGORIES,
    PARSE_MODE_ALL_SELLERS_FULL,
    PARSE_MODE_GLOBAL_CATEGORIES,
    PARSE_MODE_SELLER_CATEGORIES,
    PARSE_MODE_SELLER_FULL,
    ParseMode,
    CATEGORY_REQUIRED_PARSE_MODES,
    SELLER_PARSE_MODES,
)
from ozon_parser.auth import has_mobile_saved_session
from ozon_parser.export import ExportMeta, export_products
from ozon_parser.login import login_mobile_via_browser
from ozon_parser.parse_stats import ParseStatus
from ozon_parser.instructions import INSTRUCTIONS_TITLE, build_user_instructions
from ozon_parser.parse_checkpoint import (
    CHECKPOINT_PATH,
    clear_checkpoint,
    describe_checkpoint,
)
from ozon_parser.parser import OzonParser, ParseSettings
from ozon_parser.utils import extract_ozon_category_id
from ozon_parser.theme import (
    THEMES,
    ThemeName,
    build_stylesheet,
    load_theme_preference,
    normalize_theme,
    save_theme_preference,
)


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
    # Scale continuously on compact/HiDPI screens; the old 800 px threshold
    # jumped directly from 19 to 27 px and caused controls to overlap.
    return max(16, min(base, round(base * screen_height / 1080)))


def setup_action_button(button: QPushButton, min_height: int = 56) -> None:
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    button.setMinimumHeight(min_height)
    button.setMaximumWidth(16777215)


def setup_param_input(widget: QWidget, *, min_height: int = 40) -> None:
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    widget.setMinimumHeight(min_height)


class WrapLabel(QLabel):
    """Label with word wrap that reserves enough vertical space for all lines."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def setText(self, text: str) -> None:  # type: ignore[override]
        super().setText(text)
        self._sync_wrap_height()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_wrap_height()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_wrap_height()

    def _sync_wrap_height(self) -> None:
        if self.maximumHeight() > 0 and self.maximumHeight() < 16777215:
            # Capped labels (e.g. DetailStatus) must not grow and push buttons away.
            return
        width = self.contentsRect().width()
        if width <= 0:
            width = self.width()
        if width <= 0:
            return
        height = self.heightForWidth(width)
        if height > 0:
            self.setMinimumHeight(height)


def make_param_label(text: str) -> WrapLabel:
    label = WrapLabel(text)
    label.setObjectName("ParamLabel")
    return label


def param_field_block(label: WrapLabel | str, widget: QWidget, *, spacing: int = 8) -> QWidget:
    if isinstance(label, str):
        label = make_param_label(label)
    setup_param_input(widget)
    block = QWidget()
    block.setObjectName("ParamField")
    block.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    row = QVBoxLayout(block)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(spacing)
    row.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
    row.addWidget(label)
    row.addWidget(widget)
    return block



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
    ROLE_PENDING_SUBCATEGORIES = Qt.ItemDataRole.UserRole + 11
    ROLE_DEPTH = Qt.ItemDataRole.UserRole + 12
    ROLE_FULL_PATH = Qt.ItemDataRole.UserRole + 13

    selectionChangedCount = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.placeholder_color = QColor(THEMES["dark"].placeholder)
        self._depth_muted = QColor(THEMES["dark"].text_muted)
        self._depth_soft = QColor(THEMES["dark"].text_soft)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setAnimated(True)
        self.setItemsExpandable(True)
        self.setExpandsOnDoubleClick(True)
        self.setIndentation(32)
        self.setUniformRowHeights(False)
        self.itemChanged.connect(self._on_item_changed)
        self._block_signals = False

    def selected_category_count(self) -> int:
        """How many categories will be parsed with the current checkboxes."""
        return len(self.selected_leaf_categories())

    def _emit_selection_count(self) -> None:
        self.selectionChangedCount.emit(self.selected_category_count())

    def set_placeholder_color(self, color: str) -> None:
        self.placeholder_color = QColor(color)
        for index in range(self.topLevelItemCount()):
            self._refresh_placeholder_colors(self.topLevelItem(index))

    def set_hierarchy_colors(self, muted: str, soft: str) -> None:
        self._depth_muted = QColor(muted)
        self._depth_soft = QColor(soft)
        self._refresh_hierarchy_styles()

    def _refresh_placeholder_colors(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        if item.data(0, self.ROLE_IS_PLACEHOLDER):
            self._style_placeholder_item(item)
        for child_index in range(item.childCount()):
            self._refresh_placeholder_colors(item.child(child_index))

    def populate_categories(self, roots: list[FilterOptionNode]) -> None:
        self.clear()
        self._block_signals = True
        for root in roots:
            self.addTopLevelItem(self._create_option_item("Категория", root, depth=0))
        self.expandToDepth(2)
        self._block_signals = False
        self._emit_selection_count()

    def _style_placeholder_item(self, item: QTreeWidgetItem) -> None:
        item.setForeground(0, QBrush(self.placeholder_color))
        font = item.font(0)
        font.setItalic(True)
        item.setFont(0, font)

    def _create_pending_subcategories_item(self) -> QTreeWidgetItem:
        placeholder = QTreeWidgetItem(["Подкатегории ещё не загружены"])
        placeholder.setFlags(placeholder.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        placeholder.setData(0, self.ROLE_IS_PLACEHOLDER, True)
        placeholder.setData(0, self.ROLE_PENDING_SUBCATEGORIES, True)
        self._style_placeholder_item(placeholder)
        return placeholder

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
        self._emit_selection_count()

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
        self._emit_selection_count()

    def begin_incremental_load(self) -> None:
        self.clear()
        self._emit_selection_count()

    def set_initial_roots(
        self,
        roots: list[FilterOptionNode],
        *,
        pending_subcategories: bool = False,
    ) -> None:
        self._block_signals = True
        for root in roots:
            item = self._create_option_item("Категория", root, depth=0)
            if pending_subcategories:
                item.addChild(self._create_pending_subcategories_item())
            else:
                item.addChild(self._create_loading_placeholder())
            self.addTopLevelItem(item)
        self.expandToDepth(1)
        self._block_signals = False
        self._emit_selection_count()

    def selected_root_categories(self) -> list[CategoryTarget]:
        """Checked top-level categories (for selective subcategory load)."""
        targets: list[CategoryTarget] = []
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            if item.checkState(0) == Qt.CheckState.Unchecked:
                continue
            self._append_target(item, targets)
        return targets

    def _expand_item_branch(self, item: QTreeWidgetItem, max_depth: int = 2, depth: int = 0) -> None:
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
        depth = self._item_depth(item) if item is not None else 0
        path_prefix = ""
        if item is not None and item.parent() is not None:
            path_prefix = str(
                item.parent().data(0, self.ROLE_FULL_PATH)
                or item.parent().data(0, self.ROLE_RAW_NAME)
                or ""
            )
        new_item = self._create_option_item(
            "Категория",
            node,
            depth=depth,
            path_prefix=path_prefix,
        )
        if item is None:
            self.addTopLevelItem(new_item)
        else:
            idx = self.indexOfTopLevelItem(item)
            if idx >= 0:
                self.takeTopLevelItem(idx)
                self.insertTopLevelItem(idx, new_item)
            else:
                parent = item.parent()
                if parent is not None:
                    idx = parent.indexOfChild(item)
                    parent.takeChild(idx)
                    parent.insertChild(idx, new_item)
                else:
                    self.addTopLevelItem(new_item)
        # After subcategory load, leave the branch unchecked so the user
        # must pick concrete leaves — avoids parsing the whole root.
        new_item.setCheckState(0, Qt.CheckState.Unchecked)
        self._set_children_state(new_item, Qt.CheckState.Unchecked)
        self._expand_item_branch(new_item)
        self._block_signals = False
        self._emit_selection_count()
        self.scrollToItem(new_item)

    def _finalize_subcategory_placeholders(self, item: QTreeWidgetItem) -> None:
        # Roots the user did not expand keep the pending hint.
        for i in range(item.childCount()):
            child = item.child(i)
            if child.data(0, self.ROLE_PENDING_SUBCATEGORIES):
                return

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
        # Apply returned branches (source of truth), then clean placeholders.
        for node in categories or []:
            param = str(getattr(node, "param_value", "") or getattr(node, "category_id", "") or "")
            if not param:
                continue
            item = self._find_item_by_param_value(param)
            # Incremental branch_loaded already filled this root — do not rebuild
            # (rebuild was duplicating deep leaves in the UI).
            if item is not None and self._checkable_child_count(item) > 0:
                self._finalize_subcategory_placeholders(item)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self._set_children_state(item, Qt.CheckState.Unchecked)
                continue
            depth = self._item_depth(item) if item is not None else 0
            path_prefix = ""
            if item is not None and item.parent() is not None:
                path_prefix = str(
                    item.parent().data(0, self.ROLE_FULL_PATH)
                    or item.parent().data(0, self.ROLE_RAW_NAME)
                    or ""
                )
            new_item = self._create_option_item(
                "Категория",
                node,
                depth=depth,
                path_prefix=path_prefix,
            )
            if item is None:
                self.addTopLevelItem(new_item)
            else:
                idx = self.indexOfTopLevelItem(item)
                if idx >= 0:
                    self.takeTopLevelItem(idx)
                    self.insertTopLevelItem(idx, new_item)
                else:
                    # Non-top-level match: replace in place via parent.
                    parent = item.parent()
                    if parent is not None:
                        idx = parent.indexOfChild(item)
                        parent.takeChild(idx)
                        parent.insertChild(idx, new_item)
            # Clear checks after tree fill — user picks leaves for parsing.
            new_item.setCheckState(0, Qt.CheckState.Unchecked)
            self._set_children_state(new_item, Qt.CheckState.Unchecked)
        for i in range(self.topLevelItemCount()):
            self._finalize_subcategory_placeholders(self.topLevelItem(i))
            self._expand_item_branch(self.topLevelItem(i))
        self._refresh_hierarchy_styles()
        self._block_signals = False
        self._emit_selection_count()

    def merge_subcategories(self, mapping: dict[str, list[FilterOptionNode]]) -> int:
        added = 0
        self._block_signals = True
        for parent_id, children in mapping.items():
            item = self._find_item_by_param_value(parent_id)
            if not item or not children:
                continue
            while item.childCount():
                item.removeChild(item.child(0))
            for child in children:
                item.addChild(self._create_option_item("Категория", child, depth=self._item_depth(item) + 1))
                added += 1
            item.setCheckState(0, Qt.CheckState.Unchecked)
            self._set_children_state(item, Qt.CheckState.Unchecked)
            item.setExpanded(True)
        self._block_signals = False
        self._emit_selection_count()
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

    def _checkable_child_count(self, item: QTreeWidgetItem) -> int:
        total = 0
        for i in range(item.childCount()):
            child = item.child(i)
            if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                total += 1
        return total

    def _category_path_label(self, item: QTreeWidgetItem) -> str:
        parts: list[str] = []
        current: QTreeWidgetItem | None = item
        while current is not None:
            if current.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                parts.append(str(current.data(0, self.ROLE_RAW_NAME) or current.text(0)))
            current = current.parent()
        parts.reverse()
        return " → ".join(parts)

    def _immediate_parent_name(self, item: QTreeWidgetItem) -> str:
        parent = item.parent()
        while parent is not None:
            if parent.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                return str(parent.data(0, self.ROLE_RAW_NAME) or parent.text(0) or "")
            parent = parent.parent()
        return str(item.data(0, self.ROLE_PARENT_NAME) or "")

    def _format_category_label(
        self,
        name: str,
        child_count: int,
        depth: int,
        parent_name: str = "",
    ) -> str:
        name = str(name or "").strip() or "Без названия"
        if depth >= 2 and parent_name:
            # Deep nodes keep affiliation visible even when the tree is busy.
            label = f"↳ {name}  ← {parent_name}"
        elif depth >= 1:
            label = f"· {name}"
        else:
            label = name
        if child_count > 0:
            label = f"{label}  ({child_count})"
        return label

    def _apply_hierarchy_style(self, item: QTreeWidgetItem, depth: int) -> None:
        if item.data(0, self.ROLE_IS_PLACEHOLDER):
            return
        child_count = self._checkable_child_count(item)
        raw_name = str(item.data(0, self.ROLE_RAW_NAME) or item.text(0))
        parent_name = self._immediate_parent_name(item)
        path = self._category_path_label(item)
        # While children are built they are not attached yet; keep precomputed path.
        if item.parent() is None:
            stored = str(item.data(0, self.ROLE_FULL_PATH) or "")
            path = stored or path
        item.setData(0, self.ROLE_DEPTH, depth)
        item.setData(0, self.ROLE_FULL_PATH, path)
        if parent_name:
            item.setData(0, self.ROLE_PARENT_NAME, parent_name)
        item.setText(
            0,
            self._format_category_label(raw_name, child_count, depth, parent_name),
        )
        tip = path if path == raw_name else f"{path}\nУровень: {depth + 1}"
        if child_count:
            tip += f"\nПрямых подкатегорий: {child_count}"
        item.setToolTip(0, tip)

        font = item.font(0)
        font.setBold(depth == 0 or child_count > 0)
        item.setFont(0, font)

        if depth >= 2:
            item.setForeground(0, QBrush(self._depth_muted))
        elif depth == 1 and child_count == 0:
            item.setForeground(0, QBrush(self._depth_soft))
        else:
            item.setForeground(0, QBrush())

    def _refresh_hierarchy_styles(self, item: QTreeWidgetItem | None = None) -> None:
        def walk(node: QTreeWidgetItem, depth: int) -> None:
            self._apply_hierarchy_style(node, depth)
            for i in range(node.childCount()):
                child = node.child(i)
                if child.data(0, self.ROLE_IS_PLACEHOLDER):
                    continue
                walk(child, depth + 1)

        if item is not None:
            walk(item, self._item_depth(item))
            return
        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i), 0)

    def _create_option_item(
        self,
        section: str,
        node: FilterOptionNode,
        depth: int = -1,
        path_prefix: str = "",
    ) -> QTreeWidgetItem:
        raw_name = str(node.name or "").strip() or str(node.param_value or node.id or "")
        immediate_parent = str(node.parent_name or "").strip()
        if not immediate_parent and path_prefix:
            immediate_parent = path_prefix.rsplit(" → ", 1)[-1]
        full_path = raw_name if not path_prefix else f"{path_prefix} → {raw_name}"
        item = QTreeWidgetItem([raw_name])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        item.setData(0, self.ROLE_ID, node.id)
        item.setData(0, self.ROLE_URL, node.url)
        item.setData(0, self.ROLE_SECTION, section)
        item.setData(0, self.ROLE_PARAM_KEY, node.param_key)
        item.setData(0, self.ROLE_PARAM_VALUE, node.param_value)
        item.setData(0, self.ROLE_CATEGORY_ID, node.category_id)
        item.setData(0, self.ROLE_CATEGORY_NAME, node.category_name)
        item.setData(0, self.ROLE_PARENT_NAME, immediate_parent)
        item.setData(0, self.ROLE_RAW_NAME, raw_name)
        item.setData(0, self.ROLE_DEPTH, max(depth, 0))
        item.setData(0, self.ROLE_FULL_PATH, full_path)

        child_depth = depth + 1 if depth >= 0 else -1
        for child in node.children:
            item.addChild(
                self._create_option_item(
                    section,
                    child,
                    depth=child_depth if child_depth >= 0 else 1,
                    path_prefix=full_path,
                )
            )
        self._apply_hierarchy_style(item, depth if depth >= 0 else 0)
        return item

    def can_rename_item(self, item: QTreeWidgetItem | None) -> bool:
        return bool(
            item
            and not item.data(0, self.ROLE_IS_PLACEHOLDER)
            and item.flags() & Qt.ItemFlag.ItemIsUserCheckable
            and str(item.data(0, self.ROLE_SECTION) or "") == "Категория"
        )

    def rename_category_item(self, item: QTreeWidgetItem, new_name: str) -> bool:
        """Change only the display/export name, preserving Ozon IDs and URL."""
        name = " ".join(new_name.split())
        if not name or not self.can_rename_item(item):
            return False

        old_name = str(item.data(0, self.ROLE_RAW_NAME) or item.text(0))
        item.setData(0, self.ROLE_RAW_NAME, name)
        item.setData(0, self.ROLE_CATEGORY_NAME, name)

        # Keep direct children's parent link in sync for labels/export.
        for index in range(item.childCount()):
            child = item.child(index)
            if not (child.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            child_parent = str(child.data(0, self.ROLE_PARENT_NAME) or "")
            if child_parent in ("", old_name):
                child.setData(0, self.ROLE_PARENT_NAME, name)
        # Refresh this node and descendants so paths/tooltips stay correct.
        self._refresh_hierarchy_styles(item)
        return True

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
        self._emit_selection_count()

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
        parent_name = str(
            item.data(0, self.ROLE_PARENT_NAME) or self._immediate_parent_name(item) or ""
        )
        cat_id = str(item.data(0, self.ROLE_CATEGORY_ID) or item.data(0, self.ROLE_PARAM_VALUE) or "")
        section = str(item.data(0, self.ROLE_SECTION) or "")
        raw_name = str(item.data(0, self.ROLE_RAW_NAME) or item.text(0))
        full_path = str(item.data(0, self.ROLE_FULL_PATH) or self._category_path_label(item) or "")
        display = raw_name
        if section == "Категория":
            if " → " in full_path:
                display = full_path
            elif parent_name:
                display = f"{parent_name} → {raw_name}"
        targets.append(
            CategoryTarget(
                id=str(option_id),
                name=display,
                # Always bind parse URL to this node's own id — ROLE_URL from Ozon
                # menus often points at a parent listing.
                url=(
                    f"https://www.ozon.ru/category/{cat_id}/"
                    if cat_id.isdigit()
                    else item.data(0, self.ROLE_URL)
                ),
                section=section,
                param_key=str(item.data(0, self.ROLE_PARAM_KEY) or ""),
                param_value=str(item.data(0, self.ROLE_PARAM_VALUE) or "") or cat_id,
                category_id=cat_id,
                category_name=str(item.data(0, self.ROLE_CATEGORY_NAME) or raw_name),
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

    def _ancestor_category_ids(self, item: QTreeWidgetItem) -> set[str]:
        ids: set[str] = set()
        parent = item.parent()
        while parent is not None:
            if parent.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                cid = str(
                    parent.data(0, self.ROLE_CATEGORY_ID)
                    or parent.data(0, self.ROLE_PARAM_VALUE)
                    or ""
                ).strip()
                if cid:
                    ids.add(cid)
            parent = parent.parent()
        return ids

    def selected_targets(self) -> list[CategoryTarget]:
        """Leaf targets for parsing.

        Parent checkbox still marks all children in the UI; for parsing we walk
        down to leaves so each child is processed one-by-one in order.

        If a parent is Checked but has checkable children (e.g. after subcategory
        load with signals blocked), never treat the parent itself as the parse
        target — only explicitly checked leaves/descendants are used.

        Also drop junk nodes that re-list an ancestor category id under a deeper
        branch (common for Ozon accessory menus).
        """
        targets: list[CategoryTarget] = []

        def walk(item: QTreeWidgetItem) -> None:
            if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                for i in range(item.childCount()):
                    walk(item.child(i))
                return

            state = item.checkState(0)
            if state == Qt.CheckState.Unchecked:
                return

            # Any parent with real subcategory checkboxes → walk children only.
            if self._checkable_child_count(item) > 0:
                for i in range(item.childCount()):
                    walk(item.child(i))
                return

            if state == Qt.CheckState.PartiallyChecked:
                return

            cid = str(
                item.data(0, self.ROLE_CATEGORY_ID)
                or item.data(0, self.ROLE_PARAM_VALUE)
                or ""
            ).strip()
            # Parent section re-listed as a fake leaf — never parse it.
            if cid and cid in self._ancestor_category_ids(item):
                return

            self._append_target(item, targets)

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))

        # If both a broad parent id and a deeper leaf id somehow got selected,
        # keep only the deepest ids (drop ids that are ancestors of others).
        if len(targets) <= 1:
            return targets

        kept: list[CategoryTarget] = []
        for target in targets:
            cid = str(target.category_id or target.param_value or "").strip()
            if not cid:
                continue
            # Drop this target if some OTHER selected node is under the same branch
            # and this cid is an ancestor of that other node.
            is_ancestor_of_other = False
            for other in targets:
                other_cid = str(other.category_id or other.param_value or "").strip()
                if not other_cid or other_cid == cid:
                    continue
                other_item = self._find_item_by_param_value(other_cid)
                if other_item is None:
                    continue
                if cid in self._ancestor_category_ids(other_item):
                    is_ancestor_of_other = True
                    break
            if not is_ancestor_of_other:
                kept.append(target)
        return kept or targets

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
        specific_seller: bool = True,
        roots_only: bool = True,
    ):
        super().__init__()
        self.seller_url = seller_url
        self.browser_mode = browser_mode
        self.use_auth = use_auth
        self.specific_seller = specific_seller
        self.roots_only = roots_only
        self._bypass_event: threading.Event | None = None

    def resume_manual_bypass(self) -> None:
        if self._bypass_event:
            self._bypass_event.set()

    def run(self) -> None:
        import threading

        self._bypass_event = threading.Event()

        def on_manual_bypass(incident: str | None) -> bool:
            self.manual_bypass_needed.emit(incident or "")
            self._bypass_event.clear()
            return self._bypass_event.wait(timeout=600)

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
                specific_seller=self.specific_seller,
                prefer_cache=not self.roots_only,
                roots_only=self.roots_only,
            )
            self.finished_ok.emit(categories)
        except Exception as exc:
            self.failed.emit(str(exc))


class LoadSubcategoriesWorker(QThread):
    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)
    subcategories_begin = pyqtSignal(int)
    branch_loaded = pyqtSignal(object)
    manual_bypass_needed = pyqtSignal(str)

    def __init__(
        self,
        seller_url: str,
        categories: list[CategoryTarget],
        browser_mode: BrowserMode = DESKTOP_MODE,
        use_auth: bool = False,
        specific_seller: bool = False,
    ):
        super().__init__()
        self.seller_url = seller_url
        self.categories = categories
        self.browser_mode = browser_mode
        self.use_auth = use_auth
        self.specific_seller = specific_seller
        self._bypass_event: threading.Event | None = None
        self._parser: OzonParser | None = None
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        if self._parser:
            self._parser.stop()

    def resume_manual_bypass(self) -> None:
        if self._bypass_event:
            self._bypass_event.set()

    def run(self) -> None:
        import threading

        self._bypass_event = threading.Event()

        def on_manual_bypass(incident: str | None) -> bool:
            self.manual_bypass_needed.emit(incident or "")
            self._bypass_event.clear()
            return self._bypass_event.wait(timeout=600)

        try:
            parser = OzonParser(
                on_progress=lambda msg: self.progress.emit(msg),
                on_manual_bypass=on_manual_bypass,
            )
            self._parser = parser
            branches: list = []
            self.subcategories_begin.emit(len(self.categories))
            for target in self.categories:
                if self._cancelled:
                    break
                batch = parser.expand_selected_category_subtrees(
                    [target],
                    seller_url=self.seller_url,
                    use_auth=self.use_auth,
                    use_cdp=self.browser_mode == DESKTOP_MODE,
                    browser_mode=self.browser_mode,
                    specific_seller=self.specific_seller,
                    on_branch=lambda node: self.branch_loaded.emit(node),
                )
                branches.extend(batch)
            if not self._cancelled:
                self.finished_ok.emit(branches)
        except Exception as exc:
            if not self._cancelled:
                self.failed.emit(str(exc))
        finally:
            self._parser = None


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

        def on_manual_bypass(incident: str | None) -> bool:
            self.manual_bypass_needed.emit(incident or "")
            self._bypass_event.clear()
            return self._bypass_event.wait(timeout=600)

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
    confirmation_needed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._confirmation_event = threading.Event()
        self._cancelled = False

    def confirm_login(self) -> None:
        self._cancelled = False
        self._confirmation_event.set()

    def cancel_login(self) -> None:
        self._cancelled = True
        self._confirmation_event.set()

    def run(self) -> None:
        self._cancelled = False

        def wait_for_confirmation() -> bool:
            self._confirmation_event.clear()
            self.confirmation_needed.emit()
            signaled = self._confirmation_event.wait(timeout=600)
            return bool(signaled) and not self._cancelled

        ok = login_mobile_via_browser(
            on_progress=lambda message: self.progress.emit(message),
            wait_for_confirmation=wait_for_confirmation,
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
        self.subcategory_worker: LoadSubcategoriesWorker | None = None
        self.mobile_login_worker: MobileLoginWorker | None = None
        self._parsed_products: list = []
        self._parsed_filepath: str = ""
        self._last_export_meta: ExportMeta | None = None
        self._catalog_subcat_total = 0
        self._roots_loaded = False
        self._loaded_category_mode: BrowserMode | None = None
        self._loaded_category_auth: bool | None = None
        self._loaded_category_scope: str | None = None
        self._loaded_parse_mode: ParseMode | None = None
        self._subcat_queue: list[CategoryTarget] = []
        self._subcat_loaded_ids: set[str] = set()
        self._subcat_in_progress_ids: set[str] = set()
        self._theme: ThemeName = load_theme_preference()

        self.setWindowTitle("Ozon Parser — Баллы за отзыв")
        # Keep min width below the narrow-layout breakpoint so stacked mode is reachable.
        self.setMinimumSize(
            max(640, min(760, int(screen.width() * 0.42))),
            max(480, min(560, int(screen.height() * 0.48))),
        )
        self.resize(
            max(980, min(screen.width() - 40, int(screen.width() * 0.92))),
            max(680, min(screen.height() - 60, int(screen.height() * 0.9))),
        )

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        pad = max(10, min(24, int(min(screen.width(), screen.height()) * 0.016)))
        self._layout_pad = pad
        root.setContentsMargins(pad, pad, pad, pad)
        root.setSpacing(pad)

        # --- Шапка: заголовок + тема + статус справа ---
        header = QHBoxLayout()
        title = QLabel("Ozon Parser")
        title.setObjectName("AppTitle")
        header.addWidget(title)
        header.addStretch()

        self.help_btn = QPushButton("Инструкция")
        self.help_btn.setObjectName("SecondaryButton")
        self.help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.help_btn.setMinimumWidth(110)
        self.help_btn.setToolTip("Как пользоваться программой")
        setup_action_button(self.help_btn, min_height=40)
        self.help_btn.clicked.connect(self._show_instructions)
        header.addWidget(self.help_btn)

        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("SecondaryButton")
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.setMinimumWidth(120)
        setup_action_button(self.theme_btn, min_height=40)
        self.theme_btn.clicked.connect(self._toggle_theme)
        self._refresh_theme_button()
        header.addWidget(self.theme_btn)

        self.app_status_label = QLabel(self.STATUS_IDLE)
        self.app_status_label.setObjectName("AppStatus")
        self.app_status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.app_status_label)
        root.addLayout(header)

        # --- Основная область: адаптивные колонки ---
        self._main_box = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._main_box.setSpacing(pad)
        self._responsive_mode = "wide"

        col_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Левая колонка — параметры парсинга (форма со скроллом + кнопки снизу)
        self.params_group = QGroupBox("Параметры парсинга")
        self.params_group.setObjectName("ParamsGroup")
        self.params_group.setSizePolicy(col_policy)
        self.params_group.setMinimumWidth(260)
        params_outer = QVBoxLayout(self.params_group)
        params_outer.setContentsMargins(4, 10, 6, 8)
        params_outer.setSpacing(8)

        params_form = QWidget()
        params_form.setObjectName("ParamsForm")
        params_form.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        params_layout = QVBoxLayout(params_form)
        params_layout.setContentsMargins(4, 4, 10, 4)
        params_layout.setSpacing(12)
        params_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

        self.parse_mode_combo = QComboBox()
        self.parse_mode_combo.addItem(
            "Выбранные категории — весь Ozon (без магазина)",
            PARSE_MODE_GLOBAL_CATEGORIES,
        )
        self.parse_mode_combo.addItem(
            "Все магазины — только выбранные категории",
            PARSE_MODE_ALL_SELLERS_CATEGORIES,
        )
        self.parse_mode_combo.addItem(
            "Все магазины — полный каталог каждого",
            PARSE_MODE_ALL_SELLERS_FULL,
        )
        self.parse_mode_combo.addItem(
            "Конкретный магазин — все товары",
            PARSE_MODE_SELLER_FULL,
        )
        self.parse_mode_combo.addItem(
            "Конкретный магазин — выбранные категории",
            PARSE_MODE_SELLER_CATEGORIES,
        )
        self.parse_mode_combo.setCurrentIndex(0)
        self.parse_mode_combo.currentIndexChanged.connect(self._on_parse_mode_changed)
        params_layout.addWidget(param_field_block("Режим парсинга", self.parse_mode_combo))

        self.seller_label = make_param_label("Ссылка на продавца")
        self.seller_label.setEnabled(False)
        self.seller_input = QLineEdit()
        self.seller_input.clear()
        self.seller_input.setEnabled(False)
        self.seller_input.setPlaceholderText(
            "Не требуется — общий каталог Ozon"
        )
        params_layout.addWidget(param_field_block(self.seller_label, self.seller_input))

        self._mode_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._mode_row.setSpacing(12)
        self.browser_mode_combo = QComboBox()
        self.browser_mode_combo.addItem("Desktop", DESKTOP_MODE)
        self.browser_mode_combo.addItem("Мобильный", MOBILE_MODE)
        self.auth_mode_combo = QComboBox()
        self.auth_mode_combo.addItem("Без авторизации", False)
        self.auth_mode_combo.addItem("С авторизацией", True)
        self._mode_row.addWidget(param_field_block("Режим браузера", self.browser_mode_combo))
        self._mode_row.addWidget(param_field_block("Авторизация", self.auth_mode_combo))
        params_layout.addLayout(self._mode_row)

        self._price_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._price_row.setSpacing(12)
        self.min_price = QDoubleSpinBox()
        self.min_price.setRange(0, 9999999)
        self.min_price.setSuffix(" ₽")
        self.max_price = QDoubleSpinBox()
        self.max_price.setRange(0, 9999999)
        self.max_price.setValue(999999)
        self.max_price.setSuffix(" ₽")
        self._price_row.addWidget(param_field_block("Цена от", self.min_price))
        self._price_row.addWidget(param_field_block("Цена до", self.max_price))
        params_layout.addLayout(self._price_row)

        self.product_filter_combo = QComboBox()
        self.product_filter_combo.addItem("Только с баллами за отзыв", True)
        self.product_filter_combo.addItem("Все товары", False)
        self.product_filter_combo.setCurrentIndex(0)
        self.product_filter_combo.setToolTip(
            "«Только с баллами» — в XLSX попадут товары с акцией за отзыв на карточке.\n"
            "«Все товары» — любые товары из выбранных категорий в диапазоне цен."
        )
        self.product_filter_combo.currentIndexChanged.connect(self._on_product_filter_changed)
        params_layout.addWidget(param_field_block("Какие товары собирать", self.product_filter_combo))

        self.max_products_label = make_param_label("Количество товаров с баллами (на категорию)")
        self.max_products = QSpinBox()
        self.max_products.setRange(1, 100000)
        self.max_products.setValue(100)
        self.max_products.setToolTip(
            "До 10000+ за один запуск: парсер сам делает автопаузы "
            "каждые 25 товаров и каждые 400 (до 25 сек), "
            "чтобы снизить риск fab_/«нет соединения»."
        )
        params_layout.addWidget(param_field_block(self.max_products_label, self.max_products))

        self.chrome_mode_label = WrapLabel("Парсер работает через Chrome")
        self.chrome_mode_label.setObjectName("ChromeModeHint")
        self.chrome_mode_label.setToolTip(
            "Chrome открывается автоматически при загрузке категорий и парсинге"
        )
        params_layout.addWidget(self.chrome_mode_label)

        self.mobile_login_btn = QPushButton("Войти в мобильный Ozon")
        self.mobile_login_btn.clicked.connect(self.launch_mobile_login)
        setup_action_button(self.mobile_login_btn, min_height=44)
        params_layout.addWidget(self.mobile_login_btn)
        self.browser_mode_combo.currentIndexChanged.connect(self._on_session_mode_changed)
        self.auth_mode_combo.currentIndexChanged.connect(self._on_session_mode_changed)
        self._update_session_controls()

        self.detail_status_label = WrapLabel("Загрузите категории, чтобы начать")
        self.detail_status_label.setObjectName("DetailStatus")
        self.detail_status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        params_layout.addWidget(self.detail_status_label)

        self.params_scroll = QScrollArea()
        self.params_scroll.setObjectName("ParamsScroll")
        self.params_scroll.setWidgetResizable(True)
        self.params_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.params_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.params_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.params_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.params_scroll.setWidget(params_form)
        params_outer.addWidget(self.params_scroll, stretch=1)

        params_actions = QWidget()
        params_actions.setObjectName("ParamsActions")
        params_actions_layout = QVBoxLayout(params_actions)
        params_actions_layout.setContentsMargins(4, 4, 10, 2)
        params_actions_layout.setSpacing(8)

        self.start_btn = QPushButton("Запустить парсер")
        self.start_btn.setObjectName("PrimaryButton")
        setup_action_button(self.start_btn, min_height=48)
        self.start_btn.clicked.connect(self.toggle_parsing)
        params_actions_layout.addWidget(self.start_btn)

        self.download_btn = QPushButton("Скачать XLSX (0)")
        self.download_btn.setObjectName("SecondaryButton")
        self.download_btn.setEnabled(False)
        setup_action_button(self.download_btn, min_height=48)
        self.download_btn.clicked.connect(self.download_xlsx)
        params_actions_layout.addWidget(self.download_btn)
        params_outer.addWidget(params_actions)

        self._main_box.addWidget(self.params_group, stretch=22)

        # Центральный блок — каталог
        self.catalog_wrap = QWidget()
        self.catalog_wrap.setSizePolicy(col_policy)
        catalog_layout = QVBoxLayout(self.catalog_wrap)
        catalog_layout.setContentsMargins(0, 0, 0, 0)
        catalog_layout.setSpacing(pad)

        self.catalog_group = QGroupBox("Категории Ozon")
        self.catalog_group.setObjectName("CatalogGroup")
        self.catalog_group.setSizePolicy(col_policy)
        cat_inner = QVBoxLayout(self.catalog_group)
        cat_inner.setSpacing(pad)

        cat_hint = QLabel(
            "Дерево категорий: сверху главные разделы, внутри — вложенные. "
            "Наведите на пункт, чтобы увидеть полный путь. Отметьте нужные и запустите парсер."
        )
        cat_hint.setObjectName("CatalogHint")
        cat_hint.setWordWrap(True)
        cat_inner.addWidget(cat_hint)

        self._cat_actions_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._cat_actions_row.setSpacing(8)

        self.select_all_cat_btn = QPushButton("Выбрать все")
        self.select_all_cat_btn.setObjectName("LinkButton")
        self.select_all_cat_btn.setFlat(True)
        self.select_all_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_cat_btn.clicked.connect(self._select_all_categories)
        self._cat_actions_row.addWidget(self.select_all_cat_btn)

        self.reset_cat_btn = QPushButton("Сбросить")
        self.reset_cat_btn.setObjectName("LinkButton")
        self.reset_cat_btn.setFlat(True)
        self.reset_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_cat_btn.clicked.connect(self._reset_categories)
        self._cat_actions_row.addWidget(self.reset_cat_btn)

        self.selected_cat_count_label = QLabel("Выбрано: 0")
        self.selected_cat_count_label.setObjectName("SelectedCategoryCount")
        self.selected_cat_count_label.setToolTip(
            "Сколько категорий отмечено сейчас — столько уйдёт в парсер"
        )
        self._cat_actions_row.addWidget(self.selected_cat_count_label)

        self.rename_cat_btn = QPushButton("✎")
        self.rename_cat_btn.setObjectName("LinkButton")
        self.rename_cat_btn.setToolTip("Переименовать выбранную категорию")
        self.rename_cat_btn.setAccessibleName("Переименовать выбранную категорию")
        self.rename_cat_btn.setFlat(True)
        self.rename_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rename_cat_btn.setEnabled(False)
        self.rename_cat_btn.clicked.connect(self._rename_selected_category)
        self._cat_actions_row.addWidget(self.rename_cat_btn)
        self._cat_actions_row.addStretch(1)
        cat_inner.addLayout(self._cat_actions_row)

        self.category_tree = FilterTreeWidget()
        self.category_tree.setObjectName("CategoryTree")
        self.category_tree.setSizePolicy(col_policy)
        self.category_tree.setMinimumHeight(160)
        self.category_tree.currentItemChanged.connect(
            self._update_rename_category_button
        )
        self.category_tree.selectionChangedCount.connect(
            self._update_selected_categories_count
        )
        cat_inner.addWidget(self.category_tree, stretch=1)

        self.load_cat_btn = QPushButton("Загрузить категории")
        self.load_cat_btn.clicked.connect(self.load_categories)
        setup_action_button(self.load_cat_btn, min_height=44)
        cat_inner.addWidget(self.load_cat_btn)

        self.load_subcat_btn = QPushButton("Загрузить подкатегории")
        self.load_subcat_btn.setEnabled(False)
        self.load_subcat_btn.setToolTip(
            "Отметьте главные категории, затем загрузите подкатегории. "
            "Для магазина — только категории этого магазина, полная глубина "
            "с «Посмотреть все» на каждом уровне."
        )
        self.load_subcat_btn.clicked.connect(self.load_selected_subcategories)
        setup_action_button(self.load_subcat_btn, min_height=44)
        cat_inner.addWidget(self.load_subcat_btn)

        catalog_layout.addWidget(self.catalog_group, stretch=1)

        self._main_box.addWidget(self.catalog_wrap, stretch=46)

        # Правая колонка — прогресс и лог
        self.progress_group = QGroupBox("Прогресс и лог")
        self.progress_group.setObjectName("ProgressGroup")
        self.progress_group.setSizePolicy(col_policy)
        self.progress_group.setMinimumWidth(200)
        progress_layout = QVBoxLayout(self.progress_group)
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
        self.log_view.setMinimumHeight(80)
        progress_layout.addWidget(self.log_view, stretch=1)

        self._main_box.addWidget(self.progress_group, stretch=32)

        root.addLayout(self._main_box, stretch=1)

        footer = QLabel(f"Файлы сохраняются в: {OUTPUT_DIR}")
        footer.setObjectName("Footer")
        footer.setWordWrap(True)
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(footer)

        self.status_label = self.detail_status_label
        self._apply_styles()
        self._set_app_status(self.STATUS_IDLE, parsing=False)
        self._update_download_button()
        self._on_parse_mode_changed()
        self._update_responsive_layout(force=True)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_responsive_layout()

    def _update_responsive_layout(self, *, force: bool = False) -> None:
        """Switch between side-by-side and stacked columns when the window is narrow."""
        if not hasattr(self, "_main_box"):
            return
        width = self.width()
        if width <= 0:
            return

        if width < 900:
            mode = "narrow"
        elif width < 1180:
            mode = "medium"
        else:
            mode = "wide"

        if not force and mode == getattr(self, "_responsive_mode", None):
            if mode == "narrow":
                params_h = max(360, min(560, int(self.height() * 0.55)))
                if self.params_group.height() != params_h:
                    self.params_group.setFixedHeight(params_h)
            self._update_nested_field_rows()
            return

        self._responsive_mode = mode
        if mode == "narrow":
            self._main_box.setDirection(QBoxLayout.Direction.TopToBottom)
            self._main_box.setStretch(0, 0)
            self._main_box.setStretch(1, 3)
            self._main_box.setStretch(2, 2)
            # Fixed height so pinned buttons leave a usable scroll viewport.
            params_h = max(360, min(560, int(self.height() * 0.55)))
            self.params_group.setMinimumWidth(0)
            self.params_group.setFixedHeight(params_h)
            self.progress_group.setMinimumWidth(0)
            self.category_tree.setMinimumHeight(120)
            self.log_view.setMinimumHeight(90)
            setup_action_button(self.start_btn, min_height=40)
            setup_action_button(self.download_btn, min_height=40)
        elif mode == "medium":
            self._main_box.setDirection(QBoxLayout.Direction.LeftToRight)
            self._main_box.setStretch(0, 28)
            self._main_box.setStretch(1, 42)
            self._main_box.setStretch(2, 30)
            self.params_group.setMinimumHeight(0)
            self.params_group.setMaximumHeight(16777215)
            self.params_group.setMinimumWidth(240)
            self.progress_group.setMinimumWidth(180)
            self.category_tree.setMinimumHeight(160)
            self.log_view.setMinimumHeight(80)
            setup_action_button(self.start_btn, min_height=48)
            setup_action_button(self.download_btn, min_height=48)
        else:
            self._main_box.setDirection(QBoxLayout.Direction.LeftToRight)
            self._main_box.setStretch(0, 22)
            self._main_box.setStretch(1, 46)
            self._main_box.setStretch(2, 32)
            self.params_group.setMinimumHeight(0)
            self.params_group.setMaximumHeight(16777215)
            self.params_group.setMinimumWidth(260)
            self.progress_group.setMinimumWidth(200)
            self.category_tree.setMinimumHeight(160)
            self.log_view.setMinimumHeight(80)
            setup_action_button(self.start_btn, min_height=48)
            setup_action_button(self.download_btn, min_height=48)

        self._update_nested_field_rows()

    def _update_nested_field_rows(self) -> None:
        """Stack browser/auth and price rows when the params column is narrow."""
        if not hasattr(self, "_mode_row") or not hasattr(self, "params_group"):
            return
        col_w = self.params_group.width()
        if col_w <= 0:
            col_w = self.width()
        stack = col_w > 0 and col_w < 360
        direction = (
            QBoxLayout.Direction.TopToBottom
            if stack
            else QBoxLayout.Direction.LeftToRight
        )
        if self._mode_row.direction() != direction:
            self._mode_row.setDirection(direction)
        if self._price_row.direction() != direction:
            self._price_row.setDirection(direction)

    def _set_app_status(self, text: str, *, parsing: bool = False) -> None:
        self.app_status_label.setText(text)
        self.app_status_label.setProperty("parsing", parsing)
        self.app_status_label.style().unpolish(self.app_status_label)
        self.app_status_label.style().polish(self.app_status_label)

    def _selected_browser_mode(self) -> BrowserMode:
        return self.browser_mode_combo.currentData()

    def _selected_use_auth(self) -> bool:
        return bool(self.auth_mode_combo.currentData())

    def _selected_parse_mode(self) -> ParseMode:
        return self.parse_mode_combo.currentData()

    def _selected_bonus_only(self) -> bool:
        return bool(self.product_filter_combo.currentData())

    def _on_product_filter_changed(self, _index: int = 0) -> None:
        if self._selected_bonus_only():
            self.max_products_label.setText("Количество товаров с баллами (на категорию)")
        else:
            self.max_products_label.setText("Количество товаров (на категорию)")

    def _parse_mode_requires_seller_url(self, mode: ParseMode | None = None) -> bool:
        mode = mode or self._selected_parse_mode()
        return mode in SELLER_PARSE_MODES

    def _parse_mode_requires_categories(self, mode: ParseMode | None = None) -> bool:
        mode = mode or self._selected_parse_mode()
        return mode in CATEGORY_REQUIRED_PARSE_MODES

    def _category_tree_scope(self) -> str:
        return "seller" if self._parse_mode_requires_seller_url() else "global"

    def _on_parse_mode_changed(self, _index: int = 0) -> None:
        needs_seller = self._parse_mode_requires_seller_url()
        self.seller_label.setEnabled(needs_seller)
        self.seller_input.setEnabled(needs_seller)
        self.seller_input.setPlaceholderText(
            "https://www.ozon.ru/seller/..."
            if needs_seller
            else "Не требуется — общий каталог Ozon"
        )
        self._update_catalog_title()
        self._on_session_mode_changed()

    def _update_catalog_title(self) -> None:
        if not hasattr(self, "catalog_group"):
            return
        if self._parse_mode_requires_seller_url():
            self.catalog_group.setTitle("Категории магазина")
        else:
            self.catalog_group.setTitle("Категории Ozon")

    def _update_session_controls(self) -> None:
        mobile = self._selected_browser_mode() == MOBILE_MODE
        use_auth = self._selected_use_auth()
        self.chrome_mode_label.setVisible(not mobile)
        self.mobile_login_btn.setVisible(mobile and use_auth)
        parsing = bool(self.parse_worker and self.parse_worker.isRunning())
        self.mobile_login_btn.setEnabled(
            mobile
            and use_auth
            and not parsing
            and not (
                self.mobile_login_worker
                and self.mobile_login_worker.isRunning()
            )
        )

    def _set_parse_settings_enabled(self, enabled: bool) -> None:
        """Lock/unlock settings that would invalidate an active run."""
        self.parse_mode_combo.setEnabled(enabled)
        self.browser_mode_combo.setEnabled(enabled)
        self.auth_mode_combo.setEnabled(enabled)
        if enabled:
            self.seller_label.setEnabled(self._parse_mode_requires_seller_url())
            self.seller_input.setEnabled(self._parse_mode_requires_seller_url())
        else:
            self.seller_label.setEnabled(False)
            self.seller_input.setEnabled(False)
        self.min_price.setEnabled(enabled)
        self.max_price.setEnabled(enabled)
        self.product_filter_combo.setEnabled(enabled)
        self.max_products.setEnabled(enabled)
        self._update_session_controls()

    def _set_catalog_load_enabled(
        self,
        *,
        load_cat: bool,
        load_subcat: bool,
        allow_rename: bool | None = None,
    ) -> None:
        self.load_cat_btn.setEnabled(load_cat)
        self.load_subcat_btn.setEnabled(load_subcat)
        if allow_rename is None:
            allow_rename = load_cat
        if allow_rename:
            self._update_rename_category_button(self.category_tree.currentItem())
        else:
            self.rename_cat_btn.setEnabled(False)

    def _restore_idle_controls(self) -> None:
        """Re-enable settings after parse/load finishes or fails."""
        self._set_parse_settings_enabled(True)
        self._set_catalog_load_enabled(
            load_cat=True,
            load_subcat=self._roots_loaded,
        )
        self.start_btn.setEnabled(True)
        self._update_session_controls()

    def _on_session_mode_changed(self) -> None:
        self._update_session_controls()
        if self.parse_worker and self.parse_worker.isRunning():
            return
        if (
            self._loaded_category_mode is not None
            and (
                self._loaded_category_mode != self._selected_browser_mode()
                or self._loaded_category_auth != self._selected_use_auth()
                or self._loaded_category_scope != self._category_tree_scope()
            )
        ):
            self.category_tree.clear()
            self._update_selected_categories_count(0)
            self._loaded_category_mode = None
            self._loaded_category_auth = None
            self._loaded_category_scope = None
            self._loaded_parse_mode = None
            self._roots_loaded = False
            self._subcat_queue.clear()
            self._subcat_loaded_ids.clear()
            self._subcat_in_progress_ids.clear()
            self.load_subcat_btn.setEnabled(False)
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
            try:
                path = str(
                    export_products(self._parsed_products, self._last_export_meta).resolve()
                )
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Не удалось сохранить XLSX",
                    f"Файл результата недоступен, повторная выгрузка не удалась:\n{exc}",
                )
                return
            self._parsed_filepath = path
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        QMessageBox.warning(
            self,
            "Файл недоступен",
            "Результаты есть в памяти, но файл XLSX не найден и метаданные "
            "экспорта отсутствуют. Запустите парсер ещё раз.",
        )

    def _apply_styles(self) -> None:
        fs = self.app_font.pointSize()
        btn_fs = max(14, min(18, fs - 8))
        cb_fs = max(13, min(17, fs - 8))
        btn_h = max(44, btn_fs * 2 + 8)
        input_fs = max(14, fs - 6)
        tree_fs = max(14, fs - 4)
        input_h = max(40, input_fs + 18)
        theme = normalize_theme(self._theme)
        self.setStyleSheet(
            build_stylesheet(
                theme,
                fs=fs,
                btn_fs=btn_fs,
                cb_fs=cb_fs,
                btn_h=btn_h,
                input_fs=input_fs,
                tree_fs=tree_fs,
                input_h=input_h,
            )
        )
        if hasattr(self, "category_tree"):
            colors = THEMES[theme]
            self.category_tree.set_placeholder_color(colors.placeholder)
            self.category_tree.set_hierarchy_colors(colors.text_muted, colors.text_soft)
        if hasattr(self, "max_products"):
            for widget in (
                self.parse_mode_combo,
                self.seller_input,
                self.browser_mode_combo,
                self.auth_mode_combo,
                self.min_price,
                self.max_price,
                self.product_filter_combo,
                self.max_products,
            ):
                setup_param_input(widget, min_height=input_h)
            for label in self.findChildren(WrapLabel):
                label._sync_wrap_height()

    def _refresh_theme_button(self) -> None:
        if self._theme == "light":
            self.theme_btn.setText("Тёмная тема")
            self.theme_btn.setToolTip("Переключить на тёмную тему")
        else:
            self.theme_btn.setText("Светлая тема")
            self.theme_btn.setToolTip("Переключить на светлую тему")

    def _toggle_theme(self) -> None:
        self._theme = "light" if self._theme == "dark" else "dark"
        save_theme_preference(self._theme)
        self._refresh_theme_button()
        self._apply_styles()

    def _show_instructions(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(INSTRUCTIONS_TITLE)
        dialog.setModal(True)
        dialog.resize(640, 560)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel(INSTRUCTIONS_TITLE)
        title.setObjectName("AppTitle")
        layout.addWidget(title)

        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(build_user_instructions())
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(text, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.exec()

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
        self._append_log(message)
        # Keep the params-panel status readable; full text is in the log.
        short = message if len(message) <= 160 else message[:157] + "..."
        self.status_label.setText(short)

    def _reset_parse_display(self) -> None:
        self._reset_progress_panel()

    def _on_parse_status(self, status: ParseStatus) -> None:
        if status.total_count:
            self._set_progress_fraction(status.current_index, status.total_count)

    def launch_mobile_login(self) -> None:
        if self.mobile_login_worker and self.mobile_login_worker.isRunning():
            return
        self.mobile_login_btn.setEnabled(False)
        message = "Открываем отдельное окно мобильной авторизации..."
        self.status_label.setText(message)
        self._append_log(message)
        self.mobile_login_worker = MobileLoginWorker()
        self.mobile_login_worker.progress.connect(self._on_worker_progress)
        self.mobile_login_worker.confirmation_needed.connect(
            self._confirm_mobile_login
        )
        self.mobile_login_worker.completed.connect(self._on_mobile_login_completed)
        self.mobile_login_worker.start()

    def _confirm_mobile_login(self) -> None:
        reply = QMessageBox.question(
            self,
            "Подтвердите мобильный вход",
            "В открытом окне Ozon выполните обычный вход.\n\n"
            "Если Ozon показывает CAPTCHA или блокировку, пройдите проверку вручную "
            "и дождитесь открытия сайта. Не закрывайте окно браузера.\n\n"
            "После появления личного кабинета нажмите «Да» — приложение проверит сессию.\n"
            "Если вход ещё не завершён, можно нажать «Да» позже ещё раз.\n"
            "«Нет» — отменить вход без ошибки.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if not self.mobile_login_worker:
            return
        if reply == QMessageBox.StandardButton.Yes:
            self.mobile_login_worker.confirm_login()
        else:
            self.mobile_login_worker.cancel_login()

    def _on_mobile_login_completed(self, ok: bool) -> None:
        self._update_session_controls()
        if ok:
            message = "Мобильный профиль сохранён и готов для авторизованного режима"
            self.status_label.setText(message)
            self._append_log(message)
            QMessageBox.information(self, "Вход выполнен", message)
            return

        cancelled = bool(
            self.mobile_login_worker and self.mobile_login_worker._cancelled
        )
        if cancelled:
            message = "Мобильный вход отменён"
            self.status_label.setText(message)
            self._append_log(message)
            return

        message = (
            "Сессия не сохранена: окно Ozon было закрыто или вход не завершён. "
            "Нажмите «Войти в мобильный Ozon» ещё раз после входа в личный кабинет."
        )
        self.status_label.setText(message)
        self._append_log(message)
        QMessageBox.information(self, "Вход не завершён", message)

    def _show_manual_bypass_dialog(self, incident: str) -> None:
        msg = (
            "Ozon проверяет браузер или временно ограничил доступ.\n\n"
        )
        if incident:
            msg += f"Инцидент: {incident}\n\n"
        msg += (
            "В окне Chrome:\n"
            "1. Если видите «Antibot Challenge» — дождитесь завершения проверки\n"
            "2. Не обновляйте страницу многократно\n"
            "3. При «Инциденте» подождите 15–30 минут, затем обновите один раз\n"
            "4. Нажмите OK только после нормальной загрузки Ozon\n\n"
            "Если доступ не восстановился, парсер остановится без новых запросов."
        )
        QMessageBox.information(self, "Требуется ваше действие", msg)

    def _on_category_manual_bypass(self, incident: str) -> None:
        self._show_manual_bypass_dialog(incident)
        if self.category_worker:
            self.category_worker.resume_manual_bypass()
        if self.subcategory_worker:
            self.subcategory_worker.resume_manual_bypass()

    def _on_parse_manual_bypass(self, incident: str) -> None:
        self._show_manual_bypass_dialog(incident)
        if self.parse_worker:
            self.parse_worker.resume_manual_bypass()

    def _select_all_categories(self) -> None:
        self.category_tree.select_all_items()

    def _reset_categories(self) -> None:
        self.category_tree.reset_selection()

    def _update_selected_categories_count(self, count: int | None = None) -> None:
        if count is None:
            count = self.category_tree.selected_category_count()
        self.selected_cat_count_label.setText(f"Выбрано: {count}")

    def _update_rename_category_button(self, current=None, _previous=None) -> None:
        can_rename = (
            self.load_cat_btn.isEnabled()
            and self.category_tree.can_rename_item(current)
        )
        self.rename_cat_btn.setEnabled(can_rename)

    def _rename_selected_category(self) -> None:
        item = self.category_tree.currentItem()
        if not self.category_tree.can_rename_item(item):
            QMessageBox.information(
                self,
                "Выберите категорию",
                "Выберите собранную категорию или подкатегорию в дереве.",
            )
            return

        current_name = str(
            item.data(0, self.category_tree.ROLE_RAW_NAME) or item.text(0)
        )
        new_name, accepted = QInputDialog.getText(
            self,
            "Изменить название категории",
            "Новое название:",
            QLineEdit.EchoMode.Normal,
            current_name,
        )
        if not accepted:
            return
        if not new_name.strip():
            QMessageBox.warning(
                self,
                "Название не изменено",
                "Название категории не может быть пустым.",
            )
            return
        if self.category_tree.rename_category_item(item, new_name):
            self._append_log(
                f"Категория переименована: «{current_name}» → «{item.text(0)}»"
            )

    def _root_target_id(self, target: CategoryTarget) -> str:
        return str(target.param_value or target.category_id or target.id)

    def load_categories(self) -> None:
        if self.parse_worker and self.parse_worker.isRunning():
            QMessageBox.information(
                self,
                "Парсинг выполняется",
                "Дождитесь окончания парсинга или остановите его.",
            )
            return
        scope = self._category_tree_scope()
        needs_seller = self._parse_mode_requires_seller_url()
        url = self.seller_input.text().strip() if needs_seller else ""
        if needs_seller and not url:
            QMessageBox.warning(
                self,
                "Ссылка не указана",
                "Для режима конкретного магазина укажите ссылку на продавца.",
            )
            return
        if not self._validate_mobile_auth():
            return
        if self.subcategory_worker and self.subcategory_worker.isRunning():
            QMessageBox.information(
                self,
                "Загрузка идёт",
                "Дождитесь окончания загрузки подкатегорий.",
            )
            return
        browser_mode = self._selected_browser_mode()
        use_auth = self._selected_use_auth()
        self._catalog_subcat_total = 0
        self._roots_loaded = False
        self._set_parse_settings_enabled(False)
        self._set_catalog_load_enabled(load_cat=False, load_subcat=False, allow_rename=False)
        self.start_btn.setEnabled(False)
        self._subcat_queue.clear()
        self._subcat_loaded_ids.clear()
        self._subcat_in_progress_ids.clear()
        self.status_label.setText("Загрузка главных категорий...")
        self._append_log(
            "Загрузка главных категорий магазина..."
            if scope == "seller"
            else "Загрузка главных категорий общего каталога Ozon..."
        )
        self.category_tree.begin_incremental_load()
        self.category_worker = LoadCategoriesWorker(
            url,
            browser_mode=browser_mode,
            use_auth=use_auth,
            specific_seller=(scope == "seller"),
            roots_only=True,
        )
        self._loaded_category_mode = browser_mode
        self._loaded_category_auth = use_auth
        self._loaded_category_scope = scope
        self._loaded_parse_mode = self._selected_parse_mode()
        self.category_worker.progress.connect(self._on_worker_progress)
        self.category_worker.roots_loaded.connect(self._on_category_roots_loaded)
        self.category_worker.manual_bypass_needed.connect(self._on_category_manual_bypass)
        self.category_worker.finished_ok.connect(self._on_root_categories_loaded)
        self.category_worker.failed.connect(self._on_categories_failed)
        self.category_worker.start()

    def load_selected_subcategories(self) -> None:
        if self.parse_worker and self.parse_worker.isRunning():
            QMessageBox.information(
                self,
                "Парсинг выполняется",
                "Дождитесь окончания парсинга или остановите его.",
            )
            return
        roots = self.category_tree.selected_root_categories()
        if not roots:
            QMessageBox.warning(
                self,
                "Категории не выбраны",
                "Отметьте галочками главные категории, для которых нужны подкатегории.",
            )
            return
        self._start_subcategory_worker(roots)

    def _start_subcategory_worker(
        self,
        roots: list[CategoryTarget],
    ) -> None:
        if not roots:
            return
        if not self._validate_mobile_auth():
            return
        if self.category_worker and self.category_worker.isRunning():
            QMessageBox.information(
                self,
                "Загрузка идёт",
                "Дождитесь окончания загрузки главных категорий.",
            )
            return
        if self.subcategory_worker and self.subcategory_worker.isRunning():
            QMessageBox.information(
                self,
                "Загрузка идёт",
                "Дождитесь окончания текущей загрузки подкатегорий.",
            )
            return
        scope = self._category_tree_scope()
        needs_seller = self._parse_mode_requires_seller_url()
        url = self.seller_input.text().strip() if needs_seller else ""
        if needs_seller and not url:
            QMessageBox.warning(
                self,
                "Ссылка не указана",
                "Для режима конкретного магазина укажите ссылку на продавца.",
            )
            return
        browser_mode = self._selected_browser_mode()
        use_auth = self._selected_use_auth()
        self._set_parse_settings_enabled(False)
        self._set_catalog_load_enabled(load_cat=False, load_subcat=False, allow_rename=False)
        for target in roots:
            self._subcat_in_progress_ids.add(self._root_target_id(target))
        names = ", ".join((t.name or t.id) for t in roots[:5])
        if len(roots) > 5:
            names += f" и ещё {len(roots) - 5}"
        self.status_label.setText(f"Загрузка подкатегорий: {len(roots)} раздел(ов)...")
        self._append_log(f"Загрузка подкатегорий только для: {names}")
        self.subcategory_worker = LoadSubcategoriesWorker(
            url,
            roots,
            browser_mode=browser_mode,
            use_auth=use_auth,
            specific_seller=(scope == "seller"),
        )
        self.subcategory_worker.progress.connect(self._on_worker_progress)
        self.subcategory_worker.subcategories_begin.connect(self._on_subcategories_begin)
        self.subcategory_worker.branch_loaded.connect(self._on_category_branch_loaded)
        self.subcategory_worker.manual_bypass_needed.connect(self._on_category_manual_bypass)
        self.subcategory_worker.finished_ok.connect(self._on_subcategories_loaded)
        self.subcategory_worker.failed.connect(self._on_subcategories_failed)
        self.subcategory_worker.start()

    def _count_subcategories(self, nodes: list) -> int:
        def count_descendants(node) -> int:
            total = 0
            for child in node.children:
                total += 1 + count_descendants(child)
            return total

        return sum(count_descendants(node) for node in nodes)

    def _on_category_roots_loaded(self, roots: list) -> None:
        self.category_tree.begin_incremental_load()
        self.category_tree.set_initial_roots(roots, pending_subcategories=True)
        self.status_label.setText(
            f"Главные категории загружены ({len(roots)}). "
            "Отметьте нужные и нажмите «Загрузить подкатегории»."
        )
        self._append_log(f"Главные категории: {len(roots)}")

    def _on_root_categories_loaded(self, categories: list) -> None:
        if not self.category_tree.topLevelItemCount() and categories:
            self.category_tree.set_initial_roots(categories, pending_subcategories=True)
        self._roots_loaded = True
        self._restore_idle_controls()
        self.status_label.setText(
            f"Главных категорий: {len(categories)}. "
            "Отметьте разделы → «Загрузить подкатегории»."
        )
        self._append_log(
            "Шаг 1 готов. Отметьте нужные категории галочками, затем нажмите "
            "«Загрузить подкатегории». Парсинг можно запускать после загрузки нужных веток."
        )

    def _on_subcategories_begin(self, total: int) -> None:
        self._catalog_subcat_total = total
        self._catalog_subcat_done = 0
        self._append_log(f"Загрузка подкатегорий для {total} выбранных разделов...")

    def _on_category_branch_loaded(self, node) -> None:
        self.category_tree.update_category_branch(node)
        name = str(getattr(node, "name", "") or "")
        child_n = len(getattr(node, "children", []) or [])
        total = self._catalog_subcat_total or 0
        if total:
            self.status_label.setText(
                f"Подкатегории: сбор «{name}» ({child_n} прямых)…"
            )
        else:
            self.status_label.setText(f"Подкатегории: «{name}» ({child_n})")

    def _on_subcategories_loaded(self, branches: list) -> None:
        self.category_tree.finalize_catalog_load(branches)
        self.category_tree.expandToDepth(2)
        for node in branches:
            root_id = str(getattr(node, "param_value", "") or getattr(node, "category_id", "") or "")
            if root_id:
                self._subcat_loaded_ids.add(root_id)
                self._subcat_in_progress_ids.discard(root_id)
        self._restore_idle_controls()
        sub_count = self._count_subcategories(branches)
        self.status_label.setText(
            f"Подкатегории загружены для {len(branches)} раздел(ов), "
            f"внутри узлов: {sub_count}. Отметьте нужные галочками и запускайте парсер."
        )
        self._append_log(
            f"Подкатегории готовы: {len(branches)} веток, {sub_count} узлов. "
            "Галочки сброшены — отметьте конкретные категории для парсинга."
        )
        self._update_selected_categories_count(
            len(self.category_tree.selected_leaf_categories())
        )

    def _on_subcategories_failed(self, error: str) -> None:
        self._subcat_in_progress_ids.clear()
        self._restore_idle_controls()
        self.status_label.setText(f"Ошибка загрузки подкатегорий: {error}")
        hint = (
            "Ozon заблокировал доступ (это не ошибка интернета).\n\n"
            "Подождите 15–30 минут без F5, затем снова нажмите "
            "«Загрузить подкатегории» для выбранных разделов."
        )
        if "нет соединения" in error.lower() or "заблокировал" in error.lower():
            QMessageBox.warning(self, "Блокировка Ozon", hint)
        else:
            QMessageBox.warning(self, "Ошибка", error)

    def _on_categories_failed(self, error: str) -> None:
        self._loaded_category_mode = None
        self._loaded_category_auth = None
        self._loaded_category_scope = None
        self._loaded_parse_mode = None
        self._roots_loaded = False
        self._restore_idle_controls()
        self.status_label.setText(f"Ошибка загрузки категорий: {error}")
        hint = (
            "Ozon заблокировал доступ (это не ошибка интернета).\n\n"
            "1. Chrome откроется сам при «Загрузить категории»\n"
            "2. Если видите fab_/«Похоже, нет соединения» — подождите 15–30 минут\n"
            "3. Обновите страницу один раз только после ожидания\n"
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
        parse_mode = self._selected_parse_mode()
        needs_seller = self._parse_mode_requires_seller_url(parse_mode)
        needs_categories = self._parse_mode_requires_categories(parse_mode)
        url = self.seller_input.text().strip() if needs_seller else ""
        if needs_seller and not url:
            QMessageBox.warning(
                self,
                "Ссылка не указана",
                "Укажите ссылку на продавца для выбранного режима парсинга.",
            )
            return
        if not self._validate_mobile_auth():
            return
        if self.subcategory_worker and self.subcategory_worker.isRunning():
            self._append_log(
                "Останавливаем догрузку подкатегорий — запускаем парсинг выбранного."
            )
            self.subcategory_worker.cancel()
        browser_mode = self._selected_browser_mode()
        use_auth = self._selected_use_auth()
        if needs_categories and (
            self._loaded_category_mode != browser_mode
            or self._loaded_category_auth != use_auth
            or self._loaded_category_scope != self._category_tree_scope()
        ):
            QMessageBox.warning(
                self,
                "Категории устарели",
                "Загрузите категории заново для выбранного режима парсинга.",
            )
            return

        categories = self.category_tree.selected_leaf_categories() if needs_categories else []
        if needs_categories and not categories:
            QMessageBox.warning(
                self,
                "Ничего не выбрано",
                "Отметьте категории или подкатегории для парсинга.",
            )
            return

        if needs_categories:
            unresolved = [
                c for c in categories
                if not extract_ozon_category_id(c.category_id, c.param_value, c.id, c.url)
            ]
            if unresolved:
                names = ", ".join(
                    (c.name or c.category_name or c.id) for c in unresolved[:5]
                )
                QMessageBox.warning(
                    self,
                    "Категории без ID",
                    "Не удалось определить ID у отмеченных категорий:\n"
                    f"{names}\n\n"
                    "Сбросьте галочки, загрузите подкатегории ещё раз и отметьте "
                    "конкретные пункты в дереве.",
                )
                return

        if parse_mode == PARSE_MODE_GLOBAL_CATEGORIES and len(categories) >= 50:
            goal = self.max_products.value()
            self._append_log(
                f"Выбрано категорий: {len(categories)}. Цель: {goal} товаров. "
                "Один запуск идёт волнами с автопаузами (без ручных перезапусков)."
            )
            answer = QMessageBox.question(
                self,
                "Большой выбор категорий",
                (
                    f"Выбрано {len(categories)} категорий общего каталога.\n"
                    f"Цель: {goal} товаров за один запуск.\n\n"
                    "Парсер будет работать непрерывно с автопаузами:\n"
                    "• каждые 25 товаров (до 22 сек)\n"
                    "• каждые 400 товаров (до 25 сек)\n"
                    "• пауза волны категорий (до 25 сек)\n"
                    "• ожидание fab_ без F5 (до 90 сек)\n\n"
                    "Это может занять много часов. Не обновляйте Chrome вручную.\n\n"
                    "Продолжить?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        elif self.max_products.value() >= 1000:
            goal = self.max_products.value()
            answer = QMessageBox.question(
                self,
                "Большой объём товаров",
                (
                    f"Цель: {goal} товаров за один запуск.\n\n"
                    "Парсер автоматически делает защитные паузы и сохраняет "
                    "checkpoint. Запуск может идти несколько часов.\n\n"
                    "Продолжить?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        if (
            self.min_price.value() > 0
            and self.max_price.value() > 0
            and self.min_price.value() > self.max_price.value()
        ):
            QMessageBox.warning(
                self,
                "Неверный диапазон цен",
                "Значение «Цена от» не может быть больше «Цена до».",
            )
            return

        min_p = self.min_price.value() if self.min_price.value() > 0 else None
        max_p = self.max_price.value() if self.max_price.value() < 999999 else None

        settings = ParseSettings(
            seller_url=url,
            categories=categories or None,
            min_price=min_p,
            max_price=max_p,
            max_products=self.max_products.value(),
            use_auth=use_auth,
            import_browser_session=browser_mode == DESKTOP_MODE,
            use_cdp=browser_mode == DESKTOP_MODE,
            browser_mode=browser_mode,
            parse_mode=parse_mode,
            bonus_only=self._selected_bonus_only(),
        )

        mode_labels = {
            PARSE_MODE_GLOBAL_CATEGORIES: "Выбранные категории (весь Ozon)",
            PARSE_MODE_ALL_SELLERS_CATEGORIES: "Все магазины × выбранные категории",
            PARSE_MODE_ALL_SELLERS_FULL: "Все магазины × полный каталог",
            PARSE_MODE_SELLER_FULL: "Магазин × все товары",
            PARSE_MODE_SELLER_CATEGORIES: "Магазин × выбранные категории",
        }
        export_meta = ExportMeta(
            seller_url=url if needs_seller else "Все магазины Ozon",
            min_price=min_p,
            max_price=max_p,
            max_products=self.max_products.value(),
            categories=self._selected_targets_label(categories) if categories else "—",
        )

        resume_info = describe_checkpoint(settings)
        if resume_info:
            answer = QMessageBox.question(
                self,
                "Продолжить сохранённый сбор?",
                (
                    f"Найден сохранённый прогресс: {resume_info}.\n\n"
                    "Да — продолжить с того же места.\n"
                    "Нет — начать заново (прогресс будет очищен)."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.No:
                clear_checkpoint()
                self._append_log("Старый прогресс очищен — старт с нуля")
            else:
                self._append_log(f"Продолжение: {resume_info}")
        elif CHECKPOINT_PATH.exists():
            self._append_log(
                "Старый файл прогресса не подходит к текущим параметрам и будет игнорирован"
            )

        self._reset_parse_display()
        self.log_view.clear()
        self._append_log(
            "Запуск безопасной сессии: короткие партии, сохранение прогресса, "
            "без открытия карточек товаров."
        )
        if categories:
            preview = ", ".join(
                (c.name or c.category_name or c.id) for c in categories[:8]
            )
            more = f" … (+{len(categories) - 8})" if len(categories) > 8 else ""
            self._append_log(f"К парсингу: {len(categories)} кат. — {preview}{more}")
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
        self.download_btn.setVisible(True)
        self._set_parse_settings_enabled(False)
        self._set_catalog_load_enabled(load_cat=False, load_subcat=False, allow_rename=False)

    def _selected_targets_label(self, categories: list[CategoryTarget]) -> str:
        if not categories:
            return "—"
        labels: list[str] = []
        for target in categories:
            if " → " in (target.name or ""):
                labels.append(target.name)
            elif target.parent_name:
                labels.append(f"{target.parent_name} → {target.name}")
            else:
                labels.append(target.name)
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
        self._restore_idle_controls()
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
        has_checkpoint = CHECKPOINT_PATH.exists()
        status = (
            f"Собрано: {len(products)} товаров за {duration_text}. "
            + (
                "Прогресс сохранён — можно продолжить тем же запуском позже."
                if has_checkpoint
                else "Сбор завершён. Нажмите «Скачать XLSX»."
            )
        )
        self.status_label.setText(status)
        self._append_log(status)
        if stats and stats.section_timings:
            for timing in stats.section_timings:
                self._append_log(timing.summary_line())
        message = (
            f"Сохранено {len(products)} товаров\n"
            f"Время: {duration_text}\n\n{abs_path}"
        )
        if has_checkpoint:
            message += (
                "\n\nСбор ещё не дошёл до цели. Checkpoint сохранён — "
                "запустите парсер снова с теми же категориями после паузы."
            )
        QMessageBox.information(
            self,
            "Готово" if not has_checkpoint else "Прогресс сохранён",
            message,
        )

    def _on_parse_failed(self, error: str) -> None:
        self._set_app_status(self.STATUS_IDLE, parsing=False)
        self.start_btn.setText("Запустить парсер")
        self._restore_idle_controls()
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
