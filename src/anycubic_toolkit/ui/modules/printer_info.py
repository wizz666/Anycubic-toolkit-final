"""Printer Information: identity detected from the last log analysis."""

from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QLabel

from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class PrinterInfoPage(ModulePage):
    """Shows model, serial and firmware from the last analysis."""

    title_key = "printer.title"
    subtitle_key = "printer.subtitle"
    help_key = "printer.help"

    def build(self) -> None:
        self.card = Card()
        body = self.card.body_layout()
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(24)
        self.k_model, self.v_model = QLabel(), QLabel()
        self.k_serial, self.v_serial = QLabel(), QLabel()
        self.k_fw, self.v_fw = QLabel(), QLabel()
        self.k_source, self.v_source = QLabel(), QLabel()
        self.v_source.setWordWrap(True)
        for key, value in (
            (self.k_model, self.v_model),
            (self.k_serial, self.v_serial),
            (self.k_fw, self.v_fw),
            (self.k_source, self.v_source),
        ):
            key.setObjectName("Muted")
            self.form.addRow(key, value)
        body.addLayout(self.form)

        self.empty_label = QLabel()
        self.empty_label.setObjectName("Muted")
        self.empty_label.setWordWrap(True)
        body.addWidget(self.empty_label)

        self.content_layout.addWidget(self.card)
        self.content_layout.addStretch(1)

    def on_shown(self) -> None:
        self._refresh()

    def retranslate(self) -> None:
        super().retranslate()
        self.k_model.setText(self.tr_("printer.model"))
        self.k_serial.setText(self.tr_("printer.serial"))
        self.k_fw.setText(self.tr_("printer.firmware"))
        self.k_source.setText(self.tr_("printer.source"))
        self._refresh()

    def _refresh(self) -> None:
        analysis = self.ctx.last_analysis
        has_data = analysis is not None
        for widget in (
            self.k_model, self.v_model, self.k_serial, self.v_serial,
            self.k_fw, self.v_fw, self.k_source, self.v_source,
        ):
            widget.setVisible(has_data)
        self.empty_label.setVisible(not has_data)
        if analysis is None:
            self.empty_label.setText(self.tr_("printer.no_data"))
            return
        unknown = self.tr_("log.unknown")
        self.v_model.setText(analysis.printer_model or unknown)
        self.v_serial.setText(analysis.serial_number or unknown)
        self.v_fw.setText(analysis.firmware_version or unknown)
        self.v_source.setText(
            self.tr_("printer.source_log", path=analysis.source_path or unknown)
        )
