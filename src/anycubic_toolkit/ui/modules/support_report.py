"""Support Report: bundle the analysis into a text file for support tickets."""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QLabel, QPushButton

from anycubic_toolkit import __app_name__, __version__
from anycubic_toolkit.core.logpack import LogAnalysisResult
from anycubic_toolkit.core.redaction import mask_identifier, redact_sensitive
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class SupportReportPage(ModulePage):
    """Generates a human-readable diagnostic report (no log files)."""

    title_key = "report.title"
    subtitle_key = "report.subtitle"
    help_key = "report.help"

    def build(self) -> None:
        self.card = Card()
        body = self.card.body_layout()
        self.includes_label = QLabel()
        self.includes_label.setObjectName("Muted")
        self.includes_label.setWordWrap(True)
        body.addWidget(self.includes_label)

        self.generate_btn = QPushButton()
        self.generate_btn.setObjectName("Primary")
        self.generate_btn.clicked.connect(self._generate)
        body.addWidget(self.generate_btn)

        self.status_label = QLabel()
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)

        self.content_layout.addWidget(self.card)
        self.content_layout.addStretch(1)

    def on_shown(self) -> None:
        self._refresh_state()

    def retranslate(self) -> None:
        super().retranslate()
        self.card.set_title(self.tr_("report.title"))
        self.includes_label.setText(self.tr_("report.includes"))
        self.generate_btn.setText("\N{MEMO} " + self.tr_("report.generate"))
        self._refresh_state()

    # ------------------------------------------------------------- internal

    def _refresh_state(self) -> None:
        analysis = self.ctx.last_analysis
        if analysis is None:
            self.generate_btn.setEnabled(False)
            self.status_label.setText(self.tr_("report.no_data"))
        else:
            self.generate_btn.setEnabled(True)
            self.status_label.setText("")

    def _generate(self) -> None:
        analysis = self.ctx.last_analysis
        if analysis is None:
            return
        default_dir = Path(self.ctx.config.get("download_folder"))
        default_name = str(default_dir / "anycubic_support_report.txt")
        path, _selected = QFileDialog.getSaveFileName(
            self,
            self.tr_("report.generate"),
            default_name,
            "Text file (*.txt);;All files (*)",
        )
        if not path:
            return
        Path(path).write_text(self._build_report(analysis), encoding="utf-8")
        self.status_label.setText(self.tr_("report.saved", path=path))

    def _build_report(self, analysis: LogAnalysisResult) -> str:
        unknown = self.tr_("log.unknown")
        lines: list[str] = []
        lines.append(f"{__app_name__} — {self.tr_('report.title')}")
        lines.append("=" * 56)
        lines.append(f"Generated : {datetime.now().isoformat(timespec='seconds')}")
        lines.append(f"Toolkit   : v{__version__}")
        lines.append(
            f"System    : {platform.system()} {platform.release()} "
            f"({platform.machine()})"
        )
        lines.append(f"Privacy   : {self.tr_('report.privacy')}")
        lines.append("")
        lines.append(f"[{self.tr_('printer.title')}]")
        lines.append(f"  {self.tr_('printer.model')}   : {analysis.printer_model or unknown}")
        serial = mask_identifier(analysis.serial_number) if analysis.serial_number else unknown
        lines.append(f"  {self.tr_('printer.serial')}  : {serial}")
        lines.append(f"  {self.tr_('printer.firmware')}: {analysis.firmware_version or unknown}")
        lines.append("")
        lines.append(f"[{self.tr_('health.title')}]")
        lines.append(f"  {self.tr_('health.overall')}: {analysis.overall_score}/100")
        for component in analysis.components:
            lines.append(
                f"  - {component.component:<12}: {component.score:>3}/100 "
                f"({component.issues} issues)"
            )
        lines.append("")
        lines.append(
            f"[{self.tr_('log.errors')}: {len(analysis.errors)} / "
            f"{self.tr_('log.warnings')}: {len(analysis.warnings)}]"
        )
        for issue in (analysis.errors + analysis.warnings)[:200]:
            code = f" ({issue.code})" if issue.code else ""
            lines.append(
                f"  [{issue.severity.upper()}]{code} "
                f"{issue.source_file}:{issue.line_number}  {issue.message}"
            )
        if analysis.suggested_fixes:
            lines.append("")
            lines.append(f"[{self.tr_('log.fixes')}]")
            for fix in analysis.suggested_fixes:
                lines.append(f"  - {fix}")
        lines.append("")
        return redact_sensitive("\n".join(lines))
