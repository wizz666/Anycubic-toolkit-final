"""Print History: completed and failed prints tracked by the monitor.

While the Connect page's background monitoring is active, every print session
seen over Moonraker, Anycubic LAN mode or the cloud is recorded locally
(``print_history.jsonl`` in the app data folder). This page shows the totals
and the individual records. Nothing is uploaded anywhere.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QPushButton

from anycubic_toolkit.core.print_history import PrintHistory
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class HistoryPage(ModulePage):
    """Locally recorded print history with simple statistics."""

    title_key = "history.title"
    subtitle_key = "history.subtitle"
    help_key = "history.help"

    def build(self) -> None:
        self._history = PrintHistory()

        self.stats_card = Card()
        sbody = self.stats_card.body_layout()
        self.stats_label = QLabel()
        self.stats_label.setWordWrap(True)
        sbody.addWidget(self.stats_label)

        self.records_card = Card()
        rbody = self.records_card.body_layout()
        self.records_list = QListWidget()
        self.records_list.setMinimumHeight(260)
        rbody.addWidget(self.records_list)

        self.empty_label = QLabel()
        self.empty_label.setObjectName("Muted")
        self.empty_label.setWordWrap(True)
        rbody.addWidget(self.empty_label)

        actions = QHBoxLayout()
        self.clear_btn = QPushButton()
        self.clear_btn.setObjectName("Danger")
        self.clear_btn.clicked.connect(self._clear)
        actions.addWidget(self.clear_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        actions.addStretch(1)
        rbody.addLayout(actions)

        self.content_layout.addWidget(self.stats_card)
        self.content_layout.addWidget(self.records_card)
        self.content_layout.addStretch(1)

    def on_shown(self) -> None:
        self.refresh()

    def retranslate(self) -> None:
        super().retranslate()
        self.stats_card.set_title(self.tr_("history.stats_title"))
        self.records_card.set_title(self.tr_("history.records_title"))
        self.clear_btn.setText(self.tr_("history.clear"))
        self.empty_label.setText(self.tr_("history.empty"))
        self.refresh()

    # ------------------------------------------------------------- internal

    def refresh(self) -> None:
        stats = self._history.stats()
        self.stats_label.setText(
            self.tr_(
                "history.stats",
                total=stats.total,
                finished=stats.finished,
                failed=stats.failed,
                rate=int(stats.success_rate * 100),
                time=stats.total_time_text(),
            )
        )
        self.records_list.clear()
        records = self._history.records()
        self.empty_label.setVisible(not records)
        self.records_list.setVisible(bool(records))
        for record in records:
            glyph = (
                "\N{WHITE HEAVY CHECK MARK}"
                if record.result == "finished"
                else "\N{CROSS MARK}"
            )
            date = record.ended_at.split("T", 1)[0] if record.ended_at else ""
            name = record.filename or self.tr_("history.unnamed")
            line = f"{glyph}  {name}  \N{BULLET}  {record.duration_text()}"
            if date:
                line += f"  \N{BULLET}  {date}"
            if record.model:
                line += f"  \N{BULLET}  {record.model}"
            self.records_list.addItem(line)

    def _clear(self) -> None:
        self._history.clear()
        self.refresh()
