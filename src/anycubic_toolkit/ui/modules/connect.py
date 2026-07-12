"""Connect Printer: live status over Moonraker (Rinkhals) or Anycubic LAN mode.

Two local connection paths are tried automatically:

1. **Moonraker** (port 7125) — for printers running the Rinkhals custom
   firmware.
2. **Anycubic LAN mode** (ports 18910/9883) — for stock-firmware printers of
   the newer generation (e.g. the Kobra X) with LAN mode enabled in the
   printer settings. Credentials are provisioned once and cached locally.

Everything stays on the local network; nothing is sent to any cloud.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton

from anycubic_toolkit.core.anycubic_cloud import (
    AnycubicCloudClient,
    CloudError,
    CloudPrinter,
    find_slicer_token,
)
from anycubic_toolkit.core.anycubic_lan import (
    AnycubicLanClient,
    LanCredentials,
    LanError,
    LanPrinterStatus,
    probe_lan_mode,
    provision,
)
from anycubic_toolkit.core.ha_publisher import HaConfig, HomeAssistantPublisher
from anycubic_toolkit.core.monitor import PrinterMonitor
from anycubic_toolkit.core.moonraker import DEFAULT_PORT, MoonrakerClient, PrinterStatus
from anycubic_toolkit.core.notifications import Notifier, NotifierConfig
from anycubic_toolkit.core.print_history import PrintHistory
from anycubic_toolkit.core import telemetry
from anycubic_toolkit.core.workers import FunctionWorker, run_in_background
from anycubic_toolkit.ui.modules.base import ModulePage
from anycubic_toolkit.ui.widgets import Card


class ConnectPage(ModulePage):
    """Live printer status via Moonraker or Anycubic LAN mode."""

    title_key = "connect.title"
    subtitle_key = "connect.subtitle"
    help_key = "connect.help"

    def build(self) -> None:
        self._moonraker: MoonrakerClient | None = None

        self.form_card = Card()
        body = self.form_card.body_layout()
        self.description_label = QLabel()
        self.description_label.setObjectName("Muted")
        self.description_label.setWordWrap(True)
        body.addWidget(self.description_label)

        row = QHBoxLayout()
        self.host_input = QLineEdit()
        self.host_input.setText(str(self.ctx.config.get("moonraker_host", "")))
        self.host_input.returnPressed.connect(self._connect)
        self.connect_btn = QPushButton()
        self.connect_btn.setObjectName("Primary")
        self.connect_btn.clicked.connect(self._connect)
        row.addWidget(self.host_input, 1)
        row.addWidget(self.connect_btn)
        body.addLayout(row)

        self.status_label = QLabel()
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)

        self.monitor_check = QCheckBox()
        self.monitor_check.toggled.connect(self._on_monitor_toggled)
        body.addWidget(self.monitor_check)

        self.content_layout.addWidget(self.form_card)

        self._last_host = ""
        self._last_mode = ""
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(20000)
        self._poll_timer.timeout.connect(self._poll_once)

        # Live status card (hidden until a successful connection).
        self.status_card = Card()
        sbody = self.status_card.body_layout()
        self.mode_label = QLabel()
        self.mode_label.setObjectName("Muted")
        self.state_label = QLabel()
        self.state_label.setObjectName("CardTitle")
        self.versions_label = QLabel()
        self.versions_label.setObjectName("Muted")
        self.temps_label = QLabel()
        self.temps_label.setWordWrap(True)
        self.print_label = QLabel()
        self.print_label.setWordWrap(True)
        sbody.addWidget(self.state_label)
        sbody.addWidget(self.mode_label)
        sbody.addWidget(self.versions_label)
        sbody.addWidget(self.temps_label)
        sbody.addWidget(self.print_label)

        links = QHBoxLayout()
        self.moonraker_btn = QPushButton()
        self.moonraker_btn.setObjectName("Link")
        self.moonraker_btn.clicked.connect(self._open_moonraker)
        self.web_btn = QPushButton()
        self.web_btn.setObjectName("Link")
        self.web_btn.clicked.connect(self._open_web)
        links.addWidget(self.moonraker_btn)
        links.addWidget(self.web_btn)
        links.addStretch(1)
        sbody.addLayout(links)

        self.status_card.setVisible(False)
        self.content_layout.addWidget(self.status_card)

        # Optional Anycubic Cloud (read-only), shown only when enabled in Settings.
        self.cloud_card = Card()
        cbody = self.cloud_card.body_layout()
        self.cloud_hint = QLabel()
        self.cloud_hint.setObjectName("Muted")
        self.cloud_hint.setWordWrap(True)
        cbody.addWidget(self.cloud_hint)
        cloud_row = QHBoxLayout()
        self.cloud_token_input = QLineEdit()
        self.cloud_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.cloud_fetch_btn = QPushButton()
        self.cloud_fetch_btn.clicked.connect(self._cloud_fetch)
        cloud_row.addWidget(self.cloud_token_input, 1)
        cloud_row.addWidget(self.cloud_fetch_btn)
        cbody.addLayout(cloud_row)
        self.cloud_result = QLabel()
        self.cloud_result.setWordWrap(True)
        cbody.addWidget(self.cloud_result)
        self.cloud_card.setVisible(False)
        self.content_layout.addWidget(self.cloud_card)
        self.content_layout.addStretch(1)

    def on_shown(self) -> None:
        self._sync_cloud_card()

    def _sync_cloud_card(self) -> None:
        enabled = bool(self.ctx.config.get("cloud_enabled"))
        self.cloud_card.setVisible(enabled)
        if not enabled:
            return
        token = str(self.ctx.config.get("cloud_access_token", "") or "")
        if not token:
            token = find_slicer_token()
            if token:
                self.ctx.config.set("cloud_access_token", token)
        self.cloud_token_input.setText(token)
        if token:
            self.cloud_hint.setText(self.tr_("connect.cloud_hint_token"))
        else:
            self.cloud_hint.setText(self.tr_("connect.cloud_hint_no_token"))

    def retranslate(self) -> None:
        super().retranslate()
        self.form_card.set_title(self.tr_("connect.card_title"))
        self.description_label.setText(self.tr_("connect.description"))
        self.host_input.setPlaceholderText(self.tr_("connect.host_placeholder"))
        self.connect_btn.setText("\N{ELECTRIC PLUG} " + self.tr_("connect.connect"))
        self.monitor_check.setText(self.tr_("connect.monitor"))
        self.status_card.set_title(self.tr_("connect.live_title"))
        self.moonraker_btn.setText(
            "\N{GLOBE WITH MERIDIANS} " + self.tr_("connect.open_moonraker")
        )
        self.web_btn.setText("\N{GLOBE WITH MERIDIANS} " + self.tr_("connect.open_web"))
        self.cloud_card.set_title(self.tr_("connect.cloud_title"))
        self.cloud_token_input.setPlaceholderText(self.tr_("connect.cloud_token_placeholder"))
        self.cloud_fetch_btn.setText("\N{CLOUD} " + self.tr_("connect.cloud_fetch"))
        self._sync_cloud_card()

    # ------------------------------------------------------------- internal

    def _connect(self) -> None:
        host = self.host_input.text().strip()
        if not host:
            self.status_label.setText(self.tr_("connect.enter_host"))
            return
        self.ctx.config.set("moonraker_host", host)
        self.status_label.setText(self.tr_("connect.connecting"))
        self.status_card.setVisible(False)
        worker = FunctionWorker(self._detect_and_fetch, host)
        worker.signals.finished.connect(self._show_result)
        worker.signals.error.connect(
            lambda _msg: self.status_label.setText(self.tr_("connect.offline"))
        )
        run_in_background(worker)

    def _detect_and_fetch(self, host: str) -> dict[str, Any]:
        """Try Moonraker first, then Anycubic LAN mode. Runs off the UI thread."""
        # 1) Moonraker (Rinkhals)
        moonraker = MoonrakerClient(host, DEFAULT_PORT)
        status = moonraker.fetch_status()
        if status.online:
            self._moonraker = moonraker
            return {"mode": "moonraker", "status": status, "host": host}

        # 2) Anycubic LAN mode (stock firmware, e.g. Kobra X)
        if probe_lan_mode(host):
            creds = self._lan_credentials(host)
            lan_status = AnycubicLanClient(creds).fetch_status()
            self._moonraker = None
            return {"mode": "lan", "status": lan_status, "host": host}

        return {"mode": "none", "status": None, "host": host}

    def _lan_credentials(self, host: str) -> LanCredentials:
        """Cached LAN credentials for *host*, provisioning on first use."""
        store = self.ctx.config.get("lan_credentials", {}) or {}
        cached = LanCredentials.from_dict(store.get(host, {})) if isinstance(store, dict) else None
        if cached is not None:
            return cached
        creds = provision(host)  # may raise LanError -> worker error path
        if isinstance(store, dict):
            store[host] = creds.to_dict()
            self.ctx.config.set("lan_credentials", store)
        return creds

    def _show_result(self, result: dict[str, Any]) -> None:
        mode = result.get("mode")
        status = result.get("status")
        self._last_host = result.get("host", "")
        self._last_mode = mode if mode in ("moonraker", "lan") else ""
        self._feed_monitor(mode, status)
        if mode == "moonraker" and isinstance(status, PrinterStatus) and status.online:
            self._show_moonraker(status)
        elif mode == "lan" and isinstance(status, LanPrinterStatus):
            self._show_lan(status)
        else:
            self.status_card.setVisible(False)
            self.status_label.setText(self.tr_("connect.offline"))

    # -------------------------------------------------------- render helpers

    def _show_moonraker(self, status: PrinterStatus) -> None:
        self.status_label.setText("")
        self.status_card.setVisible(True)
        self.mode_label.setText(self.tr_("connect.mode_moonraker"))
        self.moonraker_btn.setVisible(True)
        self.web_btn.setVisible(True)
        self.state_label.setText(
            self.tr_("connect.online") + f" — {status.state or '?'}"
        )
        versions = []
        if status.moonraker_version:
            versions.append(f"Moonraker {status.moonraker_version}")
        if status.klipper_version:
            versions.append(f"Klipper {status.klipper_version}")
        self.versions_label.setText("  \N{BULLET}  ".join(versions))
        self.temps_label.setText(
            self.tr_(
                "connect.temps",
                nozzle=status.extruder_temp,
                nozzle_t=status.extruder_target,
                bed=status.bed_temp,
                bed_t=status.bed_target,
            )
        )
        if status.print_state == "printing":
            self.print_label.setText(
                self.tr_(
                    "connect.printing",
                    file=status.print_filename or "?",
                    percent=int(status.print_progress * 100),
                )
            )
        else:
            self.print_label.setText(
                self.tr_("connect.print_state", state=status.print_state or "idle")
            )

    def _show_lan(self, status: LanPrinterStatus) -> None:
        if not status.online:
            self.status_card.setVisible(False)
            key = "connect.lan_missing_mqtt" if status.error == "paho-missing" else "connect.offline"
            self.status_label.setText(self.tr_(key))
            return
        self.status_label.setText("")
        self.status_card.setVisible(True)
        self.mode_label.setText(self.tr_("connect.mode_lan"))
        # LAN mode has no Moonraker/web UI to open.
        self.moonraker_btn.setVisible(False)
        self.web_btn.setVisible(False)
        name = status.device_name or status.model_name
        self.state_label.setText(
            self.tr_("connect.online") + (f" — {name}" if name else "")
        )
        details = []
        if status.model_name:
            details.append(status.model_name)
        if status.firmware_version:
            details.append(f"Firmware {status.firmware_version}")
        self.versions_label.setText("  \N{BULLET}  ".join(details))
        self.temps_label.setText(
            self.tr_(
                "connect.temps",
                nozzle=status.nozzle_temp,
                nozzle_t=status.nozzle_target,
                bed=status.bed_temp,
                bed_t=status.bed_target,
            )
        )
        if status.print_state and status.print_state.lower() in ("printing", "print"):
            line = self.tr_(
                "connect.printing",
                file=status.print_filename or "?",
                percent=int(status.print_progress * 100),
            )
            if status.total_layers:
                line += "  \N{BULLET}  " + self.tr_(
                    "connect.layers",
                    current=status.current_layer,
                    total=status.total_layers,
                )
            if status.remaining_minutes:
                line += "  \N{BULLET}  " + self.tr_(
                    "connect.remaining", minutes=status.remaining_minutes
                )
            self.print_label.setText(line)
        else:
            self.print_label.setText(
                self.tr_("connect.print_state", state=status.print_state or "idle")
            )

    def _monitor(self) -> PrinterMonitor:
        history = PrintHistory()
        notifier = Notifier(NotifierConfig.from_config(self.ctx.config))
        ha = HomeAssistantPublisher(HaConfig.from_config(self.ctx.config))
        return PrinterMonitor(history, notifier=notifier, ha_publisher=ha,
                              on_event=self._on_print_event)

    def _feed_monitor(self, mode, status) -> None:
        snap = None
        if mode == "moonraker" and isinstance(status, PrinterStatus) and status.online:
            snap = telemetry.from_moonraker(status, self._last_host)
        elif mode == "lan" and isinstance(status, LanPrinterStatus) and status.online:
            snap = telemetry.from_lan(status, self._last_host)
        if snap is not None:
            self._monitor().ingest(snap)

    def _on_monitor_toggled(self, checked: bool) -> None:
        if checked and self._last_mode:
            self._poll_timer.start()
        else:
            self._poll_timer.stop()

    def _poll_once(self) -> None:
        if not self._last_host or not self._last_mode:
            return
        host, mode = self._last_host, self._last_mode
        worker = FunctionWorker(self._refetch, host, mode)
        worker.signals.finished.connect(lambda r: self._feed_monitor(r["mode"], r["status"]))
        run_in_background(worker)

    def _refetch(self, host: str, mode: str) -> dict[str, Any]:
        if mode == "moonraker":
            return {"mode": "moonraker", "status": MoonrakerClient(host, DEFAULT_PORT).fetch_status(), "host": host}
        creds = self._lan_credentials(host)
        return {"mode": "lan", "status": AnycubicLanClient(creds).fetch_status(), "host": host}

    def _on_print_event(self, title: str, message: str) -> None:
        self.status_label.setText(f"{title} — {message}")

    def _cloud_fetch(self) -> None:
        token = self.cloud_token_input.text().strip()
        if not token:
            self.cloud_result.setText(self.tr_("connect.cloud_hint_no_token"))
            return
        self.ctx.config.set("cloud_access_token", token)
        self.cloud_result.setText(self.tr_("connect.connecting"))
        worker = FunctionWorker(self._cloud_status, token)
        worker.signals.finished.connect(self._show_cloud)
        worker.signals.error.connect(
            lambda msg: self.cloud_result.setText(
                self.tr_("connect.cloud_error", reason=msg)
            )
        )
        run_in_background(worker)

    def _cloud_status(self, token: str) -> list[CloudPrinter]:
        client = AnycubicCloudClient(token)
        return client.printers()

    def _show_cloud(self, printers: list[CloudPrinter]) -> None:
        if not printers:
            self.cloud_result.setText(self.tr_("connect.cloud_no_printers"))
            return
        lines = []
        for printer in printers:
            name = printer.name or printer.model or "?"
            state = printer.print_state or ("online" if printer.online else "offline")
            line = f"\N{PRINTER} <b>{name}</b> — {state}"
            if printer.is_printing:
                line += f" ({int(printer.progress * 100)}%"
                if printer.filename:
                    line += f", {printer.filename}"
                line += ")"
            if printer.nozzle_temp or printer.bed_temp:
                line += f"  \N{BULLET} {printer.nozzle_temp}°C / {printer.bed_temp}°C"
            lines.append(line)
        self.cloud_result.setText("<br>".join(lines))

    def _open_moonraker(self) -> None:
        if self._moonraker:
            QDesktopServices.openUrl(QUrl(self._moonraker.base_url()))

    def _open_web(self) -> None:
        if self._moonraker:
            QDesktopServices.openUrl(QUrl(self._moonraker.web_url()))
