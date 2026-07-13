"""Dashboard: at-a-glance printer state and news from wizz.se."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QGridLayout, QLabel

from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card, StatTile, clear_layout


class DashboardPage(ModulePage):
    """Landing page with quick stats and a news feed."""

    title_key = "dashboard.welcome"
    subtitle_key = "dashboard.subtitle"
    help_key = "dashboard.help"

    def build(self) -> None:
        grid = QGridLayout()
        grid.setSpacing(14)
        self.tile_printer = StatTile()
        self.tile_health = StatTile()
        self.tile_errors = StatTile()
        self.tile_firmware = StatTile()
        grid.addWidget(self.tile_printer, 0, 0)
        grid.addWidget(self.tile_health, 0, 1)
        grid.addWidget(self.tile_errors, 1, 0)
        grid.addWidget(self.tile_firmware, 1, 1)
        self.content_layout.addLayout(grid)

        self.news_card = Card()
        self.news_layout = self.news_card.body_layout()
        self.news_status = QLabel()
        self.news_status.setObjectName("Muted")
        self.news_status.setWordWrap(True)
        self.news_layout.addWidget(self.news_status)
        self.content_layout.addWidget(self.news_card)
        self.content_layout.addStretch(1)

        self._news_loaded = False

    def on_shown(self) -> None:
        self._refresh_tiles()
        if not self._news_loaded:
            self._load_news()

    def retranslate(self) -> None:
        super().retranslate()
        self.news_card.set_title(self.tr_("dashboard.news"))
        self._refresh_tiles()

    # ------------------------------------------------------------- internal

    def _refresh_tiles(self) -> None:
        analysis = self.ctx.last_analysis
        none = self.tr_("common.none")
        if analysis is None:
            self.tile_printer.set_stat(none, self.tr_("dashboard.card_printer"))
            self.tile_health.set_stat(none, self.tr_("dashboard.card_health"))
            self.tile_errors.set_stat(none, self.tr_("dashboard.card_errors"))
            self.tile_firmware.set_stat(none, self.tr_("dashboard.card_firmware"))
            self.subtitle_label.setText(self.tr_("dashboard.get_started"))
            return
        self.subtitle_label.setText(self.tr_(self.subtitle_key))
        self.tile_printer.set_stat(
            analysis.printer_model or self.tr_("log.unknown"),
            self.tr_("dashboard.card_printer"),
        )
        self.tile_health.set_stat(
            f"{analysis.overall_score}/100", self.tr_("dashboard.card_health")
        )
        self.tile_errors.set_stat(
            str(len(analysis.errors)), self.tr_("dashboard.card_errors")
        )
        self.tile_firmware.set_stat(
            analysis.firmware_version or self.tr_("log.unknown"),
            self.tr_("dashboard.card_firmware"),
        )

    def _load_news(self) -> None:
        self.news_status.setText(self.tr_("common.loading"))
        worker = FunctionWorker(self.ctx.api.get_news)
        worker.signals.finished.connect(self._show_news)
        worker.signals.error.connect(
            lambda _msg: self.news_status.setText(self.tr_("dashboard.no_news"))
        )
        run_in_background(worker)

    def _show_news(self, items: list[dict[str, Any]]) -> None:
        self._news_loaded = True
        clear_layout(self.news_layout)
        if not items:
            status = QLabel(self.tr_("dashboard.no_news"))
            status.setObjectName("Muted")
            status.setWordWrap(True)
            self.news_layout.addWidget(status)
            return
        for item in items[:6]:
            title = QLabel(f"\N{BULLET}  {item.get('title', '')}")
            title.setWordWrap(True)
            self.news_layout.addWidget(title)
            summary = item.get("summary") or item.get("excerpt") or ""
            if summary:
                detail = QLabel(str(summary))
                detail.setObjectName("Muted")
                detail.setWordWrap(True)
                self.news_layout.addWidget(detail)
