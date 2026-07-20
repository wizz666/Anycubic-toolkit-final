"""Firmware Center: installed vs. available firmware with downloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextEdit,
)

from anycubic_toolkit.core.models import KNOWN_MODELS
from anycubic_toolkit.core.rinkhals import RINKHALS_HOME, RinkhalsClient
from anycubic_toolkit.core.websources import firmware_url, kobra_x_community_firmware_url

_KOBRA_X_CODE = "K4P"
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class FirmwareCenterPage(ModulePage):
    """Firmware from the Rinkhals catalog, with the official Anycubic fallback."""

    title_key = "firmware.title"
    subtitle_key = "firmware.subtitle"
    help_key = "firmware.help"

    def build(self) -> None:
        self._catalog: list[dict[str, Any]] = []
        self._loaded = False
        self._rinkhals = RinkhalsClient()

        self.installed_card = Card()
        ibody = self.installed_card.body_layout()

        selector_row = QHBoxLayout()
        self.model_label = QLabel()
        self.model_combo = QComboBox()
        self.model_combo.addItem("—", "")
        for code, name in KNOWN_MODELS:
            self.model_combo.addItem(name, code)
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        selector_row.addWidget(self.model_label)
        selector_row.addWidget(self.model_combo, 1)
        ibody.addLayout(selector_row)

        self.installed_value = QLabel()
        self.installed_card.body_layout().addWidget(self.installed_value)
        self.content_layout.addWidget(self.installed_card)

        self.available_card = Card()
        body = self.available_card.body_layout()

        header = QHBoxLayout()
        self.refresh_btn = QPushButton()
        self.refresh_btn.clicked.connect(self._load)
        header.addStretch(1)
        header.addWidget(self.refresh_btn)
        body.addLayout(header)

        self.fw_list = QListWidget()
        self.fw_list.setMinimumHeight(160)
        self.fw_list.currentItemChanged.connect(self._show_notes)
        body.addWidget(self.fw_list)

        self.notes_title = QLabel()
        self.notes_title.setObjectName("CardTitle")
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(110)
        body.addWidget(self.notes_title)
        body.addWidget(self.notes)

        self.download_btn = QPushButton()
        self.download_btn.setObjectName("Primary")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._download)
        body.addWidget(self.download_btn, alignment=Qt.AlignmentFlag.AlignRight)

        self.status_label = QLabel()
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)

        self.official_btn = QPushButton()
        self.official_btn.setObjectName("Link")
        self.official_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(firmware_url()))
        )
        body.addWidget(self.official_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self.community_btn = QPushButton()
        self.community_btn.setObjectName("Link")
        self.community_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(kobra_x_community_firmware_url()))
        )
        self.community_btn.setVisible(False)
        body.addWidget(self.community_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self.content_layout.addWidget(self.available_card)
        self.content_layout.addStretch(1)

    def on_shown(self) -> None:
        self._sync_model_combo()
        self._refresh_installed()
        if not self._loaded:
            self._load()

    def retranslate(self) -> None:
        super().retranslate()
        self.installed_card.set_title(self.tr_("firmware.installed"))
        self.model_label.setText(self.tr_("firmware.model_select"))
        self.available_card.set_title(self.tr_("firmware.available"))
        self.refresh_btn.setText("\N{CLOCKWISE OPEN CIRCLE ARROW} " + self.tr_("firmware.refresh"))
        self.notes_title.setText(self.tr_("firmware.release_notes"))
        self.download_btn.setText("\N{DOWNWARDS BLACK ARROW} " + self.tr_("firmware.download"))
        self.official_btn.setText(
            "\N{GLOBE WITH MERIDIANS} " + self.tr_("firmware.official")
        )
        self.community_btn.setText(
            "\N{GLOBE WITH MERIDIANS} " + self.tr_("firmware.community_archive")
        )
        self._refresh_installed()

    # ------------------------------------------------------------- internal

    def _effective_code(self) -> str:
        """Model from the analyzed log, else the manually selected model."""
        analysis = self.ctx.last_analysis
        if analysis is not None and analysis.model_code:
            return analysis.model_code
        return str(self.ctx.config.get("printer_model_code", "") or "")

    def _sync_model_combo(self) -> None:
        """Reflect the current model in the combo; lock it when a log detected one."""
        analysis = self.ctx.last_analysis
        detected = analysis.model_code if analysis else ""
        code = detected or str(self.ctx.config.get("printer_model_code", "") or "")
        index = self.model_combo.findData(code)
        self.model_combo.blockSignals(True)
        self.model_combo.setCurrentIndex(index if index >= 0 else 0)
        # If a log detected the model, the manual selector is redundant.
        self.model_combo.setEnabled(not detected)
        self.model_combo.blockSignals(False)

    def _on_model_changed(self, _index: int) -> None:
        code = self.model_combo.currentData() or ""
        self.ctx.config.set("printer_model_code", code)
        self._loaded = False
        self._refresh_installed()
        self._load()

    def _refresh_installed(self) -> None:
        analysis = self.ctx.last_analysis
        if analysis is not None and analysis.firmware_version:
            model = analysis.printer_model or self.tr_("log.unknown")
            self.installed_value.setText(f"{model} — v{analysis.firmware_version}")
        else:
            self.installed_value.setText(self.tr_("firmware.installed_unknown"))

    def _load(self) -> None:
        self.status_label.setText(self.tr_("common.loading"))
        analysis = self.ctx.last_analysis
        code = self._effective_code()
        installed = analysis.firmware_version if analysis else ""
        worker = FunctionWorker(self._fetch_catalog, code, installed)
        worker.signals.finished.connect(self._show_catalog)
        worker.signals.error.connect(lambda _msg: self._show_catalog([]))
        run_in_background(worker)

    def _fetch_catalog(self, code: str, installed: str) -> list[dict[str, Any]]:
        """Build the firmware list from the Rinkhals catalog for this model."""
        entries: list[dict[str, Any]] = []
        releases = self._rinkhals.firmware_for_model(code) if code else []
        for position, release in enumerate(releases):
            entries.append(
                {
                    "model": code,
                    "version": release.version,
                    "url": release.url,
                    "notes": release.changes or "",
                    "md5": release.md5,
                    "date": release.date,
                    "latest": position == 0,
                    "installed": bool(installed) and release.version == installed,
                }
            )
        return entries

    def _show_catalog(self, catalog: list[dict[str, Any]]) -> None:
        self._loaded = True
        self._catalog = catalog
        self.fw_list.clear()
        is_kobra_x = self._effective_code() == _KOBRA_X_CODE
        self.community_btn.setVisible(is_kobra_x and not catalog)
        if not catalog:
            key = "firmware.kobra_x_notice" if is_kobra_x else "firmware.not_in_catalog"
            self.status_label.setText(self.tr_(key))
            return
        self.status_label.setText(self.tr_("firmware.catalog_source"))
        for index, entry in enumerate(catalog):
            tags = []
            if entry.get("installed"):
                tags.append(self.tr_("firmware.installed_tag"))
            if entry.get("latest"):
                tags.append(self.tr_("firmware.latest"))
            suffix = f"   [{', '.join(tags)}]" if tags else ""
            date = f"  ({entry['date']})" if entry.get("date") else ""
            item = QListWidgetItem(f"v{entry.get('version', '?')}{date}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.fw_list.addItem(item)
        self.fw_list.setCurrentRow(0)

    def _show_notes(self, current: QListWidgetItem | None, _previous=None) -> None:
        entry = self._entry(current)
        self.download_btn.setEnabled(bool(entry and entry.get("url")))
        if not entry:
            self.notes.setPlainText("")
            return
        parts = [str(entry.get("notes", "")).strip()]
        if entry.get("md5"):
            parts.append(f"\nMD5: {entry['md5']}")
        self.notes.setPlainText("\n".join(p for p in parts if p))

    def _download(self) -> None:
        entry = self._entry(self.fw_list.currentItem())
        if not entry or not entry.get("url"):
            return
        url = str(entry["url"])
        folder = Path(self.ctx.config.get("download_folder"))
        destination = folder / (url.rsplit("/", 1)[-1] or "firmware.bin")
        self.status_label.setText(self.tr_("common.loading"))
        worker = FunctionWorker(self.ctx.api.download_file, url, destination)
        worker.signals.finished.connect(
            lambda path: self.status_label.setText(
                self.tr_("firmware.downloaded", path=str(path))
            )
        )
        worker.signals.error.connect(self.status_label.setText)
        run_in_background(worker)

    def _entry(self, item: QListWidgetItem | None) -> dict[str, Any] | None:
        if item is None:
            return None
        index = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(index, int) and 0 <= index < len(self._catalog):
            return self._catalog[index]
        return None
