"""Main application window.

Composes a sidebar (navigation), a top bar (language / theme selectors and
global actions) and a stacked content area. Built-in pages are registered
first, followed by any pages contributed by enabled plugins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from anycubic_toolkit import __app_name__, __homepage__
from anycubic_toolkit.core.assets import app_icon_path
from anycubic_toolkit.core.updater import UpdateInfo, check_for_updates
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules import (
    AboutPage,
    AppContext,
    BambuImportPage,
    ConnectPage,
    DashboardPage,
    ErrorLookupPage,
    FirmwareCenterPage,
    HealthPage,
    HistoryPage,
    LogAnalyzerPage,
    ResourcesPage,
    ModulePage,
    PrinterInfoPage,
    RinkhalsPage,
    SettingsPage,
    SupportReportPage,
)


@dataclass
class NavEntry:
    """One sidebar navigation entry."""

    key: str            # translation key for the label
    icon: str           # unicode glyph
    page: QWidget
    button: QPushButton
    is_plugin: bool = False
    fixed_title: str = ""  # non-empty for plugin pages (already localized)


class MainWindow(QWidget):
    """Top-level window shown after the splash screen."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(__app_name__)
        icon_path = app_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1080, 720)
        self.setMinimumSize(880, 560)

        self._entries: list[NavEntry] = []
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        content.addWidget(self._build_topbar())
        self.stack = QStackedWidget()
        content.addWidget(self.stack, 1)
        content_wrap = QWidget()
        content_wrap.setLayout(content)
        root.addWidget(content_wrap, 1)

        self._register_pages()
        self._register_plugin_pages()

        self.ctx.translator.language_changed.connect(lambda _c: self.retranslate())
        self.retranslate()
        if self._entries:
            self._entries[0].button.setChecked(True)
            self._activate(0)

    # --------------------------------------------------------------- layout

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(4)

        self.app_title = QLabel(__app_name__)
        self.app_title.setObjectName("AppTitle")
        self.app_subtitle = QLabel()
        self.app_subtitle.setObjectName("AppSubtitle")
        self.app_subtitle.setWordWrap(True)
        layout.addWidget(self.app_title)
        layout.addWidget(self.app_subtitle)
        layout.addSpacing(14)

        self._nav_layout = layout
        self._plugins_header: QLabel | None = None
        layout.addStretch(1)
        return sidebar

    def _build_topbar(self) -> QWidget:
        topbar = QWidget()
        topbar.setObjectName("TopBar")
        topbar.setFixedHeight(56)
        layout = QHBoxLayout(topbar)
        layout.setContentsMargins(20, 8, 20, 8)
        layout.setSpacing(10)

        self.page_heading = QLabel()
        self.page_heading.setObjectName("CardTitle")
        layout.addWidget(self.page_heading)
        layout.addStretch(1)

        self.language_combo = QComboBox()
        for code, name in self.ctx.translator.available_languages():
            self.language_combo.addItem(name, code)
        self._select_combo(self.language_combo, self.ctx.translator.language)
        self.language_combo.currentIndexChanged.connect(self._on_language_combo)
        layout.addWidget(self.language_combo)

        self.theme_combo = QComboBox()
        self._populate_theme_combo()
        self.theme_combo.currentIndexChanged.connect(self._on_theme_combo)
        layout.addWidget(self.theme_combo)

        self.update_btn = QPushButton()
        self.update_btn.clicked.connect(self._check_updates)
        layout.addWidget(self.update_btn)

        self.help_btn = QPushButton()
        self.help_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(__homepage__))
        )
        layout.addWidget(self.help_btn)
        return topbar

    # ------------------------------------------------------------ page setup

    def _register_pages(self) -> None:
        builtins: list[tuple[str, str, Callable[[], ModulePage]]] = [
            ("sidebar.dashboard", "\N{HOUSE BUILDING}", lambda: DashboardPage(self.ctx)),
            ("sidebar.log_analyzer", "\N{PAGE FACING UP}", lambda: LogAnalyzerPage(self.ctx)),
            ("sidebar.errors", "\N{HEAVY EXCLAMATION MARK SYMBOL}", lambda: ErrorLookupPage(self.ctx)),
            ("sidebar.printer_info", "\N{PRINTER}", lambda: PrinterInfoPage(self.ctx)),
            ("sidebar.firmware", "\N{FLOPPY DISK}", lambda: FirmwareCenterPage(self.ctx)),
            ("sidebar.bambu_import", "\N{PACKAGE}", lambda: BambuImportPage(self.ctx)),
            ("sidebar.rinkhals", "\N{PENGUIN}", lambda: RinkhalsPage(self.ctx)),
            ("sidebar.connect", "\N{ELECTRIC PLUG}", lambda: ConnectPage(self.ctx)),
            ("sidebar.health", "\N{BEATING HEART}", lambda: HealthPage(self.ctx)),
            ("sidebar.support_report", "\N{MEMO}", lambda: SupportReportPage(self.ctx)),
            ("sidebar.history", "\N{BAR CHART}", lambda: HistoryPage(self.ctx)),
            ("sidebar.resources", "\N{TOOLBOX}", lambda: ResourcesPage(self.ctx)),
            ("sidebar.settings", "\N{GEAR}", lambda: SettingsPage(self.ctx)),
            ("sidebar.about", "\N{INFORMATION SOURCE}", lambda: AboutPage(self.ctx)),
        ]
        for key, icon, factory in builtins:
            self._add_page(key, icon, factory())

    def _register_plugin_pages(self) -> None:
        manager = self.ctx.plugins
        if manager is None:
            return
        plugin_pages: list[tuple[str, str, QWidget]] = []
        for loaded in manager.plugins.values():
            if not loaded.enabled or loaded.instance is None:
                continue
            try:
                page = loaded.instance.create_page()
                if page is None:
                    continue
                plugin_pages.append(
                    (
                        loaded.instance.sidebar_title(),
                        loaded.instance.sidebar_icon(),
                        page,
                    )
                )
            except Exception:  # noqa: BLE001 - a plugin must not crash the window
                continue
        if not plugin_pages:
            return
        self._plugins_header = QLabel()
        self._plugins_header.setObjectName("AppSubtitle")
        insert_at = self._nav_layout.count() - 1  # before the stretch
        self._nav_layout.insertSpacing(insert_at, 10)
        self._nav_layout.insertWidget(insert_at + 1, self._plugins_header)
        for title, icon, page in plugin_pages:
            self._add_page("", icon, page, is_plugin=True, fixed_title=title)

    def _add_page(
        self,
        key: str,
        icon: str,
        page: QWidget,
        is_plugin: bool = False,
        fixed_title: str = "",
    ) -> None:
        index = self.stack.count()
        self.stack.addWidget(page)

        button = QPushButton()
        button.setObjectName("SidebarItem")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _c=False, i=index: self._activate(i))
        self._nav_group.addButton(button, index)

        # Built-in buttons are inserted just before the plugin header (or the
        # trailing stretch); plugin buttons are appended before the stretch.
        if is_plugin:
            insert_at = self._nav_layout.count() - 1
        else:
            insert_at = self._builtin_insert_index()
        self._nav_layout.insertWidget(insert_at, button)

        self._entries.append(
            NavEntry(
                key=key,
                icon=icon,
                page=page,
                button=button,
                is_plugin=is_plugin,
                fixed_title=fixed_title,
            )
        )

    def _builtin_insert_index(self) -> int:
        """Index in the sidebar layout where the next built-in button goes."""
        # Count existing built-in buttons and place after the last one.
        count = 3  # title, subtitle, spacing added in _build_sidebar
        for entry in self._entries:
            if not entry.is_plugin:
                count += 1
        return count

    # ------------------------------------------------------------ behaviour

    def _activate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        page = self.stack.widget(index)
        if isinstance(page, ModulePage):
            page.on_shown()
        if 0 <= index < len(self._entries):
            entry = self._entries[index]
            entry.button.setChecked(True)
            self.page_heading.setText(self._entry_title(entry))

    def _entry_title(self, entry: NavEntry) -> str:
        if entry.is_plugin:
            return entry.fixed_title
        return self.ctx.translator.tr(entry.key)

    def retranslate(self) -> None:
        """Refresh sidebar labels, top bar and window chrome."""
        self.app_subtitle.setText(self.ctx.translator.tr("app.tagline"))
        self.update_btn.setText(
            "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS} "
            + self.ctx.translator.tr("topbar.check_updates")
        )
        self.help_btn.setText(
            "\N{BLACK QUESTION MARK ORNAMENT} " + self.ctx.translator.tr("topbar.help")
        )
        if self._plugins_header is not None:
            self._plugins_header.setText(
                self.ctx.translator.tr("sidebar.plugins_section")
            )
        for entry in self._entries:
            entry.button.setText(f"  {entry.icon}   {self._entry_title(entry)}")
        current = self.stack.currentIndex()
        if 0 <= current < len(self._entries):
            self.page_heading.setText(self._entry_title(self._entries[current]))
        self._populate_theme_combo()

        # Each page connects its own retranslate() to the same
        # language_changed signal in ModulePage.__init__, and those
        # connections are made earlier (during _register_pages(), before
        # this handler is connected) - Qt fires same-signal slots in
        # connection order, so every page has already updated its text by
        # the time this runs. A language switch changes dozens of labels
        # across the visible page in one go, and Windows' compositor
        # sometimes doesn't flush all of them to screen - the widgets' text
        # is correct internally (confirmed via headless testing), but stale
        # glyphs stay visible until something forces a real repaint. A plain
        # .repaint() call wasn't enough to fix this in practice (confirmed
        # against the real app) - what does reliably fix it is exactly what
        # switching to another sidebar page and back already does: hide the
        # page then show it again, which pushes it through Qt's normal
        # show/hide repaint pipeline, and re-run on_shown() so any
        # data-driven content (health tiles, printer status text - set by
        # background fetch callbacks that format with the language active
        # *at fetch time*, not re-rendered by retranslate()) reloads with
        # the new language too instead of staying stuck with old text.
        current_widget = self.stack.currentWidget()
        if current_widget is not None:
            current_widget.hide()
            current_widget.show()
            if isinstance(current_widget, ModulePage):
                current_widget.on_shown()

    # -------------------------------------------------------------- top bar

    def _populate_theme_combo(self) -> None:
        blocker = self.theme_combo.blockSignals(True)
        self.theme_combo.clear()
        for theme in self.ctx.theme.available_themes():
            label = (
                self.ctx.translator.tr(f"topbar.theme_{theme}")
                if theme in ("dark", "light")
                else theme
            )
            self.theme_combo.addItem(label, theme)
        self._select_combo(self.theme_combo, self.ctx.theme.theme)
        self.theme_combo.blockSignals(blocker)

    @staticmethod
    def _select_combo(combo: QComboBox, data: str) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _on_language_combo(self) -> None:
        code = self.language_combo.currentData()
        if code and code != self.ctx.translator.language:
            self.ctx.config.set("language", code)
            self.ctx.translator.set_language(code)

    def _on_theme_combo(self) -> None:
        theme = self.theme_combo.currentData()
        if theme and theme != self.ctx.theme.theme:
            self.ctx.config.set("theme", theme)
            self.ctx.theme.apply(theme)

    # -------------------------------------------------------------- updates

    def _check_updates(self) -> None:
        self.update_btn.setEnabled(False)
        self.update_btn.setText(self.ctx.translator.tr("updates.checking"))
        channel = str(self.ctx.config.get("update_channel"))
        worker = FunctionWorker(check_for_updates, self.ctx.api, channel)
        worker.signals.finished.connect(self._on_update_result)
        worker.signals.error.connect(self._on_update_error)
        run_in_background(worker)

    def _on_update_result(self, info: UpdateInfo) -> None:
        self._restore_update_button()
        tr = self.ctx.translator.tr
        if info.available:
            box = QMessageBox(self)
            box.setWindowTitle(tr("topbar.check_updates"))
            box.setText(tr("updates.available", version=info.latest_version))
            box.setInformativeText(info.notes)
            view = box.addButton(tr("updates.view"), QMessageBox.ButtonRole.AcceptRole)
            box.addButton(tr("common.close"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is view and info.url:
                QDesktopServices.openUrl(QUrl(info.url))
        else:
            QMessageBox.information(
                self,
                tr("topbar.check_updates"),
                tr("updates.latest", version=info.latest_version),
            )

    def _on_update_error(self, message: str) -> None:
        self._restore_update_button()
        QMessageBox.warning(
            self,
            self.ctx.translator.tr("topbar.check_updates"),
            self.ctx.translator.tr("updates.error", reason=message),
        )

    def _restore_update_button(self) -> None:
        self.update_btn.setEnabled(True)
        self.update_btn.setText(
            "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS} "
            + self.ctx.translator.tr("topbar.check_updates")
        )
