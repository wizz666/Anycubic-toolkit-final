"""Connect Printers: live status over Moonraker (Rinkhals) or Anycubic LAN mode.

Any number of printers can be added, each identified by its IP address. Two
local connection paths are tried automatically for each one:

1. **Moonraker** (port 7125) — for printers running the Rinkhals custom
   firmware.
2. **Anycubic LAN mode** (ports 18910/9883) — for stock-firmware printers of
   the newer generation (e.g. the Kobra X) with LAN mode enabled in the
   printer settings. Credentials are provisioned once per printer and cached
   locally.

Everything stays on the local network; nothing is sent to any cloud.
"""

from __future__ import annotations

from typing import Any, Callable
from uuid import uuid4

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from anycubic_toolkit.core.anycubic_cloud import (
    AnycubicCloudClient,
    CloudPrinter,
    find_slicer_token,
)
from anycubic_toolkit.core.anycubic_lan import (
    AnycubicLanClient,
    LanCredentials,
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


class _PrinterCard(Card):
    """One added printer's connection status, with its own refresh/remove controls."""

    removed = Signal(str)
    refresh_requested = Signal(str)

    def __init__(self, entry: dict[str, Any], parent=None) -> None:
        super().__init__("", parent)
        self.entry_id = str(entry.get("id", ""))
        self.host = str(entry.get("host", ""))
        self.name = str(entry.get("name", "")).strip()
        self._tr: Callable[..., str] | None = None

        body = self.body_layout()

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.name_label = QLabel(f"<b>{self.name or self.host}</b>")
        self.name_label.setObjectName("CardTitle")
        self.host_label = QLabel(self.host)
        self.host_label.setObjectName("Muted")
        self.host_label.setVisible(bool(self.name))
        title_col.addWidget(self.name_label)
        title_col.addWidget(self.host_label)
        header.addLayout(title_col, 1)

        self.refresh_btn = QPushButton()
        self.refresh_btn.setObjectName("Link")
        self.refresh_btn.clicked.connect(lambda: self.refresh_requested.emit(self.entry_id))
        header.addWidget(self.refresh_btn)

        self.remove_btn = QPushButton()
        self.remove_btn.setObjectName("Link")
        self.remove_btn.clicked.connect(lambda: self.removed.emit(self.entry_id))
        header.addWidget(self.remove_btn)
        body.addLayout(header)

        self.state_label = QLabel()
        self.state_label.setObjectName("CardTitle")
        self.mode_label = QLabel()
        self.mode_label.setObjectName("Muted")
        self.versions_label = QLabel()
        self.versions_label.setObjectName("Muted")
        self.temps_label = QLabel()
        self.temps_label.setWordWrap(True)
        self.print_label = QLabel()
        self.print_label.setWordWrap(True)
        body.addWidget(self.state_label)
        body.addWidget(self.mode_label)
        body.addWidget(self.versions_label)
        body.addWidget(self.temps_label)
        body.addWidget(self.print_label)

        links = QHBoxLayout()
        self.moonraker_btn = QPushButton()
        self.moonraker_btn.setObjectName("Link")
        self.moonraker_btn.clicked.connect(self._open_moonraker)
        self.moonraker_btn.setVisible(False)
        self.web_btn = QPushButton()
        self.web_btn.setObjectName("Link")
        self.web_btn.clicked.connect(self._open_web)
        self.web_btn.setVisible(False)
        links.addWidget(self.moonraker_btn)
        links.addWidget(self.web_btn)
        links.addStretch(1)
        body.addLayout(links)

    # ------------------------------------------------------------- language

    def retranslate(self, tr: Callable[..., str]) -> None:
        self._tr = tr
        self.refresh_btn.setText("\N{ANTICLOCKWISE OPEN CIRCLE ARROW} " + tr("connect.refresh"))
        self.remove_btn.setText("\N{CROSS MARK} " + tr("connect.remove"))
        self.moonraker_btn.setText("\N{GLOBE WITH MERIDIANS} " + tr("connect.open_moonraker"))
        self.web_btn.setText("\N{GLOBE WITH MERIDIANS} " + tr("connect.open_web"))

    # --------------------------------------------------------------- display

    def show_connecting(self, text: str) -> None:
        self._clear_status(text)

    def show_offline(self, text: str) -> None:
        self._clear_status(text)

    def _clear_status(self, state_text: str) -> None:
        self.state_label.setText(state_text)
        self.mode_label.setText("")
        self.versions_label.setText("")
        self.temps_label.setText("")
        self.print_label.setText("")
        self.moonraker_btn.setVisible(False)
        self.web_btn.setVisible(False)

    def show_moonraker(self, status: PrinterStatus) -> None:
        tr = self._tr or (lambda key, **_kw: key)
        self.state_label.setText(tr("connect.online") + f" — {status.state or '?'}")
        self.mode_label.setText(tr("connect.mode_moonraker"))
        self.moonraker_btn.setVisible(True)
        self.web_btn.setVisible(True)
        versions = []
        if status.moonraker_version:
            versions.append(f"Moonraker {status.moonraker_version}")
        if status.klipper_version:
            versions.append(f"Klipper {status.klipper_version}")
        self.versions_label.setText("  \N{BULLET}  ".join(versions))
        self.temps_label.setText(
            tr(
                "connect.temps",
                nozzle=status.extruder_temp,
                nozzle_t=status.extruder_target,
                bed=status.bed_temp,
                bed_t=status.bed_target,
            )
        )
        if status.print_state == "printing":
            self.print_label.setText(
                tr(
                    "connect.printing",
                    file=status.print_filename or "?",
                    percent=int(status.print_progress * 100),
                )
            )
        else:
            self.print_label.setText(
                tr("connect.print_state", state=status.print_state or "idle")
            )

    def show_lan(self, status: LanPrinterStatus) -> None:
        tr = self._tr or (lambda key, **_kw: key)
        self.mode_label.setText(tr("connect.mode_lan"))
        self.moonraker_btn.setVisible(False)
        self.web_btn.setVisible(False)
        name = status.device_name or status.model_name
        self.state_label.setText(tr("connect.online") + (f" — {name}" if name else ""))
        details = []
        if status.model_name:
            details.append(status.model_name)
        if status.firmware_version:
            details.append(f"Firmware {status.firmware_version}")
        self.versions_label.setText("  \N{BULLET}  ".join(details))
        self.temps_label.setText(
            tr(
                "connect.temps",
                nozzle=status.nozzle_temp,
                nozzle_t=status.nozzle_target,
                bed=status.bed_temp,
                bed_t=status.bed_target,
            )
        )
        if status.print_state and status.print_state.lower() in ("printing", "print"):
            line = tr(
                "connect.printing",
                file=status.print_filename or "?",
                percent=int(status.print_progress * 100),
            )
            if status.total_layers:
                line += "  \N{BULLET}  " + tr(
                    "connect.layers", current=status.current_layer, total=status.total_layers
                )
            if status.remaining_minutes:
                line += "  \N{BULLET}  " + tr("connect.remaining", minutes=status.remaining_minutes)
            self.print_label.setText(line)
        else:
            self.print_label.setText(
                tr("connect.print_state", state=status.print_state or "idle")
            )

    # ------------------------------------------------------------------ links

    def _open_moonraker(self) -> None:
        QDesktopServices.openUrl(QUrl(MoonrakerClient(self.host, DEFAULT_PORT).base_url()))

    def _open_web(self) -> None:
        QDesktopServices.openUrl(QUrl(MoonrakerClient(self.host, DEFAULT_PORT).web_url()))


class ConnectPage(ModulePage):
    """Live status for every printer the user has added, via Moonraker or LAN mode."""

    title_key = "connect.title"
    subtitle_key = "connect.subtitle"
    help_key = "connect.help"

    def build(self) -> None:
        self._cards: dict[str, _PrinterCard] = {}
        self._history = PrintHistory()
        self._migrate_legacy_printer()

        self.form_card = Card()
        body = self.form_card.body_layout()
        self.description_label = QLabel()
        self.description_label.setObjectName("Muted")
        self.description_label.setWordWrap(True)
        body.addWidget(self.description_label)

        add_row = QHBoxLayout()
        self.name_input = QLineEdit()
        self.name_input.returnPressed.connect(self._add_printer)
        self.host_input = QLineEdit()
        self.host_input.returnPressed.connect(self._add_printer)
        self.add_btn = QPushButton()
        self.add_btn.setObjectName("Primary")
        self.add_btn.clicked.connect(self._add_printer)
        add_row.addWidget(self.name_input, 1)
        add_row.addWidget(self.host_input, 1)
        add_row.addWidget(self.add_btn)
        body.addLayout(add_row)

        self.status_label = QLabel()
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)

        monitor_row = QHBoxLayout()
        self.monitor_check = QCheckBox()
        self.monitor_check.toggled.connect(self._on_monitor_toggled)
        monitor_row.addWidget(self.monitor_check, 1)
        self.refresh_all_btn = QPushButton()
        self.refresh_all_btn.setObjectName("Link")
        self.refresh_all_btn.clicked.connect(self._refresh_all)
        monitor_row.addWidget(self.refresh_all_btn)
        body.addLayout(monitor_row)

        self.content_layout.addWidget(self.form_card)

        self.printers_title = QLabel()
        self.printers_title.setObjectName("CardTitle")
        self.content_layout.addWidget(self.printers_title)

        self.printers_layout = QVBoxLayout()
        self.printers_layout.setSpacing(12)
        self.content_layout.addLayout(self.printers_layout)

        self.empty_label = QLabel()
        self.empty_label.setObjectName("Muted")
        self.empty_label.setWordWrap(True)
        self.content_layout.addWidget(self.empty_label)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(20000)
        self._poll_timer.timeout.connect(self._poll_all)

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

        for entry in self.ctx.config.get("printers", []) or []:
            self._add_card(entry)
            self._refresh_printer(entry.get("id", ""))
        self._sync_empty_state()

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
        self.name_input.setPlaceholderText(self.tr_("connect.name_placeholder"))
        self.host_input.setPlaceholderText(self.tr_("connect.host_placeholder"))
        self.add_btn.setText("\N{ELECTRIC PLUG} " + self.tr_("connect.add"))
        self.monitor_check.setText(self.tr_("connect.monitor"))
        self.refresh_all_btn.setText(
            "\N{ANTICLOCKWISE OPEN CIRCLE ARROW} " + self.tr_("connect.refresh_all")
        )
        self.printers_title.setText(self.tr_("connect.live_title"))
        self.empty_label.setText(self.tr_("connect.empty"))
        for card in self._cards.values():
            card.retranslate(self.tr_)
        self.cloud_card.set_title(self.tr_("connect.cloud_title"))
        self.cloud_token_input.setPlaceholderText(self.tr_("connect.cloud_token_placeholder"))
        self.cloud_fetch_btn.setText("\N{CLOUD} " + self.tr_("connect.cloud_fetch"))
        self._sync_cloud_card()

    # ------------------------------------------------------------- printers

    def _migrate_legacy_printer(self) -> None:
        """One-time upgrade from the old single-printer ``moonraker_host`` setting."""
        if self.ctx.config.get("printers", []):
            return
        legacy_host = str(self.ctx.config.get("moonraker_host", "") or "").strip()
        if not legacy_host:
            return
        self.ctx.config.set(
            "printers", [{"id": uuid4().hex[:8], "name": "", "host": legacy_host}]
        )

    def _add_printer(self) -> None:
        host = self.host_input.text().strip()
        if not host:
            self.status_label.setText(self.tr_("connect.enter_host"))
            return
        printers = list(self.ctx.config.get("printers", []) or [])
        if any(p.get("host", "").lower() == host.lower() for p in printers):
            self.status_label.setText(self.tr_("connect.already_added"))
            return
        entry = {"id": uuid4().hex[:8], "name": self.name_input.text().strip(), "host": host}
        printers.append(entry)
        self.ctx.config.set("printers", printers)
        self.name_input.clear()
        self.host_input.clear()
        self.status_label.setText("")
        self._add_card(entry)
        self._sync_empty_state()
        self._refresh_printer(entry["id"], manual=True)

    def _remove_printer(self, printer_id: str) -> None:
        printers = [
            p for p in (self.ctx.config.get("printers", []) or []) if p.get("id") != printer_id
        ]
        self.ctx.config.set("printers", printers)
        card = self._cards.pop(printer_id, None)
        if card is not None:
            self.printers_layout.removeWidget(card)
            card.deleteLater()
        self._sync_empty_state()

    def _add_card(self, entry: dict[str, Any]) -> None:
        card = _PrinterCard(entry)
        card.removed.connect(self._remove_printer)
        card.refresh_requested.connect(lambda pid: self._refresh_printer(pid, manual=True))
        card.retranslate(self.tr_)
        self._cards[card.entry_id] = card
        self.printers_layout.addWidget(card)

    def _sync_empty_state(self) -> None:
        self.empty_label.setVisible(not self._cards)

    def _entry_by_id(self, printer_id: str) -> dict[str, Any] | None:
        for entry in self.ctx.config.get("printers", []) or []:
            if entry.get("id") == printer_id:
                return entry
        return None

    # ------------------------------------------------------------- fetching

    def _refresh_all(self) -> None:
        for printer_id in list(self._cards.keys()):
            self._refresh_printer(printer_id, manual=True)

    def _poll_all(self) -> None:
        for printer_id in list(self._cards.keys()):
            self._refresh_printer(printer_id)

    def _refresh_printer(self, printer_id: str, manual: bool = False) -> None:
        entry = self._entry_by_id(printer_id)
        card = self._cards.get(printer_id)
        if entry is None or card is None:
            return
        host = str(entry.get("host", ""))
        if manual:
            card.show_connecting(self.tr_("connect.connecting"))
        worker = FunctionWorker(self._detect_and_fetch, host)
        worker.signals.finished.connect(
            lambda result, pid=printer_id: self._show_result(pid, result)
        )
        worker.signals.error.connect(
            lambda _msg, pid=printer_id: self._show_offline(pid, self.tr_("connect.offline"))
        )
        run_in_background(worker)

    def _detect_and_fetch(self, host: str) -> dict[str, Any]:
        """Try Moonraker first, then Anycubic LAN mode. Runs off the UI thread."""
        # 1) Moonraker (Rinkhals)
        status = MoonrakerClient(host, DEFAULT_PORT).fetch_status()
        if status.online:
            return {"mode": "moonraker", "status": status, "host": host}

        # 2) Anycubic LAN mode (stock firmware, e.g. Kobra X)
        if probe_lan_mode(host):
            creds = self._lan_credentials(host)
            lan_status = AnycubicLanClient(creds).fetch_status()
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

    def _show_result(self, printer_id: str, result: dict[str, Any]) -> None:
        mode = result.get("mode")
        status = result.get("status")
        host = str(result.get("host", ""))
        self._feed_monitor(host, mode, status)
        card = self._cards.get(printer_id)
        if card is None:
            return
        if mode == "moonraker" and isinstance(status, PrinterStatus) and status.online:
            card.show_moonraker(status)
        elif mode == "lan" and isinstance(status, LanPrinterStatus) and status.online:
            card.show_lan(status)
        else:
            key = (
                "connect.lan_missing_mqtt"
                if isinstance(status, LanPrinterStatus) and status.error == "paho-missing"
                else "connect.offline"
            )
            self._show_offline(printer_id, self.tr_(key))

    def _show_offline(self, printer_id: str, text: str) -> None:
        card = self._cards.get(printer_id)
        if card is not None:
            card.show_offline(text)

    # -------------------------------------------------------------- monitor

    def _monitor(self) -> PrinterMonitor:
        notifier = Notifier(NotifierConfig.from_config(self.ctx.config))
        ha = HomeAssistantPublisher(HaConfig.from_config(self.ctx.config))
        return PrinterMonitor(
            self._history, notifier=notifier, ha_publisher=ha, on_event=self._on_print_event
        )

    def _feed_monitor(self, host: str, mode: str | None, status: Any) -> None:
        snap = None
        if mode == "moonraker" and isinstance(status, PrinterStatus) and status.online:
            snap = telemetry.from_moonraker(status, host)
        elif mode == "lan" and isinstance(status, LanPrinterStatus) and status.online:
            snap = telemetry.from_lan(status, host)
        if snap is not None:
            self._monitor().ingest(snap)

    def _on_monitor_toggled(self, checked: bool) -> None:
        if checked and self._cards:
            self._poll_timer.start()
            self._poll_all()
        else:
            self._poll_timer.stop()

    def _on_print_event(self, title: str, message: str) -> None:
        self.status_label.setText(f"{title} — {message}")

    # ---------------------------------------------------------------- cloud

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
