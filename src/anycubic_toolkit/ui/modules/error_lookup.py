"""Error Code Lookup — fetched directly from the Anycubic Wiki.

Given a code, the page for that code (``/en/error-codes/{code}-code``) is
fetched straight from Anycubic and parsed locally. No mirror or backend is
involved; the wizz.se service is used only for log passwords elsewhere.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton

from anycubic_toolkit.core.websources import WebSourceClient, error_codes_url
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class ErrorLookupPage(ModulePage):
    """Look up an Anycubic error code by number, straight from the wiki."""

    title_key = "errors.title"
    subtitle_key = "errors.subtitle"
    help_key = "errors.help"

    def build(self) -> None:
        self._web = WebSourceClient()
        self._wiki_url = error_codes_url()

        row = QHBoxLayout()
        self.code_input = QLineEdit()
        self.code_input.setMaxLength(10)
        self.code_input.returnPressed.connect(self._search)
        self.search_btn = QPushButton()
        self.search_btn.setObjectName("Primary")
        self.search_btn.clicked.connect(self._search)
        row.addWidget(self.code_input, 1)
        row.addWidget(self.search_btn)
        self.content_layout.addLayout(row)

        self.source_label = QLabel()
        self.source_label.setObjectName("Muted")
        self.source_label.setWordWrap(True)
        self.content_layout.addWidget(self.source_label)

        self.status_label = QLabel()
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        self.content_layout.addWidget(self.status_label)

        self.result_card = Card()
        self.result_card.setVisible(False)
        body = self.result_card.body_layout()
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.hint_label = QLabel()
        self.hint_label.setObjectName("Muted")
        self.hint_label.setWordWrap(True)
        self.wiki_btn = QPushButton()
        self.wiki_btn.setObjectName("Primary")
        self.wiki_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self._wiki_url))
        )
        body.addWidget(self.summary_label)
        body.addWidget(self.hint_label)
        body.addWidget(self.wiki_btn)
        self.content_layout.addWidget(self.result_card)

        # Always-available link to the official wiki index.
        self.open_wiki_btn = QPushButton()
        self.open_wiki_btn.setObjectName("Link")
        self.open_wiki_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(error_codes_url()))
        )
        self.content_layout.addWidget(self.open_wiki_btn)
        self.content_layout.addStretch(1)

    def retranslate(self) -> None:
        super().retranslate()
        self.code_input.setPlaceholderText(self.tr_("errors.placeholder"))
        self.search_btn.setText(
            "\N{RIGHT-POINTING MAGNIFYING GLASS} " + self.tr_("errors.search")
        )
        self.source_label.setText(self.tr_("errors.source"))
        self.wiki_btn.setText("\N{GLOBE WITH MERIDIANS} " + self.tr_("errors.open_wiki"))
        self.open_wiki_btn.setText(
            "\N{GLOBE WITH MERIDIANS} " + self.tr_("errors.browse_all")
        )

    # ------------------------------------------------------------- internal

    def _search(self) -> None:
        code = self.code_input.text().strip()
        if not code:
            self._set_status(self.tr_("errors.enter_code"))
            return
        self._set_status(self.tr_("common.loading"))
        self.result_card.setVisible(False)
        worker = FunctionWorker(self._web.fetch_error_code, code)
        worker.signals.finished.connect(lambda data: self._show(code, data))
        worker.signals.error.connect(
            lambda msg: self._set_status(self.tr_("errors.offline", reason=msg))
        )
        run_in_background(worker)

    def _show(self, code: str, data: dict[str, Any] | None) -> None:
        if not data or not data.get("description"):
            self._wiki_url = error_codes_url()
            self._set_status(self.tr_("errors.not_found", code=code))
            return
        self.status_label.setVisible(False)
        self.result_card.set_title(f"{code} — {data.get('description', '')}")

        summary = str(data.get("summary") or "")
        self.summary_label.setText(summary)
        self.summary_label.setVisible(bool(summary))

        self.hint_label.setText(self.tr_("errors.full_guide"))
        self._wiki_url = str(data.get("url") or error_codes_url())
        self.result_card.setVisible(True)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(True)
