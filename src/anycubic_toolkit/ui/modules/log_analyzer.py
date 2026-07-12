"""Log Analyzer: unlock and analyze AC_LOG.pack archives locally."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
)

from anycubic_toolkit.core.logpack import LogAnalysisResult, LogPackAnalyzer
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card, DropZone, ScoreBar


class LogAnalyzerPage(ModulePage):
    """Drag & drop analysis of AC_LOG.pack files."""

    title_key = "log.title"
    subtitle_key = "log.subtitle"
    help_key = "log.help"

    def build(self) -> None:
        self.analyzer = LogPackAnalyzer(self.ctx.api, self.ctx.passwords)
        self._busy = False

        # Drop zone + browse
        self.drop_zone = DropZone()
        self.drop_zone.file_dropped.connect(self._start_analysis)
        self.content_layout.addWidget(self.drop_zone)

        browse_row = QHBoxLayout()
        self.browse_btn = QPushButton()
        self.browse_btn.clicked.connect(self._browse)
        self.privacy_label = QLabel()
        self.privacy_label.setObjectName("Muted")
        self.privacy_label.setWordWrap(True)
        browse_row.addWidget(self.browse_btn)
        browse_row.addWidget(self.privacy_label, 1)
        self.content_layout.addLayout(browse_row)

        # Password-database status (warns when the local cache is > 30 days old).
        self.db_notice = QLabel()
        self.db_notice.setObjectName("Muted")
        self.db_notice.setWordWrap(True)
        self.db_notice.setVisible(False)
        self.content_layout.addWidget(self.db_notice)

        # Progress
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("Muted")
        self.progress_label.setVisible(False)
        self.content_layout.addWidget(self.progress)
        self.content_layout.addWidget(self.progress_label)

        # Results
        self.result_card = Card()
        self.result_card.setVisible(False)
        body = self.result_card.body_layout()

        self.summary_form = QFormLayout()
        self.summary_form.setHorizontalSpacing(24)
        self.lbl_printer_k = QLabel()
        self.lbl_printer_v = QLabel()
        self.lbl_fw_k = QLabel()
        self.lbl_fw_v = QLabel()
        self.lbl_serial_k = QLabel()
        self.lbl_serial_v = QLabel()
        self.lbl_files_k = QLabel()
        self.lbl_files_v = QLabel()
        for key, value in (
            (self.lbl_printer_k, self.lbl_printer_v),
            (self.lbl_fw_k, self.lbl_fw_v),
            (self.lbl_serial_k, self.lbl_serial_v),
            (self.lbl_files_k, self.lbl_files_v),
        ):
            key.setObjectName("Muted")
            self.summary_form.addRow(key, value)
        body.addLayout(self.summary_form)

        self.health_bar = ScoreBar()
        body.addWidget(self.health_bar)

        self.fixes_label = QLabel()
        self.fixes_label.setWordWrap(True)
        body.addWidget(self.fixes_label)

        self.issue_tree = QTreeWidget()
        self.issue_tree.setColumnCount(4)
        self.issue_tree.setRootIsDecorated(False)
        self.issue_tree.setAlternatingRowColors(True)
        self.issue_tree.setMinimumHeight(220)
        body.addWidget(self.issue_tree)

        self.content_layout.addWidget(self.result_card)

        self.error_label = QLabel()
        self.error_label.setObjectName("Muted")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        self.content_layout.addWidget(self.error_label)
        self.content_layout.addStretch(1)

    def on_shown(self) -> None:
        self._refresh_db_notice()

    def _refresh_db_notice(self) -> None:
        """Show a warning when the cached password database is old or missing."""
        service = self.ctx.passwords
        database = service.database
        # Resolve lazily the first time without forcing a network call.
        if database is None:
            database = service.load()
        age = service.cache_age_days()
        if database.is_empty():
            self.db_notice.setText("\N{WARNING SIGN} " + self.tr_("passwords.none"))
            self.db_notice.setVisible(True)
        elif service.cache_is_stale() and age is not None:
            self.db_notice.setText(
                "\N{WARNING SIGN} " + self.tr_("passwords.stale", days=int(age))
            )
            self.db_notice.setVisible(True)
        else:
            self.db_notice.setVisible(False)

    def retranslate(self) -> None:
        super().retranslate()
        self.drop_zone.set_hint(self.tr_("log.drop_hint"))
        self.browse_btn.setText(self.tr_("log.browse"))
        self.privacy_label.setText("\N{LOCK} " + self.tr_("log.privacy"))
        self.result_card.set_title(self.tr_("log.results"))
        self.lbl_printer_k.setText(self.tr_("log.printer"))
        self.lbl_fw_k.setText(self.tr_("log.firmware"))
        self.lbl_serial_k.setText(self.tr_("log.serial"))
        self.lbl_files_k.setText(self.tr_("log.files_scanned"))
        self.issue_tree.setHeaderLabels(
            ["", "Code", "File", self.tr_("log.issue_list")]
        )
        analysis = self.ctx.last_analysis
        if analysis is not None:
            self._show_result(analysis, persist=False)
        self._refresh_db_notice()

    # ------------------------------------------------------------- analysis

    def _browse(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            self.tr_("log.browse"),
            "",
            "AC_LOG pack (*.pack *.zip);;All files (*)",
        )
        if path:
            self._start_analysis(path)

    def _start_analysis(self, path: str) -> None:
        if self._busy:
            return
        self._busy = True
        self.error_label.setVisible(False)
        self.progress.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress.setValue(0)
        self.progress_label.setText(self.tr_("log.analyzing"))

        worker = FunctionWorker(self.analyzer.analyze, Path(path))
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.error.connect(self._on_error)
        run_in_background(worker)

    def _on_progress(self, value: int, task: str) -> None:
        self.progress.setValue(value)
        self.progress_label.setText(task)

    def _on_finished(self, result: LogAnalysisResult) -> None:
        self._busy = False
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self._show_result(result, persist=True)

    def _on_error(self, message: str) -> None:
        self._busy = False
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self.error_label.setText(
            "\N{WARNING SIGN} " + self.tr_("log.failed", reason=message)
        )
        self.error_label.setVisible(True)

    # -------------------------------------------------------------- display

    def _show_result(self, result: LogAnalysisResult, persist: bool) -> None:
        if persist:
            self.ctx.set_last_analysis(result)

        unknown = self.tr_("log.unknown")
        self.lbl_printer_v.setText(result.printer_model or unknown)
        self.lbl_fw_v.setText(result.firmware_version or unknown)
        self.lbl_serial_v.setText(result.serial_number or unknown)
        self.lbl_files_v.setText(str(result.files_scanned))

        self.health_bar.set_score(
            self.tr_("log.health_score"),
            result.overall_score,
            f"{self.tr_('log.errors')}: {len(result.errors)}   "
            f"{self.tr_('log.warnings')}: {len(result.warnings)}",
        )

        if result.suggested_fixes:
            fixes = "\n".join(f"\N{BULLET} {fix}" for fix in result.suggested_fixes)
            self.fixes_label.setText(f"{self.tr_('log.fixes')}:\n{fixes}")
            self.fixes_label.setVisible(True)
        else:
            self.fixes_label.setVisible(False)

        self.issue_tree.clear()
        for issue in (result.errors + result.warnings)[:500]:
            glyph = "\N{CROSS MARK}" if issue.severity == "error" else "\N{WARNING SIGN}"
            item = QTreeWidgetItem(
                [
                    glyph,
                    issue.code,
                    f"{issue.source_file}:{issue.line_number}",
                    issue.message,
                ]
            )
            self.issue_tree.addTopLevelItem(item)
        for column in range(3):
            self.issue_tree.resizeColumnToContents(column)

        self.result_card.setVisible(True)
