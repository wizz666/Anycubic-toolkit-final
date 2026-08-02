"""Bambu Import: clean Bambu Lab / MakerWorld .3mf files for direct printing
on an Anycubic printer, and validate the result against the build volume."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
)

from anycubic_toolkit.core.bambu_clean import PRINTER_PROFILES, ConversionReport, process_batch
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card, DropZone

_SUPPORTED_SUFFIXES = {".3mf", ".stl"}
_STATUS_GLYPH = {
    "OK": "\N{WHITE HEAVY CHECK MARK}",
    "WARN": "\N{WARNING SIGN}",
    "FAIL": "\N{CROSS MARK}",
}

# Domains this page recognizes as a model page worth opening for the user -
# not an API integration, just enough to catch an obviously-wrong paste
# (a random URL, a search query, ...) before bothering QDesktopServices.
_MODEL_LINK_HOSTS = ("makerworld.com", "makerworld.com.cn", "makeronline.com")


def _is_model_link(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _MODEL_LINK_HOSTS)


class BambuImportPage(ModulePage):
    """Drag & drop cleanup of Bambu Lab / MakerWorld files for the Kobra X."""

    title_key = "bambu.title"
    subtitle_key = "bambu.subtitle"
    help_key = "bambu.help"

    def build(self) -> None:
        self._busy = False
        self._outdir: Path | None = None
        self._last_reports: list[ConversionReport] = []

        printer_row = QHBoxLayout()
        self.printer_label = QLabel()
        self.printer_combo = QComboBox()
        for name in PRINTER_PROFILES:
            self.printer_combo.addItem(name, PRINTER_PROFILES[name])
        printer_row.addWidget(self.printer_label)
        printer_row.addWidget(self.printer_combo)
        printer_row.addStretch(1)
        self.content_layout.addLayout(printer_row)

        self.drop_zone = DropZone(multiple=True)
        self.drop_zone.files_dropped.connect(self._start_batch)
        self.content_layout.addWidget(self.drop_zone)

        browse_row = QHBoxLayout()
        self.browse_btn = QPushButton()
        self.browse_btn.clicked.connect(self._browse)
        browse_row.addWidget(self.browse_btn)
        browse_row.addStretch(1)
        self.content_layout.addLayout(browse_row)

        link_row = QHBoxLayout()
        self.link_input = QLineEdit()
        self.link_input.returnPressed.connect(self._open_link)
        self.link_open_btn = QPushButton()
        self.link_open_btn.clicked.connect(self._open_link)
        link_row.addWidget(self.link_input, 1)
        link_row.addWidget(self.link_open_btn)
        self.content_layout.addLayout(link_row)

        self.link_hint_label = QLabel()
        self.link_hint_label.setObjectName("Muted")
        self.link_hint_label.setWordWrap(True)
        self.content_layout.addWidget(self.link_hint_label)

        options_row = QHBoxLayout()
        self.stl_checkbox = QCheckBox()
        self.merge_checkbox = QCheckBox()
        options_row.addWidget(self.stl_checkbox)
        options_row.addWidget(self.merge_checkbox)
        options_row.addStretch(1)
        self.content_layout.addLayout(options_row)

        outdir_row = QHBoxLayout()
        self.outdir_btn = QPushButton()
        self.outdir_btn.clicked.connect(self._choose_outdir)
        self.outdir_label = QLabel()
        self.outdir_label.setObjectName("Muted")
        outdir_row.addWidget(self.outdir_btn)
        outdir_row.addWidget(self.outdir_label, 1)
        self.content_layout.addLayout(outdir_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("Muted")
        self.progress_label.setVisible(False)
        self.content_layout.addWidget(self.progress)
        self.content_layout.addWidget(self.progress_label)

        self.result_card = Card()
        self.result_card.setVisible(False)
        body = self.result_card.body_layout()

        self.result_tree = QTreeWidget()
        self.result_tree.setColumnCount(5)
        self.result_tree.setRootIsDecorated(False)
        self.result_tree.setAlternatingRowColors(True)
        self.result_tree.setMinimumHeight(220)
        self.result_tree.itemDoubleClicked.connect(self._open_result_file)
        body.addWidget(self.result_tree)

        self.open_hint_label = QLabel()
        self.open_hint_label.setObjectName("Muted")
        body.addWidget(self.open_hint_label)

        actions_row = QHBoxLayout()
        self.open_folder_btn = QPushButton()
        self.open_folder_btn.clicked.connect(self._open_output_folder)
        self.open_folder_btn.setVisible(False)
        actions_row.addWidget(self.open_folder_btn)
        actions_row.addStretch(1)
        body.addLayout(actions_row)

        self.content_layout.addWidget(self.result_card)

        self.error_label = QLabel()
        self.error_label.setObjectName("Muted")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        self.content_layout.addWidget(self.error_label)
        self.content_layout.addStretch(1)

    def retranslate(self) -> None:
        super().retranslate()
        self.printer_label.setText(self.tr_("bambu.printer_label"))
        self.drop_zone.set_hint(self.tr_("bambu.drop_hint"))
        self.browse_btn.setText(self.tr_("bambu.browse"))
        self.link_input.setPlaceholderText(self.tr_("bambu.link_placeholder"))
        self.link_open_btn.setText(self.tr_("bambu.link_open"))
        self.link_hint_label.setText(self.tr_("bambu.link_hint"))
        self.stl_checkbox.setText(self.tr_("bambu.also_stl"))
        self.merge_checkbox.setText(self.tr_("bambu.merge"))
        self.outdir_btn.setText(self.tr_("bambu.choose_outdir"))
        self.outdir_label.setText(
            str(self._outdir) if self._outdir else self.tr_("bambu.outdir_default")
        )
        self.result_card.set_title(self.tr_("bambu.results"))
        self.result_tree.setHeaderLabels(
            [
                self.tr_("bambu.col_file"),
                self.tr_("bambu.col_status"),
                self.tr_("bambu.col_dims"),
                self.tr_("bambu.col_triangles"),
                self.tr_("bambu.col_notes"),
            ]
        )
        self.open_folder_btn.setText(self.tr_("bambu.open_folder"))
        self.open_hint_label.setText(self.tr_("bambu.open_hint"))
        if self._last_reports:
            self._show_results(self._last_reports)

    # ------------------------------------------------------------- actions

    def _browse(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(
            self, self.tr_("bambu.browse"), "", "3MF/STL (*.3mf *.stl);;All files (*)"
        )
        if paths:
            self._start_batch(paths)

    def _open_link(self) -> None:
        """Open a pasted MakerWorld/MakerOnline link in the user's own
        browser, where their existing login (if any) and any anti-bot
        challenge are handled the normal way - this app never touches
        credentials or CAPTCHAs itself. Once the file lands in Downloads,
        the user drops it in above (or Browse…) to run it through the same
        clean/validate pipeline as any other file."""
        url = self.link_input.text().strip()
        if not url or not _is_model_link(url):
            self.error_label.setText("\N{WARNING SIGN} " + self.tr_("bambu.link_invalid"))
            self.error_label.setVisible(True)
            return
        self.error_label.setVisible(False)
        QDesktopServices.openUrl(QUrl(url))

    def _choose_outdir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self.tr_("bambu.choose_outdir"))
        if path:
            self._outdir = Path(path)
            self.outdir_label.setText(str(self._outdir))

    def _start_batch(self, paths: list[str]) -> None:
        if self._busy:
            return
        files = _collect_files([Path(p) for p in paths])
        if not files:
            return
        self._busy = True
        self.error_label.setVisible(False)
        self.progress.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress.setValue(0)
        self.progress_label.setText(self.tr_("bambu.converting"))

        worker = FunctionWorker(
            process_batch,
            files,
            self._outdir,
            write_stl=self.stl_checkbox.isChecked(),
            merge=self.merge_checkbox.isChecked(),
            bed_size=self.printer_combo.currentData(),
        )
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.error.connect(self._on_error)
        run_in_background(worker)

    def _on_progress(self, value: int, name: str) -> None:
        self.progress.setValue(value)
        if name:
            self.progress_label.setText(f"{self.tr_('bambu.converting')} {name}")

    def _on_finished(self, reports: list[ConversionReport]) -> None:
        self._busy = False
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self._last_reports = reports
        self._show_results(reports)

    def _on_error(self, message: str) -> None:
        self._busy = False
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self.error_label.setText(
            "\N{WARNING SIGN} " + self.tr_("bambu.failed", reason=message)
        )
        self.error_label.setVisible(True)

    def _open_output_folder(self) -> None:
        if not self._last_reports:
            return
        first = self._last_reports[0]
        target = first.output_3mf or first.output_stl or first.input_path
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))

    def _open_result_file(self, item, _column: int) -> None:
        """Double-click a row: open that row's cleaned file with Windows'
        default handler for .3mf/.stl - whatever slicer is registered for
        the extension (Slicer Next, if that's what's installed)."""
        index = self.result_tree.indexOfTopLevelItem(item)
        if index < 0 or index >= len(self._last_reports):
            return
        report = self._last_reports[index]
        target = report.output_3mf or report.output_stl
        if target is None or not target.exists():
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # -------------------------------------------------------------- display

    def _show_results(self, reports: list[ConversionReport]) -> None:
        self.result_tree.clear()
        for report in reports:
            validation = report.validation
            if validation:
                dims = "x".join(f"{d:.1f}" for d in validation.dims_mm)
                tri = str(validation.triangle_count)
            else:
                dims = "-"
                tri = "-"
            notes = list(report.errors)
            if validation:
                notes.extend(validation.errors)
                notes.extend(validation.warnings)
            if report.clean and report.clean.recentered:
                notes.append(
                    self.tr_("bambu.recentered_note", printer=self.printer_combo.currentText())
                )
            glyph = _STATUS_GLYPH.get(report.status, "")
            # A multi-plate input produces several reports sharing one
            # input_path - show the (distinct, plate-named) output file
            # instead so the rows are actually distinguishable.
            display_name = report.output_3mf.name if report.output_3mf else report.input_path.name
            item = QTreeWidgetItem(
                [
                    display_name,
                    f"{glyph} {report.status}",
                    dims,
                    tri,
                    "; ".join(notes) if notes else "",
                ]
            )
            self.result_tree.addTopLevelItem(item)
        for column in range(4):
            self.result_tree.resizeColumnToContents(column)
        self.open_folder_btn.setVisible(bool(reports))
        self.result_card.setVisible(True)


def _collect_files(paths: list[Path]) -> list[Path]:
    """Expand dropped/browsed folders into their .3mf/.stl contents."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(q for q in path.iterdir() if q.suffix.lower() in _SUPPORTED_SUFFIXES)
            )
        elif path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES:
            files.append(path)
    return files
