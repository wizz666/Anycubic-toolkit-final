"""Rinkhals page: custom-firmware releases, links and status.

Surfaces the open-source Rinkhals project inside the toolkit: the latest
release (fetched from GitHub), quick links to the project and its install
guide, and — when a log has been analyzed — whether Rinkhals maintains a
firmware catalog for the detected printer model.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from anycubic_toolkit.core.models import model_name_or_code
from anycubic_toolkit.core.rinkhals import RINKHALS_DOCS, RINKHALS_HOME, RinkhalsClient
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class RinkhalsPage(ModulePage):
    """Information and links for the Rinkhals custom firmware."""

    title_key = "rinkhals.title"
    subtitle_key = "rinkhals.subtitle"
    help_key = "rinkhals.help"

    def build(self) -> None:
        self._client = RinkhalsClient()
        self._loaded = False

        self.intro_card = Card()
        body = self.intro_card.body_layout()
        self.description_label = QLabel()
        self.description_label.setObjectName("Muted")
        self.description_label.setWordWrap(True)
        body.addWidget(self.description_label)

        self.release_label = QLabel()
        self.release_label.setWordWrap(True)
        body.addWidget(self.release_label)

        self.model_label = QLabel()
        self.model_label.setObjectName("Muted")
        self.model_label.setWordWrap(True)
        body.addWidget(self.model_label)

        button_row = QHBoxLayout()
        self.project_btn = QPushButton()
        self.project_btn.setObjectName("Primary")
        self.project_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(RINKHALS_HOME))
        )
        self.docs_btn = QPushButton()
        self.docs_btn.setObjectName("Link")
        self.docs_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(RINKHALS_DOCS))
        )
        button_row.addWidget(self.project_btn)
        button_row.addWidget(self.docs_btn)
        button_row.addStretch(1)
        body.addLayout(button_row)

        self.note_label = QLabel()
        self.note_label.setObjectName("Muted")
        self.note_label.setWordWrap(True)
        body.addWidget(self.note_label)

        self.content_layout.addWidget(self.intro_card)
        self.content_layout.addStretch(1)

    def on_shown(self) -> None:
        self._refresh_model()
        if not self._loaded:
            self._load_release()

    def retranslate(self) -> None:
        super().retranslate()
        self.intro_card.set_title(self.tr_("rinkhals.card_title"))
        self.description_label.setText(self.tr_("rinkhals.description"))
        self.project_btn.setText(
            "\N{GLOBE WITH MERIDIANS} " + self.tr_("rinkhals.open_project")
        )
        self.docs_btn.setText(self.tr_("rinkhals.install_guide"))
        self.note_label.setText(self.tr_("rinkhals.note"))
        if not self.release_label.text():
            self.release_label.setText(self.tr_("common.loading"))
        self._refresh_model()

    # ------------------------------------------------------------- internal

    def _refresh_model(self) -> None:
        analysis = self.ctx.last_analysis
        code = analysis.model_code if analysis and analysis.model_code else ""
        if not code:
            code = str(self.ctx.config.get("printer_model_code", "") or "")
        if not code:
            self.model_label.setText("")
            return
        model_display = (
            analysis.printer_model
            if analysis and analysis.printer_model
            else model_name_or_code(code)
        )
        worker = FunctionWorker(self._client.is_supported_model, code)
        worker.signals.finished.connect(
            lambda supported: self.model_label.setText(
                self.tr_(
                    "rinkhals.model_supported"
                    if supported
                    else "rinkhals.model_unsupported",
                    model=model_display,
                )
            )
        )
        worker.signals.error.connect(lambda _msg: self.model_label.setText(""))
        run_in_background(worker)

    def _load_release(self) -> None:
        worker = FunctionWorker(self._client.latest_release)
        worker.signals.finished.connect(self._show_release)
        worker.signals.error.connect(lambda _msg: self._show_release(None))
        run_in_background(worker)

    def _show_release(self, release: dict[str, str] | None) -> None:
        self._loaded = True
        if not release:
            self.release_label.setText(self.tr_("rinkhals.release_unknown"))
            return
        published = f" ({release['published']})" if release.get("published") else ""
        self.release_label.setText(
            self.tr_("rinkhals.latest", version=release.get("version", "?"))
            + published
        )
