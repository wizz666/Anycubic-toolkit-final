"""Base class for sidebar module pages.

Every page receives the shared :class:`AppContext` (services container) and
implements :meth:`retranslate` so the whole UI updates live when the language
changes — no restart required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from anycubic_toolkit.core.api import WizzApiClient
from anycubic_toolkit.core.config import ConfigManager
from anycubic_toolkit.core.i18n import Translator
from anycubic_toolkit.core.logpack import LogAnalysisResult
from anycubic_toolkit.core.passwords import PasswordService
from anycubic_toolkit.core.plugins import PluginManager
from anycubic_toolkit.core.themes import ThemeManager


@dataclass
class AppContext:
    """Shared services handed to every page and plugin."""

    config: ConfigManager
    translator: Translator
    theme: ThemeManager
    api: WizzApiClient
    passwords: PasswordService
    plugins: PluginManager | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def last_analysis(self) -> LogAnalysisResult | None:
        """The most recent log analysis, restored from config if needed."""
        cached = self.extras.get("last_analysis")
        if isinstance(cached, LogAnalysisResult):
            return cached
        stored = self.config.get("last_analysis")
        if isinstance(stored, dict):
            result = LogAnalysisResult.from_dict(stored)
            self.extras["last_analysis"] = result
            return result
        return None

    def set_last_analysis(self, result: LogAnalysisResult) -> None:
        """Store a new analysis in memory and on disk."""
        self.extras["last_analysis"] = result
        self.config.set("last_analysis", result.to_dict())


class ModulePage(QWidget):
    """Scrollable page with a title/subtitle header.

    Subclasses build their content in :meth:`build` (adding to
    ``self.content_layout``) and refresh strings in :meth:`retranslate`.
    """

    title_key: str = ""
    subtitle_key: str = ""
    help_key: str = ""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("Page")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)

        container = QWidget()
        container.setObjectName("Page")
        scroll.setWidget(container)

        self.content_layout = QVBoxLayout(container)
        self.content_layout.setContentsMargins(28, 24, 28, 24)
        self.content_layout.setSpacing(16)

        self.title_label = QLabel()
        self.title_label.setObjectName("PageTitle")
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("PageSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.content_layout.addWidget(self.title_label)
        self.content_layout.addWidget(self.subtitle_label)

        # Optional beginner-friendly help, shown when the page sets ``help_key``.
        self.help_toggle: QPushButton | None = None
        self.help_panel: QFrame | None = None
        if self.help_key:
            self._build_help()

        self.build()
        self.retranslate()
        self.ctx.translator.language_changed.connect(lambda _c: self.retranslate())

    # --------------------------------------------------------------- helpers

    def _build_help(self) -> None:
        """Create a collapsible 'How this works' help panel under the header."""
        self.help_toggle = QPushButton()
        self.help_toggle.setObjectName("Link")
        self.help_toggle.setCheckable(True)
        self.help_toggle.setCursor(Qt.CursorShape.PointingHandCursor)

        self.help_panel = QFrame()
        self.help_panel.setObjectName("Card")
        panel_layout = QVBoxLayout(self.help_panel)
        panel_layout.setContentsMargins(16, 12, 16, 12)
        self.help_text = QLabel()
        self.help_text.setObjectName("Muted")
        self.help_text.setWordWrap(True)
        self.help_text.setTextFormat(Qt.TextFormat.RichText)
        panel_layout.addWidget(self.help_text)
        self.help_panel.setVisible(False)

        self.help_toggle.toggled.connect(self._on_help_toggled)
        self.content_layout.addWidget(self.help_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.content_layout.addWidget(self.help_panel)

    def _on_help_toggled(self, checked: bool) -> None:
        if self.help_panel is not None:
            self.help_panel.setVisible(checked)
        self._update_help_toggle_text(checked)

    def _update_help_toggle_text(self, checked: bool) -> None:
        if self.help_toggle is None:
            return
        arrow = "\N{BLACK DOWN-POINTING SMALL TRIANGLE}" if checked else "\N{BLACK RIGHT-POINTING SMALL TRIANGLE}"
        self.help_toggle.setText(f"{arrow}  \N{BLACK QUESTION MARK ORNAMENT} " + self.tr_("common.how_it_works"))

    def tr_(self, key: str, **kwargs: object) -> str:
        """Shorthand for the shared translator."""
        return self.ctx.translator.tr(key, **kwargs)

    def build(self) -> None:
        """Create page content. Overridden by subclasses."""

    def retranslate(self) -> None:
        """Refresh all visible strings. Subclasses call super()."""
        if self.title_key:
            self.title_label.setText(self.tr_(self.title_key))
        if self.subtitle_key:
            self.subtitle_label.setText(self.tr_(self.subtitle_key))
        if self.help_key and self.help_toggle is not None:
            self._update_help_toggle_text(self.help_toggle.isChecked())
            self.help_text.setText(self.tr_(self.help_key))

    def on_shown(self) -> None:
        """Called each time the page becomes the active sidebar page."""

    @staticmethod
    def align_top() -> Qt.AlignmentFlag:
        """Alignment shortcut used by several pages."""
        return Qt.AlignmentFlag.AlignTop
