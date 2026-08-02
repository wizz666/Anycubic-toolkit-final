"""Dashboard: live printer view, at-a-glance stats and news from wizz.se.

The live section gives KobraOS-style local control from the desktop: real-time
temperatures with a rolling graph, print progress with layers and ETA, and
pause / resume / stop commands over Moonraker (Rinkhals) or Anycubic LAN mode
(stock firmware, e.g. Kobra X). Polling only runs while the page is visible.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
)

from anycubic_toolkit.core.anycubic_lan import (
    AnycubicLanClient,
    LanCredentials,
    LanPrinterStatus,
    probe_lan_mode,
    provision,
)
from anycubic_toolkit.core.moonraker import DEFAULT_PORT, MoonrakerClient, PrinterStatus
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card, StatTile, TempGraph, clear_layout

_POLL_MS = 10_000


class DashboardPage(ModulePage):
    """Landing page with a live printer panel, quick stats and a news feed."""

    title_key = "dashboard.welcome"
    subtitle_key = "dashboard.subtitle"
    help_key = "dashboard.help"

    def build(self) -> None:
        # ------------------------------------------------------------ live
        self.live_card = Card()
        live = self.live_card.body_layout()

        picker_row = QHBoxLayout()
        self.printer_combo = QComboBox()
        self.printer_combo.currentIndexChanged.connect(self._on_printer_changed)
        picker_row.addWidget(self.printer_combo, 1)
        self.live_refresh_btn = QPushButton()
        self.live_refresh_btn.setObjectName("Link")
        self.live_refresh_btn.clicked.connect(lambda: self._poll_live(manual=True))
        picker_row.addWidget(self.live_refresh_btn)
        live.addLayout(picker_row)

        self.live_hint = QLabel()
        self.live_hint.setObjectName("Muted")
        self.live_hint.setWordWrap(True)
        live.addWidget(self.live_hint)

        self.live_state = QLabel()
        self.live_state.setObjectName("CardTitle")
        self.live_state.setWordWrap(True)
        live.addWidget(self.live_state)

        self.live_progress = QProgressBar()
        self.live_progress.setRange(0, 100)
        self.live_progress.setTextVisible(True)
        live.addWidget(self.live_progress)

        self.live_detail = QLabel()
        self.live_detail.setObjectName("Muted")
        self.live_detail.setWordWrap(True)
        live.addWidget(self.live_detail)

        self.live_temps = QLabel()
        self.live_temps.setWordWrap(True)
        live.addWidget(self.live_temps)

        self.temp_graph = TempGraph()
        live.addWidget(self.temp_graph)

        controls = QHBoxLayout()
        self.pause_btn = QPushButton()
        self.pause_btn.clicked.connect(lambda: self._send_command("pause"))
        self.resume_btn = QPushButton()
        self.resume_btn.clicked.connect(lambda: self._send_command("resume"))
        self.stop_btn = QPushButton()
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.clicked.connect(self._confirm_stop)
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.resume_btn)
        controls.addWidget(self.stop_btn)
        controls.addStretch(1)
        live.addLayout(controls)

        self.command_status = QLabel()
        self.command_status.setObjectName("Muted")
        self.command_status.setWordWrap(True)
        live.addWidget(self.command_status)

        self.content_layout.addWidget(self.live_card)

        # ----------------------------------------------------------- tiles
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

        # ------------------------------------------------------------ news
        self.news_card = Card()
        self.news_layout = self.news_card.body_layout()
        self.news_status = QLabel()
        self.news_status.setObjectName("Muted")
        self.news_status.setWordWrap(True)
        self.news_layout.addWidget(self.news_status)
        self.content_layout.addWidget(self.news_card)
        self.content_layout.addStretch(1)

        self._news_loaded = False
        self._live_mode = ""          # "moonraker" | "lan" | ""
        self._live_task_id = ""
        self._live_busy = False
        self._command_busy = False
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_MS)
        self._poll_timer.timeout.connect(self._poll_live)

        self._reload_printers()
        self._show_idle_state()

    # ---------------------------------------------------------- lifecycle

    def on_shown(self) -> None:
        self._refresh_tiles()
        self._reload_printers()
        if not self._news_loaded:
            self._load_news()
        if self._current_host():
            self._poll_live(manual=True)
            self._poll_timer.start()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt override
        self._poll_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        if self._current_host() and not self._poll_timer.isActive():
            self._poll_timer.start()
        super().showEvent(event)

    def retranslate(self) -> None:
        super().retranslate()
        self.live_card.set_title(self.tr_("dashboard.live_title"))
        self.live_refresh_btn.setText(
            "\N{ANTICLOCKWISE OPEN CIRCLE ARROW} " + self.tr_("common.refresh")
        )
        self.pause_btn.setText("\N{DOUBLE VERTICAL BAR} " + self.tr_("dashboard.pause"))
        self.resume_btn.setText(
            "\N{BLACK RIGHT-POINTING TRIANGLE} " + self.tr_("dashboard.resume")
        )
        self.stop_btn.setText("\N{BLACK SQUARE FOR STOP} " + self.tr_("dashboard.stop"))
        self.news_card.set_title(self.tr_("dashboard.news"))
        self._refresh_tiles()
        self._sync_live_visibility()

    # ------------------------------------------------------- live: helpers

    def _local_printers(self) -> list[dict[str, Any]]:
        return [
            p
            for p in (self.ctx.config.get("printers", []) or [])
            if p.get("kind") != "cloud" and p.get("host")
        ]

    def _reload_printers(self) -> None:
        printers = self._local_printers()
        current = self.printer_combo.currentData()
        self.printer_combo.blockSignals(True)
        self.printer_combo.clear()
        for entry in printers:
            label = entry.get("name") or entry.get("host", "")
            self.printer_combo.addItem(label, entry.get("host", ""))
        if current:
            index = self.printer_combo.findData(current)
            if index >= 0:
                self.printer_combo.setCurrentIndex(index)
        self.printer_combo.blockSignals(False)
        self._sync_live_visibility()

    def _sync_live_visibility(self) -> None:
        has_printers = self.printer_combo.count() > 0
        for widget in (
            self.printer_combo,
            self.live_refresh_btn,
            self.live_state,
            self.live_progress,
            self.live_detail,
            self.live_temps,
            self.temp_graph,
            self.pause_btn,
            self.resume_btn,
            self.stop_btn,
        ):
            widget.setVisible(has_printers)
        self.live_hint.setVisible(not has_printers)
        if not has_printers:
            self.live_hint.setText(self.tr_("dashboard.live_no_printers"))

    def _current_host(self) -> str:
        return str(self.printer_combo.currentData() or "")

    def _on_printer_changed(self, _index: int) -> None:
        self.temp_graph.clear()
        self._live_mode = ""
        self._live_task_id = ""
        self._show_idle_state()
        if self._current_host():
            self._poll_live(manual=True)

    def _show_idle_state(self) -> None:
        self.live_state.setText(self.tr_("connect.connecting"))
        self.live_progress.setValue(0)
        self.live_detail.setText("")
        self.live_temps.setText("")
        self.command_status.setText("")
        self._update_buttons(state="")

    # ------------------------------------------------------- live: polling

    def _poll_live(self, manual: bool = False) -> None:
        host = self._current_host()
        if not host or self._live_busy:
            return
        self._live_busy = True
        if manual:
            self.live_state.setText(self.tr_("connect.connecting"))
        worker = FunctionWorker(self._detect_and_fetch, host)
        worker.signals.finished.connect(self._on_live_result)
        worker.signals.error.connect(self._on_live_error)
        run_in_background(worker)

    def _detect_and_fetch(self, host: str) -> dict[str, Any]:
        """Try Moonraker first, then Anycubic LAN mode. Runs off the UI thread."""
        status = MoonrakerClient(host, DEFAULT_PORT).fetch_status()
        if status.online:
            return {"mode": "moonraker", "status": status, "host": host}
        if probe_lan_mode(host):
            creds = self._lan_credentials(host)
            lan_status = AnycubicLanClient(creds).fetch_status()
            return {"mode": "lan", "status": lan_status, "host": host}
        return {"mode": "none", "status": None, "host": host}

    def _lan_credentials(self, host: str) -> LanCredentials:
        """Cached LAN credentials for *host* (same store the Connect page uses)."""
        store = self.ctx.config.get("lan_credentials", {}) or {}
        cached = LanCredentials.from_dict(store.get(host, {})) if isinstance(store, dict) else None
        if cached is not None:
            return cached
        creds = provision(host)  # may raise LanError -> worker error path
        if isinstance(store, dict):
            store[host] = creds.to_dict()
            self.ctx.config.set("lan_credentials", store)
        return creds

    def _on_live_error(self, _message: str) -> None:
        self._live_busy = False
        self.live_state.setText(self.tr_("connect.offline"))
        self._update_buttons(state="")

    def _on_live_result(self, result: dict[str, Any]) -> None:
        self._live_busy = False
        if str(result.get("host", "")) != self._current_host():
            return  # user switched printers while the worker ran
        mode = str(result.get("mode", ""))
        status = result.get("status")
        if mode == "moonraker" and isinstance(status, PrinterStatus) and status.online:
            self._live_mode = "moonraker"
            self._show_moonraker(status)
        elif mode == "lan" and isinstance(status, LanPrinterStatus) and status.online:
            self._live_mode = "lan"
            self._live_task_id = status.task_id or self._live_task_id
            self._show_lan(status)
        else:
            self._live_mode = ""
            self.live_state.setText(self.tr_("connect.offline"))
            self._update_buttons(state="")

    def _show_moonraker(self, status: PrinterStatus) -> None:
        state = (status.print_state or status.state or "").lower()
        self.live_state.setText(self._state_line(state, status.print_filename))
        self.live_progress.setValue(max(0, min(100, round(status.print_progress * 100))))
        self.live_detail.setText(
            self.tr_("dashboard.live_source_moonraker", version=status.klipper_version or "?")
        )
        self._set_temps(
            status.extruder_temp, status.extruder_target, status.bed_temp, status.bed_target
        )
        self._update_buttons(state=state)

    def _show_lan(self, status: LanPrinterStatus) -> None:
        state = (status.print_state or "").lower()
        self.live_state.setText(self._state_line(state, status.print_filename))
        self.live_progress.setValue(max(0, min(100, round(status.print_progress * 100))))
        details = []
        if status.total_layers:
            details.append(
                self.tr_(
                    "dashboard.live_layers",
                    current=status.current_layer,
                    total=status.total_layers,
                )
            )
        if status.remaining_minutes:
            hours, minutes = divmod(int(status.remaining_minutes), 60)
            eta = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
            details.append(self.tr_("dashboard.live_eta", eta=eta))
        if status.fan_speed_pct:
            details.append(self.tr_("dashboard.live_fan", pct=status.fan_speed_pct))
        self.live_detail.setText("   \N{MIDDLE DOT}   ".join(details))
        self._set_temps(
            status.nozzle_temp, status.nozzle_target, status.bed_temp, status.bed_target
        )
        self._update_buttons(state=state)

    def _state_line(self, state: str, filename: str) -> str:
        key = {
            "printing": "dashboard.state_printing",
            "paused": "dashboard.state_paused",
            "pausing": "dashboard.state_paused",
            "complete": "dashboard.state_complete",
            "finished": "dashboard.state_complete",
            "standby": "dashboard.state_idle",
            "idle": "dashboard.state_idle",
            "free": "dashboard.state_idle",
            "ready": "dashboard.state_idle",
        }.get(state, "")
        label = self.tr_(key) if key else (state or self.tr_("dashboard.state_idle"))
        return f"{label} \N{EM DASH} {filename}" if filename else label

    def _set_temps(self, nozzle: float, nozzle_t: float, bed: float, bed_t: float) -> None:
        self.live_temps.setText(
            self.tr_(
                "dashboard.live_temps",
                nozzle=f"{nozzle:.0f}",
                nozzle_target=f"{nozzle_t:.0f}",
                bed=f"{bed:.0f}",
                bed_target=f"{bed_t:.0f}",
            )
        )
        self.temp_graph.add_sample(nozzle, bed, nozzle_t, bed_t)

    def _update_buttons(self, state: str) -> None:
        printing = state in ("printing", "resuming", "heating", "leveling")
        paused = state in ("paused", "pausing")
        busy = self._command_busy
        self.pause_btn.setEnabled(printing and not busy)
        self.resume_btn.setEnabled(paused and not busy)
        self.stop_btn.setEnabled((printing or paused) and not busy)

    # ------------------------------------------------------ live: commands

    def _confirm_stop(self) -> None:
        answer = QMessageBox.question(
            self,
            self.tr_("dashboard.stop"),
            self.tr_("dashboard.stop_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._send_command("stop")

    def _send_command(self, action: str) -> None:
        host = self._current_host()
        if not host or not self._live_mode or self._command_busy:
            return
        self._command_busy = True
        self._update_buttons(state="")
        self.command_status.setText(self.tr_(f"dashboard.sending_{action}"))
        worker = FunctionWorker(self._run_command, host, self._live_mode, action)
        worker.signals.finished.connect(lambda ok, act=action: self._on_command_done(act, ok))
        worker.signals.error.connect(lambda msg, act=action: self._on_command_done(act, False, msg))
        run_in_background(worker)

    def _run_command(self, host: str, mode: str, action: str) -> bool:
        if mode == "moonraker":
            client = MoonrakerClient(host, DEFAULT_PORT)
            if action == "pause":
                return client.pause_print()
            if action == "resume":
                return client.resume_print()
            return client.cancel_print()
        creds = self._lan_credentials(host)
        error = AnycubicLanClient(creds).send_print_command(action, self._live_task_id)
        if error:
            raise RuntimeError(error)
        return True

    def _on_command_done(self, action: str, ok: bool, message: str = "") -> None:
        self._command_busy = False
        if ok:
            self.command_status.setText(self.tr_(f"dashboard.sent_{action}"))
        else:
            reason = message or self.tr_("connect.offline")
            self.command_status.setText(self.tr_("dashboard.command_failed", reason=reason))
        QTimer.singleShot(2500, lambda: self._poll_live(manual=False))

    # ---------------------------------------------------------------- tiles

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

    # ----------------------------------------------------------------- news

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
